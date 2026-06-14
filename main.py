import sys
import os
import math
import re
import cv2
import numpy as np
import pandas as pd
import requests
from io import BytesIO
from PIL import Image, ImageFile, ImageOps
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings('ignore')
from urllib.parse import quote, unquote
from google.cloud import translate_v2 as translate
from googleapiclient.discovery import build as gdrive_build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google import genai
from google.genai import types
import time
from time import sleep
from functools import wraps
from datetime import datetime
import hashlib
import tempfile
import json
import random
import logging
from openpyxl import Workbook
from flask import Flask
import pywikibot
from pywikibot import FilePage
from pywikibot.exceptions import UploadError
import socket
import urllib3.util.connection as urllib3_cn
import traceback

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
VERTEX_LOCATION = "global"
PRIMARY_MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODEL = "gemini-3.5-flash"
GEMINI_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'gemini.key') # AI Studio free API key
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF = 60.0

# Google Cloud credentials - will be loaded from JSON file
GOOGLE_CREDENTIALS = None

# OAuth2 token for Google Drive OCR
DRIVE_TOKEN_PATH = os.path.join(SCRIPT_DIR, 'drive_token.json')
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.file']

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

def allowed_gai_family():
    """Force IPv4 connections only"""
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family
print("Forced IPv4 connections to avoid K8s networking issues")

