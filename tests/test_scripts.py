"""Behaviour tests for the download scripts, run against tests/fake_server.py.

    python3 -m unittest discover -s tests -v                       # the bash script (macOS / Linux)
    ABD_SCRIPT=ps1 python -m unittest discover -s tests -v         # the PowerShell script (Windows)
    ABD_SCRIPT=ps1 ABD_PWSH=pwsh python -m unittest discover -s tests -v

Each test copies the script into a temp folder with the 30-second floor lowered to IV seconds (the published script has
no way to go below 30). Python is only needed to RUN THE TESTS; the scripts themselves need nothing but the OS.
"""
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_server as fs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIND = os.environ.get("ABD_SCRIPT", "sh")
SCRIPT = os.path.join(ROOT, "arbimon_backup_download." + KIND)
IV = int(os.environ.get("ABD_TEST_INTERVAL", "1"))
EXP = str(int(time.time()) + 3 * 86400)
EXP_PAST = str(int(time.time()) - 86400)
REC_HEADER = ["recording_id", "site_id", "datetime", "datetime_utc", "sample_rate", "duration", "samples", "bit_rate",
              "sample_encoding", "upload_time", "meta", "url"]

def media(base, path, name, token="ok", exp=EXP):
    return "%s%s?%s" % (base, path, urllib.parse.urlencode({"stream-token": token, "exp": exp, "download-name": name}))

def rec(rid, site, dt, url, meta='{"qc":{"v":1,"peak":0},"note":"a,b"}'):
    return [rid, site, dt, dt, 24000, 60, 1440000, 384000, "pcm_s16le", dt, meta, url]

def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()

def script_copy(dst_dir, floor):
    with open(SCRIPT, encoding="utf-8") as f:
        src = f.read()
    if KIND == "sh":
        pat = r"^MIN_INTERVAL=30\b"
        new = "MIN_INTERVAL=%d" % floor
    else:
        pat = r"^\$MinInterval = 30\b"
        new = "$MinInterval = %d" % floor
    out, n = re.subn(pat, new, src, count=1, flags=re.M)
    assert n == 1, "could not find the interval constant in " + SCRIPT
    p = os.path.join(dst_dir, os.path.basename(SCRIPT))
    with open(p, "w", encoding="utf-8", newline="\n" if KIND == "sh" else "\r\n") as f:
        f.write(out)
    return p

PS_OPTION = {"--backup-dir": "-BackupDir", "--out": "-Out", "--only": "-Only", "--limit": "-Limit", "--dry-run": "-DryRun",
             "--sample": "-Sample", "--interval": "-Interval", "--timeout": "-Timeout", "--retries": "-Retries",
             "--frobnicate": "-Frobnicate"}

def command(script):
    if KIND == "sh":
        return ["bash", script]
    exe = os.environ.get("ABD_PWSH", "powershell")
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script]

class Base(unittest.TestCase):
    floor = IV

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = fs.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        fs.reset()
        self.dir = tempfile.mkdtemp(prefix="abd-")
        self.backup = os.path.join(self.dir, "backup")
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.backup)
        self.script = script_copy(self.dir, self.floor)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, *extra, backup=None, timeout=300):
        opts = ["--backup-dir", backup or self.backup, "--out", self.out] + list(extra)
        if KIND == "ps1":
            opts = [PS_OPTION.get(a, a) for a in opts]
        args = command(self.script) + opts
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def files(self):
        found = {}
        for root, _d, names in os.walk(self.out):
            for n in names:
                p = os.path.relpath(os.path.join(root, n), self.out).replace(os.sep, "/")
                found[p] = os.path.getsize(os.path.join(root, n))
        return found

    def gaps(self):
        t = fs.Handler.times
        return [b - a for a, b in zip(t, t[1:])]

    def assertSpaced(self, at_least):
        g = self.gaps()
        self.assertTrue(g, "fewer than two requests")
        self.assertGreaterEqual(min(g), at_least - 0.05, g)

