"""
Lab KPIs — a graph dashboard over the HIS lab reports.

One main report (samples received) is fetched over a date span the viewer
chooses, and any other report definitions in reports/ are joined onto it by
sample number. Everything is worked out from the rows as they arrive: there is
no local database, so "look further back" means asking the HIS for a wider
span, and the numbers are always the HIS's own.

A wider span is also a much slower one — the HIS answers a day of the main
report in about two minutes and a month in about ten — so a span is cut into
chunks the report can actually finish, and the chunks that have already come
back are kept under data/cache so the next year costs only the days since the
last. That cache is the one thing here written to disk that is patient data;
see "Waiting for the HIS" in the README, and the switch for it in Advanced.

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
import re
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
from his_client import (HISAuthError, HISCancelled, HISClient, HISError,
                        parse_datetime)


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
# The expected turnaround per test, typed in on the TAT tabs.
TARGETS_FILE = os.path.join(DATA_DIR, 'targets.json')
# Fetched report chunks, kept between runs so a year costs a year once. This is
# the one place the dashboard writes patient data down — see "Waiting for the
# HIS" in the README, and the switch for it in Advanced.
CACHE_DIR = os.path.join(DATA_DIR, 'cache')

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

# The linked reports the four KPI tabs are built from, by file name. Each tab
# reads that report's own rows rather than the columns joined onto the samples:
# a rejection or a critical result is a row per analyte, and the join keeps only
# the first of them (see join_linked_reports), which is the whole sample's story
# but not each test's.
REPORT_REJECTIONS = 'sample_rejections'
REPORT_CRITICAL = 'critical_results'
REPORT_STAT = 'stat_tests'
REPORT_RESULT_TIME = 'result_time'


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
# the dates and on every poll — the HIS is the only copy worth trusting, so
# nothing here is ever the source of a number. The chunks behind it may be
# remembered on disk (cache.py); these assembled rows are not.
# ─────────────────────────────────────────────
store = {
    'rows': [],
    'from': None,
    'to': None,
    'fetched_at': None,
    'linked': {},      # report name → how many of its rows found a sample
    # Every linked report's own rows, as they came back. The KPI tabs are built
    # from these: one sample's rejection has a row per analyte and the join onto
    # the samples keeps only the first, so a tab that counts reasons or filters
    # by service name has to read the report itself.
    'linked_rows': {},
    # A wide span arrives a chunk at a time and a year takes a while, so the
    # board says how far along it is rather than sitting blank. `partial` marks
    # rows that are only some of the span — the numbers under them are real but
    # not yet the whole answer.
    'progress': {'active': False, 'done': 0, 'total': 0, 'cached': 0,
                 'fetched': 0, 'rows': 0, 'partial': False, 'phase': ''},
}

# What the viewer is looking at. A span in days rather than two dates, so the
# board still covers today after midnight.
view = {
    'days_back': 7,
    'days_ahead': 0,
    'department': '',
    # One chosen test per tab, because a tab is a different report with its own
    # spelling of the name — 'SERVICE_NAME' on the rejections, 'TEST_NAME' on
    # the STAT report — and a name picked on one is rarely a name on another.
    # '' means every test. Keyed by tab id: overview, rejections, critical,
    # referred_tat, inhouse_tat.
    'tests': {},
}

# A test's expected turnaround, in minutes, keyed by the test's name flattened
# through match_key(). One list, shared by both TAT tabs, edited on the tabs
# themselves and kept next to the view settings. A test nobody has set a time
# for is measured against DEFAULT_TARGET rather than left out.
DEFAULT_TARGET = 60
targets = {}


def load_view():
    if not os.path.exists(VIEW_FILE):
        return
    try:
        with open(VIEW_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
    except Exception as e:
        print(f"[!] Could not read {VIEW_FILE}: {e}")
        return
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
    for key, fallback in (('days_back', 7), ('days_ahead', 0)):
        try:
            view[key] = int(view[key])
        except (TypeError, ValueError):
            view[key] = fallback
    # A year is already a slow query; beyond that the report times out and the
    # dashboard looks broken rather than busy.
    view['days_back'] = max(0, min(view['days_back'], 366))
    view['days_ahead'] = max(0, min(view['days_ahead'], 7))
    view['department'] = str(view.get('department') or '')
    chosen = view.get('tests')
    view['tests'] = {str(k): str(v or '') for k, v in chosen.items()} if isinstance(chosen, dict) else {}


def load_targets():
    if not os.path.exists(TARGETS_FILE):
        return
    try:
        with open(TARGETS_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
    except Exception as e:
        print(f"[!] Could not read {TARGETS_FILE}: {e}")
        return
    if isinstance(saved, dict):
        targets.update(clean_targets(saved))


def save_targets():
    try:
        with open(TARGETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(targets, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[!] Could not write {TARGETS_FILE}: {e}")


def clean_targets(raw):
    """Minutes per test, keyed the way match_key spells a test name.

    A blank or a nonsense figure drops the test back to the default rather than
    being kept as a zero — a target of no minutes would fail every sample.
    """
    out = {}
    for name, minutes in raw.items():
        key = match_key(name)
        if not key:
            continue
        try:
            value = int(round(float(minutes)))
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[key] = min(value, 60 * 24 * 14)
    return out


def target_for(name):
    return targets.get(match_key(name), DEFAULT_TARGET)


def visible_rows():
    """The fetched rows the viewer has asked to see."""
    rows = store['rows']
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
    """Average, median and 90th percentile of a list, rounded to the minute."""
    if not values:
        return {'mean': None, 'median': None, 'p90': None, 'count': 0}
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return {
        'mean': round(sum(ordered) / len(ordered)),
        'median': round(statistics.median(ordered)),
        'p90': round(ordered[index]),
        'count': len(ordered),
    }


def counts_by(rows, col, limit=None):
    """{label, value}, biggest first, with a blank column read as 'Not stated'."""
    tally = Counter(text(r, col) or 'Not stated' for r in rows)
    return [{'label': label, 'value': value} for label, value in tally.most_common(limit)]


def build_kpis():
    """Everything the overview tab draws, from the rows now in memory."""
    rows = visible_rows()
    # The overview has a test picker of its own, spelled the main report's way.
    chosen = (view['tests'].get('overview') or '').strip()
    if chosen:
        rows = [r for r in rows if text(r, COL_TEST) == chosen]

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
        'tests': sorted({text(r, COL_TEST) for r in visible_rows() if text(r, COL_TEST)}),
        'test': chosen,
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


# ─────────────────────────────────────────────
# THE FOUR KPI TABS
# Rejections, critical results, and turnaround split two ways. Each is built
# from one linked report's own rows rather than from the columns joined onto the
# samples: those reports carry a row per analyte, and the join keeps only the
# first of them, which describes the sample but not each test on it.
#
# Every tab is narrowed to the samples the main report brought back for the same
# span — so the department filter above still means something here, and a
# rejection for a sample outside the span is not counted against it.
# ─────────────────────────────────────────────
TABS = {
    'rejections': {'report': REPORT_REJECTIONS, 'key': 'LIS_SAMPLE_NO', 'test': 'SERVICE_NAME'},
    'critical': {'report': REPORT_CRITICAL, 'key': 'SAMPLE_NO', 'test': 'TEST_NAME'},
    'inhouse_tat': {'report': REPORT_STAT, 'key': 'SAMPLE_NO', 'test': 'TEST_NAME'},
    'referred_tat': {'report': REPORT_RESULT_TIME, 'key': 'LIS_SAMPLE_NO', 'test': 'SERVICE_NAME'},
}

# How long after the machine had the result somebody signed it off. The lab's
# own line is a quarter of an hour, which is why fifteen and thirty minutes get
# counted rather than just charted.
CRITICAL_LATE = (15, 30)
CRITICAL_BUCKETS = [
    ('Under 15 min', 0, 15),
    ('15–30 min', 15, 30),
    ('30–60 min', 30, 60),
    ('1–4h', 60, 240),
    ('Over 4h', 240, None),
]

TAT_BUCKETS = [
    ('Under 30 min', 0, 30),
    ('30–60 min', 30, 60),
    ('1–2h', 60, 120),
    ('2–4h', 120, 240),
    ('Over 4h', 240, None),
]

_HMS = re.compile(r'(\d+)\s*(hour|min|sec)', re.I)


def parse_hms(value):
    """Minutes out of the HIS's '0 Hours -23 Minutes -51 Seconds' text.

    Report 1044 hands its turnaround over already worked out, but spelled as
    words with dashes between the parts. A row the report could not work out
    comes back as ' Hours  Minutes  Seconds' with the numbers missing, which is
    not a turnaround of zero and is left out instead.
    """
    found = {}
    for number, unit in _HMS.findall(str(value or '')):
        found.setdefault(unit.lower(), int(number))
    if not found:
        return None
    return found.get('hour', 0) * 60 + found.get('min', 0) + found.get('sec', 0) / 60.0


def bucket_counts(values, buckets):
    tally = Counter()
    for minutes in values:
        for label, low, high in buckets:
            if minutes >= low and (high is None or minutes < high):
                tally[label] += 1
                break
        else:
            tally[buckets[-1][0]] += 1
    return [{'label': label, 'value': tally.get(label, 0)} for label, _, _ in buckets]


def classification():
    """Which samples the lab ran itself, and which it sent on.

    The main report carries two departments: the one the sample belongs to and
    the one that accepted it. The same on both means the lab ran the test
    itself; a different one means the sample was referred. Either of them blank
    says nothing either way, so those samples are left out of both TAT tabs
    rather than counted as in house by default.

    Two maps come back, because the two TAT reports name tests differently. A
    sample and test that the main report spells the same way is answered
    exactly; anything else falls back to the sample, and only when every test on
    that sample went the same way.
    """
    by_sample = defaultdict(set)
    by_pair = {}
    for row in visible_rows():
        sample = text(row, COL_SAMPLE)
        ours = match_key(text(row, COL_DEPARTMENT))
        theirs = match_key(text(row, COL_ACCEPT_DEPT))
        if not sample or not ours or not theirs:
            continue
        kind = 'in house' if ours == theirs else 'referred'
        by_sample[sample].add(kind)
        by_pair[(sample, match_key(text(row, COL_TEST)))] = kind
    return by_sample, by_pair


def kind_for(maps, sample, test=''):
    by_sample, by_pair = maps
    kind = by_pair.get((sample, match_key(test)))
    if kind:
        return kind
    kinds = by_sample.get(sample)
    if kinds and len(kinds) == 1:
        return next(iter(kinds))
    return None


def tab_rows(tab):
    """A tab's report rows, narrowed to the samples on the board and its test.

    Returns the rows, the test names the tab could offer, and the one chosen —
    the choices are worked out before the filter is applied, or picking a test
    would leave the dropdown holding only that test.
    """
    spec = TABS[tab]
    samples = {text(r, COL_SAMPLE) for r in visible_rows() if text(r, COL_SAMPLE)}
    rows = [r for r in (store['linked_rows'].get(spec['report']) or [])
            if str(column(r, spec['key'], '')).strip() in samples]
    names = sorted({text(r, spec['test']) for r in rows if text(r, spec['test'])})
    chosen = (view['tests'].get(tab) or '').strip()
    if chosen:
        rows = [r for r in rows if text(r, spec['test']) == chosen]
    return rows, names, chosen, len(samples)


def tab_shell(tab, rows, names, chosen, received, error=None):
    return {'tab': tab, 'tests': names, 'test': chosen, 'rows': len(rows),
            'received': received, 'error': error}


def linked_error(report):
    """Whatever went wrong fetching this tab's report, if anything did."""
    return (store['linked'].get(report) or {}).get('error')


