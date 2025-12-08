import sys
import os
import math
import re
import cv2
import numpy as np
import pandas as pd
import requests
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings('ignore')
from urllib.parse import quote, unquote
from google.cloud import vision, translate_v2 as translate
from google.oauth2 import service_account
from google import genai
from time import sleep
from functools import wraps
from datetime import datetime
import hashlib
import tempfile
import json
import time
import random
import logging
from openpyxl import Workbook
from flask import Flask

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Configuration paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_CONFIG_PATH = os.path.expanduser('~/pywikibot/user-config.py')
PASSWORD_FILE_PATH = os.path.expanduser('~/pywikibot/user-password.py')

# Constants
VERTEX_LOCATION = "us-central1"
PRIMARY_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.0-flash-001"
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF = 60.0

# Google Cloud credentials - will be loaded from JSON file
GOOGLE_CREDENTIALS = None

TRANSLATION_PROMPT = (
    'Translate the following Bengali text into English in enclyclopedic style. '
    'You may rearrange words or sentences for clarity, but retain all information. '
    'Do not add or omit anything. Only output the translation text and not a single else. '
    'Do not say description or Bengali text in your answer. do not have any bengali text in your answer just give me the translation, no options and no explanations. '
    'Text: "{text}"'
)

TITLE_PROMPT = (
    'Convert this image description (below) into a single Wikimedia Commons–compliant filename (do NOT add the "File:" prefix, or wikitext, or Title:, do not add filename extention). Follow Wikimedia Commons file naming guidelines: be descriptive, specific, precise, concise and neutral; include date as YYYY-MM-DD if present; avoid photographer/source-only names. Remove any political bias or references to previous governments and strip flattering/propagandistic/honorific language. Output ONLY the filename (no explanation), Regular Case, remove illegal filesystem characters but KEEP spaces and comma and hyphen, keep ≤240 bytes, and do not add filename extention. '
    'Text: "{text}"'
)

import socket
import urllib3.util.connection as urllib3_cn

def allowed_gai_family():
    """Force IPv4 connections only"""
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family
print("Forced IPv4 connections to avoid K8s networking issues")

