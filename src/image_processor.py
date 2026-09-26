# image_processor.py
# Downloads images, splits them with the (frozen) Cropper in cropper.py,
# and runs OCR via the Google Drive API.

import os
import re
from io import BytesIO

import cv2
import numpy as np
import requests
from PIL import Image, ImageFile, ImageOps
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as gdrive_build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

import config
from src import wayback
from src.cropper import Cropper
from config import compute_checksum

# PID serves the odd truncated JPEG; decode what is there rather than raising.
ImageFile.LOAD_TRUNCATED_IMAGES = True


class ImageProcessor(Cropper):
    def __init__(self):
        self._drive_service = None
        # One session per instance; main.py builds one ImageProcessor per worker thread.
        self._session = config.http_session()

    def cleanup_temp_drive_files(self):
        """Find and delete any leftover ocr_temp.png files in Google Drive."""
        if not self._drive_service:
            return
        try:
            results = self._drive_service.files().list(
                q="name='ocr_temp.png'",
                spaces='drive',
                fields='files(id, name)'
            ).execute(num_retries=config.API_RETRIES)
            items = results.get('files', [])
            if items:
                print(f"Found {len(items)} orphaned temporary Drive files. Cleaning up...")
                for item in items:
                    try:
                        self._drive_service.files().delete(
                            fileId=item['id']).execute(num_retries=config.API_RETRIES)
                    except Exception as e:
                        print(f"Failed to clean up {item['id']}: {e}")
        except Exception as e:
            print(f"Failed to query Drive for temp files: {e}")

    def initialize_vision_client(self, cleanup_orphans=False):
        """Initialize Google Drive OCR using OAuth2 user credentials.

        cleanup_orphans is for the boot instance only: the sweep matches on
        name, so running it from a worker starting up mid-run would delete the
        ocr_temp.png another worker is still exporting.
        """
        try:
            if not os.path.exists(config.DRIVE_TOKEN_PATH):
                raise RuntimeError(
                    "drive_token.json not found. Run generate_token.py first.")
            creds = Credentials.from_authorized_user_file(
                config.DRIVE_TOKEN_PATH, config.DRIVE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(GoogleAuthRequest())
                with open(config.DRIVE_TOKEN_PATH, 'w') as f:
                    f.write(creds.to_json())
            self._drive_service = gdrive_build(
                'drive', 'v3', credentials=creds)
            
            # Clean up any leftover files from previous interrupted runs
            if cleanup_orphans:
                self.cleanup_temp_drive_files()

            return True, "Drive OCR initialized with user OAuth2 credentials"
        except Exception as e:
            return False, f"Failed to initialize Drive OCR: {str(e)}"

    def _fetch_and_decode(self, url):
        """Download one URL and decode it to (OpenCV BGR image, format, exif, raw bytes)."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = self._session.get(url, headers=headers, timeout=30, verify=False)
        response.raise_for_status()

        raw_bytes = response.content
        img_pil = Image.open(BytesIO(raw_bytes))

        # Store EXIF data before any processing
        exif_data = img_pil.info.get('exif', None)
        img_pil = ImageOps.exif_transpose(img_pil)

        # Detect image format before the mode conversion drops it
        img_format = img_pil.format.lower() if img_pil.format else 'jpg'
        if img_format == 'jpeg':
            img_format = 'jpg'

        # Anything exotic (CMYK, P, LA, ...) becomes RGB
        if img_pil.mode not in ('RGB', 'L', 'RGBA'):
            img_pil = img_pil.convert('RGB')

        img_np = np.array(img_pil)

        # Convert to OpenCV BGR format
        if img_np.ndim == 2:
            img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)   # Grayscale
        elif img_np.ndim == 3 and img_np.shape[2] == 4:
            img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGBA2BGR)
        elif img_np.ndim == 3 and img_np.shape[2] == 3:
            img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        else:
            raise ValueError("Invalid image format")

        return img_cv, img_format, exif_data, raw_bytes

    def download_image(self, url):
        """Download image from URL and return image with its extension.
        Falls back to Wayback Machine on 404.
        """
        try:
            return self._fetch_and_decode(url) + (None,)

        except requests.exceptions.RequestException as e:
            if "404" in str(e) or (hasattr(e, 'response') and e.response is not None and e.response.status_code == 404):
                wayback_url, wayback_error = wayback.get_wayback_url(url)
                if not wayback_url:
                    return None, None, None, None, f"404 error - {wayback_error}"
                try:
                    return self._fetch_and_decode(wayback_url) + ("Retrieved from Wayback Machine",)
                except Exception as wb_e:
                    return None, None, None, None, f"404 error - Wayback Machine also failed: {str(wb_e)}"
            return None, None, None, None, f"Download failed: {str(e)}"
        except Exception as e:
            return None, None, None, None, f"Image processing error: {str(e)}"

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
        text = text.replace(' \ufeff________________ ', '')
        text = text.replace('________________', '')
        text = text.replace('  ', ' ')
        text = text.replace('  ', ' ')

        return text

    def perform_ocr(self, image):
        """Perform OCR using Google Drive API (free, replaces Vision API)"""
        file_id = None
        try:
            # Encode in memory — the bytes only ever travel to Drive, so a
            # temp file on disk buys nothing but I/O and a cleanup path.
            ok, encoded = cv2.imencode('.png', image)
            if not ok:
                raise RuntimeError("cv2.imencode failed for the OCR section")

            # Upload image to Drive as a Google Doc — Drive OCRs it automatically
            file_metadata = {
                'name': 'ocr_temp.png',
                'mimeType': 'application/vnd.google-apps.document',
            }
            media = MediaIoBaseUpload(
                BytesIO(encoded.tobytes()),
                mimetype='image/png',
                resumable=False
            )
            uploaded = self._drive_service.files().create(
                body=file_metadata,
                media_body=media,
                ocrLanguage='bn',   # Bengali hint — improves accuracy
                fields='id'
            ).execute(num_retries=config.API_RETRIES)
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
                _, done = downloader.next_chunk(num_retries=config.API_RETRIES)

            raw_text = text_buffer.getvalue().decode('utf-8', errors='replace')

            # Strip the Drive separator line that appears in exported docs
            raw_text = raw_text.replace('________________\n\n', '')
            raw_text = re.sub(r'\s+', ' ', raw_text).strip()
            cleaned_text = self.clean_ocr_text(raw_text)
            return cleaned_text

        except Exception as e:
            return f"OCR Error: {str(e)}"

        finally:
            # Always delete the temp file from Drive
            if file_id:
                try:
                    self._drive_service.files().delete(
                        fileId=file_id).execute(num_retries=config.API_RETRIES)
                except Exception as del_e:
                    print(
                        f"Warning: could not delete temp Drive file {file_id}: {del_e}")

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
            image, img_format, exif_data, raw_bytes, error = self.download_image(
                image_url)
            if error and image is None:
                result['status'] = error
                return result
            # An image *with* a message is the Wayback copy of a photo PID has
            # removed. It still gets cropped, read and uploaded; stopping here
            # lost every such photo.
            from_archive = bool(error)

            result['format'] = img_format
            result['exif'] = exif_data

            # Checksum duplicate check — runs BEFORE OCR/AI (saves quota)
            if raw_bytes:
                checksum = compute_checksum(raw_bytes)
                result['checksum'] = checksum
                if wikimedia_checksums and checksum in wikimedia_checksums:
                    print(
                        f"Row {row_index}: Duplicate image detected via checksum — skipping OCR/AI")
                    result['status'] = 'Duplicate (checksum match)'
                    result['is_duplicate'] = True
                    result['image'] = image
                    return result
            else:
                result['checksum'] = ''

            print(f"Row {row_index}: Finding separator...")
            separator_row, fallback_used, separator_offset = self.find_white_separator(
                image)

            photo_section, text_section = self.crop_image_sections(
                image, separator_row, apply_side_crop=fallback_used)

            if photo_section is None or separator_row == -1:
                result['status'] = 'No separator found - using full image'
                result['image'] = image

                print(
                    f"Row {row_index}: Performing OCR on bottom 40% of image...")
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

                print(
                    f"Row {row_index}: Performing OCR on text section (trimmed by {1 * separator_offset}px)...")
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

            if from_archive and result['status'].startswith('Success'):
                result['status'] += ' (from archive)'
            print(f"Row {row_index}: Image processing completed")

        except Exception as e:
            result['status'] = f"Error: {str(e)}"
            print(f"Row {row_index}: Error - {str(e)}")

        return result