def build_rejections():
    """Samples the lab turned away, and what it said about why."""
    rows, names, chosen, received = tab_rows('rejections')
    out = tab_shell('rejections', rows, names, chosen, received,
                    linked_error(REPORT_REJECTIONS))
    rejected = {str(column(r, 'LIS_SAMPLE_NO', '')).strip() for r in rows}
    rejected.discard('')
    out['totals'] = {
        'rejected': len(rejected),
        'lines': len(rows),
        'received': received,
        # A rate is only a rate against everything the lab received. Narrow the
        # tab to one test and the denominator no longer matches the numerator —
        # the rejection report names services the main report does not — so the
        # figure is withheld rather than quietly wrong.
        'rate': (round(100.0 * len(rejected) / received, 2)
                 if received and not chosen else None),
    }
    out['by_reason'] = counts_by(rows, 'REASON')
    return out


def build_critical():
    """Critical results, and how long they sat between the machine and a name.

    Measured from MACHINE_RESULT_TIME to SECOND_AUTH_DATETIME: the result
    existed, and then somebody authorised it a second time. Fifteen and thirty
    minutes are counted out because those are the lines the lab is held to.
    """
    rows, names, chosen, received = tab_rows('critical')
    out = tab_shell('critical', rows, names, chosen, received,
                    linked_error(REPORT_CRITICAL))
    delays = []
    per_test = defaultdict(list)
    for row in rows:
        minutes = minutes_between(row, 'MACHINE_RESULT_TIME', 'SECOND_AUTH_DATETIME')
        if minutes is None:
            continue
        delays.append(minutes)
        per_test[text(row, 'TEST_NAME') or 'Not stated'].append(minutes)

    summary = summarise(delays)
    out['totals'] = {
        'results': len(rows),
        'measured': summary['count'],
        'mean': summary['mean'],
        'median': summary['median'],
        'over_15': sum(1 for m in delays if m > CRITICAL_LATE[0]),
        'over_30': sum(1 for m in delays if m > CRITICAL_LATE[1]),
        # Rows whose two stamps the HIS did not both fill in. They are still
        # critical results; they just cannot be timed.
        'untimed': len(rows) - summary['count'],
    }
    out['buckets'] = bucket_counts(delays, CRITICAL_BUCKETS)
    out['by_test'] = sorted(
        ({'label': name,
          'value': len(values),
          'mean': summarise(values)['mean'],
          'median': summarise(values)['median'],
          'over_15': sum(1 for m in values if m > CRITICAL_LATE[0]),
          'over_30': sum(1 for m in values if m > CRITICAL_LATE[1])}
         for name, values in per_test.items()),
        key=lambda d: d['value'], reverse=True)
    return out