def retry_on_failure(max_attempts=10, delay=2):
    """Decorator to retry function on failure"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    result = func(*args, **kwargs)
                    if isinstance(result, tuple) and len(result) == 2:
                        data, error = result
                        if error is None or "Retrieved from Wayback Machine" in str(error):
                            return result
                        if attempt < max_attempts - 1:
                            print(f"Attempt {attempt + 1} failed, retrying in {delay}s...")
                            sleep(delay)
                            continue
                    return result
                except Exception as e:
                    if attempt < max_attempts - 1:
                        print(f"Attempt {attempt + 1} failed: {str(e)}, retrying in {delay}s...")
                        sleep(delay)
                    else:
                        if hasattr(func, '__name__') and 'ocr' in func.__name__.lower():
                            return f"OCR Error: {str(e)}"
                        return None, f"Error after {max_attempts} attempts: {str(e)}"
            return result
        return wrapper
    return decorator

# ============================================================================
# SCRAPER FUNCTIONS
# ============================================================================

def normalize_url(url):
    """Normalize URL for comparison"""
    url = re.sub(r'^https?://', '', url)
    url = url.replace('pressinform.portal.gov.bd', 'pressinform.gov.bd')
    url = unquote(url)
    url = url.replace('%20', ' ')
    return url

def generate_unique_id(img_url, date_str, counter):
    """Generate a unique identifier for each entry"""
    url_hash = hashlib.md5(img_url.encode()).hexdigest()[:8]
    date_part = ""
    if date_str:
        match = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
        if match:
            date_part = f"{match.group(1)}{match.group(2)}{match.group(3)}_"
    unique_id = f"PID_{date_part}{url_hash}_{counter:04d}"
    return unique_id

def fetch_wikimedia_data(year):
    """Fetch data from Wikimedia Module:PIDDateData for given year"""
    headers = {
        'User-Agent': 'PressInformScraper/1.0 Python/requests'
    }

    urls_to_try = [
        f"https://commons.wikimedia.org/w/index.php?title=Module:PIDDateData/{year}&action=raw",
        f"https://commons.wikimedia.org/wiki/Module:PIDDateData/{year}?action=raw",
        f"https://commons.wikimedia.org/w/api.php?action=query&titles=Module:PIDDateData/{year}&prop=revisions&rvprop=content&format=json&formatversion=2"
    ]

    for url in urls_to_try:
        try:
            print(f"Trying URL: {url}")
            response = requests.get(url, headers=headers, timeout=10)
            print(f"Status code: {response.status_code}")

            if response.status_code == 200:
                content = response.text

                if 'api.php' in url:
                    import json
                    data = json.loads(content)
                    pages = data.get('query', {}).get('pages', [])
                    if pages and len(pages) > 0:
                        page_data = pages[0]
                        if 'revisions' in page_data and len(page_data['revisions']) > 0:
                            content = page_data['revisions'][0]['content']
                        else:
                            continue
                    else:
                        continue

                if len(content) < 50:
                    continue

                urls = set()
                pattern = r'\["(http[^"]+)"\]'
                matches = re.findall(pattern, content)
                print(f"Found {len(matches)} URLs in {year} module")

                for match in matches:
                    normalized = normalize_url(match)
                    urls.add(normalized)

                if len(urls) > 0:
                    return urls
        except Exception as e:
            print(f"Error with URL {url}: {e}")
            continue

    print(f"Could not fetch Wikimedia data for {year}")
    return set()

def extract_date_from_text(date_text):
    """Extract date and time from Bengali or English date string"""
    date_text = date_text.replace('প্রকাশের তারিখ:', '').strip()
    match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+[ap]m)', date_text)
    if match:
        return match.group(1)
    match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', date_text)
    if match:
        return match.group(1)
    return date_text.strip()

def scrape_page(page_num, wikimedia_urls):
    """Scrape a single page and return list of (url, date) tuples"""
    url = f"https://pressinform.gov.bd/site/view/daily_photo_archive/-?page={page_num}&rows=1"
    print(f"Scraping page {page_num}...")

    max_retries = 10
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                print(f"Failed to fetch page {page_num}")
                return []

            soup = BeautifulSoup(response.content, 'html.parser')
            tables = soup.find_all('table', class_='bordered')

            results = []

            for table in tables:
                header = table.find('h3')
                if header and 'আজকের ফটো রিলিজ' in header.get_text():
                    date_elem = table.find('h4')
                    if date_elem:
                        date = extract_date_from_text(date_elem.get_text())
                    else:
                        date = ""

                    img = table.find('img')
                    if img and img.get('src'):
                        img_url = img['src']
                        results.append((img_url, date))
                else:
                    thead = table.find('thead')
                    if thead:
                        rows = table.find('tbody').find_all('tr')
                        for row in rows:
                            cells = row.find_all('td')
                            if len(cells) >= 3:
                                date = extract_date_from_text(cells[1].get_text())
                                img = cells[2].find('img')
                                if img and img.get('src'):
                                    img_url = img['src']
                                    results.append((img_url, date))

            return results

        except Exception as e:
            wait_time = 2 ** attempt
            print(f"Error scraping page {page_num} (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"Failed after {max_retries} attempts")
                return []

def scrape_data():
    """Scrape data from pressinform.gov.bd"""
    from bs4 import BeautifulSoup

    output_dir = os.path.expanduser('~/output')
    os.makedirs(output_dir, exist_ok=True)

    current_year = datetime.now().year
    previous_year = current_year - 1

    print(f"Current year: {current_year}")
    print("Fetching Wikimedia data...")
    wikimedia_urls = fetch_wikimedia_data(current_year)
    print(f"Loaded {len(wikimedia_urls)} URLs from {current_year}")
    prev_year_urls = fetch_wikimedia_data(previous_year)
    print(f"Loaded {len(prev_year_urls)} URLs from {previous_year}")
    wikimedia_urls.update(prev_year_urls)
    print(f"Total URLs from Wikimedia: {len(wikimedia_urls)}")

    wb = Workbook()
    ws = wb.active

    consecutive_matches = 0
    page_num = 1
    entry_counter = 1

    while consecutive_matches < 50:
        results = scrape_page(page_num, wikimedia_urls)

        if not results:
            print(f"No results found on page {page_num}")
            consecutive_matches += 1
            page_num += 1
            time.sleep(1)
            continue

        page_has_new = False
        for img_url, date in results:
            normalized_url = normalize_url(img_url)

            if normalized_url in wikimedia_urls:
                print(f"Skipping (already in Wikimedia): {img_url}")
                consecutive_matches += 1
            else:
                unique_id = generate_unique_id(img_url, date, entry_counter)
                print(f"Adding: {unique_id} | {date} | {img_url}")
                ws.append([unique_id, date, img_url])
                entry_counter += 1
                consecutive_matches = 0
                page_has_new = True

        if not page_has_new:
            print(f"All entries on page {page_num} already exist in Wikimedia")

        if consecutive_matches >= 50:
            print(f"\nFound 50 consecutive matches. Stopping.")
            break

        page_num += 1
        time.sleep(1)

    # Check if any new entries were added
    if entry_counter == 1:  # No new entries found
        print("\nNo new images found. Skipping Excel file creation.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"pressinform_photos_{timestamp}.xlsx")
    wb.save(output_file)
    print(f"\nData saved to {output_file}")
    print(f"Total rows written: {ws.max_row}")

    return output_file

# ============================================================================
# IMAGE PROCESSOR FUNCTIONS
# ============================================================================

class ImageProcessor:
    def __init__(self):
        self.vision_client = None

    def initialize_vision_client(self):
        """Initialize Google Cloud Vision API client"""
        try:
            credentials = service_account.Credentials.from_service_account_info(GOOGLE_CREDENTIALS)
            self.vision_client = vision.ImageAnnotatorClient(credentials=credentials)
            return True, "Vision API initialized successfully"
        except Exception as e:
            return False, f"Failed to initialize Vision API: {str(e)}"

    @retry_on_failure(max_attempts=10, delay=2)
    def get_wayback_url(self, url):
        """Get the oldest archived version from Wayback Machine"""
        try:
            encoded_url = quote(url, safe='')
            api_url = f"http://archive.org/wayback/available?url={encoded_url}"

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(api_url, headers=headers, timeout=30)
            response.raise_for_status()

            data = response.json()

            if data.get('archived_snapshots') and data['archived_snapshots'].get('closest'):
                wayback_url = data['archived_snapshots']['closest']['url']

                cdx_url = f"http://web.archive.org/cdx/search/cdx?url={encoded_url}&limit=1&output=json"
                cdx_response = requests.get(cdx_url, headers=headers, timeout=30)

                if cdx_response.status_code == 200:
                    cdx_data = cdx_response.json()
                    if len(cdx_data) > 1:
                        timestamp = cdx_data[1][1]
                        original_url = cdx_data[1][2]
                        oldest_url = f"http://web.archive.org/web/{timestamp}/{original_url}"
                        return oldest_url, None

                return wayback_url, None
            else:
                return None, "No archived version found"

        except Exception as e:
            return None, f"Wayback Machine error: {str(e)}"

    def download_image(self, url):
        """Download image from URL and return image with its extension"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            from PIL import ImageFile, ImageOps
            ImageFile.LOAD_TRUNCATED_IMAGES = True

            img_pil = Image.open(BytesIO(response.content))

            # Store EXIF data before any processing
            exif_data = img_pil.info.get('exif', None)

            # CRITICAL FIX: Apply EXIF orientation before any processing
            img_pil = ImageOps.exif_transpose(img_pil)

            # CRITICAL FIX: Convert CMYK to RGB if needed
            if img_pil.mode == 'CMYK':
                # Convert CMYK to RGB using PIL's conversion
                img_pil = img_pil.convert('RGB')
            elif img_pil.mode not in ('RGB', 'L', 'RGBA'):
                # Convert any other color mode to RGB
                img_pil = img_pil.convert('RGB')

            # Detect image format
            img_format = img_pil.format.lower() if img_pil.format else 'jpg'
            if img_format == 'jpeg':
                img_format = 'jpg'

            # Convert PIL to numpy array
            img_np = np.array(img_pil)

            # Convert to OpenCV BGR format
            if len(img_np.shape) == 2:
                # Grayscale
                img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
            elif len(img_np.shape) == 3:
                if img_np.shape[2] == 4:
                    # RGBA
                    img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGBA2BGR)
                elif img_np.shape[2] == 3:
                    # RGB - convert to BGR for OpenCV
                    img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
                else:
                    img_cv = img_np
            else:
                return None, None, "Invalid image format"

            return img_cv, img_format, exif_data, None

        except requests.exceptions.RequestException as e:
            if "404" in str(e) or (hasattr(e, 'response') and e.response is not None and e.response.status_code == 404):
                wayback_url, wayback_error = self.get_wayback_url(url)
                if wayback_url:
                    try:
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        }
                        response = requests.get(wayback_url, headers=headers, timeout=30)
                        response.raise_for_status()

                        from PIL import ImageFile, ImageOps
                        ImageFile.LOAD_TRUNCATED_IMAGES = True

                        img_pil = Image.open(BytesIO(response.content))

                        # Store EXIF data before any processing
                        exif_data = img_pil.info.get('exif', None)

                        # CRITICAL FIX: Apply EXIF orientation
                        img_pil = ImageOps.exif_transpose(img_pil)

                        # CRITICAL FIX: Convert CMYK to RGB if needed
                        if img_pil.mode == 'CMYK':
                            img_pil = img_pil.convert('RGB')
                        elif img_pil.mode not in ('RGB', 'L', 'RGBA'):
                            img_pil = img_pil.convert('RGB')

                        # Detect image format
                        img_format = img_pil.format.lower() if img_pil.format else 'jpg'
                        if img_format == 'jpeg':
                            img_format = 'jpg'

                        img_np = np.array(img_pil)

                        if len(img_np.shape) == 2:
                            img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
                        elif len(img_np.shape) == 3:
                            if img_np.shape[2] == 4:
                                img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGBA2BGR)
                            elif img_np.shape[2] == 3:
                                img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
                            else:
                                img_cv = img_np
                        else:
                            return None, None, "Invalid image format"

                        return img_cv, img_format, exif_data, "Retrieved from Wayback Machine"

                    except Exception as wb_e:
                        return None, None, None, f"404 error - Wayback Machine also failed: {str(wb_e)}"
                else:
                    return None, None, None, f"404 error - {wayback_error}"
            return None, None, None, f"Download failed: {str(e)}"
        except Exception as e:
            return None, None, None, f"Image processing error: {str(e)}"

    def find_white_separator(self, image):
        """Find separator by scanning vertical columns and horizontal lines"""
        height, width = image.shape[:2]
        start_row = int(height * 0.4)

        first_columns = list(range(1, 5))
        last_columns = list(range(width-5, width-1))
        all_columns = first_columns + last_columns

        column_heights = {}
        column_colors = {}

        for col in all_columns:
            column_height = -1
            color_samples = []

            for y in range(height-6, start_row-1, -1):
                pixel = image[y, col]

                if len(color_samples) == 0:
                    color_samples.append(pixel)
                    column_height = y
                else:
                    avg_color = np.mean(color_samples, axis=0)
                    color_diff = np.abs(pixel.astype(np.float32) - avg_color)
                    max_allowed_diff = 255 * 0.02
                    is_matching = np.all(color_diff <= max_allowed_diff)

                    if is_matching:
                        color_samples.append(pixel)
                        column_height = y
                    else:
                        if len(color_samples) > 0:
                            column_heights[col] = height - 1 - y
                            column_colors[col] = np.mean(color_samples, axis=0)
                        break

            if column_height != -1 and col not in column_heights:
                column_heights[col] = height - 1 - start_row
                if len(color_samples) > 0:
                    column_colors[col] = np.mean(color_samples, axis=0)

        if not column_heights:
            return -1, False

        min_uniform_top = height
        for col, col_height in column_heights.items():
            uniform_top_row = height - col_height
            min_uniform_top = min(min_uniform_top, uniform_top_row)

        valid_lines = []

        for first_col in first_columns:
            for last_col in last_columns:
                if first_col in column_heights and last_col in column_heights:
                    height_diff = abs(column_heights[first_col] - column_heights[last_col])
                    if height_diff > 4:
                        continue

                    first_uniform_top = height - column_heights[first_col]
                    last_uniform_top = height - column_heights[last_col]
                    scan_row = max(first_uniform_top, last_uniform_top)

                    if scan_row >= start_row and scan_row < height:
                        row_pixels = image[scan_row, first_col:last_col+1]

                        if len(row_pixels) > 0:
                            line_avg_color = np.mean(row_pixels, axis=0)
                            color_diffs = np.abs(row_pixels.astype(np.float32) - line_avg_color)
                            max_allowed_diff = 255 * 0.02
                            matching_pixels = np.all(color_diffs <= max_allowed_diff, axis=1)
                            matching_percentage = np.sum(matching_pixels) / len(row_pixels)

                            if matching_percentage >= 0.98:
                                valid_lines.append(scan_row)

        if valid_lines:
            cutoff_row = min(valid_lines)
        elif column_heights:
            cutoff_row = min_uniform_top
        else:
            return -1, False

        offset = round(2 + 3/math.log(3100/670) * math.log(height/670))
        if offset < 2:
            offset = 2

        separator_row = cutoff_row - offset

        height_38_percent = int(height * 0.38)
        height_42_percent = int(height * 0.42)

        needs_fallback = (separator_row == -1) or (height_38_percent <= cutoff_row <= height_42_percent)

        if needs_fallback:
            fallback_start_row = int(height * 0.75)
            fallback_separator = self.find_separator_fallback(image, fallback_start_row)
            if fallback_separator != -1:
                separator_row = fallback_separator
                return separator_row, True

        return separator_row, False

    def find_separator_fallback(self, image, start_row):
        """Fallback method to find separator"""
        height, width = image.shape[:2]

        fallback_consecutive_similar_lines = 0
        fallback_separator_row = -1

        fallback_required_lines = round((4 / math.log(3100 / 670)) * math.log(height / 670) + 5)
        if fallback_required_lines <= 1:
            fallback_required_lines = 2

        white_color = np.array([255, 255, 255], dtype=np.uint8)
        fbf9fa_color = np.array([250, 249, 251], dtype=np.uint8)
        color_tolerance = 255 * 0.02

        for y_fallback in range(start_row, height):
            row_fallback = image[y_fallback]

            white_diff = np.abs(row_fallback.astype(np.float32) - white_color.astype(np.float32))
            white_matching = np.all(white_diff <= color_tolerance, axis=1)

            fbf9fa_diff = np.abs(row_fallback.astype(np.float32) - fbf9fa_color.astype(np.float32))
            fbf9fa_matching = np.all(fbf9fa_diff <= color_tolerance, axis=1)

            matching_pixels = white_matching | fbf9fa_matching
            matching_percentage = np.sum(matching_pixels) / width

            if matching_percentage >= 0.98:
                fallback_consecutive_similar_lines += 1
                if fallback_consecutive_similar_lines >= fallback_required_lines:
                    if fallback_consecutive_similar_lines >= 10:
                        fallback_separator_row_y_offset = fallback_consecutive_similar_lines + 5
                    elif fallback_consecutive_similar_lines in (1, 2, 3):
                        fallback_separator_row_y_offset = 2
                    else:
                        fallback_separator_row_y_offset = fallback_consecutive_similar_lines + 5

                    fallback_separator_row = y_fallback - fallback_separator_row_y_offset
                    break
            else:
                fallback_consecutive_similar_lines = 0

        return fallback_separator_row

    def crop_side_whitespace(self, image):
        """Crop white or fbf9fa colored sections from left and right sides"""
        height, width = image.shape[:2]

        white_color = np.array([255, 255, 255], dtype=np.uint8)
        fbf9fa_color = np.array([250, 249, 251], dtype=np.uint8)
        color_tolerance = 255 * 0.02

        left_crop = 0
        for x in range(width):
            column = image[:, x]

            white_diff = np.abs(column.astype(np.float32) - white_color.astype(np.float32))
            white_matching = np.all(white_diff <= color_tolerance, axis=1)

            fbf9fa_diff = np.abs(column.astype(np.float32) - fbf9fa_color.astype(np.float32))
            fbf9fa_matching = np.all(fbf9fa_diff <= color_tolerance, axis=1)

            matching_pixels = white_matching | fbf9fa_matching
            matching_percentage = np.sum(matching_pixels) / height

            if matching_percentage >= 0.98:
                left_crop = x + 1
            else:
                break

        right_crop = width
        for x in range(width-1, -1, -1):
            column = image[:, x]

            white_diff = np.abs(column.astype(np.float32) - white_color.astype(np.float32))
            white_matching = np.all(white_diff <= color_tolerance, axis=1)

            fbf9fa_diff = np.abs(column.astype(np.float32) - fbf9fa_color.astype(np.float32))
            fbf9fa_matching = np.all(fbf9fa_diff <= color_tolerance, axis=1)

            matching_pixels = white_matching | fbf9fa_matching
            matching_percentage = np.sum(matching_pixels) / height

            if matching_percentage >= 0.98:
                right_crop = x
            else:
                break

        expansion = int(round((4 / math.log(3100 / 670)) * math.log(height / 670) + 5))
        left_expanded = max(0, left_crop - expansion)
        right_expanded = min(width, right_crop + expansion)

        if left_expanded < right_expanded:
            return image[:, left_expanded:right_expanded]
        else:
            return image

    def crop_image_sections(self, image, separator_row, apply_side_crop=False):
        """Split image into photo section and text section"""
        if apply_side_crop:
            image = self.crop_side_whitespace(image)

        if separator_row == -1:
            return None, image

        photo_section = image[:separator_row, :]
        text_section = image[separator_row:, :]

        if photo_section is None or photo_section.size == 0 or photo_section.shape[0] < 1:
            return None, image

        return photo_section, text_section

    def clean_ocr_text(self, text):
        """Clean OCR text with find and replace operations"""
        if not text or text.startswith("OCR Error"):
            return text

        text = text.replace('|', '।')
        text = text.replace('। পিআইডি', '।')
        text = text.replace('।পিআইডি', '।')
        text = text.replace(' - পিআইডি', '')
        text = text.replace(' -পিআইডি', '')
        text = text.replace('- পিআইডি', '')
        text = text.replace('-পিআইডি', '')

        return text

    @retry_on_failure(max_attempts=10, delay=2)
    def perform_ocr(self, image):
        """Perform OCR on the text section using Google Cloud Vision API"""
        try:
            _, buffer = cv2.imencode('.png', image)
            image_bytes = buffer.tobytes()

            vision_image = vision.Image(content=image_bytes)
            image_context = vision.ImageContext(language_hints=['bn', 'en'])

            response = self.vision_client.text_detection(
                image=vision_image,
                image_context=image_context
            )

            if response.text_annotations:
                raw_text = response.text_annotations[0].description
                normalized = re.sub(r'\s+', ' ', raw_text).strip()
                cleaned_text = self.clean_ocr_text(normalized)

                return cleaned_text
            else:
                return ""

        except Exception as e:
            return f"OCR Error: {str(e)}"

    def process_image(self, row_index, image_url):
        """Process a single image - download, split, OCR"""
        result = {
            'image': None,
            'format': 'jpg',
            'exif': None,
            'ocr_text': '',
            'status': ''
        }

        try:
            if not image_url or image_url == 'nan':
                result['status'] = 'No URL provided'
                return result

            print(f"Row {row_index}: Downloading image...")
            image, img_format, exif_data, error = self.download_image(image_url)
            if error:
                if "404" in error:
                    result['status'] = error
                elif "Wayback Machine" in error:
                    result['status'] = "Retrieved from archive"
                else:
                    result['status'] = error
                return result

            result['format'] = img_format
            result['exif'] = exif_data

            print(f"Row {row_index}: Finding separator...")
            separator_row, fallback_used = self.find_white_separator(image)

            photo_section, text_section = self.crop_image_sections(image, separator_row, apply_side_crop=fallback_used)

            if photo_section is None or separator_row == -1:
                result['status'] = 'No separator found - using full image'
                result['image'] = image

                print(f"Row {row_index}: Performing OCR on full image...")
                ocr_text = self.perform_ocr(image)
                result['ocr_text'] = ocr_text

                if ocr_text.startswith("OCR Error"):
                    result['status'] = 'OCR failed'
                elif not ocr_text:
                    result['status'] = 'No text detected'
                else:
                    result['status'] = 'Success - full image'
            else:
                result['image'] = photo_section

                print(f"Row {row_index}: Performing OCR...")
                ocr_text = self.perform_ocr(text_section)
                result['ocr_text'] = ocr_text

                if ocr_text.startswith("OCR Error"):
                    result['status'] = 'OCR failed'
                elif not ocr_text:
                    result['status'] = 'No text detected'
                else:
                    result['status'] = 'Success'

            print(f"Row {row_index}: Image processing completed")

        except Exception as e:
            result['status'] = f"Error: {str(e)}"
            print(f"Row {row_index}: Error - {str(e)}")

        return result

