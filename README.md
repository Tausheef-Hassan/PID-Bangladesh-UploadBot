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
