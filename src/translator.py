# translator.py
# Bengali OCR text pre-processing, translation (Gemini primary / Google Translate fallback),
# and Wikimedia Commons filename generation.

import os
import random
import re
from time import sleep

import config
from config import logger


# ── Pre-translation replacement table ────────────────────────────────────────

def load_translation_replacements():
    """Load find/replace pairs from translation_replacements.tsv next to main.py.
    Format: BengaliText|||EnglishReplacement  (one per line, # for comments)
    """
    SEPARATOR = '|||'
    replacements = []
    tsv_path = os.path.join(config.SCRIPT_DIR, 'translation_replacements.tsv')
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
                    logger.warning(
                        f"translation_replacements.tsv line {line_num}: missing '{SEPARATOR}' separator, skipping: {line!r}")
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


# ── Language helpers ──────────────────────────────────────────────────────────

def contains_bengali(text):
    """Check if text contains any Bengali characters"""
    return bool(text) and re.search(r'[\u0980-\u09FF]', text) is not None


# ── Translation ───────────────────────────────────────────────────────────────

def google_translate(translate_client, text):
    """Translate Bengali text to English using Google Translate API (fallback)"""
    try:
        result = translate_client.translate(text, source_language='bn', target_language='en')
        return result['translatedText']
    except Exception as e:
        print(f"Google Translate error: {e}")
        return None


def translate_text(genai_client, vertex_client, translate_client, text, row_index):
    """Translate Bengali text to English.

    Attempt order (cheapest first):
      1. AI Studio  — primary model   (Free)
      2. Vertex AI  — primary model   (Paid)
      3. AI Studio  — fallback model  (Free)
      4. Vertex AI  — fallback model  (Paid)
      5. Google Translate             (Paid, last resort if Gemini output still has Bengali)
    """
    if not text.strip():
        return "", "EmptyText"

    prompt = config.TRANSLATION_PROMPT.format(text=text.replace('"', "'"))

    models_to_try = [
        (genai_client, config.PRIMARY_MODEL,  "AI Studio primary  (Free)"),
        (vertex_client, config.PRIMARY_MODEL,  "Vertex AI primary  (Paid)"),
        (genai_client, config.FALLBACK_MODEL, "AI Studio fallback (Free)"),
        (vertex_client, config.FALLBACK_MODEL, "Vertex AI fallback (Paid)"),
    ]

    last_exception = None

    for client, model_name, source_name in models_to_try:
        backoff = config.INITIAL_BACKOFF

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                print(f"Row {row_index}: Translation attempt {attempt} via {source_name} ({model_name})...")

                generation_config = {
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "max_output_tokens": 8192,
                }

                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=generation_config
                )
                sleep(2)

                if hasattr(resp, "text"):
                    translated = resp.text.strip()
                else:
                    translated = resp.candidates[0].content.parts[0].text.strip()

                translated = (translated or "").strip()
                if not translated:
                    raise RuntimeError("Empty response")

                # If Gemini still returns Bengali, run Google Translate on top
                if contains_bengali(translated):
                    print(f"Row {row_index}: Bengali detected in Gemini output — running Google Translate cleanup")
                    gt_result = google_translate(translate_client, translated)
                    if gt_result:
                        translated = gt_result
                        sleep(1)

                print(f"Row {row_index}: Translation succeeded via {source_name}")
                return translated, "Success"

            except Exception as e:
                last_exception = e
                msg = str(e).lower()
                is_transient = (
                    "429" in msg or "resource exhausted" in msg
                    or "timeout" in msg or "connection" in msg
                    or "temporar" in msg or "503" in msg or "500" in msg
                )

                if is_transient and attempt < config.MAX_RETRIES:
                    wait = min(backoff, config.MAX_BACKOFF)
                    print(f"Row {row_index}: Transient error on {source_name} (attempt {attempt}): {e} — retrying in {wait:.1f}s")
                    sleep(wait)
                    backoff = min(backoff * config.BACKOFF_MULTIPLIER, config.MAX_BACKOFF)
                else:
                    print(f"Row {row_index}: {source_name} gave up after attempt {attempt}: {e}")
                    break  # move to next client/model slot

    print(f"Row {row_index}: All translation clients exhausted. Last error: {last_exception}")
    return "", f"Error: All models failed — {repr(last_exception)}"