def build_tat(kind):
    """Turnaround for the samples the lab ran itself, or for those it sent on.

    In house reads the figure report 1044 has already worked out, sorting to
    result entry. Referred is acceptance to authorisation on report 593, which
    is where a sample that left the building comes back.

    Each test is measured against its own expected time — the minutes typed in
    on the tab — so "met" is per test rather than one line drawn across a
    chemistry panel and a culture alike.
    """
    maps = classification()
    if kind == 'in house':
        tab, clock = 'inhouse_tat', 'Sorting to result entry (report 1044).'
    else:
        tab, clock = 'referred_tat', 'Acceptance to authorisation (report 593).'
    spec = TABS[tab]
    rows, names, chosen, received = tab_rows(tab)
    out = tab_shell(tab, rows, names, chosen, received, linked_error(spec['report']))
    out['clock'] = clock

    per_test = defaultdict(list)
    measured = []
    other_kind = 0     # rows that belong to the other TAT tab
    unclassified = 0   # samples whose two departments say nothing either way
    untimed = 0        # rows with no usable turnaround on them
    for row in rows:
        sample = str(column(row, spec['key'], '')).strip()
        name = text(row, spec['test'])
        theirs = kind_for(maps, sample, name)
        if theirs is None:
            unclassified += 1
            continue
        if theirs != kind:
            other_kind += 1
            continue
        if kind == 'in house':
            minutes = parse_hms(column(row, 'TAT_SORT_TO_RESULT_ENTRY'))
        else:
            minutes = minutes_between(row, 'SAMPLE_ACCEPTANCE_TIME', 'AUTHORIZATION_DATE')
        if minutes is None:
            untimed += 1
            continue
        measured.append(minutes)
        per_test[name or 'Not stated'].append(minutes)

    summary = summarise(measured)
    met = sum(1 for name, values in per_test.items()
              for m in values if m <= target_for(name))
    out['totals'] = {
        'measured': summary['count'],
        'tests': len(per_test),
        'mean': summary['mean'],
        'median': summary['median'],
        'met': met,
        'met_pct': round(100.0 * met / summary['count'], 1) if summary['count'] else None,
        'other_kind': other_kind,
        'unclassified': unclassified,
        'untimed': untimed,
    }
    out['buckets'] = bucket_counts(measured, TAT_BUCKETS)
    out['by_test'] = sorted(
        ({'label': name,
          'value': len(values),
          'mean': summarise(values)['mean'],
          'median': summarise(values)['median'],
          'target': target_for(name),
          'met': sum(1 for m in values if m <= target_for(name)),
          'met_pct': round(100.0 * sum(1 for m in values if m <= target_for(name))
                           / len(values), 1)}
         for name, values in per_test.items()),
        key=lambda d: d['value'], reverse=True)
    return out


