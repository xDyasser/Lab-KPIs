"""
Disk cache for fetched report chunks.

The dashboard has no database and does not want one: the HIS is the only copy
of the numbers worth trusting. But a day of the sample-received report takes
about two minutes to come back and a month takes ten, so a year asked for in
one request does not come back at all — the report times out and the board
looks broken rather than slow.

The answer is in two halves. his_client cuts a wide span into chunks small
enough to survive; this file remembers the chunks that have already come back,
so the *second* year costs only the days that have happened since the first.

What is kept, and for how long
──────────────────────────────
One file per chunk, holding that chunk's rows exactly as the HIS returned them.
A chunk is only reused when it is asking the same question:

* the same report, over the same two dates;
* built from the same report definition — the file's own bytes are hashed into
  the key, so editing reports/samples_received.json retires its cache instead of
  serving rows the new definition would not have asked for;
* old enough to have stopped changing. A sample received on Monday can be
  accepted on Thursday, which rewrites Monday's row, so any chunk touching the
  last `settle_days` days is always refetched. Beyond that a chunk is trusted
  for `keep_days`.

This is patient data on disk, which the rest of the dashboard deliberately
avoids — see the README. It lives under data/cache, the folder is created
private to the account running the dashboard, "Clear cache" in Advanced empties
it, and turning the cache off in Advanced stops it being written at all.
"""

import gzip
import hashlib
import json
import os
import shutil
import threading
from datetime import date, datetime, timedelta


class ReportCache:
    """Chunks of report rows, kept on disk between fetches (and between runs)."""

    def __init__(self, folder):
        self.folder = folder
        self._lock = threading.Lock()

    # ── keys ──────────────────────────────────────────────────
    @staticmethod
    def fingerprint(payload):
        """A short hash of whatever makes two requests different questions."""
        raw = json.dumps(payload, sort_keys=True, default=str).encode('utf-8')
        return hashlib.sha1(raw).hexdigest()[:12]

    def _path(self, report, date_from, date_to, fingerprint):
        safe = ''.join(c for c in str(report) if c.isalnum() or c in '_-') or 'report'
        name = f'{date_from}_{date_to}_{fingerprint}.json.gz'
        return os.path.join(self.folder, safe, name)

    # ── reading ───────────────────────────────────────────────
    def get(self, report, date_from, date_to, fingerprint,
            keep_days=30, settle_days=2):
        """The rows for this chunk, or None if it is missing or too new to trust.

        Too new, not too old: a chunk covering days that may still be gaining
        acceptance timestamps has to be asked for again, however recently it was
        written.
        """
        if not still_settled(date_to, settle_days):
            return None
        path = self._path(report, date_from, date_to, fingerprint)
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                entry = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get('rows'), list):
            return None

        written = _parse_stamp(entry.get('fetched_at'))
        if written is None:
            return None
        if keep_days >= 0 and datetime.now() - written > timedelta(days=keep_days):
            return None

        # Touch it, so pruning throws away the chunks nobody is asking for
        # rather than the oldest days of a span somebody reads every morning.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return [row for row in entry['rows'] if isinstance(row, dict)]

    # ── writing ───────────────────────────────────────────────
    def put(self, report, date_from, date_to, fingerprint, rows,
            settle_days=2, max_mb=512):
        """Keep this chunk's rows, unless they are still moving."""
        if not still_settled(date_to, settle_days):
            return False
        path = self._path(report, date_from, date_to, fingerprint)
        entry = {
            'report': report,
            'from': date_from,
            'to': date_to,
            'fetched_at': datetime.now().isoformat(timespec='seconds'),
            'rows': rows,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _make_private(self.folder)
            # Written beside the target and moved into place, so a fetch
            # interrupted half way through leaves no half-written chunk that
            # the next run would read back as the truth.
            temp = path + '.part'
            with gzip.open(temp, 'wt', encoding='utf-8') as f:
                json.dump(entry, f, default=str)
            os.replace(temp, path)
        except OSError as exc:
            print(f"[!] Could not cache {report} {date_from}..{date_to}: {exc}")
            return False
        self.prune(max_mb)
        return True

    # ── housekeeping ──────────────────────────────────────────
    def entries(self):
        """Every cached chunk as (path, size in bytes, modified time)."""
        found = []
        for root, _dirs, files in os.walk(self.folder):
            for name in files:
                if not name.endswith('.json.gz'):
                    continue
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                found.append((path, stat.st_size, stat.st_mtime))
        return found

    def stats(self):
        found = self.entries()
        return {
            'chunks': len(found),
            'bytes': sum(size for _p, size, _m in found),
            'oldest': min((m for _p, _s, m in found), default=None),
        }

    def prune(self, max_mb=512):
        """Keep the cache under its size limit, dropping least-recently-read first."""
        if max_mb is None or max_mb <= 0:
            return 0
        limit = int(max_mb) * 1024 * 1024
        with self._lock:
            found = self.entries()
            total = sum(size for _p, size, _m in found)
            if total <= limit:
                return 0
            dropped = 0
            for path, size, _mtime in sorted(found, key=lambda e: e[2]):
                if total <= limit:
                    break
                try:
                    os.remove(path)
                except OSError:
                    continue
                total -= size
                dropped += 1
            return dropped

    def clear(self):
        """Empty the cache. Returns how many chunks went."""
        with self._lock:
            count = len(self.entries())
            try:
                shutil.rmtree(self.folder)
            except FileNotFoundError:
                return 0
            except OSError as exc:
                print(f"[!] Could not clear the cache: {exc}")
                return 0
            return count


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def still_settled(date_to, settle_days):
    """True when a chunk ending on `date_to` is old enough to keep.

    A sample received today may not be accepted until Thursday, and that
    acceptance rewrites today's row. So the last few days of the span are never
    cached — they are the days most likely to be wrong by tomorrow, and they are
    also the cheapest to fetch again.
    """
    try:
        end = datetime.strptime(str(date_to)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return False
    return end < date.today() - timedelta(days=max(0, int(settle_days)))


def _parse_stamp(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _make_private(folder):
    """Keep the cache readable only by the account running the dashboard."""
    if os.name == 'posix':
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