class TestDownload(Base):
    def make_backup(self):
        b = self.base
        write_csv(os.path.join(self.backup, "sites.0001.csv"), ["site_id", "name"], [[11, "North, ridge"], [12, "South"]])
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [
            rec(1, 11, "2026-03-27 08:04:00", media(b, "/internal/a/1.flac", "rec.flac")),
            rec(2, 12, "03/27/2026 08:05:00", media(b, "/internal/a/2.flac", "rec.flac")),
            rec(3, 11, "03/27/26 08:06:00", media(b, "/internal/a/3.flac", "long.003.flac"), meta='{"x":"line one\nline two, with comma"}'),
            rec(4, 11, "2026-03-27 08:07:00", media(b, "/internal/a/4.wav", "long.004.wav")),
        ])
        write_csv(os.path.join(self.backup, "recordings.0002.csv"), REC_HEADER, [
            rec(5, 11, "2026-03-27 08:08:00", media(b, "/internal/a/5.flac", "rec.flac")),
            rec(6, 12, "2026-03-27 08:09:00", b + "/legacy-api/project/p/recordings/download/6"),
            rec(7, 12, "2026-03-27 08:10:00", ""),
            rec(8, 12, "12/31/2019 23:59:59", b + "/plain/8.flac"),
            rec(9, 12, "2026-03-27 08:11:00", media(b, "/flaky/9.flac", "flaky.flac")),
        ])
        write_csv(os.path.join(self.backup, "templates.0001.csv"), ["template_id", "template_name", "url"],
                  [[71, 'Song "A", male', b + "/dl/TPL1"], [72, "t2", b + "/dl/TPL2"]])

    def test_full_run_then_rerun(self):
        self.make_backup()
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        f = self.files()
        for name in ["recordings/11/rec.flac", "recordings/12/rec.flac", "recordings/11/long.003.flac",
                     "recordings/11/long.004.wav", "recordings/11/rec__5.flac", "recordings/12/20191231_235959_8.flac",
                     "recordings/12/flaky.flac", "templates/TPL1.png", "templates/TPL2.png", "recordings/sites.csv"]:
            self.assertIn(name, f, out)
        self.assertEqual(read_bytes(os.path.join(self.out, "recordings/11/rec.flac")), fs.body_for("/internal/a/1.flac"))
        self.assertEqual(read_bytes(os.path.join(self.out, "recordings/11/rec__5.flac")), fs.body_for("/internal/a/5.flac"))
        self.assertEqual(read_bytes(os.path.join(self.out, "templates/TPL1.png")), fs.body_for("/dl/TPL1"))
        self.assertFalse([p for p in f if p.endswith(".part")], f)
        self.assertIn("soundscapes", out)
        self.assertIn("none in this backup", out)
        self.assertIn("need an arbimon.org sign-in: 1", out)
        self.assertIn("no link in the backup:   1", out)
        self.assertIn("downloaded now:          9", out)
        report = [p for p in f if p.startswith("download-report-")]
        self.assertEqual(len(report), 1, f)
        with open(os.path.join(self.out, report[0]), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        legacy = [r for r in rows if r["status"] == "legacy"]
        self.assertEqual([r["id"] for r in legacy], ["6"])
        self.assertTrue(legacy[0]["url"].endswith("/recordings/download/6"))
        self.assertEqual([r["id"] for r in rows if r["status"] == "no_url"], ["7"])
        self.assertTrue(all(ua.startswith("arbimon-backup-download/") for _m, _p, _r, ua in fs.Handler.log))
        self.assertNotIn("stream-token=ok", out)
        with open(os.path.join(self.out, "recordings/sites.csv"), encoding="utf-8") as fh:
            sites = fh.read()
        self.assertIn('11,"North, ridge",4', sites)
        self.assertIn("12,South,3", sites)
        fs.reset()
        code, out2 = self.run_script()
        self.assertEqual(code, 0, out2)
        self.assertIn("downloaded now:          0", out2)
        self.assertIn("already downloaded:      9", out2)
        self.assertEqual(fs.Handler.log, [])

    def test_limit_and_extracted_subfolder(self):
        self.make_backup()
        sub = os.path.join(self.dir, "outer")
        shutil.copytree(self.backup, os.path.join(sub, "my-project_export"))
        code, out = self.run_script("--limit", "2", backup=sub)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p for p in self.files() if p.endswith(".flac")), ["recordings/11/rec.flac", "recordings/12/rec.flac"])

    def test_dry_run_downloads_nothing(self):
        self.make_backup()
        code, out = self.run_script("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("Estimated size", out)
        self.assertIn("Estimated time", out)
        self.assertFalse([p for p in self.files() if not os.path.basename(p).startswith(".arbimon")], self.files())
        self.assertTrue(fs.Handler.log)
        self.assertTrue(all(m == "HEAD" for m, _p, _r, _u in fs.Handler.log), fs.Handler.log)
        self.assertEqual(len(fs.Handler.log), 3)

    def test_only_templates(self):
        self.make_backup()
        code, out = self.run_script("--only", "templates")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p for p in self.files() if p.endswith(".png")), ["templates/TPL1.png", "templates/TPL2.png"])
        self.assertFalse([p for p in self.files() if p.startswith("recordings/")])