def build_tabs():
    return {
        'rejections': build_rejections(),
        'critical': build_critical(),
        'inhouse_tat': build_tat('in house'),
        'referred_tat': build_tat('referred'),
    }

def choices():
    """The sites and departments the fetched rows actually contain.

    Taken from the data rather than from the report's own dropdown lists: the
    HIS renames departments and a list nobody maintains ends up offering ones
    that no longer exist.
    """
    return {
        'departments': sorted({text(r, COL_DEPARTMENT) for r in store['rows']
                               if text(r, COL_DEPARTMENT)}),
    }


def dashboard_payload():
    return {
        'kpis': build_kpis(),
        'tabs': build_tabs(),
        'targets': dict(targets),
        'default_target': DEFAULT_TARGET,
        'choices': choices(),
        'view': dict(view),
        'range': {'from': store['from'], 'to': store['to']},
        'fetched_at': store['fetched_at'],
        'linked': store['linked'],
        'progress': dict(store['progress']),
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
    # Bumped by every new fetch. A year-wide fetch already running notices that
    # it is no longer the current one and stops at its next chunk, instead of
    # making the viewer wait out a span they have already moved off.
    'generation': 0,
    'last_duration': 0.0,
}

# Handing out fetch generations. Three threads can ask for one at once — the
# poll loop, Refresh, and a moved date range.
generation_lock = threading.Lock()

