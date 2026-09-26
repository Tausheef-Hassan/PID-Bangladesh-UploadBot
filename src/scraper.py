# scraper.py
# Scrapes the PID website (pressinform.gov.bd) and builds the work queue
# for the image upload pipeline. Returns the list of new images to process.

import concurrent.futures
import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

import config

# Retries transport errors and 429/5xx internally.
session = config.http_session()


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
    """Fetch data from Wikimedia User:PID-Bangladesh-UploadBot/PIDDateData/{year}.json"""
    headers = {
        'User-Agent': 'PressInformScraper/1.0 Python/requests'
    }

    # Use the bot's user subpage instead of the Data namespace to avoid Tabular Data strictness
    title = f"User:PID-Bangladesh-UploadBot/PIDDateData/{year}.json"
    
    urls_to_try = [
        f"https://commons.wikimedia.org/w/index.php?title={title}&action=raw",
        f"https://commons.wikimedia.org/w/api.php?action=query&titles={title}&prop=revisions&rvprop=content&format=json&formatversion=2"
    ]

    for url in urls_to_try:
        try:
            print(f"Trying URL: {url}")
            response = session.get(url, headers=headers, timeout=10)
            print(f"Status code: {response.status_code}")

            if response.status_code == 200:
                content = response.text

                if 'api.php' in url:
                    api_data = json.loads(content)
                    pages = api_data.get('query', {}).get('pages', [])
                    if pages and len(pages) > 0:
                        page_data = pages[0]
                        if 'revisions' in page_data and len(page_data['revisions']) > 0:
                            content = page_data['revisions'][0]['content']
                        else:
                            continue
                    else:
                        continue

                if len(content) < 10:
                    continue

                urls = set()
                checksums = set()

                try:
                    tab_data = json.loads(config.strip_syntaxhighlight(content))
                    urls_list = []
                    
                    if isinstance(tab_data, dict) and 'data' in tab_data:
                        # Old Tabular JSON format
                        rows = tab_data['data']
                        for row in rows:
                            if len(row) > 0 and row[0]:
                                urls_list.append({"url": row[0], "checksum": row[2] if len(row) > 2 else ""})
                    elif isinstance(tab_data, list):
                        # New Normal JSON format
                        urls_list = tab_data
                    
                    for item in urls_list:
                        url = item.get("url")
                        checksum = item.get("checksum")
                        if url:
                            normalized = normalize_url(url)
                            urls.add(normalized)
                        if checksum:
                            checksums.add(checksum)
                            
                    print(f"Found {len(urls)} URLs and {len(checksums)} checksums in {year} Data")
                    if len(urls) > 0:
                        return urls, checksums
                except json.JSONDecodeError:
                    print("Failed to parse JSON data.")
                    
        except Exception as e:
            print(f"Error with URL {url}: {e}")
            continue

    print(f"Could not fetch Wikimedia data for {year}")
    return set(), set()


def convert_bengali_date_to_english(bengali_date_text):
    """Convert Bengali date to English yyyy-mm-dd hh:mm:ss format"""
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
        match = re.search(
            r'([০-৯\d]+)\s+([^\s,]+),?\s+([০-৯\d]+)\s+এ\s+([০-৯\d]+):([০-৯\d]+)\s+(AM|PM)', bengali_date_text)

        if not match:
            return ""

        month_bengali = match.group(2)
        am_pm = match.group(6)

        # int() parses Bengali digits natively — they are Unicode decimal digits
        day, year, hour, minute = (int(match.group(g)) for g in (1, 3, 4, 5))

        month = int(bengali_months.get(month_bengali, '01'))

        # Convert to 24-hour format
        if am_pm == 'PM' and hour != 12:
            hour += 12
        elif am_pm == 'AM' and hour == 12:
            hour = 0

        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:00"

    except Exception as e:
        print(f"Error converting Bengali date: {e}")
        return ""


def fetch_detail_date(detail_href):
    """Fetch date from a detail page. Returns date string or empty string.
    Transport errors and 429/5xx are retried inside `session`."""
    detail_url = f"https://pressinform.gov.bd{detail_href}"
    try:
        detail_response = session.get(detail_url, timeout=10, verify=False)
        if detail_response.status_code != 200:
            print(f"Failed to fetch detail page: {detail_url} - Status {detail_response.status_code}")
            return ""

        detail_soup = BeautifulSoup(detail_response.content, 'html.parser')
        # Try div.content-update-block first, then any <p> containing Bengali date pattern
        date_element = detail_soup.find('div', class_='content-update-block')
        if not date_element:
            # Fallback: find a <p> tag containing the Bengali date pattern (এ + AM/PM)
            for p in detail_soup.find_all('p'):
                if 'এ' in p.get_text() and ('AM' in p.get_text() or 'PM' in p.get_text()):
                    date_element = p
                    break
        if not date_element:
            print(f"No date found on detail page: {detail_url}")
            return ""

        date_text = date_element.get_text()
        print(f"Date text found: {date_text.strip()}")
        result = convert_bengali_date_to_english(date_text)
        if not result:
            print(f"Date conversion failed for text: {date_text.strip()}")
        return result

    except Exception as e:
        print(f"Error fetching detail page {detail_url}: {e}")
        return ""


