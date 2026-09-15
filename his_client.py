"""
HIS (YASASII) report client.

The sign-in half of this file is the one already proven in the Pending
dashboard, carried over unchanged in behaviour:

    POST {auth_url}/api/v1/Authentication/SignIn   → one-hour bearer token
    POST {base_url}/api/v1/MISManager/GetReportData

Getting a token: the portal scrambles the password in the browser before
sending it (the "ENCV0" in the token) with a key that lives in its JavaScript,
so the sign-in cannot be built here — browser_login.py fills in the real login
page in a browser with no window and brings the token back.

Renewing it: the portal's own encrypted sign-in payload is captured during that
login, so the token can be renewed for the rest of the shift by re-posting it,
without starting a browser again. If the HIS ever refuses the replay, the
browser login runs again by itself.

What is new here is the report half. This dashboard reads *several* reports —
one main list and others joined to it by sample number — so a report is a file
in reports/ holding the exact body the portal posts, and fetching one returns
its rows as dictionaries with the HIS's own column names left alone. Nothing
here knows what the columns mean; that is the dashboard's business.

Apart from the browser already installed on the PC, only the standard library
is used, so nothing new has to be installed and a PyInstaller build stays a
single file.
"""

import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import browser_login

# ─────────────────────────────────────────────────────────────
# DEFAULTS  (everything here is overridable from data/his_config.json)
# ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # API host. The portal the staff open is :8000 / :8047, the REST API is :8026.
    'base_url': 'https://hisalmoosaprod1.almoosahospital.com.sa:8026',
    'report_path': '/api/v1/MISManager/GetReportData',
    # Authentication lives on its own host/port (the portal itself is :8000).
    'auth_url': 'https://hisalmoosaprod1.almoosahospital.com.sa:8006',
    'login_path': '/api/v1/Authentication/SignIn',
    # The login page itself — this is the address the staff open every morning.
    'portal_url': 'https://hisalmoosaprod1.almoosahospital.com.sa:8000/',
    # Site picked on that page. The staff are not asked for it — everyone here
    # works at the same hospital. Blank means "whatever the portal offers first".
    'login_site': 'Almoosa Specialist Hospital',
    'known_sites': [],
    # Leave blank to use the Edge/Chrome already installed on this PC.
    'browser_path': '',
    'login_timeout': 120,
    # The portal identifies itself with this header when it signs in.
    'machine_name': 'YARWEB_UI',
    'login_referrer': 'https://hisalmoosaprod1.almoosahospital.com.sa:8000/',
    # Sent as headers on login when known (they are also embedded in the JWT,
    # so a pasted token teaches us the right values — see adopt_token_metadata).
    'app_client': 'hisalmoosaprod1.almoosahospital.com.sa:8000',
    'app_secret': '',
    'app_mode': '',
    'referrer': 'https://hisalmoosaprod1.almoosahospital.com.sa:8047/',

    # ── Reports ──────────────────────────────────────────────
    # The window the dashboard asks for, as a span rather than two fixed dates,
    # so it keeps covering today after every midnight. There is no local store:
    # widening this is what "look further back" means.
    'days_back': 1,
    'days_ahead': 0,
    # The portal sends the staff's local midnight written as UTC — a report for
    # the 14th goes out as "2026-09-13T21:00:00.000Z". Sending noon instead
    # would put rows in the wrong day for everyone on an early or late shift.
    'utc_offset_hours': 3,

    # Polling. A KPI board is read, not watched, so this is minutes rather than
    # the Pending dashboard's seconds — and a wide date range is a slow query.
    'poll_interval': 300,
    'verify_ssl': True,
    'timeout': 240,
}

CONFIG_KEYS = set(DEFAULT_CONFIG)


class HISError(Exception):
    """Any HIS API failure."""


class HISAuthError(HISError):
    """Token missing / rejected / expired — the operator has to log in again."""