# ============================================================================
# TRANSLATION FUNCTIONS
# ============================================================================

def contains_bengali(text):
    """Check if text contains any Bengali characters"""
    if not text:
        return False
    for char in text:
        if '\u0980' <= char <= '\u09FF':
            return True
    return False

def google_translate(translate_client, text):
    """Translate Bengali text to English using Google Translate API"""
    try:
        result = translate_client.translate(text, source_language='bn', target_language='en')
        return result['translatedText']
    except Exception as e:
        print(f"Google Translate error: {e}")
        return None

def translate_text(genai_client, translate_client, text, row_index):
    """Translate Bengali text to English"""
    if not text.strip():
        return "", "EmptyText"

    prompt = TRANSLATION_PROMPT.format(text=text.replace('"', "'"))

    for model_name in [PRIMARY_MODEL, FALLBACK_MODEL]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"Row {row_index}: Sending translation request to {model_name}...")

                # Add timeout configuration
                generation_config = {
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "max_output_tokens": 8192,
                }

                resp = genai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=generation_config
                )
                sleep(2)

                print(f"Row {row_index}: Received translation response from {model_name}")
                sleep(1)

                if hasattr(resp, "text"):
                    translated = resp.text.strip()
                else:
                    translated = resp.candidates[0].content.parts[0].text.strip()

                translated = (translated or "").strip()
                if not translated:
                    raise RuntimeError("Empty response")

                if contains_bengali(translated):
                    print(f"Row {row_index}: Bengali detected in Gemini output, using Google Translate")
                    gt_result = google_translate(translate_client, translated)
                    if gt_result:
                        translated = gt_result
                        sleep(1)

                print(f"Row {row_index}: Translated with {model_name}")
                return translated, "Success"


            except Exception as e:
                print(f"Row {row_index}: {model_name} translation attempt {attempt} failed: {e}")
                if attempt == MAX_RETRIES:
                    if model_name == FALLBACK_MODEL:
                        return "", f"Error:{repr(e)}"
                    else:
                        break

    return "", "Error: All models failed"

