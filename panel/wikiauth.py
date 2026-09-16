# wikiauth.py
# Wikimedia OAuth for the control panel.
#
# Description fixes are a person's editorial judgement, so they are attributed
# to that person. The panel never sees or needs the bot's password: you sign in
# at meta.wikimedia.org and the panel holds a token that acts as you.
#
# Set up once, as the tool maintainer:
#   1. https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose
#      Callback:  https://<tool>.toolforge.org/oauth/callback
#      Grants:    "Edit existing pages", plus "Edit structured data" for captions
#      Leave "Allow consumer to specify a callback in requests" unticked — see
#      start() below for why.
#   2. Store the approved consumer as envvars, which keeps it off NFS:
#        toolforge envvars create OAUTH_CONSUMER_KEY     # paste, then Ctrl-D
#        toolforge envvars create OAUTH_CONSUMER_SECRET
#      A $TOOL_DATA_DIR/oauth.key file (KEY=VALUE lines, chmod 600) also works
#      and is the local-development path, but /data/project/<tool> is readable
#      by every other tool on Toolforge unless its permissions are tightened.
#
# Configured neither way, the panel stays read-only for Commons and says so.

import os

from mwoauth import ConsumerToken, AccessToken, initiate, complete, identify
from requests_oauthlib import OAuth1

import config

MW_URI = 'https://meta.wikimedia.org/w/index.php'
COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
USER_AGENT = 'pid-bot-panel (https://pid-bangladesh-uploadbot2.toolforge.org)'

session = config.http_session(retries=2)


def consumer():
    """The registered OAuth consumer, or None when not configured.

    Environment first: `toolforge envvars` keeps the secret out of the tool's
    home directory entirely, which matters because /data/project/<tool> is
    readable by every other tool on Toolforge unless its permissions are
    tightened. The file is the fallback for local development.
    """
    key = os.environ.get('OAUTH_CONSUMER_KEY')
    secret = os.environ.get('OAUTH_CONSUMER_SECRET')
    if key and secret:
        return ConsumerToken(key, secret)

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
    return ConsumerToken(key, secret) if key and secret else None


def start():
    """Begin the handshake. Returns (redirect_url, request_token_tuple).

    The callback is `oob`, not our own URL. A consumer registered without
    "Allow consumer to specify a callback in requests" refuses anything else —
    `oauth_callback must be set, and must be set to "oob"` — and MediaWiki then
    redirects to the callback stored on the registration, which is the one we
    want anyway. Sending `oob` also works for a consumer that does allow a
    dynamic callback, so it is the option that works in both cases.
    """
    token = consumer()
    if token is None:
        raise RuntimeError('Wikimedia sign-in is not configured on this tool.')
    redirect_url, request_token = initiate(MW_URI, token, callback='oob')
    return redirect_url, tuple(request_token)


def finish(request_token, response_query_string):
    """Complete the handshake. Returns (access_token_tuple, username)."""
    token = consumer()
    if token is None:
        raise RuntimeError('Wikimedia sign-in is not configured on this tool.')
    access = complete(MW_URI, token,
                      _as_request_token(request_token), response_query_string)
    who = identify(MW_URI, token, access)
    return tuple(access), who['username']


def _as_request_token(stored):
    from mwoauth import RequestToken
    return RequestToken(*stored)


def _auth(access_token):
    token = consumer()
    return OAuth1(token.key, token.secret, access_token[0], access_token[1])


def edit_description(access_token, title, text, summary):
    """Replace a file page's wikitext as the signed-in user.

    Raises RuntimeError with the API's own message on failure, so the panel can
    show what Commons actually objected to rather than a generic error.
    """
    auth = _auth(access_token)
    headers = {'User-Agent': USER_AGENT}

    csrf = session.get(COMMONS_API, auth=auth, headers=headers, timeout=20,
                       params={'action': 'query', 'meta': 'tokens',
                               'type': 'csrf', 'format': 'json'}).json()
    token = csrf.get('query', {}).get('tokens', {}).get('csrftoken')
    if not token:
        raise RuntimeError('Commons did not issue an edit token; sign in again.')

    result = session.post(COMMONS_API, auth=auth, headers=headers, timeout=30,
                          data={'action': 'edit', 'format': 'json',
                                'title': title, 'text': text,
                                'summary': summary,
                                # Not a bot edit: a person decided this.
                                'bot': '0', 'minor': '0',
                                'token': token}).json()

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


def set_caption(access_token, page_id, text, lang='en'):
    """Set the structured-data caption (a MediaInfo label) on a file.

    Captions are not wikitext: they live on the M<pageid> entity and are set
    through the Wikibase API. Most PID files have none at all.
    """
    auth = _auth(access_token)
    headers = {'User-Agent': USER_AGENT}

    csrf = session.get(COMMONS_API, auth=auth, headers=headers, timeout=20,
                       params={'action': 'query', 'meta': 'tokens',
                               'type': 'csrf', 'format': 'json'}).json()
    token = csrf.get('query', {}).get('tokens', {}).get('csrftoken')
    if not token:
        raise RuntimeError('Commons did not issue an edit token; sign in again.')

    result = session.post(COMMONS_API, auth=auth, headers=headers, timeout=30,
                          data={'action': 'wbsetlabel', 'format': 'json',
                                'id': f'M{page_id}', 'language': lang,
                                'value': text.strip(),
                                'summary': 'Caption set via the PID control panel',
                                'bot': '0', 'token': token}).json()
    if 'error' in result:
        info = result['error'].get('info', str(result['error']))
        if 'permissiondenied' in str(result['error'].get('code', '')):
            raise RuntimeError(
                'Your OAuth grant does not cover structured data. Re-propose the '
                'consumer including the "Edit structured data" grant. ' + info)
        raise RuntimeError(info)
    return result
