# credentials.py
# Loads all external credentials and API keys for the PID bot.
# Handles Gemini API key, Internet Archive S3 keys, and Google Cloud credentials.

import json
import os
import sys
import tempfile

import config


# Module-level credential store (populated by load_credentials, used by setup_credentials)
_google_credentials = None


def load_gemini_api_key():
    """Load free AI Studio API key from hidden config file (secondary Google account)"""
    if not os.path.exists(config.GEMINI_CONFIG_PATH):
        raise RuntimeError(
            f"Gemini config not found: {config.GEMINI_CONFIG_PATH}\n"
            f"Fix: echo 'GEMINI_API_KEY=your_key' > {config.GEMINI_CONFIG_PATH} && chmod 600 {config.GEMINI_CONFIG_PATH}"
        )
    with open(config.GEMINI_CONFIG_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('GEMINI_API_KEY='):
                return line.split('=', 1)[1].strip()
    raise RuntimeError(f"GEMINI_API_KEY not found in {config.GEMINI_CONFIG_PATH}")


def load_ia_keys():
    """Load Internet Archive S3-like keys from ia.key for Save Page Now v2 API.
    File format (one per line):
        IA_ACCESS_KEY=your_access_key
        IA_SECRET_KEY=your_secret_key
    Get keys at: https://archive.org/account/s3.php
    """
    if not os.path.exists(config.IA_KEY_PATH):
        print(f"Note: ia.key not found at {config.IA_KEY_PATH} — Wayback archiving will use unauthenticated fallback.")
        print("  Get keys at https://archive.org/account/s3.php and save to ia.key")
        return
    with open(config.IA_KEY_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('IA_ACCESS_KEY='):
                config.IA_KEYS['access'] = line.split('=', 1)[1].strip()
            elif line.startswith('IA_SECRET_KEY='):
                config.IA_KEYS['secret'] = line.split('=', 1)[1].strip()
    if config.IA_KEYS.get('access') and config.IA_KEYS.get('secret'):
        # No key material here: the control panel serves this log publicly.
        print("Internet Archive keys loaded.")
    else:
        print("Warning: ia.key found but IA_ACCESS_KEY/IA_SECRET_KEY missing — check file format.")


def _validate(creds, source):
    """Exit unless the credential dict carries every field Google needs."""
    missing = [f for f in ("type", "project_id", "private_key", "client_email")
               if f not in creds]
    if missing:
        print(f"ERROR: Credentials from {source} missing required fields: {', '.join(missing)}")
        sys.exit(1)
    print(f"Credentials loaded from {source}")


def load_credentials():
    """Load Google Cloud credentials from environment variable or JSON file"""
    global _google_credentials

    # Try environment variable first (for Toolforge)
    creds_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')
    if creds_json:
        try:
            _google_credentials = json.loads(creds_json)
            _validate(_google_credentials, "environment variable")
            return True
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON in environment variable: {e}")

    # Fallback to JSON file (for local development)
    creds_file = os.path.join(config.CREDS_DIR, 'JSON.json')
    print(f"Loading credentials from: {creds_file}")

    if not os.path.exists(creds_file):
        print(f"ERROR: Credentials file not found: {creds_file}")
        sys.exit(1)

    try:
        with open(creds_file, 'r') as f:
            _google_credentials = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Failed to load credentials from file: {e}")
        sys.exit(1)

    _validate(_google_credentials, "file")
    return True


def setup_credentials():
    """Write Google credentials to a temp file and set env vars. Returns temp file path."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(_google_credentials, f)
        creds_path = f.name

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    os.environ["GOOGLE_CLOUD_PROJECT"] = _google_credentials["project_id"]

    return creds_path