# ─────────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────────
def decode_jwt(token):
    """Return the JWT payload as a dict (no signature check — we only read it)."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))
    except Exception:
        return {}


def _looks_like_jwt(value):
    return isinstance(value, str) and value.count('.') == 2 and value.startswith('ey') and len(value) > 60


def _find_token(obj, depth=0):
    """Depth-first search for a JWT anywhere in a decoded JSON response."""
    if depth > 6:
        return None
    if _looks_like_jwt(obj):
        return obj
    if isinstance(obj, dict):
        # Prefer obviously-named keys, then fall back to a full scan.
        for key in ('token', 'accessToken', 'access_token', 'jwtToken', 'authToken',
                    'bearerToken', 'Token', 'AccessToken'):
            if key in obj:
                found = _find_token(obj[key], depth + 1)
                if found:
                    return found
        for value in obj.values():
            found = _find_token(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_token(item, depth + 1)
            if found:
                return found
    return None


# ─────────────────────────────────────────────────────────────
# Values coming back from the HIS
# ─────────────────────────────────────────────────────────────
# The reports answer with '08-Sep-2026 12:53' — day, month name, no seconds —
# but a report captured on another day has been seen with seconds, and a few
# columns arrive as .NET epoch strings, so every known shape is accepted.
_IN_DATE_FORMATS = [
    '%d-%b-%Y %H:%M', '%d-%b-%Y %I:%M %p',
    '%d-%b-%Y %H:%M:%S', '%d-%b-%Y %I:%M:%S %p', '%d-%b-%Y',
    '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d',
    '%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %H:%M:%S',
    '%d/%m/%Y %I:%M:%S %p', '%d/%m/%Y %H:%M:%S',
]


def parse_datetime(value):
    """Return `value` as a datetime, or None when it is blank or unreadable."""
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text:
        return None

    # .NET style "/Date(1787723638000)/"
    dotnet = re.match(r'^/Date\((-?\d+)', text)
    if dotnet:
        try:
            return datetime.fromtimestamp(int(dotnet.group(1)) / 1000.0)
        except Exception:
            return None

    cleaned = text.replace('Z', '').split('+')[0].strip()
    for fmt in _IN_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _rows_look_like_report(rows):
    """True when a list of dicts carries a report's own column names."""
    if not rows or not isinstance(rows[0], dict):
        return False
    # Every report in this dashboard is a list of samples, so the sample number
    # is the one column all of them share.
    keys = {re.sub(r'[^A-Z0-9]', '', str(k).upper()) for k in rows[0]}
    return bool(keys & {'SAMPLENO', 'LISSAMPLENO', 'SLNO', 'MRNO'})


def extract_records(payload, depth=0):
    """
    Pull the result rows out of whatever shape GetReportData answers with.

    Returns None when nothing row-shaped is found, so that a report with no
    rows at all (an empty list) is not mistaken for a broken response.

    Handles the live shape {"data": {"reportData": [...]}} as well as a bare
    list, {"Table": [...]}, and rows arriving as an embedded JSON string.
    """
    if depth > 6:
        return None

    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith('[') or text.startswith('{'):
            try:
                return extract_records(json.loads(text), depth + 1)
            except ValueError:
                return None
        return None

    if isinstance(payload, list):
        if not payload:
            return None
        if _rows_look_like_report(payload) or isinstance(payload[0], (list, tuple)):
            return payload
        # A list of wrappers, e.g. [{"Table": [...]}]
        for item in payload:
            found = extract_records(item, depth + 1)
            if found is not None:
                return found
        return payload if isinstance(payload[0], dict) else None

    if isinstance(payload, dict):
        for key in ('reportData', 'ReportData', 'reportDataList', 'data', 'Data',
                    'table', 'Table', 'table1', 'Table1', 'result', 'Result',
                    'rows', 'Rows', 'items', 'dataSet', 'DataSet', 'dsReport'):
            if key not in payload:
                continue
            value = payload[key]
            # An empty list under a rows key is a real, empty report.
            if isinstance(value, list) and not value and key not in ('data', 'Data'):
                return []
            found = extract_records(value, depth + 1)
            if found is not None:
                return found
        for value in payload.values():
            if isinstance(value, (list, dict, str)):
                found = extract_records(value, depth + 1)
                if found is not None:
                    return found
    return None