# One fetch at a time. The poll loop, the Refresh button and a widened date
# span can all ask at once, and the HIS is slow enough for that to overlap.
fetch_lock = threading.Lock()

# Where the fetched chunks are remembered between runs.
his['client'].attach_cache(CACHE_DIR)


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
        'last_duration': his['last_duration'],
        'cache': cache_stats(),
        'error': his['error'],
        'login_endpoint': his['login_endpoint'],
        'reports': his_client.list_reports(),
        'config': config,
    }


def cache_stats():
    """What is on disk, for the line in Advanced next to "Clear cache"."""
    cache = his['client'].cache
    if not cache:
        return {'enabled': False, 'chunks': 0, 'bytes': 0}
    stats = cache.stats()
    stats['enabled'] = bool(his['client'].config.get('cache_enabled', True))
    return stats


def match_key(value):
    """One spelling of a test name, so two reports' spacing cannot part them."""
    return re.sub(r'\s+', ' ', str(value or '')).strip().upper()


def fetch_linked_rows(name, meta, date_from, date_to, generation=None):
    """One linked report's rows, chunked and cached like the main one.

    A report whose mandatory filter has no "all" option — the rejection report
    picks one hospital and there is no % — says so in its _meta as `fan_out`,
    and is asked once per value instead. Each pass carries the value it was
    asked for into the rows as a column of its own, so a rejection still says
    which hospital turned the sample away once the passes are put together.
    """
    fan = meta.get('fan_out') or {}
    passes = fan.get('values') or [None]
    filter_name = fan.get('filter')
    column_name = fan.get('column') or filter_name

    rows = []
    for one in passes:
        filters = None
        label = None
        if one is not None and filter_name:
            value = one.get('value') if isinstance(one, dict) else one
            label = one.get('label') if isinstance(one, dict) else one
            # Pinned the way the portal pins a dropdown: the id it selects on
            # and the name that was chosen, in case the report reads both.
            filters = {filter_name: {'value': value, 'text': label}}
        phase = f'joining {name}' + (f' — {label}' if label else '')
        part = his['client'].fetch_report_chunked(
            name, date_from, date_to, filters=filters,
            on_progress=lambda p, ph=phase: note_progress(p, ph),
            should_stop=stop_check(generation),
        )
        if label and column_name:
            for row in part:
                row.setdefault(column_name, label)
        rows.extend(part)
    return rows