class TestFailureModes(Base):
    def test_resume_partial_part_file(self):
        url = media(self.base, "/internal/r/1.flac", "big.flac")
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [rec(1, 5, "2026-01-01 00:00:00", url)])
        full = fs.body_for("/internal/r/1.flac")
        os.makedirs(os.path.join(self.out, "recordings", "5"))
        with open(os.path.join(self.out, "recordings", "5", "recording-1.part"), "wb") as f:
            f.write(full[:20000])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(read_bytes(os.path.join(self.out, "recordings/5/big.flac")), full)
        self.assertIn(("GET", "/internal/r/1.flac", "bytes=20000-"), [x[:3] for x in fs.Handler.log])

    def test_416_restarts_from_zero(self):
        url = media(self.base, "/internal/r/9.flac", "r9.flac")
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [rec(9, 5, "2026-01-01 00:00:00", url)])
        full = fs.body_for("/internal/r/9.flac")
        os.makedirs(os.path.join(self.out, "recordings", "5"))
        with open(os.path.join(self.out, "recordings", "5", "recording-9.part"), "wb") as f:
            f.write(full + b"extra")
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(read_bytes(os.path.join(self.out, "recordings/5/r9.flac")), full)
        self.assertEqual([x[2] is not None for x in fs.Handler.log], [True, False])
        self.assertSpaced(IV)

    def test_401_stops_with_expired_message(self):
        rows = [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "/internal/x/%d.flac" % i, "%d.flac" % i, token="bad"))
                for i in range(1, 30)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        code, out = self.run_script()
        self.assertEqual(code, 3, out)
        self.assertIn("have expired", out)
        self.assertEqual(out.count("have expired"), 1)
        self.assertEqual(len(fs.Handler.log), 1, "kept going after a 401")
        self.assertFalse([p for p in self.files() if p.endswith(".flac")])

    def test_past_exp_warns_but_still_tries(self):
        url = media(self.base, "/internal/p/1.flac", "p.flac", exp=EXP_PAST)
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [rec(1, 1, "2026-01-01 00:00:00", url)])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("look expired", out)

    def test_expired_short_links_stop(self):
        write_csv(os.path.join(self.backup, "templates.0001.csv"), ["template_id", "template_name", "url"],
                  [[i, "t", self.base + "/dl/gone"] for i in range(1, 8)])
        code, out = self.run_script()
        self.assertEqual(code, 3, out)
        self.assertIn("have expired", out)
        self.assertEqual(len(fs.Handler.log), 3)

    def test_one_dead_short_link_is_a_failure_not_expiry(self):
        write_csv(os.path.join(self.backup, "templates.0001.csv"), ["template_id", "template_name", "url"],
                  [[1, "t", self.base + "/dl/OK1"], [2, "t", self.base + "/dl/gone"], [3, "t", self.base + "/dl/OK3"]])
        code, out = self.run_script()
        self.assertEqual(code, 1, out)
        self.assertIn("failed:                  1", out)
        self.assertNotIn("have expired", out)

    def test_redirect_to_login_found_while_downloading(self):
        url = self.base + "/legacy-api/elsewhere/9"   # not recognisable from the URL: found from the 302
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [rec(1, 1, "2026-01-01 00:00:00", url)])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertIn("need an arbimon.org sign-in: 1", out)
        self.assertIn("failed:                  0", out)

    def test_same_name_same_size_in_one_site(self):
        b = self.base
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [
            rec(1, 3, "2026-01-01 00:00:00", media(b, "/same/1.flac", "x.flac")),
            rec(2, 3, "2026-01-01 00:01:00", media(b, "/same/2.flac", "x.flac"))])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        got = {read_bytes(os.path.join(self.out, "recordings/3", n)) for n in os.listdir(os.path.join(self.out, "recordings/3"))}
        self.assertEqual(got, {fs.body_for("/same/1.flac"), fs.body_for("/same/2.flac")})
        code, out = self.run_script()
        self.assertIn("already downloaded:      2", out)

    def test_unsafe_names_are_cleaned(self):
        b = self.base
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, [
            rec(1, "../x", "2026-01-01 00:00:00", media(b, "/internal/u/1.flac", "../a:b*c?.flac")),
            rec(2, 4, "2026-01-01 00:00:00", media(b, "/internal/u/2.wav", "CON.wav"))])
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        f = self.files()
        self.assertIn("recordings/x/a_b_c_.flac", f, f)
        self.assertIn("recordings/4/_CON.wav", f, f)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x")))

    def test_nothing_found(self):
        code, out = self.run_script()
        self.assertEqual(code, 2, out)
        self.assertIn("No backup CSV files", out)

    def test_bad_option(self):
        code, out = self.run_script("--frobnicate")
        # bash: our usage message, exit 2. PowerShell rejects an unknown -Parameter itself (exit 1) before the script runs.
        self.assertIn(code, (2,) if KIND == "sh" else (1, 2), out)
        self.assertEqual(fs.Handler.log, [])

