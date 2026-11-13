#!/usr/bin/env python3
"""Arbimon project backup: bulk download of recordings, templates and soundscapes.

A project backup is a ZIP of CSV files. The audio is not inside the ZIP: the
`recordings`, `templates` and `soundscapes` CSVs each have a `url` column that
links to the file. This script reads those CSVs and downloads every linked file.

    python3 arbimon_backup_download.py                 # run inside the extracted backup folder
    python3 arbimon_backup_download.py --backup-dir ~/Downloads/my-backup --out ~/Music/my-project
    python3 arbimon_backup_download.py --dry-run       # count and estimate, download nothing

Standard library only (Python 3.9+). Safe to stop and re-run: finished files are
skipped and half-finished files continue where they stopped.

To keep the load on Arbimon's servers low, the script starts at most one request
every 30 seconds (about 2,880 files a day). A large project takes days: leave it
running, or stop and re-run it as often as you like.

Help: https://arbimon.org/support/arbimon/settings/downloading-recordings-from-a-project-backup/
Source: https://github.com/rfcx/arbimon-project-backup
Support: contact@arbimon.org
"""

import argparse
import csv
import email.message
import io
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

__version__ = "1.0.0"

USER_AGENT = "arbimon-backup-download/%s (+https://arbimon.org)" % __version__
KINDS = ("recordings", "templates", "soundscapes")
ID_COLUMN = {"recordings": "recording_id", "templates": "template_id", "soundscapes": "soundscape_id"}
CSV_NAME = re.compile(r"^(?P<kind>[a-z_]+?)(?:[._](?P<n>\d+))?\.csv$", re.IGNORECASE)
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%y %H:%M:%S")
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
STATE_FILE = ".arbimon-download-state.jsonl"
RATE_FILE = ".arbimon-download-rate"
MIN_INTERVAL = 30.0  # seconds between the START of two requests; a floor, --interval may only raise it (tests lower it)
MAX_WORKERS = 4
DEFAULT_WORKERS = 2
CHUNK = 256 * 1024
PROGRESS_EVERY = 60.0  # seconds between progress lines
BACKOFF_BASE = 2.0  # seconds; tests lower this
BACKOFF_CAP = 60.0

EXPIRED_MESSAGE = (
    "The download links in this backup have expired (they work for about 7 days after the backup is made).\n"
    "Please request a new project backup in Arbimon (Project Settings > Project backup) and run this\n"
    "script on the new backup. Files you already downloaded are kept and will be skipped."
)

try:
    csv.field_size_limit(2 ** 31 - 1)
except OverflowError:  # pragma: no cover
    csv.field_size_limit(2 ** 30)


class Expired(Exception):
    pass


