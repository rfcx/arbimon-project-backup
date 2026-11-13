"""Tests for arbimon_backup_download.py against a local fake server. Run: python3 -m unittest -v"""
import contextlib
import csv
import hashlib
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import arbimon_backup_download as abd

abd.BACKOFF_BASE = 0.01
abd.BACKOFF_CAP = 0.05
abd.MIN_INTERVAL = 0.0   # the pacing tests below raise it; everything else runs unpaced
abd.PROGRESS_EVERY = 3600
EXP = str(int(time.time()) + 3 * 86400)
EXP_PAST = str(int(time.time()) - 86400)

def body_for(path):
    seed = hashlib.sha256(path.encode()).digest()
    if "/same/" in path:
        return (seed * 4000)[:40000]  # different bytes, identical size
    return (seed * 4000)[: 50000 + seed[0] * 100]

def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log = []
    times = []  # server-side arrival time of every request (monotonic)
    flaky_seen = set()

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        Handler.times.append(time.monotonic())
        Handler.log.append((self.command, u.path, self.headers.get("Range"), self.headers.get("User-Agent")))
        if not (self.headers.get("User-Agent") or "").startswith("arbimon-backup-download/"):
            return self._send(403, b"error code: 1010")  # what Cloudflare does to Python-urllib/x.y
        if u.path.startswith("/legacy-api/") or u.path.startswith("/to-login/"):
            return self._send(302, b"Found. Redirecting to /legacy-login", {"Location": "/legacy-login"})
        if u.path.startswith("/dl/"):
            if self.command == "HEAD" or u.path == "/dl/gone":
                return self._send(404, b'{"error":"not_found"}')
            body = body_for(u.path)
            return self._send(200, body, {"Content-Disposition": 'attachment; filename="%s"' % (u.path.rsplit("/", 1)[1] + ".png")})
        if u.path.startswith("/flaky/"):
            if u.path not in Handler.flaky_seen:
                Handler.flaky_seen.add(u.path)
                return self._send(429, b"slow down", {"Retry-After": "0"})
        if u.path.startswith(("/internal/", "/flaky/", "/plain/", "/same/")):
            if u.path.startswith("/internal/") and q.get("stream-token", [""])[0] != "ok":
                return self._send(401, b"Unauthorized")
            body = body_for(u.path)
            headers = {"Content-Type": "audio/flac", "Accept-Ranges": "bytes"}
            name = q.get("download-name", [""])[0]
            if name and not u.path.startswith("/plain/"):
                headers["Content-Disposition"] = 'attachment; filename="%s"' % name
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(body):
                    return self._send(416, b"", {"Content-Range": "bytes */%d" % len(body)})
                headers["Content-Range"] = "bytes %d-%d/%d" % (start, len(body) - 1, len(body))
                return self._send(206, body[start:], headers)
            return self._send(200, body, headers)
        return self._send(404, b"nope")

def media(base, path, name, token="ok", exp=EXP):
    return "%s%s?%s" % (base, path, urllib.parse.urlencode({"stream-token": token, "exp": exp, "download-name": name}))

REC_HEADER = ["recording_id", "site_id", "datetime", "datetime_utc", "sample_rate", "duration", "samples", "bit_rate",
              "sample_encoding", "upload_time", "meta", "url"]

def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

def rec(rid, site, dt, url):
    return [rid, site, dt, dt, 24000, 60, 1440000, 384000, "pcm_s16le", dt, '{"x":"a,b"}', url]

