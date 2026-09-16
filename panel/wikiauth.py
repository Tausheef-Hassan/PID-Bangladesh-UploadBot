# wikiauth.py
# Wikimedia OAuth 2.0 for the control panel.
#
# Description fixes are a person's editorial judgement, so they are attributed
# to that person. The panel never sees or needs the bot's password: you sign in
# at meta.wikimedia.org and the panel holds a bearer token that acts as you.
#
# Set up once, as the tool maintainer:
#   1. https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose
#      OAuth version:  2.0, "confidential" (this is a web app with a secret)
#      Callback:       https://<tool>.toolforge.org/oauth/callback
#      Grants:         "Edit existing pages", plus "Edit structured data" if you
#                      want captions as well as descriptions
#   2. Store the Client ID and Client secret as envvars, which keeps them off
#      NFS entirely:
#        toolforge envvars create OAUTH_CONSUMER_KEY     # the Client ID
#        toolforge envvars create OAUTH_CONSUMER_SECRET  # the Client secret
#      A $TOOL_DATA_DIR/oauth.key file (KEY=VALUE lines, chmod 600) also works
#      and is the local-development path, but /data/project/<tool> is readable
#      by every other tool on Toolforge unless its permissions are tightened.
#
# Configured neither way, the panel stays read-only for Commons and says so.

import os
import secrets
import time
from urllib.parse import urlencode

import config

# OAuth 2.0 lives under rest.php, not the 1.0a endpoints on index.php. A 1.0a
# handshake against a 2.0 consumer is what "Wrong OAuth version, E012" means.
OAUTH2 = 'https://meta.wikimedia.org/w/rest.php/oauth2'
AUTHORIZE_URL = f'{OAUTH2}/authorize'
TOKEN_URL = f'{OAUTH2}/access_token'
PROFILE_URL = f'{OAUTH2}/resource/profile'

COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
USER_AGENT = 'pid-bot-panel (https://pid-bangladesh-uploadbot2.toolforge.org)'

session = config.http_session(retries=2)


def consumer():
    """(client_id, client_secret), or None when not configured.

    Environment first: `toolforge envvars` keeps the secret out of the tool's
    home directory entirely, which matters because /data/project/<tool> is
    readable by every other tool on Toolforge unless its permissions are
    tightened. The file is the fallback for local development.
    """
    key = os.environ.get('OAUTH_CONSUMER_KEY')
    secret = os.environ.get('OAUTH_CONSUMER_SECRET')
    if key and secret:
        return key, secret

    values = {}
    try:
        with open(config.OAUTH_KEY_PATH, encoding='utf-8') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    name, _, value = line.partition('=')
                    values[name.strip()] = value.strip()
    except OSError:
        return None

    key = values.get('OAUTH_CONSUMER_KEY')
    secret = values.get('OAUTH_CONSUMER_SECRET')
    return (key, secret) if key and secret else None


def _rest_json(response, what):
    """Parse a rest.php reply, raising with MediaWiki's own words on failure.

    Two error shapes come out of these endpoints and neither looks like the
    success body, so without this every failure reads as "the field I wanted
    was missing" and the real reason never reaches the person signing in:

      {"error": "access_denied", "error_description": "...", "hint": "..."}
      {"errorKey": "mwoauth-invalid-authorization",
       "messageTranslations": {"en": "..."}, "httpCode": 403}
    """
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(
            f'{what}: Wikimedia returned {response.status_code} and no JSON.')

    if not isinstance(body, dict):
        raise RuntimeError(f'{what}: unexpected reply from Wikimedia.')

    detail = (body.get('messageTranslations', {}).get('en')
              or body.get('error_description')
              or body.get('hint')
              or body.get('errorKey')
              or body.get('error')
              or body.get('message'))
    if detail or response.status_code >= 400:
        raise RuntimeError(
            f'{what}: {detail or response.status_code} '
            f'(HTTP {response.status_code})')
    return body


def start(redirect_uri):
    """Begin the handshake. Returns (authorize_url, state).

    `state` is ours to remember and check when the browser comes back: 2.0 has
    no request token, so this is the only thing tying the callback to the
    sign-in that started it.
    """
    pair = consumer()
    if pair is None:
        raise RuntimeError('Wikimedia sign-in is not configured on this tool.')
    client_id, _secret = pair

    state = secrets.token_urlsafe(24)
    query = urlencode({'response_type': 'code',
                       'client_id': client_id,
                       'redirect_uri': redirect_uri,
                       'state': state})
    return f'{AUTHORIZE_URL}?{query}', state