class NeedsSignIn(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


# --------------------------------------------------------------------------- helpers

def parse_datetime(value: str) -> Optional[datetime]:
    """Backups have written dates as YYYY-MM-DD, MM/DD/YYYY and MM/DD/YY. Never raises."""
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def safe_name(name: str) -> str:
    """A file name that is valid on Windows, macOS and Linux (no folders, no reserved names)."""
    name = re.split(r"[\\/]", name or "")[-1]
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        return ""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    if len(stem) > 150:
        stem = stem[:150]
    return stem + ("." + ext[:12] if ext else "")


def filename_from_disposition(header: Optional[str]) -> str:
    if not header:
        return ""
    msg = email.message.Message()
    msg["content-disposition"] = header
    return safe_name(msg.get_filename() or "")


def query_param(url: str, key: str) -> str:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(key, [""])[0]
    except ValueError:
        return ""


def url_kind(url: str) -> str:
    """'empty', 'legacy' (needs an arbimon.org sign-in), 'media' (signed audio link) or 'link'."""
    if not url:
        return "empty"
    path = urllib.parse.urlparse(url).path
    if "/legacy-api/" in path and "/recordings/download/" in path:
        return "legacy"
    if query_param(url, "stream-token"):
        return "media"
    return "link"


def redact(url: str) -> str:
    """Links are passwords: never print the query string."""
    p = urllib.parse.urlparse(url)
    return "%s://%s%s%s" % (p.scheme, p.netloc, p.path[:60], "?…" if p.query else "")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "%d B" % n


# --------------------------------------------------------------------------- reading the backup

class Backup:
    """The CSVs of one backup, from a folder or straight from the ZIP."""

    def __init__(self, location: str):
        self.location = location
        self.zip = None  # type: Optional[zipfile.ZipFile]
        self.files = {}  # type: Dict[str, List[str]]
        if os.path.isfile(location) and zipfile.is_zipfile(location):
            self.zip = zipfile.ZipFile(location)
            names = [n for n in self.zip.namelist() if not n.endswith("/")]
        else:
            names = self._scan(location)
        for name in sorted(names, key=self._sort_key):
            m = CSV_NAME.match(os.path.basename(name))
            if m:
                self.files.setdefault(m.group("kind").lower(), []).append(name)

    @staticmethod
    def _sort_key(name: str):
        m = CSV_NAME.match(os.path.basename(name))
        return (os.path.basename(name).split(".")[0].split("_")[0], int(m.group("n") or 0) if m else 0, name)

    @staticmethod
    def _scan(folder: str) -> List[str]:
        def csvs(d):
            try:
                return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".csv")]
            except OSError:
                return []
        found = csvs(folder)
        if not any(CSV_NAME.match(os.path.basename(f)) and os.path.basename(f).split(".")[0].split("_")[0].lower() in KINDS + ("sites",) for f in found):
            # The ZIP was extracted into a sub-folder: look one level down.
            try:
                subdirs = [os.path.join(folder, d) for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))]
            except OSError:
                subdirs = []
            for d in subdirs:
                found += csvs(d)
        return found

    def rows(self, kind: str):
        for name in self.files.get(kind, []):
            if self.zip is not None:
                stream = io.TextIOWrapper(self.zip.open(name), encoding="utf-8-sig", newline="")
            else:
                stream = open(name, encoding="utf-8-sig", newline="")
            with stream:
                for row in csv.DictReader(stream):
                    yield row


# --------------------------------------------------------------------------- state (resume)

class State:
    """One JSON line per finished file: {"kind", "id", "path", "size"}. Small, append-only, crash-safe."""

    def __init__(self, out_dir: str):
        self.path = os.path.join(out_dir, STATE_FILE)
        self.done = {}  # type: Dict[Tuple[str, str], Tuple[str, int]]
        self.lock = threading.Lock()
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        self.done[(rec["kind"], str(rec["id"]))] = (rec["path"], int(rec["size"]))
                    except (ValueError, KeyError, TypeError):
                        continue  # a torn last line after a crash

    def get(self, kind: str, item_id: str) -> Optional[Tuple[str, int]]:
        return self.done.get((kind, item_id))

    def add(self, kind: str, item_id: str, rel_path: str, size: int) -> None:
        rec = {"kind": kind, "id": item_id, "path": rel_path, "size": size}
        with self.lock:
            self.done[(kind, item_id)] = (rel_path, size)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")


# --------------------------------------------------------------------------- pacing

