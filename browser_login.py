"""
Sign in to the HIS portal through a hidden browser.

The portal scrambles the password inside the browser before posting it to
/api/v1/Authentication/SignIn (that is what the "ENCV0" mode in the token
means), and the key lives in its own JavaScript. A server therefore cannot
build a sign-in request from a user id and a password by itself.

So we let the portal do it: a browser is started with no window, the real
login page is opened, the user id / password / site are typed into the real
form, and the token the portal hands back is taken out of the page.

The browser is the one already installed on the PC (Edge or Chrome — Edge
ships with Windows), driven over its DevTools protocol with nothing but the
standard library, so there is nothing extra to install and the PyInstaller
build is unaffected.
"""

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

# The login form, straight from the portal's page.
SEL_USER = ('input[formcontrolname="username"]', 'input[placeholder="User Id"]',
            'input[type="text"]')
SEL_PASS = ('input[formcontrolname="password"]', '#passFocus', 'input[type="password"]')
SEL_SITE = ('mat-select[formcontrolname="site"]', '#focusablesite', 'mat-select')
SEL_SUBMIT = ('#submitFocus', 'button.custom-button.pmry')
# Where the portal shows "wrong password", "no privilege to sites", …
SEL_ERROR = ('.alert-danger:not(.d-none)', '.swal2-html-container', '.swal2-title',
             '.mat-snack-bar-container', 'mat-error')

SIGNIN_MARK = '/Authentication/SignIn'

# Some users are set up for a one-time code; nobody is standing at this screen
# to type one, so say so plainly instead of waiting for the timeout.
OTP_JS = "!!document.querySelector('ng-otp-input, .otp-input, input[name=otp]')" 


class BrowserError(Exception):
    """The hidden browser could not be started or driven."""


class LoginRejected(Exception):
    """The portal itself said no (bad user id / password / site)."""


