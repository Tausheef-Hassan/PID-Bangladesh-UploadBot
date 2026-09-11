# PID Image Processor & Uploader

## Project Overview
The PID Image Processor & Uploader is a Python-based automation tool designed to archive images from the Bangladesh Press Information Department (PID) website (`pressinform.gov.bd`) to Wikimedia Commons. The project automates the extraction of images, performs image processing to separate visual content from text, uses AI for OCR and translation, and handles the submission to Wikimedia Commons with appropriate metadata.

### Main Technologies
- **Language:** Python 3.x
- **AI Services:** 
    - **Google Gemini (genai):** Used for translating Bengali descriptions to English and generating descriptive filenames.
    - **Google Drive API:** Leveraged for high-quality OCR (Optical Character Recognition) by utilizing Google Doc export capabilities.
    - **Google Cloud Translate:** Secondary fallback for text translation.
- **Image Processing:** 
    - **OpenCV (cv2):** Used for separator detection and image segmentation.
    - **Pillow (PIL):** Used for image format handling and EXIF data preservation.
    - **NumPy:** Used for array-based image manipulations.
- **Web Interaction:**
    - **BeautifulSoup4 & Requests:** For scraping the PID archive pages.
    - **Pywikibot:** The official framework for interacting with the Wikimedia Commons API.
- **Data Handling:**
    - Plain dicts and `json`: run results are written to a JSON log page on Commons.
- **Deployment:**
    - **Toolforge jobs:** `toolforge/job.yaml` runs the bot hourly; no web service is involved.

## Architecture
The application operates as a sequential pipeline:
1.  **Scraper:** Crawls the `daily-photos` section of the PID website, filtering for images not yet present on Wikimedia Commons using checksums and URL history.
2.  **Image Processor:** 
    - Downloads images (with Wayback Machine integration for 404 handling).
    - **Automatic Archiving:** Automatically submits the source image URL and detail page URL to the Wayback Machine for permanent preservation.
    - Detects a horizontal white separator to isolate the photograph from the printed Bengali text.
    - Performs OCR on the text section via the Google Drive API.
3.  **Linguistic Processing:**
    - Cleans OCR output using manual replacements (`translation_replacements.tsv`).
    - Translates Bengali text to English using Gemini (Primary) or Google Translate (Fallback).
4.  **Metadata & Filename Generation:** Uses Gemini to craft a policy-compliant, descriptive filename for Wikimedia Commons.
5.  **Uploader:** Uploads the processed image to Commons using `pywikibot`, adding standard templates (`{{Information}}`, `{{PD-BDGov-PID}}`, etc.).
6.  **Post-Process Logging:** Updates a central Wikimedia data module (`Module:PIDDateData`) and logs execution results to a bot log page on Commons.

## Building and Running

### Prerequisites
1.  **Python Environment:** Install dependencies (inferred):
    ```bash
    pip install -r requirements.txt
    ```
2.  **Authentication Files:**
    - `user-config.py` & `user-password.py`: Pywikibot credentials. The script looks for these in:
        1. `$TOOL_DATA_DIR` (set by the Toolforge Build Service)
        2. The same directory as `main.py` (local dev)
        4. The current working directory
    - `gemini.key`: Contains `GEMINI_API_KEY=your_key`.
    - `JSON.json`: Google Cloud Service Account key.
    - `drive_token.json` & `drive_oauth_client.json`: Google Drive OAuth2 credentials.

### Execution
Run the main pipeline:
```bash
python main.py
```

## Key Files
- `main.py`: Core logic encompassing scraping, processing, translation, and uploading.
- `gemini.key`: Configuration for Gemini API access.
- `JSON.json`: Credentials for Google Cloud services.
- `drive_token.json`: Persistent OAuth2 token for Google Drive integration.
- `drive_oauth_client.json`: Client secrets for Google Drive API.

## Development Conventions
- **Resilience:** The bot uses aggressive retry logic with exponential backoff for all network and AI API calls to handle transient failures.
- **Duplicate Prevention:** MD5 checksums of raw image bytes are compared against existing Wikimedia records before any AI processing occurs to save on API quota.
- **IPv4 Enforcement:** Specifically forces IPv4 to avoid common networking issues in certain Kubernetes environments (like Toolforge).
- **Toolforge Ready:** Runs as a one-shot background job; Toolforge's scheduler owns the hourly cadence.