class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Handler.log = []
        Handler.times = []
        Handler.flaky_seen = set()
        self.dir = tempfile.mkdtemp(prefix="abd-")
        self.backup = os.path.join(self.dir, "backup")
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.backup)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_main(self, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = abd.main(["--backup-dir", self.backup, "--out", self.out, "--workers", "3"] + list(extra))
        return code, buf.getvalue()

    def files(self):
        found = {}
        for root, _d, names in os.walk(self.out):
            for n in names:
                p = os.path.relpath(os.path.join(root, n), self.out).replace(os.sep, "/")
                found[p] = os.path.getsize(os.path.join(root, n))
        return found

class TestHappyPath(Base):
    def make_backup(self):
        b = self.base
        write_csv(os.path.join(self.backup, "sites.0001.csv"), ["site_id", "name"], [[11, "North, ridge"], [12, "South"]])
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [
            rec(1, 11, "2026-03-27 08:04:00", media(b, "/internal/a/1.flac", "rec.flac")),           # ISO date
            rec(2, 12, "03/27/2026 08:05:00", media(b, "/internal/a/2.flac", "rec.flac")),           # same name, other site
            rec(3, 11, "03/27/26 08:06:00", media(b, "/internal/a/3.flac", "long.003.flac")),        # MM/DD/YY, segment name
            rec(4, 11, "2026-03-27 08:07:00", media(b, "/internal/a/4.wav", "long.004.wav")),
        ])
        write_csv(os.path.join(self.backup, "recordings.0002.csv"), REC_HEADER, [
            rec(5, 11, "2026-03-27 08:08:00", media(b, "/internal/a/5.flac", "rec.flac")),           # same name, SAME site
            rec(6, 12, "2026-03-27 08:09:00", b + "/legacy-api/project/p/recordings/download/6"),   # legacy
            rec(7, 12, "2026-03-27 08:10:00", ""),                                                     # no link
            rec(8, 12, "12/31/2019 23:59:59", b + "/plain/8.flac"),                                   # no name at all
            rec(9, 12, "2026-03-27 08:11:00", media(b, "/flaky/9.flac", "flaky.flac")),              # 429 then OK
        ])
        write_csv(os.path.join(self.backup, "templates.0001.csv"), ["template_id", "template_name", "url"],
                  [[71, "t1", b + "/dl/TPL1"], [72, "t2", b + "/dl/TPL2"]])
        # no soundscapes file: an empty dataset has no CSV

    def test_full_run_then_rerun(self):
        self.make_backup()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        f = self.files()
        for name in ["recordings/11/rec.flac", "recordings/12/rec.flac", "recordings/11/long.003.flac",
                     "recordings/11/long.004.wav", "recordings/11/rec__5.flac", "recordings/12/20191231_235959_8.flac",
                     "recordings/12/flaky.flac", "templates/TPL1.png", "templates/TPL2.png", "recordings/sites.csv"]:
            self.assertIn(name, f, out)
        self.assertEqual(f["recordings/11/rec.flac"], len(body_for("/internal/a/1.flac")))
        self.assertEqual(read_bytes(os.path.join(self.out, "recordings/11/rec__5.flac")), body_for("/internal/a/5.flac"))
        self.assertFalse([p for p in f if p.endswith(".part")])
        self.assertIn("soundscapes", out)
        self.assertIn("none in this backup", out)
        self.assertIn("need an arbimon.org sign-in: 1", out)
        self.assertIn("no link in the backup:   1", out)
        self.assertIn("downloaded now:          9", out)
        report = [p for p in f if p.startswith("download-report-")]
        self.assertEqual(len(report), 1)
        with open(os.path.join(self.out, report[0]), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        legacy = [r for r in rows if r["status"] == "legacy"]
        self.assertEqual([r["id"] for r in legacy], ["6"])
        self.assertTrue(legacy[0]["url"].endswith("/recordings/download/6"))
        self.assertTrue(all(ua.startswith("arbimon-backup-download/") for _m, _p, _r, ua in Handler.log))
        self.assertNotIn("stream-token=ok", out)  # links never printed
        with open(os.path.join(self.out, "recordings/sites.csv"), encoding="utf-8") as fh:
            sites = fh.read()
        self.assertIn('11,"North, ridge",4', sites)
        # re-run: nothing downloaded again
        Handler.log = []
        code, out2 = self.run_main()
        self.assertEqual(code, 0, out2)
        self.assertIn("downloaded now:          0", out2)
        self.assertIn("already downloaded:      9", out2)
        self.assertEqual([x for x in Handler.log if x[1].startswith("/internal/")], [])

    def test_reads_zip_directly_and_limit(self):
        self.make_backup()
        z = os.path.join(self.dir, "backup.zip")
        with zipfile.ZipFile(z, "w") as zf:
            for n in os.listdir(self.backup):
                zf.write(os.path.join(self.backup, n), n)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = abd.main(["--backup-dir", z, "--out", self.out, "--limit", "2"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(sorted(p for p in self.files() if p.endswith(".flac")), ["recordings/11/rec.flac", "recordings/12/rec.flac"])

    def test_dry_run_downloads_nothing(self):
        self.make_backup()
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("Estimated size", out)
        self.assertFalse([p for p in self.files() if not p.startswith(".arbimon")])
        self.assertTrue(all(m == "HEAD" for m, _p, _r, _u in Handler.log))

class TestFailureModes(Base):
    def test_resume_partial_part_file(self):
        url = media(self.base, "/internal/r/1.flac", "big.flac")
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [rec(1, 5, "2026-01-01 00:00:00", url)])
        full = body_for("/internal/r/1.flac")
        os.makedirs(os.path.join(self.out, "recordings", "5"))
        with open(os.path.join(self.out, "recordings", "5", "recording-1.part"), "wb") as f:
            f.write(full[:20000])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(read_bytes(os.path.join(self.out, "recordings/5/big.flac")), full)
        self.assertIn(("GET", "/internal/r/1.flac", "bytes=20000-"), [x[:3] for x in Handler.log])

    def test_401_stops_with_expired_message(self):
        rows = [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "/internal/x/%d.flac" % i, "%d.flac" % i, token="bad"))
                for i in range(1, 60)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        code, out = self.run_main()
        self.assertEqual(code, 3, out)
        self.assertIn("have expired", out)
        self.assertEqual(out.count("have expired"), 1)
        self.assertLess(len(Handler.log), 20, "kept hammering after a 401")

    def test_past_exp_warns_but_still_tries(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER,
                  [rec(1, 1, "2026-01-01 00:00:00", media(self.base, "/internal/p/1.flac", "p.flac", exp=EXP_PAST))])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)  # the fake server ignores exp: the clock warning must not block
        self.assertIn("look expired", out)

    def test_expired_short_links_stop(self):
        write_csv(os.path.join(self.backup, "templates.0001.csv"), ["template_id", "url"],
                  [[i, self.base + "/dl/gone"] for i in range(1, 30)])
        code, out = self.run_main("--workers", "1")
        self.assertEqual(code, 3, out)
        self.assertIn("have expired", out)

    def test_one_dead_short_link_is_a_failure_not_expiry(self):
        write_csv(os.path.join(self.backup, "templates.0001.csv"), ["template_id", "url"],
                  [[1, self.base + "/dl/OK1"], [2, self.base + "/dl/gone"], [3, self.base + "/dl/OK3"]])
        code, out = self.run_main("--workers", "1")
        self.assertEqual(code, 1, out)
        self.assertIn("failed:                  1", out)
        self.assertNotIn("have expired", out)

    def test_redirect_to_login_found_while_downloading(self):
        # a link that does not LOOK like a legacy link but redirects to the sign-in page
        write_csv(os.path.join(self.backup, "soundscapes.0001.csv"), ["soundscape_id", "url"],
                  [[1, self.base + "/to-login/1"], [2, self.base + "/plain/2.png"]])
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("need an arbimon.org sign-in: 1", out)
        self.assertIn("failed:                  0", out)

    def test_same_name_same_size_in_one_site(self):
        rows = [rec(i, 3, "2026-01-01 00:00:00", "%s/same/%d.flac?download-name=dup.flac" % (self.base, i)) for i in (1, 2)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        code, out = self.run_main("--workers", "1")
        self.assertEqual(code, 0, out)
        got = {read_bytes(os.path.join(self.out, "recordings/3", n)) for n in ("dup.flac", "dup__2.flac")}
        self.assertEqual(got, {body_for("/same/1.flac"), body_for("/same/2.flac")})
        code, out = self.run_main("--workers", "1")
        self.assertIn("already downloaded:      2", out)

    def test_nothing_found(self):
        code, out = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("No backup CSV files", out)

class TestUnits(unittest.TestCase):
    def test_dates(self):
        for v in ("2026-03-27 08:04:00", "03/27/2026 08:04:00", "03/27/26 08:04:00", "2026-03-27 08:04:00.123"):
            self.assertEqual(abd.parse_datetime(v).strftime("%Y%m%d%H%M"), "202603270804", v)
        self.assertIsNone(abd.parse_datetime("garbage"))
        self.assertIsNone(abd.parse_datetime(""))

    def test_safe_name(self):
        self.assertEqual(abd.safe_name('../a:b*c?.flac'), "a_b_c_.flac")
        self.assertEqual(abd.safe_name("CON.wav"), "_CON.wav")
        self.assertEqual(abd.safe_name("x.003.flac"), "x.003.flac")
        self.assertEqual(abd.safe_name(".."), "")

    def test_csv_names(self):
        for n, kind in (("recordings.0001.csv", "recordings"), ("recordings.csv", "recordings"),
                        ("recordings_001.csv", "recordings"), ("recording_tags.0001.csv", "recording_tags"),
                        ("pattern_matching_rois.0002.csv", "pattern_matching_rois")):
            self.assertEqual(abd.CSV_NAME.match(n).group("kind"), kind, n)

if __name__ == "__main__":
    unittest.main()


class TestPacing(Base):
    """The 30 s rule, with the floor lowered to IV seconds so the suite stays fast."""
    IV = 0.4
    TOL = 0.08  # arrival jitter on a busy CI runner; the gate itself never releases early (mutations below stay red)

    def setUp(self):
        super().setUp()
        self._old = abd.MIN_INTERVAL
        abd.MIN_INTERVAL = self.IV

    def tearDown(self):
        abd.MIN_INTERVAL = self._old
        super().tearDown()

    def gaps(self):
        t = Handler.times
        return [b - a for a, b in zip(t, t[1:])]

    def rows(self, n, prefix="/internal/p/"):
        return [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "%s%d.flac" % (prefix, i), "%d.flac" % i)) for i in range(1, n + 1)]

    def test_starts_are_spaced_across_workers(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(6))
        code, out = self.run_main("--workers", "4")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(Handler.times), 6)
        self.assertGreaterEqual(min(self.gaps()), self.IV - self.TOL, self.gaps())
        self.assertIn("One download starts every 0.4 seconds", out)
        self.assertIn("Estimated time:", out)

    def test_retries_wait_their_turn(self):
        rows = [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "/flaky/%d.flac" % i, "%d.flac" % i)) for i in range(1, 4)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        code, out = self.run_main("--workers", "4")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(Handler.times), 6)  # each file: one 429 + one retry
        self.assertGreaterEqual(min(self.gaps()), self.IV - self.TOL, self.gaps())

    def test_resume_and_416_restart_are_paced(self):
        url = media(self.base, "/internal/r/9.flac", "r9.flac")
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [rec(9, 5, "2026-01-01 00:00:00", url)])
        full = body_for("/internal/r/9.flac")
        os.makedirs(os.path.join(self.out, "recordings", "5"))
        with open(os.path.join(self.out, "recordings", "5", "recording-9.part"), "wb") as f:
            f.write(full + b"extra")  # longer than the file: server answers 416, script restarts from 0
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual([x[2] is not None for x in Handler.log], [True, False])
        self.assertGreaterEqual(min(self.gaps()), self.IV - self.TOL, self.gaps())

    def test_finished_files_cost_no_request_and_no_wait(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(4))
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        Handler.times = []
        Handler.log = []
        abd.MIN_INTERVAL = 5.0  # a re-run that slept per skipped file (or for the saved last start) would take >= 5 s
        t0 = time.monotonic()
        code, out2 = self.run_main()
        took = time.monotonic() - t0
        self.assertEqual(code, 0, out2)
        self.assertEqual(Handler.log, [])
        self.assertIn("already downloaded:      4", out2)
        self.assertLess(took, 2.0, out2)

    def test_interval_below_floor_is_refused(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(1))
        abd.MIN_INTERVAL = 30.0
        code, out = self.run_main("--interval", "10")
        self.assertEqual(code, 2, out)
        self.assertIn("at least 30", out)
        self.assertEqual(Handler.log, [])
        code, out = self.run_main("--interval", "nan")
        self.assertEqual(code, 2, out)

    def test_interval_can_be_raised(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(3))
        code, out = self.run_main("--interval", "0.8")
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(min(self.gaps()), 0.8 - self.TOL, self.gaps())

    def test_last_start_is_remembered_between_runs(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(2))
        abd.MIN_INTERVAL = 0.8
        code, out = self.run_main("--limit", "1")
        self.assertEqual(code, 0, out)
        code, out = self.run_main()  # immediately again: the 2nd file must still wait for the 1st's slot
        self.assertEqual(code, 0, out)
        self.assertEqual(len(Handler.times), 2)
        self.assertGreaterEqual(self.gaps()[0], 0.8 - self.TOL, self.gaps())

    def test_future_saved_time_waits_at_most_one_interval(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(1))
        os.makedirs(self.out)
        with open(os.path.join(self.out, abd.RATE_FILE), "w") as f:
            f.write("%f\n" % (time.time() + 86400))  # clock moved back a day since the last run
        abd.MIN_INTERVAL = 0.5
        t0 = time.monotonic()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_dry_run_heads_are_paced_and_print_duration(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(10))
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual([m for m, _p, _r, _u in Handler.log], ["HEAD"] * 3)  # default --sample 3
        self.assertGreaterEqual(min(self.gaps()), self.IV - self.TOL, self.gaps())
        self.assertIn("Estimated time: about 4 seconds for 10 files", out)

    def test_dry_run_warns_when_links_expire_first(self):
        soon = str(int(time.time()) + 615)  # links expire in ~10 minutes
        rows = [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "/internal/e/%d.flac" % i, "%d.flac" % i, exp=soon))
                for i in range(1, 51)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        abd.MIN_INTERVAL = 30.0  # 50 files x 30 s = 25 min > 10 min
        code, out = self.run_main("--dry-run", "--sample", "0")
        self.assertEqual(code, 0, out)
        self.assertIn("Estimated time: about 25 minutes for 50 files", out)
        self.assertIn("about 2,880 a day".replace(",", ""), out.replace(",", ""))
        self.assertIn("before all files can be downloaded (about 20 of 50 will fit)", out)
        self.assertEqual(Handler.log, [])