def response_message(payload):
    """The API's own status text, when it carries one."""
    if not isinstance(payload, dict):
        return ''
    for key in ('message', 'Message', 'error', 'Error'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    inner = payload.get('data')
    if isinstance(inner, dict):
        validation = inner.get('validationResponse')
        if isinstance(validation, dict):
            value = validation.get('message')
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ''


# ─────────────────────────────────────────────────────────────
# Report definitions
# ─────────────────────────────────────────────────────────────
def reports_dir():
    """Folder holding the captured report bodies (works frozen too)."""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'reports')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')


# Keys this file adds to a captured body for its own use. They are notes to
# whoever reads the file and must never reach the HIS.
LOCAL_KEYS = ('_comment', '_meta')


def list_reports():
    """Every report definition on disk, as {name: meta}."""
    found = {}
    try:
        names = sorted(os.listdir(reports_dir()))
    except OSError:
        return found
    for filename in names:
        if not filename.endswith('.json'):
            continue
        name = filename[:-5]
        try:
            body = load_report(name)
        except HISError:
            continue
        meta = dict(body.get('_meta') or {})
        meta.setdefault('title', body.get('reportName') or name)
        meta.setdefault('key_column', 'SAMPLE_NO')
        meta['report_id'] = body.get('genReportListId')
        found[name] = meta
    return found


