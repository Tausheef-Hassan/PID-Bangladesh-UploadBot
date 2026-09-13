# translator.py
# Bengali OCR text pre-processing, translation (Gemini primary / Google Translate fallback),
# and Wikimedia Commons filename generation.

import os
import random
import re
from datetime import datetime
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


_TRANSIENT_MARKERS = ("429", "resource exhausted", "timeout",
                      "connection", "temporar", "503", "500")


class _Retry(Exception):
    """Raised by an `accept` callback to ask the same model for another sample."""


def _gemini_text(genai_client, vertex_client, prompt, max_tokens, row_index, accept):
    """Walk the free→paid model ladder with backoff until `accept` takes an answer.

    Attempt order (cheapest first): AI Studio primary (Free), Vertex primary
    (Paid), AI Studio fallback (Free), Vertex fallback (Paid). Each slot gets
    MAX_RETRIES tries on a transient error or an `accept` that raises _Retry.

    Returns (accepted_value, source_name); raises RuntimeError if all slots fail.
    """
    ladder = (
        (genai_client,  config.PRIMARY_MODEL,  "AI Studio primary  (Free)"),
        (vertex_client, config.PRIMARY_MODEL,  "Vertex AI primary  (Paid)"),
        (genai_client,  config.FALLBACK_MODEL, "AI Studio fallback (Free)"),
        (vertex_client, config.FALLBACK_MODEL, "Vertex AI fallback (Paid)"),
    )
    last_exception = None

    for client, model, source_name in ladder:
        backoff = config.INITIAL_BACKOFF

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                print(f"Row {row_index}: Attempt {attempt} via {source_name} ({model})...")

                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"temperature": 1.0, "top_p": 0.95,
                            "max_output_tokens": max_tokens},
                )
                sleep(2)   # client-side spacing; the free tier is rate limited

                text = (getattr(resp, "text", None)
                        or resp.candidates[0].content.parts[0].text or "").strip()
                if not text:
                    raise RuntimeError("Empty response")

                return accept(text), source_name

            except Exception as e:
                last_exception = e
                retryable = isinstance(e, _Retry) or any(
                    m in str(e).lower() for m in _TRANSIENT_MARKERS)

                if retryable and attempt < config.MAX_RETRIES:
                    wait = min(backoff, config.MAX_BACKOFF) + random.uniform(0, backoff * 0.5)
                    print(f"Row {row_index}: Retryable error on {source_name} (attempt {attempt}): {e} — retrying in {wait:.1f}s")
                    sleep(wait)
                    backoff = min(backoff * config.BACKOFF_MULTIPLIER, config.MAX_BACKOFF)
                else:
                    print(f"Row {row_index}: {source_name} gave up after attempt {attempt}: {e}")
                    break  # move to next client/model slot

    raise RuntimeError(f"All models failed — {last_exception!r}")


def translate_text(genai_client, vertex_client, translate_client, text, row_index):
    """Translate Bengali text to English via the Gemini ladder.

    Google Translate is the last resort, applied on top when Gemini's own
    output still contains Bengali.
    """
    if not text.strip():
        return "", "EmptyText"

    def accept(translated):
        if contains_bengali(translated):
            print(f"Row {row_index}: Bengali detected in Gemini output — running Google Translate cleanup")
            gt_result = google_translate(translate_client, translated)
            if gt_result:
                sleep(1)
                return gt_result
        return translated

    prompt = config.TRANSLATION_PROMPT.format(text=text.replace('"', "'"))
    try:
        translated, source_name = _gemini_text(
            genai_client, vertex_client, prompt, 8192, row_index, accept)
    except RuntimeError as e:
        print(f"Row {row_index}: All translation clients exhausted. {e}")
        return "", f"Error: {e}"

    print(f"Row {row_index}: Translation succeeded via {source_name}")
    return translated, "Success"


# ── Title / filename generation ───────────────────────────────────────────────

def replace_date_if_needed(title, col_b_date_str):
    """Replace date in title if difference > 7 days from the scraper date"""
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

    if not text:
        return "", "EmptyText"

    def accept(title):
        # Commons caps page titles at 255 bytes; the prompt asks for ≤240.
        if len(title.encode('utf-8')) > 240:
            raise _Retry(f"Title too long ({len(title.encode('utf-8'))} bytes)")
        title = replace_date_if_needed(title, date_str)
        print(f"Row {row_index}: Title generated (without extension): {title}")
        return f"{title}.{img_format}"

    prompt = config.TITLE_PROMPT.format(text=text.replace('"', "'"))
    try:
        title, source_name = _gemini_text(
            genai_client, vertex_client, prompt, 2048, row_index, accept)
    except RuntimeError as e:
        print(f"Row {row_index}: Failed all models: {e}")
        return "", f"Error: {e}"

    print(f"Row {row_index}: Final title from {source_name}: {title}")
    return title, "Success"