class RateGate:
    """At most one request START per `interval` seconds, across all threads and across runs.

    Every request (download, retry, resume, size check) calls wait() right before it is sent.
    The time of the last start is saved in the output folder, so stopping and immediately
    re-running the script does not send a burst of requests.
    """

    def __init__(self, interval: float, out_dir: Optional[str], stop: threading.Event):
        self.interval = interval
        self.stop = stop
        self.lock = threading.Lock()
        self.path = os.path.join(out_dir, RATE_FILE) if out_dir else None
        self.starts = 0
        self.next = time.monotonic()
        last = self._load()
        if last is not None:
            now_wall = time.time()
            last = min(last, now_wall)  # a saved time in the future means the clock moved: wait one interval at most
            self.next = time.monotonic() + max(0.0, last + interval - now_wall)

    def _load(self) -> Optional[float]:
        if not self.path:
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                return float(f.read().strip())
        except (OSError, ValueError):
            return None

    def _save(self, wall: float) -> None:
        if not self.path:
            return
        tmp = "%s.%d.tmp" % (self.path, threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("%.3f\n" % wall)
            os.replace(tmp, self.path)
        except OSError:
            pass  # pacing within this run still holds; only the cross-run record is lost

    def first_wait(self) -> float:
        return max(0.0, self.next - time.monotonic())

    def wait(self) -> None:
        """Block until this caller may start a request. Raises IOError('stopped') if stopping."""
        # Waiters queue on the lock; the holder sleeps until its turn. The next turn is counted from the
        # moment this one ACTUALLY starts, so a thread that wakes late can never crowd the one after it.
        with self.lock:
            while True:
                if self.stop.is_set():
                    raise IOError("stopped")
                left = self.next - time.monotonic()
                if left <= 0:
                    break
                self.stop.wait(min(left, 1.0))
            self.next = time.monotonic() + self.interval
            self.starts += 1
            self._save(time.time())

# --------------------------------------------------------------------------- downloading

class Item:
    __slots__ = ("kind", "id", "url", "site", "folder", "fallback")

    def __init__(self, kind, item_id, url, site, folder, fallback):
        self.kind, self.id, self.url, self.site, self.folder, self.fallback = kind, item_id, url, site, folder, fallback


class Downloader:
    def __init__(self, out_dir: str, state: State, timeout: float, retries: int, log, gate: "RateGate"):
        self.out = out_dir
        self.state = state
        self.timeout = timeout
        self.retries = retries
        self.log = log
        self.gate = gate
        self.stop = gate.stop
        self.expired = threading.Event()
        self.lock = threading.Lock()
        self.claimed = {}  # (folder, lower-case name) -> (kind, id)
        for (kind, item_id), (path, _size) in state.done.items():
            d, n = os.path.split(path)
            self.claimed[(d, n.lower())] = (kind, item_id)
        self.counts = {"downloaded": 0, "skipped": 0, "legacy": 0, "failed": 0, "no_url": 0}
        self.short_ok = 0
        self.runtime_legacy = 0
        self.short_404 = 0
        self.bytes = 0
        self.problems = []  # rows for the report

    # -- naming
    def _claim(self, item: Item, name: str, size: int) -> str:
        """Return a relative path for this item's file that no other item uses."""
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        candidates = [name, "%s__%s%s" % (stem, item.id, "." + ext if ext else "")]
        with self.lock:
            for i, cand in enumerate(candidates):
                key = (item.folder, cand.lower())
                owner = self.claimed.get(key)
                if owner == (item.kind, item.id):
                    return os.path.join(item.folder, cand)
                if owner is not None:
                    continue
                full = os.path.join(self.out, item.folder, cand)
                if os.path.exists(full) and os.path.getsize(full) != size and i == 0:
                    continue  # someone else's file of the same name: do not overwrite it
                self.claimed[key] = (item.kind, item.id)
                return os.path.join(item.folder, cand)
        raise RuntimeError("could not find a free file name for %s %s" % (item.kind, item.id))

    # -- one request with retries
    def _open(self, url: str, offset: int):
        attempt = 0
        while True:
            headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
            if offset:
                headers["Range"] = "bytes=%d-" % offset
            req = urllib.request.Request(url, headers=headers)
            self.gate.wait()  # every attempt, including retries and resumes, waits its turn
            try:
                return _OPENER.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                status = e.code
                location = e.headers.get("Location", "") if e.headers else ""
                retry_after = e.headers.get("Retry-After") if e.headers else None
                e.close()
                if status == 401:
                    raise Expired()
                if 300 <= status < 400:
                    if "login" in location.lower():
                        raise NeedsSignIn()
                    raise IOError("unexpected redirect (HTTP %d)" % status)
                if status == 404 and urllib.parse.urlparse(url).path.startswith("/dl/"):
                    # arb.mn short links answer 404 once expired (or revoked). One 404 is one bad
                    # link; three with none working means the whole backup has expired.
                    with self.lock:
                        self.short_404 += 1
                        all_dead = self.short_ok == 0 and self.short_404 >= 3
                    if all_dead:
                        raise Expired()
                    raise IOError("link not found or expired (HTTP 404)")
                if status == 410:
                    raise IOError("the file no longer exists on the server (HTTP 410)")
                if status == 416 and offset:
                    return None  # our partial file is already complete or bad; caller restarts
                if status == 403:
                    raise IOError("access refused (HTTP 403) - please report this to contact@arbimon.org")
                if status not in RETRY_STATUS or attempt >= self.retries:
                    raise IOError("HTTP %d" % status)
                self._sleep(attempt, retry_after)
            except (urllib.error.URLError, OSError) as e:  # timeouts, resets, DNS
                if isinstance(e, (Expired, NeedsSignIn)):
                    raise
                if attempt >= self.retries:
                    raise IOError("network error: %s" % getattr(e, "reason", e))
                self._sleep(attempt, None)
            attempt += 1
            if self.stop.is_set():
                raise IOError("stopped")

    def _sleep(self, attempt: int, retry_after: Optional[str]) -> None:
        delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt)) * (0.5 + random.random() / 2)
        if retry_after:
            try:
                delay = max(delay, min(300.0, float(retry_after)))
            except ValueError:
                pass
        self.stop.wait(delay)

    # -- one file
    def fetch(self, item: Item) -> None:
        if self.stop.is_set():
            return
        try:
            self._fetch(item)
        except Expired:
            if not self.expired.is_set():
                self.expired.set()
                self.stop.set()
        except NeedsSignIn:
            with self.lock:
                self.runtime_legacy += 1
            self._problem(item, "legacy", "needs an arbimon.org sign-in")
        except Exception as e:  # one bad file must not stop the rest
            if self.stop.is_set():
                return  # stopping: the .part file is kept and resumes next run
            self._problem(item, "failed", str(e))

    def _problem(self, item: Item, what: str, reason: str) -> None:
        with self.lock:
            self.counts[what] += 1
            self.problems.append({"type": item.kind, "id": item.id, "site_id": item.site, "status": what, "reason": reason,
                                  "url": item.url if what == "legacy" else ""})
        if what == "failed":
            self.log("  failed: %s %s (%s)" % (item.kind[:-1], item.id, reason))

    def _fetch(self, item: Item) -> None:
        done = self.state.get(item.kind, item.id)
        if done and os.path.isfile(os.path.join(self.out, done[0])) and os.path.getsize(os.path.join(self.out, done[0])) == done[1]:
            self._count("skipped")
            return
        folder = os.path.join(self.out, item.folder)
        os.makedirs(folder, exist_ok=True)
        part = os.path.join(folder, "%s-%s.part" % (item.kind[:-1], safe_name(item.id) or "x"))
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        resp = self._open(item.url, offset)
        if resp is None:  # 416: start again from zero
            os.remove(part)
            offset = 0
            resp = self._open(item.url, 0)
        if urllib.parse.urlparse(item.url).path.startswith("/dl/"):
            with self.lock:
                self.short_ok += 1
        with resp:
            status = resp.status
            if status == 206:
                m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", resp.headers.get("Content-Range", ""))
                if not m or int(m.group(1)) != offset:
                    raise IOError("server answered an unexpected byte range")
                total = int(m.group(2)) if m.group(2) != "*" else None
            else:
                offset = 0
                length = resp.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else None
            name = (filename_from_disposition(resp.headers.get("Content-Disposition"))
                    or safe_name(query_param(item.url, "download-name"))
                    or item.fallback)
            if total is not None:
                rel = self._claim(item, name, total)
                final = os.path.join(self.out, rel)
                if offset == 0 and os.path.isfile(final) and os.path.getsize(final) == total:
                    self.state.add(item.kind, item.id, rel, total)  # downloaded before the state file existed
                    self._count("skipped")
                    return
            with open(part, "ab" if offset else "wb") as f:
                got = offset
                while True:
                    if self.stop.is_set():
                        raise IOError("stopped")
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    with self.lock:
                        self.bytes += len(chunk)
        if total is not None and got != total:
            raise IOError("incomplete download (%d of %d bytes); run the script again to resume" % (got, total))
        if total is None:
            total = got
            rel = self._claim(item, name, total)
            final = os.path.join(self.out, rel)
        os.replace(part, final)
        self.state.add(item.kind, item.id, rel, total)
        self._count("downloaded")

    def _count(self, what: str) -> None:
        with self.lock:
            self.counts[what] += 1