def load_report(name):
    """Read one captured report body. Raises HISError if it is missing."""
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', name or ''):
        raise HISError(f'{name!r} is not a report name.')
    path = os.path.join(reports_dir(), name + '.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise HISError(f'No report definition called {name!r} in reports/.')
    except ValueError as exc:
        raise HISError(f'reports/{name}.json is not valid JSON: {exc}')


class HISClient:
    def __init__(self, config=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.update_config(config)
        self.token = ''
        self.token_claims = {}
        self.expires_at = None
        self._username = ''
        self._password = ''
        self._site = ''
        self._signin_payload = ''   # encrypted body the portal posted
        self._templates = {}

    # ── config ────────────────────────────────────────────────
    # Blanking one of these would break every request, so an empty value is
    # ignored and the previous setting is kept.
    REQUIRED_TEXT = ('base_url', 'auth_url', 'report_path', 'login_path')

    def update_config(self, values):
        for key, value in (values or {}).items():
            if key not in CONFIG_KEYS or value is None:
                continue
            if key in self.REQUIRED_TEXT and not str(value).strip():
                continue
            self.config[key] = value
        for key in ('poll_interval', 'days_back', 'days_ahead', 'timeout',
                    'login_timeout', 'utc_offset_hours'):
            try:
                self.config[key] = int(self.config[key])
            except (TypeError, ValueError):
                self.config[key] = DEFAULT_CONFIG[key]
        self.config['poll_interval'] = max(30, self.config['poll_interval'])
        self.config['days_back'] = max(0, self.config['days_back'])
        self.config['days_ahead'] = max(0, self.config['days_ahead'])
        self.config['login_timeout'] = max(30, self.config['login_timeout'])
        self.config['timeout'] = max(30, self.config['timeout'])
        if not isinstance(self.config.get('known_sites'), list):
            self.config['known_sites'] = []
        # A blank site is not a choice — it is a settings file written before the
        # site was fixed. There is no need for one either: a site an account does
        # not have already falls back to the portal's own first site.
        if not str(self.config.get('login_site') or '').strip():
            self.config['login_site'] = DEFAULT_CONFIG['login_site']
        for key in ('base_url', 'auth_url'):
            self.config[key] = str(self.config[key]).rstrip('/')

    # ── token state ───────────────────────────────────────────
    @property
    def username(self):
        return self._username

    @property
    def has_credentials(self):
        """True when the token can be renewed without asking the operator."""
        return bool(self._signin_payload or (self._username and self._password))

    @property
    def has_signin_payload(self):
        return bool(self._signin_payload)

    def set_signin_payload(self, payload):
        """Store the encrypted sign-in body copied out of the browser.

        It carries the operator's password, so it is kept in memory only — the
        same lifetime as a typed password.
        """
        payload = (payload or '').strip()
        if not payload:
            raise HISAuthError('No sign-in payload given.')
        if payload.startswith('{'):
            raise HISAuthError(
                'That is plain JSON, not the encrypted body. Copy the value of '
                '"body" from the SignIn request (a long line ending in "==").'
            )
        if len(payload) < 32:
            raise HISAuthError('That sign-in body looks too short to be the real one.')
        self._signin_payload = payload

    def seconds_left(self):
        if not self.expires_at:
            return 0
        return int((self.expires_at - datetime.now()).total_seconds())

    def is_token_valid(self, margin=120):
        return bool(self.token) and self.seconds_left() > margin

    def set_token(self, token):
        token = (token or '').strip()
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        if not _looks_like_jwt(token):
            raise HISAuthError('That does not look like a HIS bearer token.')
        self.token = token
        self.token_claims = decode_jwt(token)
        exp = self.token_claims.get('exp')
        self.expires_at = datetime.fromtimestamp(exp) if exp else datetime.now() + timedelta(minutes=30)
        self._username = str(self.token_claims.get('UserName') or self.token_claims.get('sub') or '')
        self.adopt_token_metadata()
        return self.token_claims

    def adopt_token_metadata(self):
        """Learn X-App-* values from the token so future logins can send them."""
        claims = self.token_claims or {}
        for claim, key in (('X-App-Client', 'app_client'),
                           ('X-App-Secret', 'app_secret'),
                           ('X-App-Mode', 'app_mode')):
            value = claims.get(claim)
            if value:
                self.config[key] = value

    def logout(self):
        self.token = ''
        self.token_claims = {}
        self.expires_at = None
        self._username = ''
        self._password = ''
        self._site = ''
        self._signin_payload = ''

    # ── HTTP ──────────────────────────────────────────────────
    def _ssl_context(self):
        if self.config.get('verify_ssl', True):
            return ssl.create_default_context()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _request(self, path, payload, auth=False, timeout=None,
                 base=None, raw_body=None, content_type='application/json',
                 extra_headers=None):
        """POST to the HIS and return the decoded response (dict, list or text)."""
        url = path if path.startswith('http') else (base or self.config['base_url']) + path
        body = raw_body.encode('utf-8') if raw_body is not None else json.dumps(payload).encode('utf-8')

        request = urllib.request.Request(url, data=body, method='POST')
        request.add_header('Content-Type', content_type)
        request.add_header('Accept', 'application/json, text/plain, */*')
        request.add_header('Referer', self.config.get('referrer', ''))
        if self.config.get('app_client'):
            request.add_header('X-App-Client', self.config['app_client'])
        if self.config.get('app_secret'):
            request.add_header('X-App-Secret', self.config['app_secret'])
        if self.config.get('app_mode'):
            request.add_header('X-App-Mode', self.config['app_mode'])
        for name, value in (extra_headers or {}).items():
            if value:
                request.add_header(name, value)
        if auth:
            if not self.token:
                raise HISAuthError('Not logged in to the HIS.')
            request.add_header('Authorization', 'Bearer ' + self.token)

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout or self.config['timeout'],
                context=self._ssl_context(),
            ) as response:
                raw = response.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read().decode('utf-8', errors='replace')[:400]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise HISAuthError(f'HIS rejected the credentials (HTTP {exc.code}). {detail}'.strip())
            raise HISError(f'HIS returned HTTP {exc.code} for {url}. {detail}'.strip())
        except urllib.error.URLError as exc:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, ssl.SSLError) or 'CERTIFICATE' in str(reason).upper():
                raise HISError(
                    f'TLS error talking to {url}: {reason}. '
                    'If the HIS uses an internal certificate, turn off "Verify certificate".'
                )
            raise HISError(f'Cannot reach {url}: {reason}')

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    # ── login ─────────────────────────────────────────────────
    def _signin_headers(self):
        return {
            'machinename': self.config.get('machine_name', ''),
            'Referer': self.config.get('login_referrer') or self.config.get('referrer', ''),
        }

    def _post_signin(self, path, payload=None, raw_body=None, content_type='application/json'):
        """One sign-in attempt against the authentication host."""
        return self._request(
            path,
            payload,
            timeout=25,
            base=self.config.get('auth_url') or self.config['base_url'],
            raw_body=raw_body,
            content_type=content_type,
            extra_headers=self._signin_headers(),
        )

    def _adopt_login(self, token, path):
        self.set_token(token)
        self.config['login_path'] = path
        return {'endpoint': path, 'claims': self.token_claims}

    def login_with_payload(self, payload=None):
        """
        Sign in by re-posting the encrypted body captured from the browser.

        The portal encrypts the credentials client-side (X-App-Mode "ENCV0"), so
        this is the only way to renew a token unattended without the key that
        lives in its JavaScript.
        """
        if payload is not None:
            self.set_signin_payload(payload)
        if not self._signin_payload:
            raise HISAuthError('No sign-in body stored.')

        path = self.config.get('login_path') or DEFAULT_CONFIG['login_path']
        stale = 'The stored sign-in is no longer accepted.'
        try:
            response = self._post_signin(path, raw_body=self._signin_payload,
                                         content_type='text/plain')
        except HISAuthError as exc:
            raise HISAuthError(f'The HIS refused the stored sign-in. ({exc})')

        token = _find_token(response)
        if not token:
            message = response_message(response)
            raise HISAuthError(
                'The HIS answered the sign-in without a token. '
                + (f'It said: {message}. ' if message else '')
                + stale
            )
        result = self._adopt_login(token, path)
        self._username = str(self.token_claims.get('UserName')
                             or self.token_claims.get('sub') or self._username)
        return result

    def login(self, username, password, site=None):
        """
        Sign in with the user id and password from the HIS login page.

        The portal encrypts the password in the browser, so the sign-in is done
        by filling in its own login page in a browser with no window. The
        encrypted payload it posts is kept (in memory) so the token can be
        renewed later without opening a browser again.
        """
        username = (username or '').strip()
        if not username or not password:
            raise HISAuthError('User id and password are required.')

        # A site named in the call is one somebody asked for, so it has to exist.
        # The one in the settings is only a preference — the sign-in form does not
        # offer a site, so an account that does not have it takes whichever site
        # the portal offers first rather than failing on something the user
        # cannot see or change.
        site = (site or '').strip()
        required = bool(site)
        if not site:
            site = (self.config.get('login_site') or '').strip()

        try:
            result = browser_login.sign_in(
                self.config['portal_url'], username, password, site=site,
                site_required=required,
                browser_path=self.config.get('browser_path', ''),
                ignore_cert=not self.config.get('verify_ssl', True),
                timeout=self.config.get('login_timeout', 120),
            )
        except browser_login.LoginRejected as exc:
            raise HISAuthError(str(exc))
        except browser_login.BrowserError as exc:
            raise HISError(str(exc))

        self.set_token(result['token'])
        self._username = username or self._username
        self._password = password
        self._site = site
        if result.get('signin_body'):
            # Lets the next hour's renewal skip the browser entirely.
            self._signin_payload = result['signin_body']
        if result.get('sites'):
            self.config['known_sites'] = result['sites']
        self.config['login_site'] = site
        return {'endpoint': 'login page', 'browser': result.get('browser', ''),
                'sites': result.get('sites', []), 'claims': self.token_claims}

    def list_sites(self, username=''):
        """Read the Site dropdown off the login page (for the sign-in window)."""
        try:
            sites = browser_login.list_sites(
                self.config['portal_url'], username=username,
                browser_path=self.config.get('browser_path', ''),
                ignore_cert=not self.config.get('verify_ssl', True),
                timeout=self.config.get('login_timeout', 120),
            )
        except browser_login.BrowserError as exc:
            raise HISError(str(exc))
        if sites:
            self.config['known_sites'] = sites
        return sites

    def _relogin(self):
        """Get a fresh token: replay the captured sign-in, else log in again."""
        if self._signin_payload:
            try:
                return self.login_with_payload()
            except (HISError, HISAuthError):
                # The payload goes stale on a password change, and some servers
                # refuse a replay outright — fall back to the login page.
                if not (self._username and self._password):
                    raise
                self._signin_payload = ''
        if self._username and self._password:
            return self.login(self._username, self._password, self._site)
        raise HISAuthError('HIS session expired — please sign in again.')

    def ensure_token(self):
        """Renew the token from stored credentials when it is about to expire."""
        if self.is_token_valid():
            return
        self._relogin()

    # ── reports ───────────────────────────────────────────────
    def _template(self, name):
        if name not in self._templates:
            self._templates[name] = load_report(name)
        return json.loads(json.dumps(self._templates[name]))  # deep copy per request

    def date_span(self, date_from=None, date_to=None):
        """The dates to ask for, as 'YYYY-MM-DD', rolling with the clock."""
        today = datetime.now().date()
        if not date_to:
            date_to = (today + timedelta(days=self.config['days_ahead'])).strftime('%Y-%m-%d')
        if not date_from:
            date_from = (today - timedelta(days=self.config['days_back'])).strftime('%Y-%m-%d')
        return date_from, date_to

    def _stamp(self, date_text):
        """Local midnight of `date_text`, written the way the portal writes it.

        The portal posts a report for the 14th as '2026-09-13T21:00:00.000Z' —
        midnight in +03. Sending noon UTC instead (which is what the Pending
        dashboard does, because that report truncates to the day) would land
        rows either side of the boundary in the wrong day here.
        """
        try:
            day = datetime.strptime(str(date_text)[:10], '%Y-%m-%d')
        except ValueError:
            raise HISError(f'{date_text!r} is not a date the report can use.')
        moment = day - timedelta(hours=self.config.get('utc_offset_hours', 3))
        return moment.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    def build_body(self, name, date_from, date_to, filters=None):
        """Fill a captured report body in with the dates and the signed-in user."""
        body = self._template(name)
        meta = body.get('_meta') or {}
        for key in LOCAL_KEYS:
            body.pop(key, None)

        date_params = meta.get('date_filters') or {'from': 'FROM_DATE', 'to': 'TO_DATE'}
        values = {
            date_params.get('from', 'FROM_DATE'): self._stamp(date_from),
            date_params.get('to', 'TO_DATE'): self._stamp(date_to),
        }
        # Anything else the caller wants pinned, by the report's own parameter
        # name. A value of None means "all of them", which these reports spell
        # as a null storedValue with the text "All".
        values.update(filters or {})

        for param in body.get('getReportFilters', []):
            name_ = param.get('spParamsName')
            if name_ not in values:
                continue
            value = values[name_]
            if value is None:
                param['storedValue'] = None
                param['storedValueText'] = 'All'
            else:
                param['storedValue'] = value
                param['storedValueText'] = str(value)

        claims = self.token_claims or {}
        body['loginUserId'] = str(claims.get('UserName') or claims.get('sub')
                                  or self._username or '')
        return body

    def fetch_report(self, name, date_from=None, date_to=None, filters=None):
        """Fetch one report and return its rows as dicts, column names untouched."""
        self.ensure_token()
        date_from, date_to = self.date_span(date_from, date_to)

        def post():
            return self._request(self.config['report_path'],
                                 self.build_body(name, date_from, date_to, filters),
                                 auth=True)

        try:
            response = post()
        except HISAuthError:
            # A token can expire between the check above and the call itself.
            if not self.has_credentials:
                raise
            self._relogin()
            response = post()

        records = extract_records(response)
        if records is None:
            message = response_message(response)
            raise HISError(f'The {name} report returned no readable rows'
                           + (f' — it said: {message}' if message else
                              ' (the response format may have changed)') + '.')
        return [row for row in records if isinstance(row, dict)]