# ─────────────────────────────────────────────────────────────
# Finding a browser
# ─────────────────────────────────────────────────────────────
def _candidates():
    system = platform.system()
    if system == 'Windows':
        roots = [os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                 os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'),
                 os.environ.get('LOCALAPPDATA', '')]
        tails = [r'Microsoft\Edge\Application\msedge.exe',
                 r'Google\Chrome\Application\chrome.exe',
                 r'Chromium\Application\chrome.exe']
        return [os.path.join(root, tail) for root in roots if root for tail in tails]
    if system == 'Darwin':
        return ['/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                '/Applications/Chromium.app/Contents/MacOS/Chromium']
    names = ['microsoft-edge', 'microsoft-edge-stable', 'google-chrome',
             'google-chrome-stable', 'chromium', 'chromium-browser']
    found = [shutil.which(name) for name in names]
    return [path for path in found if path] + ['/opt/pw-browsers/chromium/chrome-linux/chrome']


def find_browser(configured=''):
    """Path to Edge / Chrome / Chromium, or '' when the PC has none."""
    for path in [configured, os.environ.get('HIS_BROWSER', '')]:
        if path and os.path.exists(path):
            return path
    for path in _candidates():
        if path and os.path.exists(path):
            return path
    return ''


# ─────────────────────────────────────────────────────────────
# A very small WebSocket client (DevTools speaks nothing else)
# ─────────────────────────────────────────────────────────────
class _WebSocket:
    def __init__(self, url, timeout=30):
        match = re.match(r'ws://([^:/]+):(\d+)(/.*)$', url)
        if not match:
            raise BrowserError(f'Unusable DevTools address: {url}')
        host, port, path = match.group(1), int(match.group(2)), match.group(3)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f'GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
        )
        self.sock.sendall(handshake.encode())

        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        header = b''
        while b'\r\n\r\n' not in header:
            chunk = self.sock.recv(1)
            if not chunk:
                raise BrowserError('The browser closed the DevTools connection.')
            header += chunk
        if expected.lower() not in header.decode('latin-1').lower():
            raise BrowserError('The browser refused the DevTools handshake.')
        self._buffer = b''

    # -- framing --------------------------------------------------
    def send(self, text):
        payload = text.encode('utf-8')
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += length.to_bytes(2, 'big')
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, 'big')
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, count):
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BrowserError('The browser closed the DevTools connection.')
            self._buffer += chunk
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head

    def recv(self):
        """Next text message, reassembling continuation frames."""
        message = b''
        while True:
            first, second = self._read(2)
            final, opcode = first & 0x80, first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._read(2), 'big')
            elif length == 127:
                length = int.from_bytes(self._read(8), 'big')
            payload = self._read(length) if length else b''

            if opcode == 0x8:                       # close
                raise BrowserError('The browser closed the DevTools connection.')
            if opcode == 0x9:                       # ping -> pong
                self.sock.sendall(b'\x8a\x80' + os.urandom(4))
                continue
            if opcode == 0xA:                       # pong
                continue
            message += payload
            if final:
                return message.decode('utf-8', errors='replace')

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# The hidden browser
# ─────────────────────────────────────────────────────────────
class HiddenBrowser:
    def __init__(self, executable, headless=True, ignore_cert=False, timeout=60):
        self.executable = executable
        self.timeout = timeout
        self.ignore_cert = ignore_cert
        self.profile = tempfile.mkdtemp(prefix='his-login-')
        self.process = None
        self._id = 0
        self.events = []

        # "--headless=new" is the modern flag; older Edge/Chrome builds only
        # understand the original one and quit on sight of the new spelling.
        modes = ['--headless=new', '--headless'] if headless else ['']
        failure = None
        for mode in modes:
            try:
                self._spawn(mode)
                self.ws = _WebSocket(self._page_socket(self.port), timeout=max(10, timeout))
                break
            except BrowserError as exc:
                failure = exc
                self._stop_process()
        else:
            shutil.rmtree(self.profile, ignore_errors=True)
            raise failure

        for domain in ('Page', 'Runtime', 'Network'):
            self.call(f'{domain}.enable')

    def _spawn(self, headless_flag):
        self.port = self._free_port()
        self.deadline = time.time() + self.timeout
        args = [
            self.executable,
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile}',
            '--no-first-run', '--no-default-browser-check', '--disable-extensions',
            '--disable-background-networking', '--disable-sync', '--no-sandbox',
            '--disable-dev-shm-usage', '--disable-gpu', '--window-size=1280,900',
            'about:blank',
        ]
        if headless_flag:
            args.insert(1, headless_flag)
        if self.ignore_cert:
            args.insert(1, '--ignore-certificate-errors')

        try:
            self.process = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except OSError as exc:
            raise BrowserError(f'Could not start {self.executable}: {exc}')

    def _stop_process(self):
        if not self.process:
            return
        for stop in (self.process.terminate, self.process.kill):
            try:
                stop()
                self.process.wait(timeout=5)
                return
            except Exception:
                continue

    # -- plumbing -------------------------------------------------
    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def _page_socket(self, port):
        """Wait for the browser to come up and return its page's DevTools URL."""
        last = ''
        while time.time() < self.deadline:
            if self.process.poll() is not None:
                raise BrowserError('The browser exited immediately after starting.')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=2) as reply:
                    targets = json.loads(reply.read().decode('utf-8'))
                for target in targets:
                    if target.get('type') == 'page' and target.get('webSocketDebuggerUrl'):
                        return target['webSocketDebuggerUrl']
            except Exception as exc:      # not listening yet
                last = str(exc)
            time.sleep(0.2)
        raise BrowserError(f'The browser did not open its DevTools port. {last}'.strip())

    def call(self, method, params=None, timeout=None):
        self._id += 1
        message_id = self._id
        self.ws.send(json.dumps({'id': message_id, 'method': method, 'params': params or {}}))
        limit = time.time() + (timeout or 30)
        while time.time() < limit:
            message = json.loads(self.ws.recv())
            if message.get('id') != message_id:
                if 'method' in message:
                    self.events.append(message)
                continue
            if 'error' in message:
                raise BrowserError(f"{method} failed: {message['error'].get('message')}")
            return message.get('result', {})
        raise BrowserError(f'The browser did not answer {method} in time.')

    def drain(self, seconds=0.0):
        """Collect any events the page has sent (optionally waiting a little)."""
        limit = time.time() + seconds
        while True:
            self.ws.sock.settimeout(max(0.05, limit - time.time()) if seconds else 0.05)
            try:
                message = json.loads(self.ws.recv())
            except (socket.timeout, OSError):
                break
            except BrowserError:
                break
            if 'method' in message:
                self.events.append(message)
            if time.time() >= limit:
                break
        self.ws.sock.settimeout(self.timeout)

    # -- page helpers --------------------------------------------
    def evaluate(self, expression, timeout=None):
        result = self.call('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
        }, timeout=timeout)
        details = result.get('exceptionDetails')
        if details:
            raise BrowserError('The login page raised: '
                               + str(details.get('exception', {}).get('description', details)))
        return result.get('result', {}).get('value')

    def wait_for(self, expression, seconds, poll=0.25):
        """Poll a JavaScript expression until it is truthy; returns its value."""
        limit = min(time.time() + seconds, self.deadline)
        while time.time() < limit:
            value = self.evaluate(expression)
            if value:
                return value
            self.drain()
            time.sleep(poll)
        return None

    def type_into(self, selectors, text):
        found = self.evaluate(_focus_js(selectors))
        if not found:
            return False
        self.call('Input.insertText', {'text': text})
        # Angular listens for input/change; insertText fires input already.
        self.evaluate(_dispatch_js(selectors))
        return True

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self._stop_process()
        shutil.rmtree(self.profile, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# JavaScript snippets
# ─────────────────────────────────────────────────────────────
def _pick_js(selectors):
    """Expression returning the first element matching any of the selectors."""
    return '[' + ','.join(json.dumps(s) for s in selectors) + ']' \
           '.map(s=>document.querySelector(s)).find(e=>e)'


def _focus_js(selectors):
    return f'(()=>{{const e={_pick_js(selectors)}; if(!e) return false;' \
           'e.focus(); e.select && e.select(); return true;})()'


def _dispatch_js(selectors):
    return f'(()=>{{const e={_pick_js(selectors)}; if(!e) return false;' \
           "e.dispatchEvent(new Event('input',{bubbles:true}));" \
           "e.dispatchEvent(new Event('change',{bubbles:true}));" \
           "e.dispatchEvent(new Event('blur',{bubbles:true})); return true;})()"


def _click_js(selectors):
    return f'(()=>{{const e={_pick_js(selectors)}; if(!e) return false;' \
           'e.click(); return true;})()'


OPTIONS_JS = ("(()=>Array.from(document.querySelectorAll('.cdk-overlay-container mat-option,"
              ".cdk-overlay-container .mat-option')).map(o=>o.textContent.trim()).filter(t=>t))()")

ERROR_JS = ('(()=>{const s=' + json.dumps(list(SEL_ERROR)) + ';'
            'for(const sel of s){for(const e of document.querySelectorAll(sel)){'
            'const t=(e.textContent||"").trim();'
            'if(t && e.offsetParent!==null) return t.slice(0,300);}}return "";})()')

# Everything the app might have stashed the token in.
TOKEN_JS = """
(()=>{const out=[];
 for (const store of [window.localStorage, window.sessionStorage]) {
   try { for (let i=0;i<store.length;i++) out.push(store.getItem(store.key(i))); }
   catch(e) {}
 }
 out.push(document.cookie||'');
 const hit=[];
 for (const value of out) {
   if (typeof value !== 'string') continue;
   const m = value.match(/eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{4,}/);
   if (m) hit.push(m[0]);
 }
 return hit[0] || '';})()
"""


# ─────────────────────────────────────────────────────────────
# The login itself
# ─────────────────────────────────────────────────────────────
def _open_login_page(browser, portal_url, seconds=40):
    browser.call('Page.navigate', {'url': portal_url}, timeout=max(15, seconds))
    if not browser.wait_for(f'!!({_pick_js(SEL_USER)})', seconds):
        message = browser.evaluate(ERROR_JS) or ''
        raise BrowserError('The HIS login page did not load. '
                           + (f'It said: {message}' if message else
                              'Check the portal address and that this PC can reach it.'))


def _choose_site(browser, wanted='', required=True):
    """Open the Site dropdown, pick `wanted` (or leave the default), list the options.

    With `required` off, a site this account does not have is not an error: the
    portal's own first site is taken instead. That is what the fixed site in the
    settings wants — the sign-in form does not offer a site, so a mismatch there
    is nothing the person signing in could act on.
    """
    if not browser.evaluate(f'!!({_pick_js(SEL_SITE)})'):
        return []
    if not browser.evaluate(_click_js(SEL_SITE)):
        return []
    options = browser.wait_for(f'(()=>{{const o={OPTIONS_JS}; return o.length?o:null;}})()', 8) or []
    if not options:
        return []

    target = ''
    if wanted:
        low = str(wanted).strip().lower()
        matches = [o for o in options if o.lower() == low] or \
                  [o for o in options if low in o.lower()]
        if not matches and required:
            browser.evaluate("document.body.click()")
            raise LoginRejected(
                f'"{wanted}" is not one of this user\'s sites. Available: '
                + ', '.join(options) + '.')
        target = matches[0] if matches else options[0]
    else:
        target = options[0]

    browser.evaluate(
        '(()=>{const t=%s;for(const o of document.querySelectorAll('
        "'.cdk-overlay-container mat-option, .cdk-overlay-container .mat-option')){"
        'if((o.textContent||"").trim()===t){o.click();return true;}}return false;})()'
        % json.dumps(target))
    browser.wait_for('!document.querySelector(".cdk-overlay-container mat-option")', 5)
    return options


def _scan_signin(browser, deadline):
    """Look through DevTools traffic for the SignIn request body and its reply."""
    request_id, post_data, token = '', '', ''
    for event in list(browser.events):
        method, params = event.get('method'), event.get('params', {})
        if method == 'Network.requestWillBeSent':
            url = params.get('request', {}).get('url', '')
            if SIGNIN_MARK in url:
                request_id = params.get('requestId', '')
                post_data = params.get('request', {}).get('postData', '') or post_data
        elif method == 'Network.loadingFinished' and request_id and \
                params.get('requestId') == request_id and time.time() < deadline:
            try:
                body = browser.call('Network.getResponseBody', {'requestId': request_id}, timeout=10)
            except BrowserError:
                continue
            text = body.get('body', '')
            if body.get('base64Encoded'):
                try:
                    text = base64.b64decode(text).decode('utf-8', errors='replace')
                except Exception:
                    text = ''
            found = re.search(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{4,}', text)
            if found:
                token = found.group(0)
    return token, post_data


def sign_in(portal_url, username, password, site='', site_required=True,
            browser_path='', headless=True, ignore_cert=False, timeout=120):
    """
    Log in to the HIS portal in a hidden browser and bring back its token.

    Returns {'token', 'signin_body', 'sites', 'browser'}. `signin_body` is the
    encrypted payload the portal posted; re-posting it renews the token later
    without starting a browser again.
    """
    executable = find_browser(browser_path)
    if not executable:
        raise BrowserError(
            'No browser found on this PC to sign in with. Microsoft Edge or Google '
            'Chrome has to be installed, or its path set in Advanced settings.')

    browser = HiddenBrowser(executable, headless=headless,
                            ignore_cert=ignore_cert, timeout=timeout)
    try:
        _open_login_page(browser, portal_url)

        if not browser.type_into(SEL_USER, str(username)):
            raise BrowserError('Could not find the User Id box on the login page.')
        if not browser.type_into(SEL_PASS, str(password)):
            raise BrowserError('Could not find the Password box on the login page.')
        sites = _choose_site(browser, site, required=site_required)

        problem = browser.evaluate(ERROR_JS)
        if not browser.evaluate(_click_js(SEL_SUBMIT)):
            raise BrowserError('Could not find the Login button on the login page.')

        deadline = min(time.time() + timeout, browser.deadline)
        token, body = '', ''
        while time.time() < deadline:
            browser.drain(0.4)
            token, body = _scan_signin(browser, deadline)
            if not token:
                token = browser.evaluate(TOKEN_JS) or ''
            if token:
                return {'token': token, 'signin_body': body, 'sites': sites,
                        'browser': os.path.basename(executable)}
            message = browser.evaluate(ERROR_JS)
            if message and message != problem:
                raise LoginRejected(message)
            if browser.evaluate(OTP_JS):
                raise LoginRejected(
                    'This HIS user is asked for a one-time code, which the dashboard '
                    'cannot answer. Use a HIS account without two-step sign-in.')
        raise BrowserError(
            'Signed in but the HIS did not hand over a token in time. '
            'If the HIS is slow just now, try again.')
    finally:
        browser.close()


def list_sites(portal_url, username='', browser_path='', headless=True,
               ignore_cert=False, timeout=90):
    """Open the login page and read the Site dropdown, without signing in."""
    executable = find_browser(browser_path)
    if not executable:
        return []
    browser = HiddenBrowser(executable, headless=headless,
                            ignore_cert=ignore_cert, timeout=timeout)
    try:
        _open_login_page(browser, portal_url)
        if username:
            browser.type_into(SEL_USER, str(username))
        try:
            return _choose_site(browser, '')
        except LoginRejected:
            return []
    finally:
        browser.close()
