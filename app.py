"""
Lab KPIs — a graph dashboard over the HIS lab reports.

One main report (samples received) is fetched over a date span the viewer
chooses, and any other report definitions in reports/ are joined onto it by
sample number. Everything is worked out from the rows as they arrive: there is
no local database, so "look further back" means asking the HIS for a wider
span, and the numbers are always the HIS's own.

Signing in is the Pending dashboard's proven path, unchanged: a browser with no
window fills in the portal's real login page, and the encrypted sign-in body it
posts is kept in memory so the hour-old token renews itself in silence.

Unlike that dashboard this one has no websocket. A board that refreshes every
few minutes does not need one, and doing without means the page pulls no
script off the internet — which a PC inside the hospital network cannot always
reach. The page asks for its numbers over plain HTTP instead, and a heartbeat
from it is what tells the server the window is still open.
"""

import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

import browser_login
import his_client
from his_client import HISAuthError, HISClient, HISError, parse_datetime


# ─────────────────────────────────────────────
# WHERE THINGS LIVE
# A frozen build unpacks its code to a temporary folder, so settings go next to
# the executable instead — they have to survive the program closing.
# ─────────────────────────────────────────────
def _base_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    if getattr(sys, 'frozen', False):
        folder = os.path.join(os.path.dirname(sys.executable), 'data')
    else:
        folder = os.path.join(_base_dir(), 'data')
    os.makedirs(folder, exist_ok=True)
    return folder


BASE_DIR = _base_dir()
DATA_DIR = _data_dir()
HIS_CONFIG_FILE = os.path.join(DATA_DIR, 'his_config.json')
VIEW_FILE = os.path.join(DATA_DIR, 'view.json')

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)
app.config['SECRET_KEY'] = 'lab-kpis'


def background(target, *args):
    """Run something off the request thread and forget about it."""
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


# ─────────────────────────────────────────────
# THE REPORT COLUMNS
# Named once here so a HIS rename is a one-line change. The main report spells
# two of them without underscores ("RECEVID BY NAME" — the HIS's own spelling,
# typo included), which is why every lookup goes through column().
# ─────────────────────────────────────────────
COL_SAMPLE = 'SAMPLE_NO'
COL_MRN = 'MRNO'
COL_LOCATION = 'PATIENT_LOCATION'
COL_CATEGORY = 'INV_CATEGORY_NAME'
COL_TEST = 'TEST_NAME'
COL_RECEIVED_BY = 'RECEVID BY NAME'
COL_DEPARTMENT = 'DEPARTMENT_NAME'
COL_ENTRY = 'ENTRY DATE'
COL_COLLECTED = 'COLLECTION_DATE'
COL_ACTION = 'ACTION'
COL_ACCEPTED = 'SAMPLE_ACCEPTANCE_DATE'
COL_ACCEPTED_BY = 'SAMPLE_ACCEPTANCE_BY'
COL_ACCEPT_DEPT = 'DEPARTMENT_SAMPLE_ACCEPTANCE_BY'
COL_SITE = 'SITE_NAME'


def column(row, name, default=''):
    """Read a column, forgiving the HIS's spacing and underscores."""
    if name in row:
        value = row[name]
        return default if value is None else value
    wanted = name.replace('_', ' ').strip().upper()
    for key, value in row.items():
        if str(key).replace('_', ' ').strip().upper() == wanted:
            return default if value is None else value
    return default


def text(row, name):
    value = column(row, name, '')
    return str(value).strip()


# ─────────────────────────────────────────────
# THE FETCHED ROWS
# One span's worth of the HIS, held in memory. Refetched when the viewer moves
# the dates and on every poll; never written to disk, because it is patient data
# and because the HIS is the only copy worth trusting.
# ─────────────────────────────────────────────
store = {
    'rows': [],
    'from': None,
    'to': None,
    'fetched_at': None,
    'linked': {},      # report name → how many of its rows found a sample
}

# What the viewer is looking at. A span in days rather than two dates, so the
# board still covers today after midnight.
#
# The span the board opens on belongs to the HIS settings — days_back there is
# the one place to change it — and what the viewer picks with the range buttons
# then overrides it for this PC.
view = {
    'days_back': his_client.DEFAULT_CONFIG['days_back'],
    'days_ahead': his_client.DEFAULT_CONFIG['days_ahead'],
    'site': '',        # '' means every site
    'department': '',
}