def load_gemini_api_key():
    """Load free AI Studio API key from hidden config file (secondary Google account)"""
    if not os.path.exists(GEMINI_CONFIG_PATH):
        raise RuntimeError(
            f"Gemini config not found: {GEMINI_CONFIG_PATH}\n"
            f"Fix: echo 'GEMINI_API_KEY=your_key' > {GEMINI_CONFIG_PATH} && chmod 600 {GEMINI_CONFIG_PATH}"
        )
    with open(GEMINI_CONFIG_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('GEMINI_API_KEY='):
                return line.split('=', 1)[1].strip()
    raise RuntimeError(f"GEMINI_API_KEY not found in {GEMINI_CONFIG_PATH}")

def compute_checksum(raw_bytes):
    """Compute MD5 checksum of raw image bytes for duplicate detection"""
    return hashlib.md5(raw_bytes).hexdigest()

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
                checksums = set()
                pattern = r'\["(http[^"]+)"\]'
                matches = re.findall(pattern, content)
                print(f"Found {len(matches)} URLs in {year} module")

                for match in matches:
                    normalized = normalize_url(match)
                    urls.add(normalized)

                # Extract checksums from new-format entries - backwards compatible
                # Old format: ["url"] = "date"
                # New format: ["url"] = {date="date", checksum="hash"}
                checksum_pattern = r'\["[^"]+"\]\s*=\s*\{[^}]*checksum\s*=\s*"([^"]+)"'
                for cs in re.findall(checksum_pattern, content):
                    checksums.add(cs)
                print(f"Found {len(checksums)} checksums in {year} module")

                if len(urls) > 0:
                    return urls, checksums
        except Exception as e:
            print(f"Error with URL {url}: {e}")
            continue

    print(f"Could not fetch Wikimedia data for {year}")
    return set(), set()

def convert_bengali_date_to_english(bengali_date_text):
    """Convert Bengali date to English yyyy-mm-dd hh:mm:ss format"""
    # Bengali to English digit mapping
    bengali_digits = {'০': '0', '১': '1', '২': '2', '৩': '3', '৪': '4',
                     '৫': '5', '৬': '6', '৭': '7', '৮': '8', '৯': '9'}

    # Bengali to English month mapping
    bengali_months = {
        'জানুয়ারী': '01', 'জানুয়ারি': '01',
        'ফেব্রুয়ারী': '02', 'ফেব্রুয়ারি': '02',
        'মার্চ': '03',
        'এপ্রিল': '04',
        'মে': '05',
        'জুন': '06',
        'জুলাই': '07',
        'আগস্ট': '08',
        'সেপ্টেম্বর': '09',
        'অক্টোবর': '10',
        'নভেম্বর': '11',
        'ডিসেম্বর': '12'
    }

    try:
        # Extract date from text like "বৃহস্পতিবার, ৮ জানুয়ারী, ২০২৬ এ ০৯:৪৩ PM"
        # Pattern: day, date month, year এ time AM/PM
        match = re.search(r'([০-৯\d]+)\s+([^\s,]+),?\s+([০-৯\d]+)\s+এ\s+([০-৯\d]+):([০-৯\d]+)\s+(AM|PM)', bengali_date_text)

        if not match:
            return ""

        day = match.group(1)
        month_bengali = match.group(2)
        year = match.group(3)
        hour = match.group(4)
        minute = match.group(5)
        am_pm = match.group(6)

        # Convert Bengali digits to English
        day_en = ''.join(bengali_digits.get(c, c) for c in day)
        year_en = ''.join(bengali_digits.get(c, c) for c in year)
        hour_en = ''.join(bengali_digits.get(c, c) for c in hour)
        minute_en = ''.join(bengali_digits.get(c, c) for c in minute)

        # Convert month
        month_en = bengali_months.get(month_bengali, '01')

        # Convert to 24-hour format
        hour_int = int(hour_en)
        if am_pm == 'PM' and hour_int != 12:
            hour_int += 12
        elif am_pm == 'AM' and hour_int == 12:
            hour_int = 0

        # Format as yyyy-mm-dd hh:mm:ss
        formatted_date = f"{year_en}-{month_en.zfill(2)}-{day_en.zfill(2)} {str(hour_int).zfill(2)}:{minute_en.zfill(2)}:00"

        return formatted_date

    except Exception as e:
        print(f"Error converting Bengali date: {e}")
        return ""

def fetch_detail_date(detail_href):
    """Fetch date from a detail page, with retries. Returns date string or empty string."""
    detail_url = f"https://pressinform.gov.bd{detail_href}"
    detail_max_retries = 10
    for detail_attempt in range(detail_max_retries):
        try:
            detail_response = requests.get(detail_url, timeout=10, verify=False)
            if detail_response.status_code == 200:
                detail_soup = BeautifulSoup(detail_response.content, 'html.parser')
                # Try div.content-update-block first, then any <p> containing Bengali date pattern
                date_element = detail_soup.find('div', class_='content-update-block')
                if not date_element:
                    # Fallback: find a <p> tag containing the Bengali date pattern (এ + AM/PM)
                    for p in detail_soup.find_all('p'):
                        if 'এ' in p.get_text() and ('AM' in p.get_text() or 'PM' in p.get_text()):
                            date_element = p
                            break
                if date_element:
                    date_text = date_element.get_text()
                    print(f"Date text found: {date_text.strip()}")
                    result = convert_bengali_date_to_english(date_text)
                    if result:
                        return result
                    else:
                        print(f"Date conversion failed for text: {date_text.strip()}")
                        return ""
                else:
                    print(f"No date found on detail page: {detail_url}")
                return ""
            else:
                print(f"Failed to fetch detail page (attempt {detail_attempt + 1}/{detail_max_retries}): {detail_url} - Status {detail_response.status_code}")
                if detail_attempt < detail_max_retries - 1:
                    time.sleep(2 ** detail_attempt)
        except Exception as e:
            print(f"Error fetching detail page (attempt {detail_attempt + 1}/{detail_max_retries}) {detail_url}: {e}")
            if detail_attempt < detail_max_retries - 1:
                time.sleep(2 ** detail_attempt)
    return ""


def scrape_page(page_num, wikimedia_urls, hard_stop_url):
    """Scrape a single page (page_size=50) row by row.
    Returns (results, hard_stop_hit) where results is a list of (img_url, detail_href) tuples
    for images not yet in Wikimedia, and hard_stop_hit is True if the hard stop URL was encountered.
    Date fetching is deferred — only done for images that need uploading.
    """
    url = f"https://pressinform.gov.bd/pages/daily-photos?archived=true&page={page_num}&page_size=50"
    print(f"Scraping page {page_num} (50 items)...")

    max_retries = 10
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=10, verify=False)
            if response.status_code != 200:
                print(f"Failed to fetch page {page_num} (status {response.status_code})")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                return [], False

            soup = BeautifulSoup(response.content, 'html.parser')
            table = soup.find('table', id='noticeTable')

            if not table:
                print(f"No table found on page {page_num}")
                return [], False

            results = []
            hard_stop_hit = False
            total_images_seen = 0
            rows = table.find('tbody', class_='table-tbody').find_all('tr', class_='table-tr')

            for row in rows:
                # Skip the search input row
                if 'toggle-hidden' in row.get('class', []):
                    continue

                # Find image TD
                img_td = row.find('td', {'data-column': 'file'})
                if not img_td:
                    continue

                # Get ALL img tags in this TD
                img_tags = img_td.find_all('img')
                if not img_tags:
                    continue

                detail_link = row.find('a', href=lambda x: x and '/pages/daily-photos/' in x and x != '#')
                detail_href = detail_link['href'] if detail_link else None

                # Collect new image URLs for this row, stopping at hard stop
                for img_tag in img_tags:
                    if not img_tag.get('src'):
                        continue
                    img_url = img_tag['src']
                    normalized_url = normalize_url(img_url)
                    if hard_stop_url in normalized_url:
                        print(f"\n{'='*60}")
                        print("HARD STOP: Reached the specified stopping point")
                        print(f"Image URL: {img_url}")
                        print(f"{'='*60}")
                        hard_stop_hit = True
                        break  # Do not include this image or any after it in this row
                    total_images_seen += 1
                    # Only keep images not already in Wikimedia
                    if normalized_url not in wikimedia_urls:
                        results.append((img_url, detail_href))
                    else:
                        print(f"Skipping (already in Wikimedia): {img_url}")

                if hard_stop_hit:
                    break  # Stop processing further rows on this page

            # If page had no images at all (not just all-uploaded), treat as end of content
            if total_images_seen == 0 and not hard_stop_hit:
                print(f"Page {page_num}: no images found at all, end of content")
                return None, False  # None signals "end of content" vs [] which means "all uploaded"

            return results, hard_stop_hit

        except Exception as e:
            wait_time = 2 ** attempt
            print(f"Error scraping page {page_num} (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"Failed after {max_retries} attempts")
                return [], False

    return [], False

def scrape_data():
    """Scrape data from pressinform.gov.bd"""
    output_dir = os.path.expanduser('~/output')
    os.makedirs(output_dir, exist_ok=True)

    current_year = datetime.now().year
    previous_year = current_year - 1

    print(f"Current year: {current_year}")
    print("Fetching Wikimedia data...")
    wikimedia_urls, wikimedia_checksums = fetch_wikimedia_data(current_year)
    print(f"Loaded {len(wikimedia_urls)} URLs from {current_year}")
    prev_year_urls, prev_year_checksums = fetch_wikimedia_data(previous_year)
    print(f"Loaded {len(prev_year_urls)} URLs from {previous_year}")
    wikimedia_urls.update(prev_year_urls)
    wikimedia_checksums.update(prev_year_checksums)
    print(f"Total URLs from Wikimedia: {len(wikimedia_urls)}, checksums: {len(wikimedia_checksums)}")

    wb = Workbook()
    ws = wb.active

    fully_uploaded_pages = 0
    page_num = 1
    entry_counter = 1

    # Hard stop URL - if this image is encountered, stop scraping
    HARD_STOP_URL = "objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/axvjbnqprylg/b/V2Ministry/o/office-pressinform/2024/12/ec18321a25e844ab9503b7b704aafb34.jpg"

    while fully_uploaded_pages < 3:
        new_items, hard_stop_hit = scrape_page(page_num, wikimedia_urls, HARD_STOP_URL)

        if new_items is None:
            print(f"Page {page_num}: no images found, end of content. Stopping scraper.")
            break

        if hard_stop_hit and not new_items:
            print(f"Hard stop reached on page {page_num} with no new items. Stopping scraper.")
            break

        if not new_items and not hard_stop_hit:
            # scrape_page filters out already-uploaded images, so empty means fully uploaded page
            fully_uploaded_pages += 1
            print(f"Page {page_num}: all items already uploaded ({fully_uploaded_pages}/3 fully-uploaded pages)")
        else:
            fully_uploaded_pages = 0
            # Fetch dates only for new images
            for img_url, detail_href in new_items:
                if detail_href:
                    date = fetch_detail_date(detail_href)
                    time.sleep(0.5)
                else:
                    date = ""
                    print(f"No detail link for image: {img_url}")
                unique_id = generate_unique_id(img_url, date, entry_counter)
                print(f"Adding: {unique_id} | {date} | {img_url}")
                ws.append([unique_id, date, img_url])
                entry_counter += 1
            print(f"Page {page_num}: {len(new_items)} new items added")

        if hard_stop_hit:
            print(f"Hard stop reached on page {page_num}. Stopping scraper.")
            break

        if fully_uploaded_pages >= 3:
            print(f"\nStopping scraper: 3 consecutive fully-uploaded pages of 50.")
            break

        page_num += 1
        time.sleep(1)

    # Check if any new entries were added
    if entry_counter == 1:  # No new entries found
        print("\nNo new images found. Skipping Excel file creation.")
        return None, wikimedia_checksums

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(output_dir, f"pressinform_photos_{timestamp}.xlsx")
    wb.save(output_file)
    print(f"\nData saved to {output_file}")
    print(f"Total rows written: {ws.max_row}")

    return output_file, wikimedia_checksums

# ============================================================================
# IMAGE PROCESSOR FUNCTIONS
# ============================================================================

class ImageProcessor:
    def __init__(self):
        self._drive_service = None

    def initialize_vision_client(self):
        """Initialize Google Drive OCR using OAuth2 user credentials"""
        try:
            if not os.path.exists(DRIVE_TOKEN_PATH):
                raise RuntimeError("drive_token.json not found. Run generate_token.py first.")
            creds = Credentials.from_authorized_user_file(DRIVE_TOKEN_PATH, DRIVE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
                with open(DRIVE_TOKEN_PATH, 'w') as f:
                    f.write(creds.to_json())
            self._drive_service = gdrive_build('drive', 'v3', credentials=creds)
            return True, "Drive OCR initialized with user OAuth2 credentials"
        except Exception as e:
            return False, f"Failed to initialize Drive OCR: {str(e)}"

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
            response = requests.get(url, headers=headers, timeout=30, verify=False)
            response.raise_for_status()

            ImageFile.LOAD_TRUNCATED_IMAGES = True

            raw_bytes = response.content
            img_pil = Image.open(BytesIO(raw_bytes))

            # Store EXIF data before any processing
            exif_data = img_pil.info.get('exif', None)
            img_pil = ImageOps.exif_transpose(img_pil)

            # onvert CMYK to RGB if needed
            if img_pil.mode == 'CMYK':
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
                return None, None, None, None, "Invalid image format"

            return img_cv, img_format, exif_data, raw_bytes, None

        except requests.exceptions.RequestException as e:
            if "404" in str(e) or (hasattr(e, 'response') and e.response is not None and e.response.status_code == 404):
                wayback_url, wayback_error = self.get_wayback_url(url)
                if wayback_url:
                    try:
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        }
                        response = requests.get(wayback_url, headers=headers, timeout=30, verify=False)
                        response.raise_for_status()

                        ImageFile.LOAD_TRUNCATED_IMAGES = True

                        raw_bytes = response.content
                        img_pil = Image.open(BytesIO(raw_bytes))

                        # Store EXIF data before any processing
                        exif_data = img_pil.info.get('exif', None)
                        img_pil = ImageOps.exif_transpose(img_pil)
                        # Convert CMYK to RGB if needed
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
                            return None, None, None, None, "Invalid image format"

                        return img_cv, img_format, exif_data, raw_bytes, "Retrieved from Wayback Machine"

                    except Exception as wb_e:
                        return None, None, None, None, f"404 error - Wayback Machine also failed: {str(wb_e)}"
                else:
                    return None, None, None, None, f"404 error - {wayback_error}"
            return None, None, None, None, f"Download failed: {str(e)}"
        except Exception as e:
            return None, None, None, None, f"Image processing error: {str(e)}"

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
                return separator_row, True, offset

        return separator_row, False, offset

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
        text = text.replace(' ﻿________________ ', '')
        text = text.replace('________________', '')
        text = text.replace('  ', ' ')
        text = text.replace('  ', ' ')

        return text

    @retry_on_failure(max_attempts=10, delay=2)
    def perform_ocr(self, image):
        """Perform OCR using Google Drive API (free, replaces Vision API)"""
        file_id = None
        temp_path = None
        try:
            # Save image section to a temp file
            temp_fd, temp_path = tempfile.mkstemp(suffix='.png')
            os.close(temp_fd)
            cv2.imwrite(temp_path, image)

            # Upload image to Drive as a Google Doc — Drive OCRs it automatically
            file_metadata = {
                'name': 'ocr_temp.png',
                'mimeType': 'application/vnd.google-apps.document',
            }
            media = MediaIoBaseUpload(
                open(temp_path, 'rb'),
                mimetype='image/png',
                resumable=False
            )
            uploaded = self._drive_service.files().create(
                body=file_metadata,
                media_body=media,
                ocrLanguage='bn',   # Bengali hint — improves accuracy
                fields='id'
            ).execute()
            file_id = uploaded.get('id')

            # Export the OCR'd Google Doc as plain text
            request = self._drive_service.files().export_media(
                fileId=file_id,
                mimeType='text/plain'
            )
            text_buffer = BytesIO()
            downloader = MediaIoBaseDownload(text_buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            raw_text = text_buffer.getvalue().decode('utf-8', errors='replace')

            # Strip the Drive separator line that appears in exported docs
            raw_text = raw_text.replace('________________\n\n', '')
            raw_text = re.sub(r'\s+', ' ', raw_text).strip()
            cleaned_text = self.clean_ocr_text(raw_text)
            return cleaned_text

        except Exception as e:
            return f"OCR Error: {str(e)}"

        finally:
            # Always delete the local temp file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception as del_e:
                    print(f"Warning: could not delete local temp file {temp_path}: {del_e}")
            # Always delete the temp file from Drive
            if file_id:
                try:
                    self._drive_service.files().delete(fileId=file_id).execute()
                except Exception as del_e:
                    print(f"Warning: could not delete temp Drive file {file_id}: {del_e}")

    def process_image(self, row_index, image_url, wikimedia_checksums=None):
        """Process a single image - download, split, OCR"""
        result = {
            'image': None,
            'format': 'jpg',
            'exif': None,
            'ocr_text': '',
            'status': '',
            'checksum': '',
            'is_duplicate': False
        }

        try:
            if not image_url or image_url == 'nan':
                result['status'] = 'No URL provided'
                return result

            print(f"Row {row_index}: Downloading image...")
            image, img_format, exif_data, raw_bytes, error = self.download_image(image_url)
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

            # Checksum duplicate check — runs BEFORE OCR/AI (saves quota)
            if raw_bytes:
                checksum = compute_checksum(raw_bytes)
                result['checksum'] = checksum
                if wikimedia_checksums and checksum in wikimedia_checksums:
                    print(f"Row {row_index}: Duplicate image detected via checksum — skipping OCR/AI")
                    result['status'] = 'Duplicate (checksum match)'
                    result['is_duplicate'] = True
                    result['image'] = image
                    return result
            else:
                result['checksum'] = ''

            print(f"Row {row_index}: Finding separator...")
            separator_row, fallback_used, separator_offset = self.find_white_separator(image)

            photo_section, text_section = self.crop_image_sections(image, separator_row, apply_side_crop=fallback_used)

            if photo_section is None or separator_row == -1:
                result['status'] = 'No separator found - using full image'
                result['image'] = image

                print(f"Row {row_index}: Performing OCR on bottom 40% of image...")
                height_fallback = image.shape[0]
                ocr_section = image[int(height_fallback * 0.60):, :]
                ocr_text = self.perform_ocr(ocr_section)
                result['ocr_text'] = ocr_text

                if ocr_text.startswith("OCR Error"):
                    result['status'] = 'OCR failed'
                elif not ocr_text:
                    result['status'] = 'No text detected'
                else:
                    result['status'] = 'Success - full image'
            else:
                result['image'] = photo_section

                print(f"Row {row_index}: Performing OCR on text section (trimmed by {1 * separator_offset}px)...")
                trim_top = min(2 * separator_offset, text_section.shape[0] - 1)
                ocr_section = text_section[trim_top:, :]
                ocr_text = self.perform_ocr(ocr_section)
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

def load_translation_replacements():
    """Load find/replace pairs from translation_replacements.tsv next to main.py.
    Format: BengaliText|||EnglishReplacement  (one per line, # for comments)
    """
    SEPARATOR = '|||'
    replacements = []
    tsv_path = os.path.join(SCRIPT_DIR, 'translation_replacements.tsv')
    if not os.path.exists(tsv_path):
        logger.info("No translation_replacements.tsv found, skipping pre-translation replacements")
        return replacements
    try:
        with open(tsv_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.rstrip('\n')
                if not line or line.startswith('#'):
                    continue
                if SEPARATOR not in line:
                    logger.warning(f"translation_replacements.tsv line {line_num}: missing '{SEPARATOR}' separator, skipping: {line!r}")
                    continue
                find_text, replace_text = line.split(SEPARATOR, 1)
                find_text = find_text.strip()
                replace_text = replace_text.strip()
                if find_text:
                    replacements.append((find_text, replace_text))
        logger.info(f"Loaded {len(replacements)} translation replacements from {tsv_path}")
    except Exception as e:
        logger.error(f"Error loading translation_replacements.tsv: {e}")
    return replacements


def apply_translation_replacements(text, replacements):
    """Apply pre-translation find/replace pairs to Bengali OCR text"""
    for find_text, replace_text in replacements:
        text = text.replace(find_text, replace_text)
    return text


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

    last_exception = None
    for model_name in [PRIMARY_MODEL, FALLBACK_MODEL]:
        backoff = INITIAL_BACKOFF
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
                last_exception = e
                msg = str(e).lower()
                is_429 = ("429" in msg) or ("resource exhausted" in msg)
                is_transient = is_429 or ("timeout" in msg) or ("connection" in msg) or ("temporar" in msg) or ("503" in msg) or ("500" in msg)

                if is_transient and attempt < MAX_RETRIES:
                    wait = min(backoff, MAX_BACKOFF) + random.uniform(0, backoff * 0.5)
                    print(f"Row {row_index}: Transient error on {model_name} (attempt {attempt}): {e}, retrying in {wait:.1f}s")
                    sleep(wait)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue
                else:
                    print(f"Row {row_index}: {model_name} translation error (attempt {attempt}): {e}")
                    break

    return "", f"Error:{repr(last_exception)}"

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

        site = pywikibot.Site('commons', 'commons')
        site.login()

        logger.info(f"Successfully logged in to Wikimedia Commons")
        return site, FilePage

    except Exception as e:
        logger.error(f"Failed to initialize Pywikibot: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None

def find_available_filename(site, FilePage, target_filename):
    """Check if filename exists on Commons; if so, append (1), (2), ... until a free slot is found."""
    if not FilePage(site, f'File:{target_filename}').exists():
        return target_filename

    dot_index = target_filename.rfind('.')
    if dot_index == -1:
        base, ext = target_filename, ''
    else:
        base, ext = target_filename[:dot_index], target_filename[dot_index:]

    for n in range(1, 100):
        candidate = f"{base} ({n}){ext}"
        if not FilePage(site, f'File:{candidate}').exists():
            logger.info(f"Filename collision resolved: '{target_filename}' → '{candidate}'")
            return candidate

    logger.warning(f"Could not resolve filename collision after 99 attempts: {target_filename}")
    return target_filename

def upload_to_commons(site, FilePage, image, target_filename, img_format, exif_data, description, max_attempts=10):
    """Upload image to Wikimedia Commons"""

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

        # Resolve collision once before the retry loop
        target_filename = find_available_filename(site, FilePage, target_filename)

        # Try uploading with retries
        for attempt in range(max_attempts):
            try:
                file_page = FilePage(site, f'File:{target_filename}')

                logger.info(f"Uploading {target_filename} (attempt {attempt + 1}/{max_attempts})")

                success = file_page.upload(
                    source=temp_file.name,
                    comment=f"Pypan 0.1.1a0",
                    text=description,
                    ignore_warnings=True,
                )

                if success:
                    logger.info(f"Successfully uploaded {target_filename}")
                    return True, '', target_filename
                else:
                    logger.warning(f"Upload failed - server response for {target_filename}")

            except UploadError as e:
                logger.warning(f"Upload warning for {target_filename}: {str(e)}")

            except Exception as e:
                logger.error(f"Error uploading {target_filename}: {str(e)}")

            if attempt < max_attempts - 1:
                logger.info(f"Waiting 10 seconds before retry...")
                sleep(10)

        return False, 'Max attempts reached', target_filename

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
    creds_file = os.path.join(SCRIPT_DIR, 'JSON.json')
    print(f"Loading credentials from: {creds_file}")

    if not os.path.exists(creds_file):
        print(f"ERROR: Credentials file not found: {creds_file}")
        sys.exit(1)

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

    # Initialize Pywikibot early for infrastructure checks
    print("\nInitializing Pywikibot for pre-scrape checks...")
    _early_pywikibot_result = initialize_pywikibot()
    if _early_pywikibot_result is None:
        print("Error: Failed to initialize Pywikibot")
        sys.exit(1)
    _early_site, _ = _early_pywikibot_result
    print("\nChecking and creating categories/modules before scraping...")
    _ensure_pid_infrastructure(_early_site)

    # Load pre-translation replacements
    _translation_replacements = load_translation_replacements()

    # Step 1: Scrape data
    print("\n" + "=" * 60)
    print("STEP 1: Scraping data from pressinform.gov.bd")
    print("=" * 60)
    excel_file, wikimedia_checksums = scrape_data()

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

        print("Loading Gemini AI Studio key (free secondary account)...")
        gemini_api_key = load_gemini_api_key()
        genai_client = genai.Client(api_key=gemini_api_key)
        translate_client = translate.Client()
        print("Gemini (AI Studio) and Translate clients initialized")

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
                result = image_processor.process_image(idx + 1, image_url, wikimedia_checksums)

                df.iat[idx, 4] = result['ocr_text']  # Column E: OCR text
                df.iat[idx, 5] = result['status']     # Column F: Status
                df.to_excel(excel_file, index=False, header=False)

                # Checksum duplicate — image content already on Commons under a different URL
                if result.get('is_duplicate'):
                    print(f"Row {idx + 1}: Checksum match — registering URL in module, skipping upload")
                    dup_checksum = result.get('checksum', '')
                    dup_entry = f'        ["{image_url}"] = {{date="{date_str}", checksum="{dup_checksum}"}},'
                    df.iat[idx, 10] = dup_entry
                    df.iat[idx, 13] = "Skipped (checksum duplicate)"
                    if update_pid_date_data(site, dup_entry):
                        df.iat[idx, 11] = "Success (dup)"
                        success_count += 1
                    else:
                        df.iat[idx, 11] = "Failed"
                        failed_count += 1
                    df.to_excel(excel_file, index=False, header=False)
                    continue

                if result['image'] is None or result['status'].startswith('Error') or result['status'].startswith('OCR failed'):
                    print(f"Row {idx + 1}: Image processing failed")
                    failed_count += 1
                    continue

                # Get image format
                img_format = result.get('format', 'jpg')

                # Step 3: Translate Bengali to English
                print(f"\nSTEP 3: Translating text...")
                bengali_text_raw = result['ocr_text']
                print(f"Sanitized OCR Data: {bengali_text_raw}")
                bengali_text = apply_translation_replacements(bengali_text_raw, _translation_replacements)
                print(f"After pre-translation replacements: {bengali_text}")
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
                img_checksum = result.get('checksum', '')
                data_entry = f'        ["{image_url}"] = {{date="{date_str}", checksum="{img_checksum}"}},'
                df.iat[idx, 10] = data_entry  # Column K: Data entry

                description = f'''=={{{{int:filedesc}}}}==
{{{{Information
 |description = {{{{bn|1={bengali_text_raw.strip().lstrip('\ufeff').strip()}}}}}{{{{en|1={translation.strip()}{{{{Auto-translated PID English description}}}}}}}}
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
                upload_success, upload_error, actual_title = upload_to_commons(
                    site, FilePage, result['image'], title, img_format, result.get('exif'), description
                )

                if upload_success:
                    # Persist the resolved filename if it changed due to a collision
                    if actual_title != title:
                        print(f"Row {idx + 1}: Filename adjusted for collision: {actual_title}")
                        df.iat[idx, 8] = actual_title
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

def _ensure_pid_infrastructure(site):
    """Ensure all required categories and modules exist for current date"""
    now = datetime.now()
    year = now.year
    month = now.month

    y1 = str(year)[:3]        # e.g. "202"
    y2 = str(year)[3:]        # e.g. "6"
    month_padded = str(month).zfill(2)  # e.g. "03"
    month_name = now.strftime("%B")     # e.g. "March"
    date_str = now.strftime("%Y-%m-%d") # e.g. "2026-03-09"

    pages_to_ensure = [
        (
            f"Category:Bangladesh photographs taken on {date_str}",
            "{{World photos}}"
        ),
        (
            f"Category:{month_name} {year} Bangladesh photographs",
            "{{Countryphotomonth}}"
        ),
        (
            f"Category:{month_name} {year} in Bangladesh",
            "{{{{Monthbyyearbangladesh|{y1}|{y2}|{month}}}}}".format(
                y1=y1, y2=y2, month=month)
        ),
        (
            f"Category:{year} in Bangladesh",
            "{{{{Bangladeshyear|{y1}|{y2}}}}}\n{{{{Countries of Asia|prefix=:Category:{year} in }}}}}}\n{{{{Wikidata Infobox}}}}".format(
                y1=y1, y2=y2, year=year)
        ),
        (
            f"Category:{month_name} {year} in Asia",
            "{{{{Asiamonthyear|{year}|{month_name}}}}}\n{{{{Wikidata Infobox}}}}".format(
                year=year, month_name=month_name)
        ),
        (
            f"Category:{month_name} {year} by country",
            "{{{{Monthbycountryyear|{y1}|{y2}|{month_padded}}}}}\n{{{{Wikidata Infobox}}}}".format(
                y1=y1, y2=y2, month_padded=month_padded)
        ),
        (
            f"Category:{year} photographs of Bangladesh",
            "{{{{Bangladesh-photoyear|{y1}|{y2}}}}}".format(y1=y1, y2=y2)
        ),
        (
            f"Category:PID-BD images from {month_name} {year}",
            "{{PID-BD image category navigation}}"
        ),
        (
            f"Category:PID-BD images from {year}",
            f"[[Category:Press Information Department images|{year}]]\n"
            f"[[Category:{year} in Bangladesh]]"
        ),
    ]

    for title, content in pages_to_ensure:
        try:
            page = pywikibot.Page(site, title)
            if not page.exists():
                page.text = content
                page.save(summary="Creating category for PID uploads")
                logger.info(f"Created: {title}")
            else:
                logger.info(f"Already exists: {title}")
        except Exception as e:
            logger.error(f"Error creating {title}: {e}")

    # Ensure Module:PIDDateData/YEAR exists
    module_title = f"Module:PIDDateData/{year}"
    try:
        module_page = pywikibot.Page(site, module_title)
        if not module_page.exists():
            module_page.text = "return {\n\n\n}"
            module_page.save(summary="Creating PIDDateData module for new year")
            logger.info(f"Created: {module_title}")
        else:
            logger.info(f"Already exists: {module_title}")
    except Exception as e:
        logger.error(f"Error creating {module_title}: {e}")


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
