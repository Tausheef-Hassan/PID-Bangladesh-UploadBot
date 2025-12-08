# PID-Bangladesh-UploadBot

An automated bot for uploading images from the Press Information Department (PID) of Bangladesh to Wikimedia Commons.

[![Bot Status](https://img.shields.io/badge/status-active-brightgreen)](https://commons.wikimedia.org/wiki/User:PID-Bangladesh-UploadBot)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Overview

The Press Information Department (PID) of Bangladesh is a government body responsible for disseminating official information to the media. In August 2025, the PID released over 55,000 images into the public domain. This bot automates the process of uploading these historically valuable images to Wikimedia Commons.

**Bot Account:** [PID-Bangladesh-UploadBot](https://commons.wikimedia.org/wiki/User:PID-Bangladesh-UploadBot)  
**Bot Permission:** [Commons:Bots/Requests/PID-Bangladesh-UploadBot](https://commons.wikimedia.org/wiki/Commons:Bots/Requests/PID-Bangladesh-UploadBot)  
**Operators:** [Tausheef Hassan](https://commons.wikimedia.org/wiki/User:Tausheef_Hassan)

## Features

- **Automated Scraping:** Discovers new images from the PID website daily
- **Intelligent Processing:** Uses Google Cloud Vision API for OCR and text extraction
- **ML-Powered Translation:** Translates Bengali descriptions to English using Google Gemini AI
- **Smart Filename Generation:** Creates Wikimedia Commons-compliant filenames automatically
- **Image Optimization:** Crops embedded captions and processes EXIF data correctly
- **Wayback Machine Integration:** Retrieves archived images if originals are unavailable
- **Duplicate Detection:** Checks existing uploads to avoid redundancy
- **Comprehensive Logging:** Maintains detailed logs on Wikimedia Commons

## How It Works

### 1. Discovery Phase
The bot scrapes the [PID Daily Photo Archive](https://pressinform.gov.bd/site/view/daily_photo_archive/) to find new images. It compares discovered images against the `Module:PIDDateData` on Commons to identify images not yet uploaded.

### 2. Processing Pipeline

For each new image, the bot performs the following steps:

1. **Download:** Fetches the image from PID servers (or Wayback Machine if needed)
2. **Image Processing:**
   - Applies EXIF orientation correction
   - Converts CMYK to RGB if necessary
   - Detects and removes embedded white separator bars
   - Crops the photo section from the text caption section
   - Removes side whitespace
3. **OCR Extraction:** Uses Google Cloud Vision API to extract Bengali text from the caption area
4. **Translation:** Translates Bengali description to English using Google Gemini 2.5 Flash
5. **Filename Generation:** Creates a Commons-compliant filename following naming guidelines
6. **Upload:** Uploads to Commons with proper licensing and categorization
7. **Database Update:** Updates `Module:PIDDateData` with the new entry

### 3. Metadata Generation

The bot creates comprehensive file descriptions including:
- Bilingual descriptions (Bengali and English)
- Source attribution with original URL
- Date metadata
- Licensing information (PD-BDGov-PID)
- Automatic categorization

## Technical Architecture

### Technologies Used

- **Python 3.13**
- **Pywikibot:** For Wikimedia Commons API interaction
- **Google Cloud Vision API:** For OCR
- **Google Gemini AI:** For translation and filename generation
- **Google Translate API:** Fallback translation service
- **OpenCV & PIL:** Image processing
- **BeautifulSoup:** Web scraping
- **Pandas & OpenPyXL:** Data management

### Deployment Environment

The bot runs on **Wikimedia Toolforge**, a cloud computing platform for Wikimedia tool developers.

- **Platform:** Kubernetes-based Toolforge
- **Schedule:** Runs hourly via cron job (`7 * * * *`)
- **Runtime:** Python 3.13 container
- **Timeout:** 55 minutes per run
- **Maximum Upload Rate:** 12 uploads per minute

## Setup Instructions

### Prerequisites

1. Wikimedia Commons bot account with approved bot flag
2. Google Cloud Platform account with APIs enabled:
   - Cloud Vision API
   - Cloud Translation API
   - Vertex AI API (for Gemini)
3. Toolforge account (for production deployment)

### Local Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Tausheef-Hassan/PID-Bangladesh-UploadBot.git
   cd PID-Bangladesh-UploadBot
   ```

2. **Create virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure credentials (see Configuration section below)**

5. **Run the bot:**
   ```bash
   python3 main.py
   ```

### Toolforge Production Setup

1. **SSH into Toolforge:**
   ```bash
   ssh <username>@login.toolforge.org
   become pid-bangladesh-uploadbot
   ```

2. **Upload required files:**
   - `main.py` - Main bot script
   - `requirements.txt` - Python dependencies
   - `setup_venv.sh` - Virtual environment setup script
   - `run_bot.sh` - Bot execution wrapper
   - `user-config.py` - Pywikibot configuration
   - `user-password.py` - Pywikibot credentials
   - `JSON.json` - Google Cloud credentials (or set environment variable)

3. **Run setup script:**
   ```bash
   chmod +x setup_venv.sh run_bot.sh
   ./setup_venv.sh
   ```

4. **Set Google Cloud credentials environment variable:**
   ```bash
   # Add to ~/.profile or set in job configuration
   export GOOGLE_APPLICATION_CREDENTIALS_JSON='<json_content>'
   ```

5. **Create scheduled job:**
   ```bash
   toolforge jobs run pid-upload-bot \
     --command "$HOME/run_bot.sh" \
     --image python3.13 \
     --schedule "7 * * * *" \
     --filelog
   ```

6. **Monitor the bot:**
   ```bash
   toolforge jobs logs pid-upload-bot
   ```

## Configuration

### Required Files

#### 1. `user-config.py`

Pywikibot configuration file. Place in `~/pywikibot/` directory:

```python
family = 'commons'
mylang = 'commons'
usernames['commons']['commons'] = 'PID-Bangladesh-UploadBot'
```

#### 2. `user-password.py`

Pywikibot authentication. Place in `~/pywikibot/` directory:

```python
('PID-Bangladesh-UploadBot', BotPassword('<bot_username>', '<bot_password>'))
```

Get bot password from: [Special:BotPasswords](https://commons.wikimedia.org/wiki/Special:BotPasswords)

#### 3. Google Cloud Credentials

**Option A: JSON File (Local Development)**

Place your Google Cloud service account JSON file in the same directory as `main.py`. The bot will automatically detect it.

**Option B: Environment Variable (Production/Toolforge)**

```bash
export GOOGLE_APPLICATION_CREDENTIALS_JSON='{"type":"service_account","project_id":"...","private_key":"...","client_email":"..."}'
```

**Required Google Cloud APIs:**
- Cloud Vision API
- Cloud Translation API
- Vertex AI API

**Required Service Account Permissions:**
- Cloud Vision API User
- Cloud Translation API User
- Vertex AI User

## File Structure

```
PID-Bangladesh-UploadBot/
├── main.py                 # Main bot script (all functionality)
├── requirements.txt        # Python dependencies
├── setup_venv.sh          # Virtual environment setup
├── run_bot.sh             # Bot execution wrapper
├── README.md              # This file
├── pywikibot/
│   ├── user-config.py     # Pywikibot config
│   └── user-password.py   # Pywikibot credentials
└── JSON.json              # Google Cloud credentials (optional)
```

## Usage

### Manual Execution

```bash
# Activate virtual environment
source pwbvenv/bin/activate

# Run the bot
python3 main.py
```

### Scheduled Execution (Toolforge)

The bot runs automatically every hour at 7 minutes past the hour:

```bash
# Check job status
toolforge jobs list

# View logs
toolforge jobs logs pid-upload-bot

# Restart job
toolforge jobs restart pid-upload-bot
```

## Monitoring & Logs

### Toolforge Logs

- **Output Log:** `~/pid-bangladesh-uploadbot.out`
- **Error Log:** `~/pid-bangladesh-uploadbot.err`

### Commons Logs

Monthly logs are automatically created at:
`User:PID-Bangladesh-UploadBot/Log/<Month> <Year>`

Example: [User:PID-Bangladesh-UploadBot/Log/December 2025](https://commons.wikimedia.org/wiki/User:PID-Bangladesh-UploadBot/Log/December_2025)

## Bot Statistics

- **Total Dataset:** ~60,000 images
- **Upload Rate:** Up to 12 images per minute
- **OCR Confidence Threshold:** ≥95%
- **Success Rate:** Monitored in monthly log pages
- **Categories:** Automatically added to relevant categories

## Troubleshooting

### Common Issues

**1. Authentication Errors**
- Verify `user-password.py` contains correct bot password
- Ensure bot account has bot flag enabled
- Check that bot is logged in: Run Pywikibot script to test login

**2. Google Cloud API Errors**
- Verify all required APIs are enabled in GCP console
- Check service account has proper permissions
- Ensure credentials JSON is valid and properly formatted
- Verify project has billing enabled

**3. Image Processing Errors**
- Check internet connectivity to PID servers
- Verify Wayback Machine is accessible
- Ensure OpenCV and PIL are properly installed

**4. Toolforge Job Not Running**
- Check job status: `toolforge jobs list`
- View logs: `toolforge jobs logs pid-upload-bot`
- Verify virtual environment exists: `ls -la ~/pwbvenv`
- Check for existing processes: `pgrep -f main.py`

### Debug Mode

Add verbose logging to track issues:

```python
# In main.py, change logging level
logging.basicConfig(level=logging.DEBUG)
```

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is licensed under the MIT License. See LICENSE file for details.

## Acknowledgments

- **Wikimedia Foundation** for Toolforge hosting
- **Press Information Department of Bangladesh** for releasing images to public domain
- **Wikimedia Bangladesh Community** for support and collaboration
- **Google Cloud Platform** for API services

## Contact

- **Bot Issues:** Leave comments on the [bot's talk page](https://commons.wikimedia.org/wiki/User_talk:PID-Bangladesh-UploadBot)
- **Operator Contact:**
  - [Yahya](https://commons.wikimedia.org/wiki/User_talk:Yahya)
  - [Tausheef Hassan](https://commons.wikimedia.org/wiki/User_talk:Tausheef_Hassan)

## Related Links

- [Bot User Page](https://commons.wikimedia.org/wiki/User:PID-Bangladesh-UploadBot)
- [Bot Permission Request](https://commons.wikimedia.org/wiki/Commons:Bots/Requests/PID-Bangladesh-UploadBot)
- [PID Project Page](https://commons.wikimedia.org/wiki/Commons:Press_Information_Department_of_Bangladesh/en)
- [Category: Press Information Department images](https://commons.wikimedia.org/wiki/Category:Press_Information_Department_images)
- [Wikimedia Diff Blog Post](https://diff.wikimedia.org/2025/09/11/a-milestone-for-open-knowledge-bangladesh-press-information-department-releases-50000-images-to-the-public-domain/)

---

**Status:** Active | **Last Updated:** December 2025 | **Version:** 0.1.1a0


# Technical Documentation: PID-Bangladesh-UploadBot

## Complete Processing Flow

This document provides a detailed technical explanation of how the bot processes images from the Press Information Department (PID) of Bangladesh.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Bot Workflow                             │
└─────────────────────────────────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │   1. DISCOVERY PHASE    │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │  2. PROCESSING PHASE    │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │    3. UPLOAD PHASE      │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │    4. LOGGING PHASE     │
                    └─────────────────────────┘
```

---

## Phase 1: Discovery Phase (Web Scraping)

### Purpose
Discover new images from the PID website that haven't been uploaded to Commons yet.

### Detailed Flow

```
START
  │
  ├─► Fetch Module:PIDDateData/{current_year} from Commons
  │   └─► Extract all previously uploaded image URLs
  │
  ├─► Fetch Module:PIDDateData/{previous_year} from Commons
  │   └─► Extract all previously uploaded image URLs
  │
  ├─► Combine into a set of "already uploaded" URLs
  │
  ├─► Initialize page counter (page_num = 1)
  ├─► Initialize consecutive_matches = 0
  │
  └─► LOOP: While consecutive_matches < 50
       │
       ├─► Scrape page_num from PID website
       │   URL: https://pressinform.gov.bd/site/view/daily_photo_archive/-?page={page_num}
       │
       ├─► Parse HTML tables to extract:
       │   ├─► Image URL
       │   └─► Publication date/time
       │
       ├─► For each image found:
       │   ├─► Normalize URL (remove protocol, decode URL encoding)
       │   ├─► Check if normalized URL exists in "already uploaded" set
       │   │
       │   ├─► IF EXISTS:
       │   │   ├─► Skip this image
       │   │   └─► Increment consecutive_matches
       │   │
       │   └─► IF NEW:
       │       ├─► Generate unique ID: PID_{date}_{hash}_{counter}
       │       ├─► Add to Excel: [unique_id, date, url]
       │       ├─► Reset consecutive_matches = 0
       │       └─► Increment entry_counter
       │
       ├─► Increment page_num
       ├─► Sleep 1 second (rate limiting)
       │
       └─► IF consecutive_matches >= 50: BREAK
           (Found 50 images in a row that were already uploaded)

IF no new images found:
  └─► Return None (skip processing, log "no new images")
ELSE:
  └─► Save Excel file to ~/output/pressinform_photos_{timestamp}.xlsx
  └─► Return Excel file path

END
```

### Key Functions

**`scrape_data()`**
- Main scraping orchestrator
- Creates Excel workbook with columns: [Unique ID, Date, Image URL]
- Stops after finding 50 consecutive duplicates

**`fetch_wikimedia_data(year)`**
- Downloads Module:PIDDateData/{year} from Commons
- Parses Lua table to extract previously uploaded URLs
- Returns set of normalized URLs

**`normalize_url(url)`**
- Removes protocol (http:// or https://)
- Standardizes domain variations
- Decodes URL encoding for consistent comparison

**`scrape_page(page_num, wikimedia_urls)`**
- Fetches single page from PID website
- Parses HTML tables with BeautifulSoup
- Returns list of (image_url, date) tuples

---

## Phase 2: Processing Phase (Image Processing)

This is the most complex phase, involving image analysis, OCR, translation, and metadata generation.

### Detailed Flow

```
FOR each row in Excel file:
  │
  ├─► STEP 2.1: Download Image
  │   ├─► Fetch image from URL
  │   ├─► IF 404 error:
  │   │   └─► Try Wayback Machine to retrieve archived version
  │   │
  │   ├─► Load with PIL (Python Imaging Library)
  │   ├─► Store EXIF data
  │   ├─► Apply EXIF orientation correction
  │   ├─► Convert CMYK → RGB if needed
  │   ├─► Convert PIL → NumPy array → OpenCV BGR format
  │   └─► Return: (opencv_image, format, exif_data, error_status)
  │
  ├─► STEP 2.2: Find White Separator
  │   │
  │   ├─► Scan image to find white/light separator bar
  │   │   between photo and caption sections
  │   │
  │   ├─► PRIMARY METHOD:
  │   │   ├─► Scan columns at edges (first 4 and last 4 columns)
  │   │   ├─► From bottom to 40% height, find uniform color sections
  │   │   ├─► Identify horizontal line with 98% uniform color
  │   │   ├─► Calculate offset: round(2 + 3/log(3100/670) × log(height/670))
  │   │   └─► separator_row = cutoff_row - offset
  │   │
  │   └─► FALLBACK METHOD (if primary fails or result is ~40% height):
  │       ├─► Start from 75% height
  │       ├─► Scan downward for consecutive white/near-white lines
  │       ├─► Count consecutive similar lines
  │       └─► separator_row = detected_row - offset
  │
  ├─► STEP 2.3: Crop Image Sections
  │   ├─► IF separator found:
  │   │   ├─► photo_section = image[0:separator_row]
  │   │   ├─► text_section = image[separator_row:bottom]
  │   │   └─► IF fallback was used: crop side whitespace
  │   │
  │   └─► IF no separator:
  │       └─► Use entire image for both photo and OCR
  │
  ├─► STEP 2.4: Perform OCR
  │   ├─► Encode text_section as PNG
  │   ├─► Send to Google Cloud Vision API
  │   ├─► Extract Bengali text with language hints: ['bn', 'en']
  │   ├─► Clean text:
  │   │   ├─► Replace '|' with '।' (Bengali punctuation)
  │   │   └─► Remove "পিআইডি" (PID) suffixes
  │   └─► Return cleaned OCR text
  │
  ├─► Save to Excel: OCR text, status
  │
  ├─► STEP 2.5: Translate Text
  │   ├─► Send Bengali text to Google Gemini 2.5 Flash
  │   ├─► Prompt: "Translate to encyclopedic English, retain all info"
  │   ├─► IF response contains Bengali characters:
  │   │   └─► Fallback to Google Translate API
  │   │
  │   ├─► Retry logic: Try primary model → fallback model → error
  │   └─► Return: (translated_text, status)
  │
  ├─► Save to Excel: translation, translation status
  │
  ├─► STEP 2.6: Generate Filename
  │   ├─► Combine: translation + date
  │   ├─► Send to Google Gemini 2.5 Flash
  │   ├─► Prompt: "Create Commons-compliant filename ≤240 bytes"
  │   ├─► AI generates descriptive filename
  │   ├─► Replace incorrect dates (if >7 days difference)
  │   ├─► Add file extension: .jpg or .png
  │   └─► Return: (filename_with_extension, status)
  │
  └─► Save to Excel: title, title status

NEXT row
```

---

## Detailed: Image Cropping Mechanism

### Problem Statement

PID images have embedded captions at the bottom that look like this:

```
┌─────────────────────────────────────┐
│                                     │
│         ACTUAL PHOTOGRAPH           │
│                                     │
│─────────────────────────────────────│ ← White separator bar
│                                     │
│  Bengali Caption Text               │
│  প্রকাশের তারিখ: 2024-11-15           │
│                                     │
└─────────────────────────────────────┘
```

**Goal:** Separate the photo from the caption text.

### Primary Separator Detection Algorithm

#### Step 1: Column Scanning

```python
# Scan columns at edges (avoid middle content)
first_columns = [1, 2, 3, 4]  # Left edge
last_columns = [width-5, width-4, width-3, width-2]  # Right edge

for each column:
    start from bottom (height - 6)
    scan upward to 40% of image height
    
    for each pixel moving upward:
        if first pixel in scan:
            start color_samples = [current_pixel]
        else:
            calculate average of color_samples
            if current_pixel matches average (within 2% tolerance):
                add to color_samples
                continue scanning upward
            else:
                # Found where uniform color ends
                record column_height = pixels_from_bottom
                break
```

**What this finds:** Height of uniform-colored section at bottom of each edge column.

#### Step 2: Horizontal Line Validation

```python
# Find columns with similar heights (within 4 pixels)
for first_col in first_columns:
    for last_col in last_columns:
        if abs(column_heights[first_col] - column_heights[last_col]) <= 4:
            
            # Scan the horizontal line between these columns
            scan_row = max(uniform_top_of_first_col, uniform_top_of_last_col)
            row_pixels = image[scan_row, first_col:last_col]
            
            # Check if this row is uniformly colored
            calculate average color of row
            count pixels matching average (within 2% tolerance)
            
            if matching_percentage >= 98%:
                # This is a valid separator line
                valid_lines.append(scan_row)
```

**What this finds:** Horizontal lines that are uniformly colored across the width.

#### Step 3: Calculate Final Separator Position

```python
cutoff_row = min(valid_lines)  # Topmost valid line

# Calculate dynamic offset based on image height
# This accounts for different image sizes
offset = round(2 + 3/log(3100/670) × log(height/670))
if offset < 2:
    offset = 2

separator_row = cutoff_row - offset
```

**Why the offset?** The white bar has some thickness. We want to cut *above* the bar, not in the middle of it.

### Fallback Separator Detection Algorithm

Used when:
1. Primary method finds no separator
2. Detected separator is around 40% of image height (suspicious)

#### Fallback Method

```python
start_row = int(height × 0.75)  # Start at 75% from top

required_consecutive_lines = round((4 / log(3100/670)) × log(height/670) + 5)
consecutive_similar_lines = 0

for y from start_row to bottom:
    row = image[y]
    
    # Check if row matches white (255,255,255) or near-white (250,249,251)
    white_matching = pixels within 2% of (255,255,255)
    fbf9fa_matching = pixels within 2% of (250,249,251)
    
    matching_percentage = (white_matching OR fbf9fa_matching) / width
    
    if matching_percentage >= 98%:
        consecutive_similar_lines += 1
        
        if consecutive_similar_lines >= required_consecutive_lines:
            # Found enough consecutive white lines
            if consecutive_similar_lines >= 10:
                offset = consecutive_similar_lines + 5
            elif consecutive_similar_lines in [1, 2, 3]:
                offset = 2
            else:
                offset = consecutive_similar_lines + 5
            
            separator_row = y - offset
            break
    else:
        consecutive_similar_lines = 0  # Reset counter
```

**What this does:** Scans from 75% downward looking for a thick band of white lines.

### Side Whitespace Cropping

Only applied when fallback method is used:

```python
# Scan from left
for x from 0 to width:
    column = image[:, x]
    
    if 98% of pixels are white/near-white:
        left_crop = x + 1
    else:
        break

# Scan from right
for x from width-1 to 0:
    column = image[:, x]
    
    if 98% of pixels are white/near-white:
        right_crop = x
    else:
        break

# Expand boundaries slightly to avoid cutting content
expansion = int(round((4 / log(3100/670)) × log(height/670) + 5))
left_expanded = max(0, left_crop - expansion)
right_expanded = min(width, right_crop + expansion)

cropped_image = image[:, left_expanded:right_expanded]
```

### Visual Example

```
Original Image (1000×1500 pixels):

┌─────────────────────────────────────┐ 0
│                                     │
│                                     │
│         Actual Photograph           │ photo_section
│                                     │
│                                     │
├─────────────────────────────────────┤ 1000 ← separator_row detected here
│   White/light gray separator bar    │
├─────────────────────────────────────┤ 1020
│                                     │
│   Bengali Caption:                  │ text_section
│   বাংলাদেশের প্রধানমন্ত্রী...             │ (sent to OCR)
│   2024-11-15 14:30:00 pm            │
│                                     │
└─────────────────────────────────────┘ 1500

After Cropping:

photo_section = image[0:1000, :]      → Uploaded to Commons
text_section = image[1000:1500, :]    → Sent to OCR API
```

---

## Phase 3: Upload Phase

### Detailed Flow

```
FOR each successfully processed image:
  │
  ├─► STEP 3.1: Prepare Description
  │   └─► Generate wikitext:
  │       {{Information
  │        |description = {{bn|1=<bengali_text>}}{{en|1=<english_translation>}}
  │        |date = {{Date-PID|<date>}}
  │        |source = {{Source-PID | url=<original_url>}}
  │        |author = {{Institution:Press Information Department}}
  │       }}
  │       {{PD-BDGov-PID}}
  │       [[Category: Uploaded with pypan]]
  │
  ├─► STEP 3.2: Save Image Temporarily
  │   ├─► Convert OpenCV BGR → PIL RGB
  │   ├─► Save with EXIF data preserved
  │   ├─► Format: PNG (optimize=True) or JPEG (quality=95)
  │   └─► Return temp file path
  │
  ├─► STEP 3.3: Upload to Commons
  │   ├─► Initialize Pywikibot FilePage
  │   ├─► Check if file already exists
  │   │   └─► IF EXISTS: Skip (return "File already exists")
  │   │
  │   ├─► Attempt upload (max 10 attempts)
  │   │   ├─► file_page.upload(
  │   │   │       source=temp_file,
  │   │   │       comment="Pypan 0.1.1a0",
  │   │   │       text=description,
  │   │   │       ignore_warnings=True
  │   │   │   )
  │   │   │
  │   │   ├─► IF upload fails: Wait 10 seconds, retry
  │   │   └─► IF success: Return True
  │   │
  │   └─► Clean up temp file
  │
  ├─► STEP 3.4: Update Module:PIDDateData
  │   ├─► Generate entry: ["<url>"] = "<date>",
  │   ├─► Fetch Module:PIDDateData/{current_year}
  │   ├─► Find last closing brace: }
  │   ├─► Insert new entry before closing brace
  │   ├─► Save page with summary: "added another image"
  │   └─► Return success/failure
  │
  └─► Update Excel: upload status, PIDDateData status

NEXT image
```

### Pywikibot Authentication

```
Configuration Files Required:

1. ~/pywikibot/user-config.py:
   family = 'commons'
   mylang = 'commons'
   usernames['commons']['commons'] = 'PID-Bangladesh-UploadBot'

2. ~/pywikibot/user-password.py:
   ('PID-Bangladesh-UploadBot', BotPassword('<username>', '<password>'))
   
   Get bot password from:
   https://commons.wikimedia.org/wiki/Special:BotPasswords
```

---

## Phase 4: Logging Phase

### Detailed Flow

```
AFTER all images processed:
  │
  ├─► Convert DataFrame to Wikitable format
  │   ├─► Add column headers
  │   ├─► For each row:
  │   │   ├─► Column 0 (Unique ID): Add [[File:title|100px]] thumbnail
  │   │   ├─► Column 12 (Description): Wrap in <nowiki> tags
  │   │   └─► Other columns: Escape wiki markup (| → {{!}})
  │   │
  │   └─► Generate complete wikitable with sortable class
  │
  ├─► Generate log page title:
  │   └─► User:PID-Bangladesh-UploadBot/Log/{Month} {Year}
  │       Example: User:PID-Bangladesh-UploadBot/Log/December 2025
  │
  ├─► Create log entry:
  │   == YYYY-MM-DD HH:MM:SS UTC ==
  │   Processed N images. Successful uploads: X, Failed: Y
  │   
  │   {wikitable with all processing details}
  │
  ├─► IF page exists:
  │   └─► Append log entry to existing content
  │   
  └─► IF page doesn't exist:
      └─► Create new page with header + log entry
  │
  ├─► Save page with summary: "Bot log update"
  │
  └─► IF logging successful:
      └─► Delete temporary Excel file

END
```

---

## Error Handling & Retry Logic

### Retry Decorator

```python
@retry_on_failure(max_attempts=10, delay=2)
def risky_function():
    # Automatically retries up to 10 times
    # Waits 2 seconds between attempts
    pass
```

**Applied to:**
- Image download
- Wayback Machine retrieval
- OCR operations
- API calls

### Exponential Backoff

For AI model calls (Gemini):

```python
Initial backoff: 1.0 second
Multiplier: 2.0×
Max backoff: 60 seconds

Attempt 1: Wait 1.0s
Attempt 2: Wait 2.0s
Attempt 3: Wait 4.0s
Attempt 4: Wait 8.0s
Attempt 5: Wait 16.0s
...
Attempt N: Wait min(1.0 × 2^N, 60.0)s
```

### Model Fallback Chain

```
Translation/Title Generation:
  ├─► Try: gemini-2.5-flash (PRIMARY_MODEL)
  │   └─► Max 5 attempts with backoff
  │
  └─► Fallback: gemini-2.0-flash-001 (FALLBACK_MODEL)
      └─► Max 5 attempts with backoff
      
  If both fail → Return error status
```

---

## Google Cloud APIs Configuration

### Required APIs

1. **Cloud Vision API**
   - Used for: OCR (text detection)
   - Language hints: Bengali (bn) and English (en)
   - Input: PNG-encoded image bytes
   - Output: Text annotations

2. **Cloud Translation API**
   - Used for: Fallback translation when Gemini returns Bengali
   - Source: Bengali (bn)
   - Target: English (en)

3. **Vertex AI API (Gemini)**
   - Used for: Translation and filename generation
   - Location: us-central1
   - Models: gemini-2.5-flash, gemini-2.0-flash-001
   - Temperature: 1.0, Top-p: 0.95

### Credentials Setup

```python
# Option 1: Environment Variable (Production)
export GOOGLE_APPLICATION_CREDENTIALS_JSON='{"type":"service_account",...}'

# Option 2: JSON File (Development)
Place JSON file in same directory as main.py

# At runtime:
load_credentials() → setup_credentials() → Initialize clients
```

---

## Data Flow Diagram

```
PID Website
    │
    ├─→ Scraper → Excel File (Unique ID, Date, URL)
    │                 │
    │                 ├─→ ImageProcessor
    │                 │       ├─→ Download Image
    │                 │       ├─→ Find Separator
    │                 │       ├─→ Crop Sections
    │                 │       └─→ OCR (Vision API)
    │                 │
    │                 ├─→ Translator (Gemini + Translate API)
    │                 │       └─→ Bengali → English
    │                 │
    │                 ├─→ Title Generator (Gemini)
    │                 │       └─→ Commons-compliant filename
    │                 │
    │                 ├─→ Pywikibot Uploader
    │                 │       ├─→ Upload to Commons
    │                 │       └─→ Update Module:PIDDateData
    │                 │
    │                 └─→ Logger
    │                         └─→ Create wikitable on Commons
    │
    └─→ Module:PIDDateData (Commons)
            └─→ Track uploaded URLs
```

---

## Performance & Rate Limiting

### Rate Limits

- **Web Scraping:** 1 second delay between pages
- **Commons Upload:** 12 uploads per minute (5 seconds between uploads)
- **API Calls:** 
  - OCR: 2 seconds after each call
  - Translation: 2 seconds after each call
  - Title Generation: 2 seconds after each call
- **PIDDateData Update:** 5 seconds after each upload

### Resource Usage

- **Memory:** Primarily image data in RAM (1-5 MB per image)
- **Disk:** Temporary files cleaned up after each upload
- **Network:** Sustained API calls to Google Cloud and Commons
- **Runtime:** ~2-5 minutes per image (with AI processing)

### Toolforge Job Configuration

```bash
Schedule: 7 * * * *  (Every hour at 7 minutes past)
Timeout: 55 minutes (3300 seconds)
Image: python3.13
Resources: default
Retry: No (to avoid duplicate runs)
```

---

## Excel Data Structure

| Column | Content | Description |
|--------|---------|-------------|
| A | Unique ID | PID_{date}_{hash}_{counter} |
| B | Date | YYYY-MM-DD HH:MM:SS format |
| C | Image URL | Original PID URL |
| D | (empty) | Reserved |
| E | OCR Text | Bengali caption extracted |
| F | Status | Processing status |
| G | Translation | English translation |
| H | Trans Status | Translation success/error |
| I | Title | Generated filename with extension |
| J | Title Status | Title generation success/error |
| K | Data Entry | Lua table entry for Module:PIDDateData |
| L | PIDDateData Status | Update success/error |
| M | Description | Complete wikitext for file page |
| N | Upload Status | Upload success/error |

---

## Key Algorithms Summary

### 1. URL Normalization
- Remove protocol
- Standardize domain variations
- URL decode
- Result: Consistent string for comparison

### 2. Separator Detection
- **Primary:** Edge column scanning + horizontal validation
- **Fallback:** Consecutive white line detection
- **Offset calculation:** Dynamic based on image height
- **Result:** Y-coordinate to split image

### 3. Color Matching
- Tolerance: 2% (255 × 0.02 = ~5 units per channel)
- Matches: White (255,255,255) and near-white (250,249,251)
- Threshold: 98% of pixels must match

### 4. Date Validation
- Extract dates from title and source
- If difference > 7 days: Replace with source date
- Ensures accuracy of temporal metadata

---

## Common Edge Cases

### Case 1: No Separator Found
- **Action:** Use entire image for both photo and OCR
- **Status:** "No separator found - using full image"

### Case 2: 404 Error
- **Action:** Try Wayback Machine
- **If successful:** Use archived image, note in status
- **If failed:** Skip image, log error

### Case 3: CMYK Images
- **Action:** Convert to RGB before processing
- **Reason:** OpenCV doesn't support CMYK natively

### Case 4: Gemini Returns Bengali
- **Action:** Detect Bengali characters, fallback to Google Translate
- **Reason:** Ensures English output

### Case 5: Title > 240 Bytes
- **Action:** Retry with same prompt (AI may generate shorter title)
- **Max retries:** 5
- **If still too long:** Fail with error

---

## Success Criteria

An image is considered successfully processed when:

1. ✅ Image downloaded (or retrieved from Wayback Machine)
2. ✅ Separator detected OR full image used
3. ✅ OCR extracted text
4. ✅ Translation to English succeeded
5. ✅ Filename generated (≤240 bytes)
6. ✅ Uploaded to Commons
7. ✅ Module:PIDDateData updated
8. ✅ Logged to Commons

Any failure in steps 1-5 → Skip to next image  
Any failure in steps 6-7 → Log as "failed upload"

---

## Monitoring & Debugging

### Log Files (Toolforge)

```bash
# View output log
cat ~/pid-bangladesh-uploadbot.out

# View error log
cat ~/pid-bangladesh-uploadbot.err

# Check job status
toolforge jobs list
toolforge jobs show pid-upload-bot
```

### Commons Logs

Monthly log pages contain:
- Thumbnail of each uploaded image
- All processing data (OCR, translation, title, etc.)
- Success/failure status for each step
- Searchable and sortable wikitable

### Debug Checklist

```
Issue: No images found
├─→ Check: Is Module:PIDDateData up to date?
├─→ Check: Is PID website accessible?
└─→ Check: Are there actually new images on PID site?

Issue: OCR fails
├─→ Check: Is Vision API quota exceeded?
├─→ Check: Is image actually in Bengali?
└─→ Check: Network connectivity to Google Cloud

Issue: Upload fails
├─→ Check: Is bot logged in? (Pywikibot credentials)
├─→ Check: Does filename already exist?
├─→ Check: Is filename valid? (illegal characters, length)
└─→ Check: Network connectivity to Commons

Issue: Translation contains Bengali
├─→ Check: Did fallback to Google Translate trigger?
├─→ Check: Google Translate API credentials
└─→ Expected: Rare, but handled automatically
```

---

## Future Improvements

Potential enhancements mentioned in documentation:

1. **Structured Data (Depicts)**
   - Add depicts statements post-upload
   - Requires Pywikibot feature enhancement

2. **Automated Categorization**
   - ML-based person recognition
   - Pre-defined category lists for ministers/officials

3. **Quality Improvements**
   - Higher OCR confidence thresholds
   - Manual review queue for borderline cases

4. **Performance Optimization**
   - Parallel processing of multiple images
   - Batch API calls where possible

---