def load_view():
    config = his['client'].config
    view['days_back'] = config['days_back']
    view['days_ahead'] = config['days_ahead']

    if os.path.exists(VIEW_FILE):
        try:
            with open(VIEW_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
        except Exception as e:
            print(f"[!] Could not read {VIEW_FILE}: {e}")
            saved = {}
        for key in view:
            if key in saved:
                view[key] = saved[key]
    clean_view()


def save_view():
    try:
        with open(VIEW_FILE, 'w', encoding='utf-8') as f:
            json.dump(view, f, indent=2)
    except Exception as e:
        print(f"[!] Could not write {VIEW_FILE}: {e}")


def clean_view():
    config = his['client'].config
    for key in ('days_back', 'days_ahead'):
        try:
            view[key] = int(view[key])
        except (TypeError, ValueError):
            view[key] = config[key]
    # A year is already a slow query; beyond that the report times out and the
    # dashboard looks broken rather than busy.
    view['days_back'] = max(0, min(view['days_back'], 366))
    view['days_ahead'] = max(0, min(view['days_ahead'], 7))
    view['site'] = str(view.get('site') or '')
    view['department'] = str(view.get('department') or '')


def visible_rows():
    """The fetched rows the viewer has asked to see."""
    rows = store['rows']
    if view['site']:
        rows = [r for r in rows if text(r, COL_SITE) == view['site']]
    if view['department']:
        rows = [r for r in rows if text(r, COL_DEPARTMENT) == view['department']]
    return rows


# ─────────────────────────────────────────────
# KPIs
# ─────────────────────────────────────────────
# How long a sample waited between being taken and being accepted. The buckets
# are the ones a lab argues about: within the hour, within the shift, same day,
# and then the ones somebody has to explain.
DELAY_BUCKETS = [
    ('Under 1h', 0, 60),
    ('1–4h', 60, 240),
    ('4–24h', 240, 1440),
    ('1–3 days', 1440, 4320),
    ('Over 3 days', 4320, None),
]


def minutes_between(row, start_col, end_col):
    """Minutes from one timestamp column to another, or None if either is blank."""
    start = parse_datetime(column(row, start_col))
    end = parse_datetime(column(row, end_col))
    if not start or not end:
        return None
    delta = (end - start).total_seconds() / 60.0
    # A negative gap is a clock or a data problem, not a fast lab.
    return delta if delta >= 0 else None


def bucket_for(minutes):
    for label, low, high in DELAY_BUCKETS:
        if minutes >= low and (high is None or minutes < high):
            return label
    return DELAY_BUCKETS[-1][0]


def summarise(values):
    """Median and 90th percentile of a list of numbers, rounded to the minute."""
    if not values:
        return {'median': None, 'p90': None, 'count': 0}
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return {
        'median': round(statistics.median(ordered)),
        'p90': round(ordered[index]),
        'count': len(ordered),
    }


def counts_by(rows, col, limit=None):
    """{label, value}, biggest first, with a blank column read as 'Not stated'."""
    tally = Counter(text(r, col) or 'Not stated' for r in rows)
    return [{'label': label, 'value': value} for label, value in tally.most_common(limit)]


def build_kpis():
    """Everything the page draws, worked out from the rows now in memory."""
    rows = visible_rows()

    delays = []            # collection → acceptance, in minutes
    entry_delays = []      # entry (order) → acceptance
    per_day = defaultdict(int)
    per_hour = Counter()
    buckets = Counter()
    slowest = []

    for row in rows:
        accepted = parse_datetime(column(row, COL_ACCEPTED))
        if accepted:
            per_day[accepted.date().isoformat()] += 1
            per_hour[accepted.hour] += 1

        minutes = minutes_between(row, COL_COLLECTED, COL_ACCEPTED)
        if minutes is not None:
            delays.append(minutes)
            buckets[bucket_for(minutes)] += 1
            slowest.append((minutes, row))

        entry_minutes = minutes_between(row, COL_ENTRY, COL_ACCEPTED)
        if entry_minutes is not None:
            entry_delays.append(entry_minutes)

    slowest.sort(key=lambda pair: pair[0], reverse=True)

    # An empty day is part of the picture — a lab that received nothing on a
    # Friday should show a gap, not skip to the next day it worked.
    days = []
    if store['from'] and store['to']:
        day = datetime.strptime(store['from'], '%Y-%m-%d').date()
        last = datetime.strptime(store['to'], '%Y-%m-%d').date()
        while day <= last:
            key = day.isoformat()
            days.append({'label': key, 'value': per_day.get(key, 0)})
            day += timedelta(days=1)
    else:
        days = [{'label': k, 'value': v} for k, v in sorted(per_day.items())]

    return {
        'totals': {
            'samples': len(rows),
            'patients': len({text(r, COL_MRN) for r in rows if text(r, COL_MRN)}),
            'tests': len({text(r, COL_TEST) for r in rows if text(r, COL_TEST)}),
            'sites': len({text(r, COL_SITE) for r in rows if text(r, COL_SITE)}),
        },
        'delay': summarise(delays),
        'entry_delay': summarise(entry_delays),
        'per_day': days,
        'per_hour': [{'label': f'{h:02d}', 'value': per_hour.get(h, 0)} for h in range(24)],
        'buckets': [{'label': label, 'value': buckets.get(label, 0)}
                    for label, _, _ in DELAY_BUCKETS],
        'by_department': counts_by(rows, COL_DEPARTMENT),
        'by_site': counts_by(rows, COL_SITE),
        'by_category': counts_by(rows, COL_CATEGORY, 12),
        'by_location': counts_by(rows, COL_LOCATION, 8),
        'by_acceptor': counts_by(rows, COL_ACCEPTED_BY, 10),
        'by_receiver': counts_by(rows, COL_RECEIVED_BY, 10),
        'slowest': [{
            'sample': text(r, COL_SAMPLE),
            'category': text(r, COL_CATEGORY),
            'test': text(r, COL_TEST),
            'department': text(r, COL_DEPARTMENT),
            'site': text(r, COL_SITE),
            'collected': text(r, COL_COLLECTED),
            'accepted': text(r, COL_ACCEPTED),
            'minutes': round(minutes),
        } for minutes, r in slowest[:15]],
    }


def choices():
    """The sites and departments the fetched rows actually contain.

    Taken from the data rather than from the report's own dropdown lists: the
    HIS renames departments and a list nobody maintains ends up offering ones
    that no longer exist.
    """
    return {
        'sites': sorted({text(r, COL_SITE) for r in store['rows'] if text(r, COL_SITE)}),
        'departments': sorted({text(r, COL_DEPARTMENT) for r in store['rows']
                               if text(r, COL_DEPARTMENT)}),
    }


def dashboard_payload():
    return {
        'kpis': build_kpis(),
        'choices': choices(),
        'view': dict(view),
        'range': {'from': store['from'], 'to': store['to']},
        'fetched_at': store['fetched_at'],
        'linked': store['linked'],
    }


# ─────────────────────────────────────────────
# HIS WEBSITE DATA SOURCE
# The operator signs in once with their own HIS account; the server then polls
# the hospital report API on their behalf. Credentials — a typed password or the
# encrypted sign-in body copied from the browser — live in memory only and are
# never written to disk, so a server restart requires a fresh sign-in.
# ─────────────────────────────────────────────
MAIN_REPORT = 'samples_received'

his = {
    'client': HISClient(),
    'logged_in': False,
    'polling': False,
    'fetching': False,
    'error': None,
    'last_fetch': None,
    'last_rows': 0,
    'last_error_at': None,
    'login_endpoint': None,
}

# One fetch at a time. The poll loop, the Refresh button and a widened date
# span can all ask at once, and the HIS is slow enough for that to overlap.
fetch_lock = threading.Lock()


def load_his_config():
    if os.path.exists(HIS_CONFIG_FILE):
        try:
            with open(HIS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                his['client'].update_config(json.load(f))
        except Exception as e:
            print(f"[!] Could not read {HIS_CONFIG_FILE}: {e}")


def save_his_config():
    """Persist connection settings. Credentials are deliberately excluded."""
    try:
        with open(HIS_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(his['client'].config, f, indent=2)
    except Exception as e:
        print(f"[!] Could not write {HIS_CONFIG_FILE}: {e}")


def his_status_payload():
    client = his['client']
    config = dict(client.config)
    if config.get('app_secret'):
        config['app_secret'] = '••••••'
    return {
        'logged_in': his['logged_in'],
        'polling': his['polling'],
        'fetching': his['fetching'],
        # The user id stays on the server: the dashboard runs on a screen other
        # people walk past, so it says "connected", not who is connected.
        'can_renew': client.has_credentials,
        'expires_in': client.seconds_left() if his['logged_in'] else 0,
        'last_fetch': his['last_fetch'],
        'last_rows': his['last_rows'],
        'error': his['error'],
        'login_endpoint': his['login_endpoint'],
        'reports': his_client.list_reports(),
        'config': config,
    }


def join_linked_reports(rows, date_from, date_to):
    """Merge every other report in reports/ onto the main rows by sample number.

    A linked report adds its own columns to the sample it belongs to, prefixed
    with its name so two reports carrying a DEPARTMENT_NAME cannot overwrite
    each other. A sample a linked report does not mention is left as it is.
    """
    joined = {}
    index = defaultdict(list)
    for row in rows:
        key = text(row, COL_SAMPLE)
        if key:
            index[key].append(row)

    for name, meta in his_client.list_reports().items():
        if name == MAIN_REPORT:
            continue
        key_column = meta.get('key_column') or COL_SAMPLE
        try:
            extra = his['client'].fetch_report(name, date_from, date_to)
        except HISError as e:
            # One report being unavailable should not cost the whole board.
            print(f"[!] Linked report {name} failed: {e}")
            joined[name] = {'rows': 0, 'matched': 0, 'error': str(e)}
            continue

        matched = 0
        for extra_row in extra:
            key = str(column(extra_row, key_column, '')).strip()
            targets = index.get(key)
            if not targets:
                continue
            matched += 1
            for target in targets:
                for field, value in extra_row.items():
                    if field == key_column:
                        continue
                    target[f'{name}.{field}'] = value
        joined[name] = {'rows': len(extra), 'matched': matched, 'error': None}
        print(f"[*] Linked {name}: {len(extra)} rows, {matched} matched a sample")

    return joined


def his_fetch_once():
    """Pull the reports once and rebuild the board. Returns the row count."""
    client = his['client']
    today = datetime.now().date()
    date_from = (today - timedelta(days=view['days_back'])).strftime('%Y-%m-%d')
    date_to = (today + timedelta(days=view['days_ahead'])).strftime('%Y-%m-%d')

    with fetch_lock:
        his['fetching'] = True
        try:
            rows = client.fetch_report(MAIN_REPORT, date_from, date_to)
            linked = join_linked_reports(rows, date_from, date_to)
        finally:
            his['fetching'] = False

    store['rows'] = rows
    store['from'] = date_from
    store['to'] = date_to
    store['fetched_at'] = datetime.now().isoformat()
    store['linked'] = linked

    his['last_fetch'] = store['fetched_at']
    his['last_rows'] = len(rows)
    his['error'] = None
    return len(rows)


def fetch_and_report():
    """A fetch nobody is waiting on — the error goes to the status line."""
    try:
        count = his_fetch_once()
        print(f"[*] HIS fetch: {count} rows at {his['last_fetch']}")
    except HISAuthError as e:
        his['logged_in'] = False
        his['client'].logout()
        his['error'] = str(e)
        his['last_error_at'] = datetime.now().isoformat()
        print(f"[!] HIS session ended: {e}")
    except Exception as e:
        his['error'] = str(e)
        his['last_error_at'] = datetime.now().isoformat()
        print(f"[!] HIS fetch failed: {e}")


def _first_fetch_task():
    fetch_and_report()
    start_his_polling()


def _his_poll_task():
    """Background loop: sleep, fetch, repeat — until the operator logs out.

    The first fetch has already happened by the time this starts, so it waits
    before asking again.
    """
    his['polling'] = True
    failures = 0
    try:
        while his['logged_in']:
            # Back off a little while the HIS is unhappy, but keep trying.
            interval = his['client'].config['poll_interval']
            if failures:
                interval = min(interval * min(failures, 5), 1800)
            for _ in range(int(interval)):
                if not his['logged_in']:
                    break
                time.sleep(1)
            if not his['logged_in']:
                break

            before = his['last_fetch']
            fetch_and_report()
            failures = 0 if his['last_fetch'] != before else failures + 1
    finally:
        his['polling'] = False


def start_his_polling():
    if not his['polling'] and his['logged_in']:
        background(_his_poll_task)


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/his/status')
def his_status():
    return jsonify(his_status_payload())


@app.route('/api/his/login', methods=['POST'])
def his_login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    site = (data.get('site') or '').strip()

    if not username or not password:
        return jsonify({'error': 'User id and password are required.'}), 400

    try:
        result = his['client'].login(username, password, site)
    except HISAuthError as e:
        return jsonify({'error': str(e)}), 401
    except HISError as e:
        return jsonify({'error': str(e)}), 502

    his['logged_in'] = True
    his['error'] = None
    his['login_endpoint'] = result.get('endpoint')
    save_his_config()

    # Fetch straight away so the board has something on it before the first
    # poll comes round.
    background(_first_fetch_task)
    return jsonify({'ok': True, 'status': his_status_payload()})


@app.route('/api/his/sites', methods=['POST'])
def his_sites():
    data = request.get_json(silent=True) or {}
    try:
        sites = his['client'].list_sites((data.get('username') or '').strip())
    except HISError as e:
        return jsonify({'error': str(e)}), 502
    save_his_config()
    return jsonify({'sites': sites})


@app.route('/api/his/token', methods=['POST'])
def his_token():
    """Break glass: paste a bearer token when no browser can be started.

    It cannot be renewed, so the dashboard goes quiet in about an hour.
    """
    data = request.get_json(silent=True) or {}
    try:
        his['client'].set_token(data.get('token') or '')
    except HISAuthError as e:
        return jsonify({'error': str(e)}), 400

    his['logged_in'] = True
    his['error'] = None
    his['login_endpoint'] = 'pasted token'
    background(_first_fetch_task)
    return jsonify({'ok': True, 'status': his_status_payload()})


@app.route('/api/his/logout', methods=['POST'])
def his_logout():
    his['logged_in'] = False
    his['client'].logout()
    store['rows'] = []
    store['fetched_at'] = None
    store['linked'] = {}
    his['login_endpoint'] = None
    return jsonify({'ok': True})


@app.route('/api/his/fetch', methods=['POST'])
def his_fetch_now():
    if not his['logged_in']:
        return jsonify({'error': 'Not signed in to the HIS.'}), 401
    try:
        count = his_fetch_once()
    except HISAuthError as e:
        his['logged_in'] = False
        his['client'].logout()
        return jsonify({'error': str(e)}), 401
    except HISError as e:
        his['error'] = str(e)
        return jsonify({'error': str(e)}), 502
    return jsonify({'ok': True, 'rows': count})


@app.route('/api/his/config', methods=['POST'])
def his_config():
    data = request.get_json(silent=True) or {}
    his['client'].update_config(data)
    save_his_config()
    return jsonify({'ok': True, 'config': his['client'].config})


@app.route('/api/view', methods=['GET', 'POST'])
def api_view():
    """The span and the filters the viewer is looking through."""
    if request.method == 'GET':
        return jsonify({'view': dict(view), 'choices': choices()})

    data = request.get_json(silent=True) or {}
    moved = False
    for key in view:
        if key not in data:
            continue
        if key in ('days_back', 'days_ahead') and str(data[key]) != str(view[key]):
            moved = True
        view[key] = data[key]
    clean_view()
    save_view()

    # Moving the dates means asking the HIS again; changing a filter only means
    # looking again at the rows already here.
    if moved and his['logged_in']:
        background(fetch_and_report)
    return jsonify({'ok': True, 'view': dict(view), 'refetching': moved})


@app.route('/api/dashboard')
def api_dashboard():
    return jsonify({'dashboard': dashboard_payload(), 'his': his_status_payload()})


@app.route('/api/alive', methods=['POST'])
def api_alive():
    """The open page saying it is still there — see the window watch below."""
    desktop['last_beat'] = time.time()
    desktop['seen_window'] = True
    return jsonify({'ok': True})


# ─────────────────────────────────────────────
# DESKTOP MODE
# The dashboard is one person's program, not a site on the network: it listens
# on this PC only, opens itself in a window of the PC's own browser, and stops
# when that window is closed.
# ─────────────────────────────────────────────
HOST = '127.0.0.1'
PORT = 5050

# A page that has gone quiet gets this long to come back before the server
# stops, so that reloading (F5) does not take the dashboard down with it.
CLOSE_GRACE = 12

desktop = {
    'window': None,        # the browser process, when we started it ourselves
    'seen_window': False,  # a page has said hello at least once
    'last_beat': 0.0,
    'stopping': False,
    'watch': True,         # False with --no-window: stay up until Ctrl-C
}


def open_window(url):
    """Open the dashboard in a window of the PC's browser. Returns the process.

    Edge / Chrome are asked for a plain window — no address bar, no tabs — with
    a profile of their own, both so the window remembers its size and so it is
    a process we can watch instead of a tab handed to a browser already running.
    A PC with neither falls back to whatever it uses for links, and then only
    closing the page (not the browser) stops the server.
    """
    profile = os.path.join(DATA_DIR, 'window')
    path = browser_login.find_browser(his['client'].config.get('browser_path', ''))
    if path:
        args = [path, f'--app={url}', f'--user-data-dir={profile}',
                '--no-first-run', '--no-default-browser-check']
        if os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0:
            # Chromium refuses to run as root with its sandbox on.
            args.append('--no-sandbox')
        try:
            return subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception as e:
            print(f"[!] Could not open the dashboard window: {e}")
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[!] Could not open a browser: {e}  —  open {url} yourself.")
    return None


def _open_window_task(url):
    """Wait for the server to answer, then put the window on screen."""
    for _ in range(100):
        time.sleep(0.2)
        probe = socket.socket()
        probe.settimeout(0.5)
        try:
            if probe.connect_ex((HOST, PORT)) == 0:
                break
        finally:
            probe.close()
    desktop['window'] = open_window(url)


def stop_dashboard(reason):
    """Close the HIS session and end the program."""
    if desktop['stopping']:
        return
    desktop['stopping'] = True
    print(f"\n[i] {reason} — stopping the dashboard.")

    his['logged_in'] = False
    try:
        save_view()
    except Exception:
        pass

    window = desktop['window']
    if window is not None and window.poll() is None:
        try:
            window.terminate()
        except Exception:
            pass

    # Flask's server has no clean way back out of app.run(), and everything
    # worth keeping is already on disk.
    os._exit(0)


def _window_watch_task():
    """Stop once the dashboard window is gone."""
    while desktop['watch'] and not desktop['stopping']:
        time.sleep(1)
        window = desktop['window']
        if window is not None and window.poll() is not None:
            # Our own window has gone. Nothing waits on the page having loaded
            # first, so a window closed before it rendered still stops here.
            stop_dashboard('The dashboard window was closed')
            return
        if desktop['seen_window'] and time.time() - desktop['last_beat'] >= CLOSE_GRACE:
            stop_dashboard('The dashboard page was closed')
            return


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
if __name__ == '__main__':
    # --no-window keeps the plain server behaviour: nothing is opened and
    # nothing shuts itself down. Handy for tests and for a headless PC.
    windowed = '--no-window' not in sys.argv
    desktop['watch'] = windowed

    url = f"http://localhost:{PORT}"

    print("=" * 60)
    print("  Lab KPIs")
    print()
    print(f"  Runs on this PC only:  {url}")
    if windowed:
        print("  Closing the dashboard window stops the program.")
    else:
        print("  No window (--no-window). Press Ctrl-C to stop.")
    print()
    print("  Data source: HIS website — open the dashboard and sign in")
    print("=" * 60)

    load_his_config()
    load_view()

    if windowed:
        background(_open_window_task, url)
        background(_window_watch_task)

    app.run(host=HOST, port=PORT, threaded=True)