# --------------------------------------------------------------------------- planning

def make_item(kind: str, row: dict, n: int) -> Item:
    item_id = str(row.get(ID_COLUMN[kind]) or "").strip() or "row%d" % n
    url = (row.get("url") or "").strip()
    site = str(row.get("site_id") or "").strip()
    folder = os.path.join(kind, safe_name(site) or "no-site") if kind == "recordings" else kind
    when = parse_datetime(row.get("datetime") or row.get("date_created") or "")
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1][:8] if url else ""
    fallback = safe_name("%s%s%s" % (when.strftime("%Y%m%d_%H%M%S") + "_" if when else "", item_id, ext)) or item_id
    return Item(kind, item_id, url, site, folder, fallback)

def iter_items(backup: Backup, kinds, limit: Optional[int]):
    """Stream items (projects can have millions of recordings: never hold them all)."""
    n = 0
    for kind in kinds:
        for row in backup.rows(kind):
            if limit is not None and n >= limit:
                return
            n += 1
            yield make_item(kind, row, n)

def plan_items(backup: Backup, kinds, limit: Optional[int], sample: int):
    """One counting pass: per-kind stats, the links' expiry, and a random sample of audio links."""
    plan = {k: {"rows": 0, "media": 0, "link": 0, "legacy": 0, "empty": 0} for k in kinds}
    expiry, reservoir, seen_media = None, [], 0
    rng = random.Random(709)
    for it in iter_items(backup, kinds, limit):
        k = url_kind(it.url)
        plan[it.kind]["rows"] += 1
        plan[it.kind][k] += 1
        if k == "media":
            seen_media += 1
            if expiry is None:
                exp = query_param(it.url, "exp")
                expiry = datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp.isdigit() else None
            if len(reservoir) < sample:
                reservoir.append(it)
            else:
                j = rng.randrange(seen_media)
                if j < sample:
                    reservoir[j] = it
    return plan, expiry, reservoir