def finish(code, redirect_uri):
    """Exchange the code for a token. Returns (token_dict, username)."""
    pair = consumer()
    if pair is None:
        raise RuntimeError('Wikimedia sign-in is not configured on this tool.')
    client_id, client_secret = pair

    payload = _rest_json(session.post(
        TOKEN_URL, timeout=20, headers={'User-Agent': USER_AGENT},
        data={'grant_type': 'authorization_code',
              'code': code,
              'client_id': client_id,
              'client_secret': client_secret,
              'redirect_uri': redirect_uri}), 'Exchanging the code')

    if 'access_token' not in payload:
        raise RuntimeError('Wikimedia did not issue a token.')

    # ponytail: the refresh token is deliberately dropped. Both tokens are long
    # JWTs and the session is a 4 KB signed cookie, so keeping both risks the
    # browser silently discarding the whole thing. Four hours covers a review
    # session; if people start getting signed out mid-pass, move the token to
    # server-side storage rather than squeezing it into the cookie.
    token = {'access_token': payload['access_token'],
             'expires_at': time.time() + int(payload.get('expires_in', 14400))}

    who = _rest_json(session.get(PROFILE_URL, timeout=20,
                                 headers=_headers(token)), 'Reading your profile')
    username = who.get('username')
    if not username:
        # A 200 with no username means the shape changed, not that the sign-in
        # was refused; naming the fields we did get is what makes that legible.
        raise RuntimeError('Wikimedia did not say who signed in. It returned: '
                           + ', '.join(sorted(who)[:8]))
    return token, username


def expired(token):
    return not token or time.time() >= token.get('expires_at', 0)


def _headers(token):
    return {'Authorization': 'Bearer ' + token['access_token'],
            'User-Agent': USER_AGENT}


def _csrf(token):
    """A CSRF token for the signed-in user, or a clear reason why not."""
    if expired(token):
        raise RuntimeError('Your Commons sign-in expired. Sign in again.')
    data = session.get(COMMONS_API, headers=_headers(token), timeout=20,
                       params={'action': 'query', 'meta': 'tokens',
                               'type': 'csrf', 'format': 'json'}).json()
    csrf = data.get('query', {}).get('tokens', {}).get('csrftoken')
    # Anonymous gets the literal '+\\', which would then fail confusingly at
    # the edit itself; catch it here where the cause is still obvious.
    if not csrf or csrf == '+\\':
        raise RuntimeError('Commons did not accept that sign-in. Sign in again.')
    return csrf


def edit_description(token, title, text, summary):
    """Replace a file page's wikitext as the signed-in user.

    Raises RuntimeError with the API's own message on failure, so the panel can
    show what Commons actually objected to rather than a generic error.
    """
    result = session.post(COMMONS_API, headers=_headers(token), timeout=30,
                          data={'action': 'edit', 'format': 'json',
                                'title': title, 'text': text,
                                'summary': summary,
                                # Not a bot edit: a person decided this.
                                'bot': '0', 'minor': '0',
                                'token': _csrf(token)}).json()

    if 'error' in result:
        raise RuntimeError(result['error'].get('info', str(result['error'])))
    if result.get('edit', {}).get('result') != 'Success':
        raise RuntimeError(f"Commons declined the edit: {result}")
    return result['edit']


def fetch_wikitext(title):
    """Current wikitext of a page, or None. Read-only, so no auth needed."""
    try:
        r = session.get(COMMONS_API, timeout=20,
                        headers={'User-Agent': USER_AGENT},
                        params={'action': 'parse', 'page': title,
                                'prop': 'wikitext', 'formatversion': 2,
                                'format': 'json'})
        return r.json()['parse']['wikitext']
    except Exception:
        return None


def set_caption(token, page_id, text, lang='en'):
    """Set the structured-data caption (a MediaInfo label) on a file.

    Captions are not wikitext: they live on the M<pageid> entity and are set
    through the Wikibase API. Most PID files have none at all.
    """
    result = session.post(COMMONS_API, headers=_headers(token), timeout=30,
                          data={'action': 'wbsetlabel', 'format': 'json',
                                'id': f'M{page_id}', 'language': lang,
                                'value': text.strip(),
                                'summary': 'Caption set via the PID control panel',
                                'bot': '0', 'token': _csrf(token)}).json()
    if 'error' in result:
        info = result['error'].get('info', str(result['error']))
        if 'permissiondenied' in str(result['error'].get('code', '')):
            raise RuntimeError(
                'Your OAuth grant does not cover structured data. Re-propose the '
                'consumer including the "Edit structured data" grant. ' + info)
        raise RuntimeError(info)
    return result
