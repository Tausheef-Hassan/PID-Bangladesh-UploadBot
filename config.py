# config.py
# Central configuration, constants, logging setup, IPv4 enforcement,
# and shared utility functions for the PID Image Processor & Uploader bot.

import hashlib
import logging
import os
import socket
import sys
import warnings

import requests
import urllib3.util.connection as urllib3_cn
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

warnings.filterwarnings('ignore')

# ── UTF-8 stdout/stderr (Windows Bengali support) ─────────────────────────────
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ── IPv4 enforcement (avoids K8s / Toolforge IPv6 issues) ─────────────────────
def allowed_gai_family():
    """Force IPv4 connections only"""
    return socket.AF_INET

urllib3_cn.allowed_gai_family = allowed_gai_family
print("Forced IPv4 connections to avoid K8s networking issues")

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# $TOOL_DATA_DIR is set by the Toolforge Build Service container to
# /data/project/<toolname>. $HOME inside the container points to /app,
# so credential files must be looked up via TOOL_DATA_DIR, not SCRIPT_DIR.
TOOL_DATA_DIR = os.environ.get('TOOL_DATA_DIR', '')   # '' means local dev

# Prefer TOOL_DATA_DIR (Toolforge Build Service), fall back to SCRIPT_DIR (local)
CREDS_DIR = TOOL_DATA_DIR if TOOL_DATA_DIR else SCRIPT_DIR


def find_pywikibot_config(filename):
    """Locate a pywikibot config file: $TOOL_DATA_DIR first, then next to main.py."""
    for path in (os.path.join(CREDS_DIR, filename), os.path.join(SCRIPT_DIR, filename)):
        if os.path.exists(path):
            return path
    return os.path.join(CREDS_DIR, filename)  # Fallback


USER_CONFIG_PATH = find_pywikibot_config('user-config.py')
PASSWORD_FILE_PATH = find_pywikibot_config('user-password.py')

# Critical fix for Toolforge: Tell Pywikibot exactly where to look for user-config.py
os.environ['PYWIKIBOT_DIR'] = CREDS_DIR

# ── AI / API settings ─────────────────────────────────────────────────────────
VERTEX_LOCATION = "global"
PRIMARY_MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODEL = "gemini-3.5-flash"

# Credential files: $TOOL_DATA_DIR on Toolforge, SCRIPT_DIR locally
GEMINI_CONFIG_PATH = os.path.join(CREDS_DIR, 'gemini.key')   # AI Studio free API key
IA_KEY_PATH = os.path.join(CREDS_DIR, 'ia.key')              # Internet Archive S3-like keys
SECRET_KEY_PATH = os.path.join(CREDS_DIR, 'secret.key')      # Signs the panel's session cookie
MAINTAINERS_PATH = os.path.join(CREDS_DIR, 'maintainers.json')  # Who the owner has granted panel access
OAUTH_KEY_PATH = os.path.join(CREDS_DIR, 'oauth.key')        # Wikimedia OAuth consumer for panel edits
WAYBACK_QUEUE_PATH = os.path.join(CREDS_DIR, 'wayback_pending.json')  # Persistent retry queue
RUN_STATE_PATH = os.path.join(CREDS_DIR, 'run_state.json')   # Per-run outcomes, read by the panel

# Toolforge job this bot runs as; the panel drives it through the Jobs API.
JOB_NAME = 'pid-bot'
TOOL_NAME = os.environ.get('TOOL_NAME') or os.path.basename(TOOL_DATA_DIR) or 'pid-bangladesh-uploadbot2'

# AI call retries (translator's own backoff ladder)
MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF = 60.0

# HTTP retries, applied inside the session returned by http_session()
HTTP_RETRIES = 10
# Google API client retries (Drive OCR upload/export/delete)
API_RETRIES = 5

# ── Mutable shared state (populated at runtime by credentials module) ─────────
IA_KEYS = {'access': None, 'secret': None}  # set by credentials.load_ia_keys()

# ── Google Drive OCR ──────────────────────────────────────────────────────────
DRIVE_TOKEN_PATH = os.path.join(CREDS_DIR, 'drive_token.json')
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.file']

# ── Prompt templates ──────────────────────────────────────────────────────────
TRANSLATION_PROMPT = (
    'Translate the following Bengali text into English in enclyclopedic style. '
    'You may rearrange words or sentences for clarity, but retain all information. '
    'Do not add or omit anything. Only output the translation text and not a single else. '
    'Do not say description or Bengali text in your answer. do not have any bengali text in your answer just give me the translation, no options and no explanations. '
    'Text: "{text}"'
)

TITLE_PROMPT = (
    'Convert this image description (below) into a single Wikimedia Commons\u2013compliant filename (do NOT add the \u201cFile:\u201d prefix, or wikitext, or Title:, do not add filename extention). Follow Wikimedia Commons file naming guidelines: be descriptive, specific, precise, concise and neutral; include date as YYYY-MM-DD if present; avoid photographer/source-only names. Remove any political bias or references to previous governments and strip flattering/propagandistic/honorific language. Output ONLY the filename (no explanation), Regular Case, remove illegal filesystem characters but KEEP spaces and comma and hyphen, keep \u2264240 bytes, and do not add filename extention. '
    'Text: "{text}"'
)

# ── Shared utilities ──────────────────────────────────────────────────────────

def compute_checksum(raw_bytes):
    """Compute MD5 checksum of raw image bytes for duplicate detection"""
    return hashlib.md5(raw_bytes).hexdigest()


def strip_syntaxhighlight(content):
    """Unwrap the <syntaxhighlight lang="json"> block older PIDDateData revisions carry."""
    return (content.removeprefix('<syntaxhighlight lang="json">\n')
                   .removesuffix('\n</syntaxhighlight>'))


def http_session(retries=HTTP_RETRIES, backoff=1.0):
    """A requests Session that retries transport errors and 429/5xx with
    exponential backoff + jitter (urllib3 caps each wait at 120 s).

    Only idempotent methods are retried — Retry's default — so a POST such as
    Save Page Now is never resubmitted behind our back.
    """
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        backoff_jitter=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,   # hand the final response back to the caller
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session