# ── Title / filename generation ───────────────────────────────────────────────

def replace_date_if_needed(title, col_b_date_str):
    """Replace date in title if difference > 7 days from the scraper date"""
    from datetime import datetime
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


def generate_title(genai_client, vertex_client, description, date_str, row_index, img_format='jpg'):
    """Generate a Wikimedia Commons–compliant filename via Gemini"""
    text = f"{description} {date_str}".strip()

    if not text.strip():
        return "", "EmptyText"

    prompt = config.TITLE_PROMPT.format(text=text.replace('"', "'"))

    models_to_try = [
        (genai_client, config.PRIMARY_MODEL,  "AI Studio primary  (Free)"),
        (vertex_client, config.PRIMARY_MODEL,  "Vertex AI primary  (Paid)"),
        (genai_client, config.FALLBACK_MODEL, "AI Studio fallback (Free)"),
        (vertex_client, config.FALLBACK_MODEL, "Vertex AI fallback (Paid)"),
    ]
    last_exception = None

    for client, model, source_name in models_to_try:
        backoff = config.INITIAL_BACKOFF

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                print(f"Row {row_index}: Sending request to {model} via {source_name}...")

                generation_config = {
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "max_output_tokens": 2048,
                }

                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=generation_config
                )
                sleep(2)

                print(f"Row {row_index}: Received response from {source_name}")
                sleep(1)

                if hasattr(resp, "text"):
                    title = resp.text.strip()
                else:
                    title = resp.candidates[0].content.parts[0].text.strip()

                title = (title or "").strip()

                if not title:
                    raise RuntimeError("Empty response")

                if len(title.encode('utf-8')) > 240:
                    if attempt < config.MAX_RETRIES:
                        print(f"Row {row_index}: Title too long ({len(title.encode('utf-8'))} bytes), retrying")
                        wait = min(backoff, config.MAX_BACKOFF) + random.uniform(0, backoff * 0.5)
                        sleep(wait)
                        backoff = min(backoff * config.BACKOFF_MULTIPLIER, config.MAX_BACKOFF)
                        continue
                    else:
                        raise RuntimeError(f"Title exceeds 240 bytes after {config.MAX_RETRIES} attempts")

                title = replace_date_if_needed(title, date_str)

                print(f"Row {row_index}: Title generated with {source_name} (without extension): {title}")

                # Append file extension
                title = title + '.' + img_format

                print(f"Row {row_index}: Final title (with extension): {title}")
                sleep(2)
                return title, "Success"

            except Exception as e:
                last_exception = e
                msg = str(e).lower()

                is_429 = ("429" in msg) or ("resource exhausted" in msg)
                is_transient = is_429 or ("timeout" in msg) or ("connection" in msg) or \
                               ("temporar" in msg) or ("503" in msg) or ("500" in msg)

                if is_transient and attempt < config.MAX_RETRIES:
                    wait = min(backoff, config.MAX_BACKOFF) + random.uniform(0, backoff * 0.5)
                    print(f"Row {row_index}: Transient error on {source_name} (attempt {attempt}): {e}, retrying in {wait:.1f}s")
                    sleep(wait)
                    backoff = min(backoff * config.BACKOFF_MULTIPLIER, config.MAX_BACKOFF)
                    continue
                else:
                    print(f"Row {row_index}: {source_name} error (no more retries): {e}")
                    break

    print(f"Row {row_index}: Failed all models: {last_exception}")
    return "", f"Error:{repr(last_exception)}"