def estimate_sizes(sample_items: List[Item], timeout: float, gate: RateGate) -> Optional[float]:
    """Average size from HEAD requests on a few signed audio links (short links do not support HEAD)."""
    sizes = []
    for it in sample_items:
        req = urllib.request.Request(it.url, method="HEAD", headers={"User-Agent": USER_AGENT})
        gate.wait()
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                n = r.headers.get("Content-Length")
                if n and n.isdigit():
                    sizes.append(int(n))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise Expired()
        except (urllib.error.URLError, OSError):
            pass
    return sum(sizes) / len(sizes) if sizes else None


def human_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 120:
        return "%d seconds" % seconds
    if seconds < 2 * 3600:
        return "%d minutes" % round(seconds / 60.0)
    if seconds < 2 * 86400:
        return "%.1f hours" % (seconds / 3600.0)
    return "%.1f days" % (seconds / 86400.0)

def time_estimate(log, todo: int, interval: float, expiry: Optional[datetime]) -> None:
    """Print how long `todo` files take at one start per `interval`, and warn if the links expire first."""
    if todo <= 0:
        return
    need = todo * interval
    log("Estimated time: about %s for %d files (one file every %g seconds, about %d a day)."
        % (human_duration(need), todo, interval, int(86400 // interval) if interval > 0 else 0))
    if expiry is not None:
        left = (expiry - datetime.now(timezone.utc)).total_seconds()
        if 0 < left < need:
            fits = int(left // interval)
            log("Warning: the links expire in %s, before all files can be downloaded (about %d of %d will fit)."
                % (human_duration(left), fits, todo))
            log("  Let it run until the links expire, then request a new project backup and run the script")
            log("  on the new backup with the same --out folder: files you already have are skipped.")

def site_names(backup: Backup) -> Dict[str, str]:
    return {str(r.get("site_id", "")).strip(): (r.get("name") or "").strip() for r in backup.rows("sites")}


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="arbimon_backup_download.py",
        description="Download the recordings, templates and soundscapes linked from an Arbimon project backup.",
        epilog="Exit codes: 0 done, 1 some files failed, 2 bad arguments / nothing to do, 3 links expired.")
    ap.add_argument("--backup-dir", default=".", help="the extracted backup folder, or the backup .zip itself (default: current folder)")
    ap.add_argument("--out", default="downloads", help="where to save files (default: ./downloads)")
    ap.add_argument("--only", choices=KINDS, action="append", help="download only this kind (repeatable)")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between the start of two downloads; at least %g (default %g)" % (MIN_INTERVAL, MIN_INTERVAL))
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="downloads that may overlap when one file is slow, 1-%d (default %d)" % (MAX_WORKERS, DEFAULT_WORKERS))
    ap.add_argument("--limit", type=int, help="stop after N files (for a quick test)")
    ap.add_argument("--dry-run", action="store_true", help="count files and estimate the size; download nothing")
    ap.add_argument("--sample", type=int, default=3, help="files to check for the --dry-run size estimate (default 3)")
    ap.add_argument("--timeout", type=float, default=120, help="seconds before a stalled request is retried (default 120)")
    ap.add_argument("--retries", type=int, default=5, help="retries per file on temporary errors (default 5)")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)

    def log(msg=""):
        print(msg, flush=True)

    interval = MIN_INTERVAL if args.interval is None else args.interval
    if not interval >= MIN_INTERVAL:  # also refuses NaN
        log("--interval must be at least %g seconds (to keep the load on Arbimon's servers low)." % MIN_INTERVAL)
        return 2
    workers = max(1, min(MAX_WORKERS, args.workers))
    if workers != args.workers:
        log("Using %d workers (allowed range 1-%d)." % (workers, MAX_WORKERS))
    kinds = tuple(args.only) if args.only else KINDS

    backup = Backup(os.path.expanduser(args.backup_dir))
    if not backup.files:
        log("No backup CSV files found in %s." % os.path.abspath(args.backup_dir))
        log("Extract the backup ZIP first, then run this script inside that folder (or pass --backup-dir).")
        return 2
    plan, expiry, sample_items = plan_items(backup, kinds, args.limit, args.sample if args.dry_run else 0)
    log("arbimon_backup_download %s - backup: %s" % (__version__, os.path.abspath(args.backup_dir)))
    for kind in kinds:
        p = plan[kind]
        if not backup.files.get(kind):
            log("  %-12s none in this backup" % kind)
            continue
        extra = ", %d need an arbimon.org sign-in" % p["legacy"] if p["legacy"] else ""
        extra += ", %d without a link" % p["empty"] if p["empty"] else ""
        log("  %-12s %d files%s" % (kind, p["rows"], extra))
    total_rows = sum(p["rows"] for p in plan.values())
    if not total_rows:
        log("Nothing to download.")
        return 0
    if expiry is not None:
        left = (expiry - datetime.now(timezone.utc)).total_seconds()
        local = expiry.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        if left > 0:
            log("Links valid until %s (%.1f days left)." % (local, left / 86400))
        else:
            log("Warning: the links look expired (valid until %s by this computer's clock). Trying anyway." % local)

    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)
    state = State(out)
    gate = RateGate(interval, out, threading.Event())

    if args.dry_run:
        already = sum(1 for it in iter_items(backup, kinds, args.limit) if state.get(it.kind, it.id))
        try:
            if sample_items:
                log("Checking the size of %d sample files (one request every %g seconds)..." % (len(sample_items), interval))
            avg = estimate_sizes(sample_items, args.timeout, gate)
        except Expired:
            log(EXPIRED_MESSAGE)
            return 3
        todo = sum(p["media"] + p["link"] for p in plan.values()) - already
        log("Dry run: %d to download, %d already downloaded, output folder %s" % (max(0, todo), already, out))
        n_media = sum(p["media"] for p in plan.values())
        if avg:
            log("Estimated size: about %s for %d audio files (average %s each, from %d samples)."
                % (human_bytes(avg * n_media), n_media, human_bytes(avg), len(sample_items)))
        time_estimate(log, max(0, todo), interval, expiry)
        return 0

    dl = Downloader(out, state, args.timeout, args.retries, log, gate)
    total = sum(p["media"] + p["link"] for p in plan.values())
    already = sum(1 for it in iter_items(backup, kinds, args.limit) if state.get(it.kind, it.id))
    log("Saving to %s. Press Ctrl+C to stop; run again to resume." % out)
    log("One download starts every %g seconds, to keep the load on Arbimon's servers low." % interval)
    time_estimate(log, total - already, interval, expiry)
    w0 = gate.first_wait()
    if w0 >= 1 and total - already > 0:
        log("Waiting %d seconds since the last run's final request..." % round(w0))
    started = time.time()
    stop_progress = threading.Event()

    def progress():
        while not stop_progress.wait(PROGRESS_EVERY):
            c = dl.counts
            n = c["downloaded"] + c["skipped"] + c["failed"] + dl.runtime_legacy
            left = max(0, total - n)
            log("  %d/%d done (%d new, %d already there, %d failed) - %s - about %s left"
                % (n, total, c["downloaded"], c["skipped"], c["failed"], human_bytes(dl.bytes), human_duration(left * interval)))

    ticker = threading.Thread(target=progress, daemon=True)
    ticker.start()
    interrupted = False
    pool = ThreadPoolExecutor(max_workers=workers)
    slots = threading.BoundedSemaphore(workers * 4)
    pending = [0]
    pending_lock = threading.Lock()

    def run(it):
        try:
            dl.fetch(it)
        finally:
            with pending_lock:
                pending[0] -= 1
            slots.release()

    try:
        for it in iter_items(backup, kinds, args.limit):
            if dl.stop.is_set():
                break
            k = url_kind(it.url)
            if k == "empty":
                dl._problem(it, "no_url", "no link in the backup")
                continue
            if k == "legacy":
                dl._problem(it, "legacy", "needs an arbimon.org sign-in")
                continue
            while not slots.acquire(timeout=0.2):
                pass
            with pending_lock:
                pending[0] += 1
            pool.submit(run, it)
        while pending[0] > 0:  # short sleeps keep Ctrl+C responsive
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
        dl.stop.set()
        log("\nStopping... (partly downloaded files are kept and will resume next time)")
    finally:
        try:
            pool.shutdown(wait=True)
        except KeyboardInterrupt:
            interrupted = True
            dl.stop.set()
            pool.shutdown(wait=True)
        stop_progress.set()

    # ---- summary
    c = dl.counts
    report = None
    if dl.problems:
        report = os.path.join(out, "download-report-%s.csv" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(report, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["type", "id", "site_id", "status", "reason", "url"])
            w.writeheader()
            w.writerows(dl.problems)
    log("")
    log("Summary (%s, %.0f s)" % (out, time.time() - started))
    log("  downloaded now:          %d (%s)" % (c["downloaded"], human_bytes(dl.bytes)))
    log("  already downloaded:      %d" % c["skipped"])
    if c["legacy"]:
        log("  need an arbimon.org sign-in: %d - older uploads that cannot be downloaded by a script." % c["legacy"])
        log("      Open each link from the report in a browser while signed in to arbimon.org,")
        log("      or ask contact@arbimon.org for help with a large number.")
    if c["no_url"]:
        log("  no link in the backup:   %d" % c["no_url"])
    log("  failed:                  %d%s" % (c["failed"], " - run the script again to retry them" if c["failed"] else ""))
    rec_sites = {}
    for (kind, _id), (path, _size) in state.done.items():
        if kind == "recordings":
            sid = path.replace("\\", "/").split("/")[1]
            rec_sites[sid] = rec_sites.get(sid, 0) + 1
    if rec_sites:
        names = site_names(backup)
        log("  recordings by site folder (recordings/<site_id>/):")
        with open(os.path.join(out, "recordings", "sites.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["site_id", "site_name", "files"])
            for sid in sorted(rec_sites, key=lambda s: (len(s), s)):
                w.writerow([sid, names.get(sid, ""), rec_sites[sid]])
                log("      %-10s %6d  %s" % (sid, rec_sites[sid], names.get(sid, "")))
    if report:
        log("  report: %s" % report)
    if dl.expired.is_set():
        log("")
        log(EXPIRED_MESSAGE)
        return 3
    if interrupted:
        return 130
    return 1 if c["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