# ============================================================================
# TITLE GENERATION FUNCTIONS
# ============================================================================

def check_internet():
    """Check if internet is available"""
    try:
        requests.get("https://www.google.com", timeout=5)
        return True
    except:
        return False

def replace_date_if_needed(title, col_b_date_str):
    """Replace date in title if difference > 7 days"""
    col_b_match = re.search(r'(\d{4}-\d{2}-\d{2})', col_b_date_str)
    if not col_b_match:
        return title

    col_b_date_str_clean = col_b_match.group(1)
    col_b_date = datetime.strptime(col_b_date_str_clean, '%Y-%m-%d')

    title_dates = re.findall(r'\d{4}-\d{2}-\d{2}', title)
    if not title_dates:
        return title

    closest_date = None
    min_diff = float('inf')

    for date_str in title_dates:
        title_date = datetime.strptime(date_str, '%Y-%m-%d')
        diff_days = abs((title_date - col_b_date).days)

        if diff_days > 7 and diff_days < min_diff:
            min_diff = diff_days
            closest_date = date_str

    if closest_date:
        title = title.replace(closest_date, col_b_date_str_clean, 1)

    return title

def generate_title(genai_client, description, date_str, row_index, img_format='jpg'):
    """Generate Wikimedia Commons compliant filename"""
    text = f"{description} {date_str}".strip()

    if not text.strip():
        return "", "EmptyText"

    prompt = TITLE_PROMPT.format(text=text.replace('"',"'"))

    models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL]
    last_exception = None

    for model in models_to_try:
        backoff = INITIAL_BACKOFF

        for attempt in range(1, MAX_RETRIES + 1):
            while not check_internet():
                print(f"Row {row_index}: Waiting for internet connection...")
                sleep(5)

            try:
                print(f"Row {row_index}: Sending request to {model}...")

                # Add timeout configuration
                generation_config = {
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "max_output_tokens": 2048,
                }

                resp = genai_client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=generation_config
                )
                sleep(2)

                print(f"Row {row_index}: Received response from {model}")
                sleep(1)

                if hasattr(resp, "text"):
                    title = resp.text.strip()
                else:
                    title = resp.candidates[0].content.parts[0].text.strip()

                title = (title or "").strip()

                if not title:
                    raise RuntimeError("Empty response")

                if len(title.encode('utf-8')) > 240:
                    if attempt < MAX_RETRIES:
                        print(f"Row {row_index}: Title too long ({len(title.encode('utf-8'))} bytes), retrying")
                        wait = min(backoff, MAX_BACKOFF) + random.uniform(0, backoff * 0.5)
                        sleep(wait)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue
                    else:
                        raise RuntimeError(f"Title exceeds 240 bytes after {MAX_RETRIES} attempts")

                title = replace_date_if_needed(title, date_str)

                print(f"Row {row_index}: Title generated with {model} (without extension): {title}")

                # Add extension at the very end
                title = title + '.' + img_format

                print(f"Row {row_index}: Final title (with extension): {title}")
                sleep(2)
                return title, "Success"

            except Exception as e:
                last_exception = e
                msg = str(e).lower()

                is_429 = ("429" in msg) or ("resource exhausted" in msg)
                is_transient = is_429 or ("timeout" in msg) or ("connection" in msg) or ("temporar" in msg) or ("503" in msg) or ("500" in msg)

                if is_transient and attempt < MAX_RETRIES:
                    wait = min(backoff, MAX_BACKOFF) + random.uniform(0, backoff * 0.5)
                    print(f"Row {row_index}: Transient error on {model} (attempt {attempt}): {e}, retrying in {wait:.1f}s")
                    sleep(wait)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue
                else:
                    print(f"Row {row_index}: Model {model} error (no more retries): {e}")
                    break

    print(f"Row {row_index}: Failed all models: {last_exception}")
    return "", f"Error:{repr(last_exception)}"