def join_linked_reports(rows, date_from, date_to, generation=None):
    """Merge every other report in reports/ onto the main rows by sample number.

    A linked report adds its own columns to the sample it belongs to, prefixed
    with its name so two reports carrying a DEPARTMENT_NAME cannot overwrite
    each other. A sample a linked report does not mention is left as it is.

    The main report has a row per test rather than per sample, so a linked
    report that is also per test would otherwise land its last row on every
    test of the sample. Such a report says so in its _meta as `match_column`,
    naming the column on each side that has to agree as well as the sample
    number. Without one, the first row a sample brings back is taken to
    describe the whole sample, and a later row that disagrees is counted as
    ambiguous rather than quietly preferred — that count is how you find out a
    report needs a match_column.

    `skip_columns` drops what is true of one row rather than of the sample: a
    per-analyte result on a report that is joined by sample.
    """
    joined = {}
    kept = {}
    index = defaultdict(list)
    for row in rows:
        key = text(row, COL_SAMPLE)
        if key:
            index[key].append(row)

    for name, meta in his_client.list_reports().items():
        if name == MAIN_REPORT:
            continue
        key_column = meta.get('key_column') or COL_SAMPLE
        pair = meta.get('match_column') or {}
        their_column = pair.get('linked')
        our_column = pair.get('main')
        # SLNO is the report's own row numbering. It says nothing about the
        # sample and changes with every chunk boundary, so it never joins.
        skip = {key_column, 'SLNO'} | set(meta.get('skip_columns') or ())
        try:
            extra = fetch_linked_rows(name, meta, date_from, date_to, generation)
        except HISError as e:
            # One report being unavailable should not cost the whole board.
            print(f"[!] Linked report {name} failed: {e}")
            joined[name] = {'rows': 0, 'matched': 0, 'ambiguous': 0, 'error': str(e)}
            kept[name] = []
            continue

        # The tabs read these rows directly: the join below keeps one row per
        # sample, which is the sample's story but not each analyte's.
        kept[name] = extra
        matched = 0
        ambiguous = 0
        taken = {}   # id(main row) → the linked row already written onto it
        counts = {}  # id(main row) → how many linked rows it could have taken
        for extra_row in extra:
            key = str(column(extra_row, key_column, '')).strip()
            targets = index.get(key)
            if not targets:
                continue
            theirs = match_key(column(extra_row, their_column, '')) if their_column else None
            landed = False
            for target in targets:
                if their_column and match_key(column(target, our_column, '')) != theirs:
                    continue
                counts[id(target)] = counts.get(id(target), 0) + 1
                held = taken.get(id(target))
                if held is not None:
                    # Two rows for the same sample, and nothing to tell the
                    # dashboard which of them this test belongs to. The first
                    # stands; the disagreement is reported rather than hidden.
                    if held != extra_row:
                        ambiguous += 1
                    continue
                taken[id(target)] = extra_row
                landed = True
                for field, value in extra_row.items():
                    if field in skip:
                        continue
                    # The HIS pads some columns out to their database width.
                    target[f'{name}.{field}'] = value.strip() if isinstance(value, str) else value
            if landed:
                matched += 1

        # How many of the report's rows each sample brought back. One is the
        # ordinary case; more says the columns above describe the first of
        # several — two critical results on one sample, say — so the board can
        # show that rather than imply there was only ever one.
        for row in rows:
            seen = counts.get(id(row))
            if seen:
                row[f'{name}.ROWS'] = seen
        joined[name] = {'rows': len(extra), 'matched': matched,
                        'ambiguous': ambiguous, 'error': None}
        print(f"[*] Linked {name}: {len(extra)} rows, {matched} matched a sample"
              + (f", {ambiguous} could not be told apart" if ambiguous else ""))

    return joined, kept


def stop_check(generation):
    """A callable the fetch asks between chunks: am I still the current fetch?

    A year is a long time to hold the viewer to a range they have moved off,
    and longer still to make the next fetch queue behind. So a fetch that has
    been overtaken — or a sign-out — stops where it is instead of finishing an
    answer nobody is waiting for.
    """
    def stopped():
        if generation is not None and his['generation'] != generation:
            return True
        return not his['logged_in']
    return stopped


def note_progress(payload, phase, partial=True):
    """Publish how far along a fetch is, for the line under the date buttons."""
    progress = store['progress']
    progress.update(payload)
    progress['phase'] = phase
    progress['active'] = True
    progress['partial'] = partial and payload.get('done', 0) < payload.get('total', 0)