class TestPacing(Base):
    def rows(self, n, prefix="/internal/p/"):
        return [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "%s%d.flac" % (prefix, i), "%d.flac" % i)) for i in range(1, n + 1)]

    def test_starts_are_spaced(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(4))
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(fs.Handler.times), 4)
        self.assertSpaced(IV)
        self.assertIn("One download starts every %d seconds" % IV, out)

    def test_retries_wait_their_turn(self):
        rows = [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "/flaky/%d.flac" % i, "%d.flac" % i)) for i in range(1, 3)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(fs.Handler.times), 4)
        self.assertSpaced(IV)

    def test_finished_files_cost_no_request_and_no_wait(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(3))
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        fs.reset()
        self.script = script_copy(self.dir, 20)   # a re-run that slept per skipped file (or for the last start) would take >= 20 s
        t0 = time.monotonic()
        code, out2 = self.run_script()
        took = time.monotonic() - t0
        self.assertEqual(code, 0, out2)
        self.assertEqual(fs.Handler.log, [])
        self.assertIn("already downloaded:      3", out2)
        self.assertLess(took, 15, out2)

    def test_interval_below_floor_is_refused(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(1))
        self.script = script_copy(self.dir, 30)
        code, out = self.run_script("--interval", "10")
        self.assertEqual(code, 2, out)
        self.assertIn("at least 30", out)
        self.assertEqual(fs.Handler.log, [])

    def test_interval_can_be_raised(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(3))
        code, out = self.run_script("--interval", str(IV + 1))
        self.assertEqual(code, 0, out)
        self.assertSpaced(IV + 1)

    def test_last_start_is_remembered_between_runs(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(2))
        self.script = script_copy(self.dir, 2)
        code, out = self.run_script("--limit", "1")
        self.assertEqual(code, 0, out)
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(fs.Handler.times), 2)
        self.assertGreaterEqual(self.gaps()[0], 2 - 0.05, self.gaps())

    def test_future_saved_time_waits_at_most_one_interval(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(1))
        os.makedirs(self.out)
        with open(os.path.join(self.out, ".arbimon-download-rate"), "w") as f:
            f.write("%d\n" % (time.time() + 86400))
        self.script = script_copy(self.dir, 2)
        t0 = time.monotonic()
        code, out = self.run_script(timeout=60)
        self.assertEqual(code, 0, out)
        self.assertLess(time.monotonic() - t0, 10)

    def test_dry_run_heads_are_paced_and_print_duration(self):
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(10))
        code, out = self.run_script("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual([m for m, _p, _r, _u in fs.Handler.log], ["HEAD"] * 3)
        self.assertSpaced(IV)
        self.assertIn("Estimated time: about %d seconds for 10 files" % (10 * IV), out)

    def test_dry_run_warns_when_links_expire_first(self):
        soon = str(int(time.time()) + 615)
        rows = [rec(i, 1, "2026-01-01 00:00:00", media(self.base, "/internal/e/%d.flac" % i, "%d.flac" % i, exp=soon))
                for i in range(1, 51)]
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, rows)
        self.script = script_copy(self.dir, 30)
        code, out = self.run_script("--dry-run", "--sample", "0")
        self.assertEqual(code, 0, out)
        self.assertIn("Estimated time: about 25 minutes for 50 files", out)
        self.assertIn("about 2880 a day", out)
        self.assertIn("before all files can be downloaded (about 20 of 50 will fit)", out)
        self.assertEqual(fs.Handler.log, [])

    def test_python_state_file_is_honoured(self):
        # Someone who started with the 1.0.0 Python script and switches to this one: nothing is downloaded twice.
        write_csv(os.path.join(self.backup, "recordings.0001.csv"), REC_HEADER, self.rows(2))
        os.makedirs(os.path.join(self.out, "recordings", "1"))
        with open(os.path.join(self.out, "recordings", "1", "1.flac"), "wb") as f:
            f.write(fs.body_for("/internal/p/1.flac"))
        with open(os.path.join(self.out, ".arbimon-download-state.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"kind": "recordings", "id": "1", "path": "recordings/1/1.flac", "size": %d}\n' % len(fs.body_for("/internal/p/1.flac")))
        code, out = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual([p for _m, p, _r, _u in fs.Handler.log], ["/internal/p/2.flac"])
        self.assertIn("already downloaded:      1", out)

if __name__ == "__main__":
    unittest.main()