# ============================================================================
# PYWIKIBOT UPLOAD FUNCTIONS
# ============================================================================

def initialize_pywikibot():
    """Initialize Pywikibot"""
    try:
        if not os.path.exists(USER_CONFIG_PATH):
            logger.error(f"Config file not found: {USER_CONFIG_PATH}")
            return None
        if not os.path.exists(PASSWORD_FILE_PATH):
            logger.error(f"Password file not found: {PASSWORD_FILE_PATH}")
            return None

        import pywikibot
        from pywikibot import FilePage

        site = pywikibot.Site('commons', 'commons')
        site.login()

        logger.info(f"Successfully logged in to Wikimedia Commons")
        return site, FilePage

    except Exception as e:
        logger.error(f"Failed to initialize Pywikibot: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None

def upload_to_commons(site, FilePage, image, target_filename, img_format, exif_data, description, max_attempts=10):
    """Upload image to Wikimedia Commons"""
    from pywikibot.exceptions import UploadError

    # Filename should already have correct extension from title generation
    # No extension checking or modification here - use filename as-is

    # Save image temporarily with correct format
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{img_format}')
    try:
        # Convert OpenCV image back to PIL to preserve EXIF
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(img_rgb)

        # Save with EXIF data if available
        save_kwargs = {}
        if exif_data:
            save_kwargs['exif'] = exif_data

        if img_format == 'png':
            img_pil.save(temp_file.name, 'PNG', optimize=True, **save_kwargs)
        elif img_format == 'jpg':
            img_pil.save(temp_file.name, 'JPEG', quality=95, **save_kwargs)
        else:
            img_pil.save(temp_file.name, **save_kwargs)

        # Try uploading with retries
        for attempt in range(max_attempts):
            try:
                file_page = FilePage(site, f'File:{target_filename}')

                if file_page.exists():
                    logger.info(f"File already exists: {target_filename}")
                    return False, 'File already exists'

                logger.info(f"Uploading {target_filename} (attempt {attempt + 1}/{max_attempts})")

                success = file_page.upload(
                    source=temp_file.name,
                    comment=f"Pypan 0.1.1a0",
                    text=description,
                    ignore_warnings=True,
                )

                if success:
                    logger.info(f"Successfully uploaded {target_filename}")
                    return True, ''
                else:
                    logger.warning(f"Upload failed - server response for {target_filename}")

            except UploadError as e:
                logger.warning(f"Upload warning for {target_filename}: {str(e)}")

            except Exception as e:
                logger.error(f"Error uploading {target_filename}: {str(e)}")

            if attempt < max_attempts - 1:
                logger.info(f"Waiting 10 seconds before retry...")
                sleep(10)

        return False, 'Max attempts reached'

    finally:
        # Clean up temp file
        try:
            os.unlink(temp_file.name)
        except:
            pass

def update_pid_date_data(site, data_entry):
    """Update the PIDDateData module page with new entry"""
    try:
        current_year = datetime.now().year
        page_title = f"Module:PIDDateData/{current_year}"

        import pywikibot
        page = pywikibot.Page(site, page_title)

        if not page.exists():
            logger.error(f"Page does not exist: {page_title}")
            return False

        page_text = page.text
        last_brace_index = page_text.rfind('}')

        if last_brace_index == -1:
            logger.error("Could not find closing brace in page")
            return False

        new_entry = f"    {data_entry}\n"
        updated_text = page_text[:last_brace_index] + new_entry + page_text[last_brace_index:]

        page.text = updated_text
        page.save(summary="added another image")

        logger.info(f"Successfully updated {page_title}")
        return True

    except Exception as e:
        logger.error(f"Error updating PIDDateData: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False

def excel_to_wikitable(df):
    """Convert pandas DataFrame to wikitable format"""
    wikitable = '{| class="wikitable sortable"\n'

    # Add headers
    wikitable += '! Unique ID !! Date !! Image URL !! !! OCR Text !! Status !! Translation !! Trans Status !! Title !! Title Status !! Data Entry !! PIDDateData Status !! Description !! Upload Status\n'

    # Add rows
    for idx in range(len(df)):
        wikitable += '|-\n'
        for col in range(min(14, df.shape[1])):
            cell_value = str(df.iat[idx, col]) if pd.notna(df.iat[idx, col]) else ""

            # Special handling for column 0 (Unique ID) - add File link with title from column 8
            if col == 0:
                title_value = str(df.iat[idx, 8]) if pd.notna(df.iat[idx, 8]) else ""
                if title_value:
                    # Add [[File:title]] before the unique ID
                    cell_value = f"[[File:{title_value}|100px]] {cell_value}"
                # Escape wiki markup
                cell_value = cell_value.replace('|', '{{!}}').replace('\n', '<br>')

            # Special handling for column 12 (Description) - wrap in <nowiki> tags
            elif col == 12:
                # Remove the leading apostrophe if present
                if cell_value.startswith("'"):
                    cell_value = cell_value[1:]
                # Wrap in <nowiki> tags
                cell_value = f"<nowiki>{cell_value}</nowiki>"

            # Default handling for all other columns
            else:
                # Escape wiki markup
                cell_value = cell_value.replace('|', '{{!}}').replace('\n', '<br>')

            wikitable += f'| {cell_value}\n'

    wikitable += '|}'
    return wikitable

def log_to_commons(site, df=None, success_count=0, failed_count=0, total_rows=0):
    """Log processing results to Wikimedia Commons user page"""
    try:
        import pywikibot
        from datetime import datetime

        # Generate log page title with current month and year
        current_date = datetime.now()
        month_name = current_date.strftime("%B")  # Full month name (e.g., "November")
        year = current_date.strftime("%Y")
        page_title = f"User:PID-Bangladesh-UploadBot/Log/{month_name} {year}"

        page = pywikibot.Page(site, page_title)

        # Generate timestamp
        timestamp = current_date.strftime("%Y-%m-%d %H:%M:%S UTC")

        if df is None:
            # No new images found
            log_entry = f"\n\n{timestamp} \nBot run completed. No new images found.\n"
        else:
            # Convert DataFrame to wikitable
            wikitable = excel_to_wikitable(df)

            # Create log entry
            log_entry = f"\n\n== {timestamp} ==\n"
            log_entry += f"Processed {total_rows} images. "
            log_entry += f"Successful uploads: {success_count}, Failed: {failed_count}\n\n"
            log_entry += wikitable + "\n"

        # Append to existing page or create new one
        if page.exists():
            page.text = page.text + log_entry
        else:
            page.text = f"Upload Log for {month_name} {year} =\n" + log_entry

        page.save(summary="Bot log update")
        logger.info(f"Successfully logged to {page_title}")
        return True

    except Exception as e:
        logger.error(f"Error logging to Commons: {str(e)}")
        return False

# ============================================================================
# MAIN PIPELINE
# ============================================================================

def load_credentials():
    """Load Google Cloud credentials from environment variable or JSON file"""
    global GOOGLE_CREDENTIALS

    # Try environment variable first (for Toolforge)
    creds_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')
    if creds_json:
        try:
            GOOGLE_CREDENTIALS = json.loads(creds_json)

            # Validate required fields
            required_fields = ["type", "project_id", "private_key", "client_email"]
            missing_fields = [field for field in required_fields if field not in GOOGLE_CREDENTIALS]

            if missing_fields:
                print(f"ERROR: Credential missing required fields: {', '.join(missing_fields)}")
                sys.exit(1)

            print("Credentials loaded from environment variable")
            return True
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON in environment variable: {e}")

    # Fallback to JSON file (for local development)
    try:
        json_files = [f for f in os.listdir(SCRIPT_DIR) if f.endswith('.json')]
    except FileNotFoundError:
        print(f"ERROR: Script directory not found: {SCRIPT_DIR}")
        sys.exit(1)

    if not json_files:
        print("ERROR: No credentials found in environment or JSON file")
        sys.exit(1)

    if len(json_files) > 1:
        print(f"WARNING: Multiple JSON files found. Using the first one: {json_files[0]}")

    creds_file = os.path.join(SCRIPT_DIR, json_files[0])
    print(f"Loading credentials from: {creds_file}")

    try:
        with open(creds_file, 'r') as f:
            GOOGLE_CREDENTIALS = json.load(f)

        # Validate required fields
        required_fields = ["type", "project_id", "private_key", "client_email"]
        missing_fields = [field for field in required_fields if field not in GOOGLE_CREDENTIALS]

        if missing_fields:
            print(f"ERROR: Credential file missing required fields: {', '.join(missing_fields)}")
            sys.exit(1)

        print("Credentials loaded from file")
        return True
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Failed to load credentials from file: {e}")
        sys.exit(1)

def setup_credentials():
    """Set up Google credentials"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(GOOGLE_CREDENTIALS, f)
        creds_path = f.name

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    os.environ["GOOGLE_CLOUD_PROJECT"] = GOOGLE_CREDENTIALS["project_id"]
    os.environ["GOOGLE_CLOUD_LOCATION"] = VERTEX_LOCATION
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"

    return creds_path

def main():
    print("=" * 60)
    print("PID Image Processor & Uploader")
    print("=" * 60)
    print()

    # Check Pywikibot config files
    if not os.path.exists(USER_CONFIG_PATH):
        print(f"ERROR: Config file not found: {USER_CONFIG_PATH}")
        print("Please create user-config.py in the same directory as this script")
        sys.exit(1)

    if not os.path.exists(PASSWORD_FILE_PATH):
        print(f"ERROR: Password file not found: {PASSWORD_FILE_PATH}")
        print("Please create user-password.py in the same directory as this script")
        sys.exit(1)

    # Step 1: Scrape data
    print("\n" + "=" * 60)
    print("STEP 1: Scraping data from pressinform.gov.bd")
    print("=" * 60)
    excel_file = scrape_data()

    # Load and setup Google credentials
    print("\nLoading Google credentials...")
    load_credentials()
    print("Setting up Google credentials...")
    creds_path = setup_credentials()

    try:
        # Initialize clients
        print("Initializing Google Cloud clients...")
        image_processor = ImageProcessor()
        success, message = image_processor.initialize_vision_client()
        if not success:
            print(f"Error: {message}")
            sys.exit(1)
        print(message)

        genai_client = genai.Client(
            vertexai=True,
            project=GOOGLE_CREDENTIALS["project_id"],
            location=VERTEX_LOCATION
        )
        translate_client = translate.Client()
        print("Google GenAI and Translate clients initialized")

        # Initialize Pywikibot
        print("\nInitializing Pywikibot...")
        result = initialize_pywikibot()
        if result is None:
            print("Error: Failed to initialize Pywikibot")
            sys.exit(1)

        site, FilePage = result

        # Check if Excel file was created
        if excel_file is None:
            print("\nNo new images found. Logging to Commons...")
            if log_to_commons(site, df=None):
                print("Log entry created on Commons.")
            else:
                print("Warning: Failed to log to Commons.")
            return

        # Load Excel file
        print(f"\nLoading Excel file: {excel_file}")
        df = pd.read_excel(excel_file, header=None)
        total_rows = len(df)
        print(f"Total rows to process: {total_rows}")

        # Ensure enough columns exist
        while df.shape[1] < 14:
            df[df.shape[1]] = ""

        # Process each row
        success_count = 0
        failed_count = 0

        for idx in range(total_rows):
            print(f"\n{'='*60}")
            print(f"Processing row {idx + 1}/{total_rows}")
            print(f"{'='*60}")

            try:
                unique_id = str(df.iat[idx, 0]) if pd.notna(df.iat[idx, 0]) else f"image_{idx}"
                date_str = str(df.iat[idx, 1]) if pd.notna(df.iat[idx, 1]) else ""
                image_url = str(df.iat[idx, 2]) if pd.notna(df.iat[idx, 2]) else ""

                if not image_url or image_url == 'nan':
                    print(f"Row {idx + 1}: No URL, skipping")
                    df.iat[idx, 5] = "No URL"
                    df.to_excel(excel_file, index=False, header=False)
                    continue

                # Step 2: Process image (download, split, OCR)
                print(f"\nSTEP 2: Processing image...")
                result = image_processor.process_image(idx + 1, image_url)

                df.iat[idx, 4] = result['ocr_text']  # Column E: OCR text
                df.iat[idx, 5] = result['status']     # Column F: Status
                df.to_excel(excel_file, index=False, header=False)

                if result['image'] is None or result['status'].startswith('Error') or result['status'].startswith('OCR failed'):
                    print(f"Row {idx + 1}: Image processing failed")
                    failed_count += 1
                    continue

                # Get image format
                img_format = result.get('format', 'jpg')

                # Step 3: Translate Bengali to English
                print(f"\nSTEP 3: Translating text...")
                bengali_text = result['ocr_text']
                print(f"Sanitized OCR Data: {bengali_text}")
                translation, trans_status = translate_text(genai_client, translate_client, bengali_text, idx + 1)
                print(f"Translation Data: {translation}")

                df.iat[idx, 6] = translation     # Column G: Translation
                df.iat[idx, 7] = trans_status    # Column H: Translation status
                df.to_excel(excel_file, index=False, header=False)

                if trans_status != "Success":
                    print(f"Row {idx + 1}: Translation failed")
                    failed_count += 1
                    continue

                # Step 4: Generate title
                print(f"\nSTEP 4: Generating title...")
                title, title_status = generate_title(genai_client, translation, date_str, idx + 1, img_format)
                print(f"Full Title Data (with extension): {title}")

                df.iat[idx, 8] = title          # Column I: Title
                df.iat[idx, 9] = title_status   # Column J: Title status
                df.to_excel(excel_file, index=False, header=False)

                if title_status != "Success":
                    print(f"Row {idx + 1}: Title generation failed")
                    failed_count += 1
                    continue

                # Step 5: Prepare description and data entry
                print(f"\nSTEP 5: Preparing metadata...")
                data_entry = f'''        ["{image_url}"] = "{date_str}",'''
                df.iat[idx, 10] = data_entry  # Column K: Data entry

                description = f'''=={{{{int:filedesc}}}}==
{{{{Information
 |description = {{{{bn|1={bengali_text}}}}}{{{{en|1={translation}{{{{Auto-translated PID English description}}}}}}}}
 |date = {{{{Date-PID|{date_str}}}}}
 |source = {{{{Source-PID | url={image_url}}}}}
 |author = {{{{Institution:Press Information Department}}}}
 |permission =
 |other versions =
}}}}
=={{{{int:license-header}}}}==
{{{{PD-BDGov-PID}}}}
[[Category: Uploaded with pypan]]'''

                df.iat[idx, 12] = "'" + description  # Column M: Description
                df.to_excel(excel_file, index=False, header=False)

                # Step 6: Upload to Wikimedia Commons
                print(f"\nSTEP 6: Uploading to Wikimedia Commons...")
                upload_success, upload_error = upload_to_commons(
                    site, FilePage, result['image'], title, img_format, result.get('exif'), description
                )

                if upload_success:
                    df.iat[idx, 13] = "Success"  # Column N: Upload status
                    success_count += 1
                    print(f"Row {idx + 1}: Upload successful")

                    sleep(5)

                    # Update PIDDateData
                    print(f"Row {idx + 1}: Updating PIDDateData...")
                    if update_pid_date_data(site, data_entry):
                        df.iat[idx, 11] = "Success"  # Column L: PIDDateData status
                        print(f"Row {idx + 1}: PIDDateData updated")
                    else:
                        df.iat[idx, 11] = "Failed"
                        print(f"Row {idx + 1}: PIDDateData update failed")
                else:
                    df.iat[idx, 13] = f"Failed: {upload_error}"
                    failed_count += 1
                    print(f"Row {idx + 1}: Upload failed - {upload_error}")

                df.to_excel(excel_file, index=False, header=False)

            except Exception as e:
                logger.error(f"Error processing row {idx + 1}: {str(e)}")
                df.iat[idx, 13] = f"Error: {str(e)}"
                failed_count += 1
                df.to_excel(excel_file, index=False, header=False)

        # Final save
        df.to_excel(excel_file, index=False, header=False)

        # Log results to Commons
        print("\nLogging results to Wikimedia Commons...")
        if log_to_commons(site, df, success_count, failed_count, total_rows):
            # Delete Excel file after successful logging
            try:
                os.unlink(excel_file)
                print(f"Excel file deleted: {excel_file}")
            except Exception as e:
                print(f"Warning: Could not delete Excel file: {e}")

        print("\n" + "=" * 60)
        print("PROCESSING COMPLETED")
        print("=" * 60)
        print(f"Total rows processed: {total_rows}")
        print(f"Successful uploads: {success_count}")
        print(f"Failed uploads: {failed_count}")
        print(f"Results saved to: {excel_file}")
        print("=" * 60)

    finally:
        # Clean up credentials file
        try:
            os.unlink(creds_path)
        except:
            pass

def run_as_job():
    """Run as a Toolforge job"""
    main()

if __name__ == "__main__":
    # Check if running as web service
    if os.environ.get('TOOLFORGE_WEBSERVICE'):
        app = Flask(__name__)

        @app.route('/')
        def home():
            return "PID Image Processor is running. Use job submission to process images."

        @app.route('/health')
        def health():
            return {'status': 'healthy'}

        app.run(host='0.0.0.0', port=8000)
    else:
        main()