def his_fetch_once():
    """Pull the reports once and rebuild the board. Returns the row count.

    A wide span is not one request. The HIS answers a day of the main report in
    about two minutes and a month in ten, so a year asked for in one go times
    out; it is fetched a chunk at a time instead, the chunks already on disk are
    read back rather than asked for again, and the rows are put on the board as
    they land so the board fills in rather than waiting an hour to appear.
    """
    client = his['client']
    today = datetime.now().date()
    date_from = (today - timedelta(days=view['days_back'])).strftime('%Y-%m-%d')
    date_to = (today + timedelta(days=view['days_ahead'])).strftime('%Y-%m-%d')

    # Claim the fetch before waiting for the lock, so an hour-long fetch still
    # running sees that it has been overtaken and lets go.
    with generation_lock:
        his['generation'] += 1
        generation = his['generation']
    stopped = stop_check(generation)
    started = time.time()

    with fetch_lock:
        if stopped():
            return len(store['rows'])
        his['fetching'] = True
        # The board is rebuilt from this list on every poll of the page, so
        # appending to it as the chunks land is what makes a year fill in.
        rows = []
        store['rows'] = rows
        store['from'] = date_from
        store['to'] = date_to
        store['linked'] = {}
        store['linked_rows'] = {}
        store['progress'] = {'active': True, 'done': 0, 'total': 0, 'cached': 0,
                             'fetched': 0, 'rows': 0, 'partial': True,
                             'phase': 'reading the report'}
        complete = False
        try:
            client.fetch_report_chunked(
                MAIN_REPORT, date_from, date_to,
                on_rows=rows.extend,
                on_progress=lambda p: note_progress(p, 'reading the report'),
                should_stop=stopped,
            )
            linked, linked_rows = join_linked_reports(rows, date_from, date_to, generation)
            complete = True
        finally:
            his['fetching'] = False
            store['progress']['active'] = False
            # A fetch that fell over part way leaves real rows on the board —
            # just not all of them. Saying so is better than either hiding them
            # or letting them pass for the whole span.
            store['progress']['partial'] = not complete

    store['fetched_at'] = datetime.now().isoformat()
    store['linked'] = linked
    store['linked_rows'] = linked_rows

    his['last_fetch'] = store['fetched_at']
    his['last_rows'] = len(rows)
    his['last_duration'] = round(time.time() - started, 1)
    his['error'] = None
    return len(rows)


def fetch_and_report():
    """A fetch nobody is waiting on — the error goes to the status line."""
    try:
        count = his_fetch_once()
        print(f"[*] HIS fetch: {count} rows in {his['last_duration']}s "
              f"at {his['last_fetch']}")
    except HISCancelled:
        # The viewer moved the dates or signed out. Nothing went wrong, and the
        # fetch that overtook this one is already saying what is happening.
        print('[*] HIS fetch overtaken — stopping it.')
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
            # A year takes longer to fetch than the refresh interval, and a
            # board that is always fetching is a board nobody else can query
            # against. Wait at least as long as the last one took.
            interval = max(interval, his['last_duration'])
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
    except HISCancelled:
        return jsonify({'ok': True, 'rows': len(store['rows']), 'overtaken': True})
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


@app.route('/api/cache/clear', methods=['POST'])
def his_cache_clear():
    """Throw away every remembered chunk.

    Here because the cache is the one thing the dashboard writes down that is
    nobody else's business, so whoever sets the PC up needs a way to empty it
    without going looking for the folder.
    """
    cache = his['client'].cache
    dropped = cache.clear() if cache else 0
    return jsonify({'ok': True, 'dropped': dropped, 'cache': cache_stats()})


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
        if key == 'tests' and isinstance(data[key], dict):
            # One tab's test at a time, so choosing on the rejections tab does
            # not clear what the TAT tabs are showing.
            view['tests'].update(data[key])
            continue
        view[key] = data[key]
    clean_view()
    save_view()

    # Moving the dates means asking the HIS again; changing a filter only means
    # looking again at the rows already here.
    if moved and his['logged_in']:
        background(fetch_and_report)
    return jsonify({'ok': True, 'view': dict(view), 'refetching': moved})


@app.route('/api/targets', methods=['GET', 'POST'])
def api_targets():
    """The minutes each test is expected to take, typed in on the TAT tabs.

    A POST carries only the tests the viewer edited; a test sent with a blank or
    a zero is forgotten rather than stored, which is how a test goes back to the
    default time.
    """
    if request.method == 'GET':
        return jsonify({'targets': dict(targets), 'default_target': DEFAULT_TARGET})

    data = (request.get_json(silent=True) or {}).get('targets') or {}
    for name, minutes in data.items():
        key = match_key(name)
        if not key:
            continue
        cleaned = clean_targets({name: minutes})
        if cleaned:
            targets.update(cleaned)
        else:
            targets.pop(key, None)
    save_targets()
    return jsonify({'ok': True, 'targets': dict(targets)})


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
    load_targets()

    if windowed:
        background(_open_window_task, url)
        background(_window_watch_task)

    app.run(host=HOST, port=PORT, threaded=True)