def scrape_page(page_num, wikimedia_urls, hard_stop_url):
    """Scrape a single page (page_size=50) row by row.
    Returns (results, hard_stop_hit) where results is a list of (img_url, detail_href) tuples
    for images not yet in Wikimedia, and hard_stop_hit is True if the hard stop URL was encountered.
    Date fetching is deferred — only done for images that need uploading.
    """
    url = f"https://pressinform.gov.bd/pages/daily-photos?archived=true&page={page_num}&page_size=50"
    print(f"Scraping page {page_num} (50 items)...")

    try:
        response = session.get(url, timeout=10, verify=False)
        if response.status_code != 200:
            print(f"Failed to fetch page {page_num} (status {response.status_code})")
            return [], False

        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.find('table', id='noticeTable')

        if not table:
            print(f"No table found on page {page_num}")
            return [], False

        results = []
        hard_stop_hit = False
        total_images_seen = 0
        rows = table.find(
            'tbody', class_='table-tbody').find_all('tr', class_='table-tr')

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

            detail_link = row.find(
                'a', href=lambda x: x and '/pages/daily-photos/' in x and x != '#')
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
            print(
                f"Page {page_num}: no images found at all, end of content")
            return None, False  # None signals "end of content" vs [] which means "all uploaded"

        return results, hard_stop_hit

    except Exception as e:
        print(f"Error scraping page {page_num}: {e}")
        return [], False


def scrape_data():
    """Scrape data from pressinform.gov.bd"""
    current_year = datetime.now().year
    previous_year = current_year - 1

    print(f"Current year: {current_year}")
    print("Fetching Wikimedia data...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        (wikimedia_urls, wikimedia_checksums), (prev_year_urls, prev_year_checksums) =             executor.map(fetch_wikimedia_data, (current_year, previous_year))
    print(f"Loaded {len(wikimedia_urls)} URLs from {current_year}, "
          f"{len(prev_year_urls)} from {previous_year}")
    wikimedia_urls.update(prev_year_urls)
    wikimedia_checksums.update(prev_year_checksums)
    print(
        f"Total URLs from Wikimedia: {len(wikimedia_urls)}, checksums: {len(wikimedia_checksums)}")

    scraped_data = []

    fully_uploaded_pages = 0
    page_num = 1
    entry_counter = 1

    # Hard stop URL - if this image is encountered, stop scraping
    HARD_STOP_URL = "objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/axvjbnqprylg/b/V2Ministry/o/office-pressinform/2024/12/ec18321a25e844ab9503b7b704aafb34.jpg"

    while fully_uploaded_pages < 3:
        new_items, hard_stop_hit = scrape_page(
            page_num, wikimedia_urls, HARD_STOP_URL)

        if new_items is None:
            print(
                f"Page {page_num}: no images found, end of content. Stopping scraper.")
            break

        if hard_stop_hit and not new_items:
            print(
                f"Hard stop reached on page {page_num} with no new items. Stopping scraper.")
            break

        if not new_items and not hard_stop_hit:
            # scrape_page filters out already-uploaded images, so empty means fully uploaded page
            fully_uploaded_pages += 1
            print(
                f"Page {page_num}: all items already uploaded ({fully_uploaded_pages}/3 fully-uploaded pages)")
        else:
            fully_uploaded_pages = 0
            # Fetch dates only for new images concurrently
            def process_item(item):
                img_url, detail_href = item
                if detail_href:
                    date = fetch_detail_date(detail_href)
                    time.sleep(0.1) # Be gentle on the server
                else:
                    date = ""
                    print(f"No detail link for image: {img_url}")
                return img_url, detail_href, date

            print(f"Fetching dates for {len(new_items)} new images concurrently...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                processed_items = list(executor.map(process_item, new_items))

            for img_url, detail_href, date in processed_items:
                unique_id = generate_unique_id(img_url, date, entry_counter)
                detail_url = f"https://pressinform.gov.bd{detail_href}" if detail_href else ""
                print(f"Adding: {unique_id} | {date} | {img_url}")
                scraped_data.append([unique_id, date, img_url, detail_url])
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
        print("\nNo new images found.")
        return None, wikimedia_checksums

    print(f"\nTotal rows fetched: {len(scraped_data)}")
    return scraped_data, wikimedia_checksums
