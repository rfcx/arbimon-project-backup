"""Red-test probe: break the script one way at a time (in a copy) and confirm the test suite FAILS.
The unmutated control must PASS. Run: python3 tests/red_probe.py [sh|ps1]   (ps1 needs Windows / pwsh)"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIND = sys.argv[1] if len(sys.argv) > 1 else "sh"
SRC = os.path.join(ROOT, "arbimon_backup_download." + KIND)
MUTATIONS = {
    "sh": [
        ("control (no mutation)", None, None),
        ("default curl User-Agent", 'curl -sS -A "$UA" -H \'Accept: */*\' --connect-timeout 30 --speed-time "$TIMEOUT" --speed-limit 1 -D "$hdr" -o "$tmp" "$url"',
         'curl -sS -H \'Accept: */*\' --connect-timeout 30 --speed-time "$TIMEOUT" --speed-limit 1 -D "$hdr" -o "$tmp" "$url"'),
        ("401 not treated as expiry", "      401) EXPIRED=1; return ;;", "      401) problem \"$kind\" \"$id\" \"$site\" failed 401 \"\"; return ;;"),
        ("login redirect not legacy", "if header_value \"$hdr\" location | grep -qi login;", "if false;"),
        ("no per-name dedupe", "    claimed_by_other \"$folder/$cand\" \"$kind\" \"$id\" && continue\n", ""),
        ("resume ignores .part", '    offset=0; [ -f "$part" ] && offset=$(file_size "$part")', "    offset=0; rm -f \"$part\""),
        ("re-run ignores state", '  if [ -n "$donepath" ] && [ -f "$OUT_ABS/$donepath" ]; then', "  if false; then"),
        ("no retry on 429", "      0|200|206|408|425|429|500|502|503|504) ;;", "      0|200|206|408|425|500|502|503|504) ;;"),
        ("quoted newline breaks a record", "  if (nq % 2 == 1) next ", "  if (0) next "),
        ("unsafe names kept", '      sub(/^[ ]+/, "", n); sub(/[. ]+$/, "", n)', '      n = $0'),
        ("gate bypassed for downloads", "    gate_wait || return\n", ""),
        ("gate bypassed for dry-run HEADs", "    gate_wait || break\n", ""),
        ("interval floor removed", 'if ! is_int "$INTERVAL" || [ "$INTERVAL" -lt "$MIN_INTERVAL" ]; then', "if ! is_int \"$INTERVAL\"; then"),
        ("persisted start ignored", 'LAST=""\nif [ -f "$RATE" ]; then', 'LAST=""\nif false; then'),
        ("future saved time not clamped", '[ -n "$LAST" ] && [ "$LAST" -gt "$NOW" ] && LAST=$NOW', ":"),
        ("Python state file ignored", '[ -f "$OLD_STATE" ] || OLD_STATE=/dev/null', "OLD_STATE=/dev/null"),
        ("dry-run sample default 10", "SAMPLE=3\n", "SAMPLE=10\n"),
    ],
    "ps1": [
        ("control (no mutation)", None, None),
        ("default curl User-Agent", '"-sS", "-A", $UA, ', '"-sS", '),
        ("401 not treated as expiry", '            401 { $script:Expired = $true; return }', '            401 { Problem $it "failed" "401"; return }'),
        ("no retry on 429", "{ $_ -in 0, 200, 206, 408, 425, 429, 500, 502, 503, 504 } { }", "{ $_ -in 0, 200, 206, 408, 425, 500, 502, 503, 504 } { }"),
        ("resume ignores .part", '$offset = 0; if (Test-Path -LiteralPath $part) { $offset = (Get-Item -LiteralPath $part).Length }',
         '$offset = 0; if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force }'),
        ("gate bypassed for downloads", "    while ($true) {\n        GateWait\n", "    while ($true) {\n"),
        ("interval floor removed", "-or $Interval -lt $MinInterval) {", ") {"),
        ("persisted start ignored", "if (Test-Path -LiteralPath $RateFile) {\n    $t = 0.0", "if ($false) {\n    $t = 0.0"),
        ("no per-name dedupe", "        if ($owner -and $owner -ne $key) { continue }\n", ""),
    ],
}[KIND]

def command():
    if KIND == "sh":
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]

src = open(SRC, encoding="utf-8").read()
ok = True
for label, old, new in MUTATIONS:
    d = tempfile.mkdtemp(prefix="abd-red-")
    body = src
    if old is not None:
        assert src.count(old) == 1, ("anchor not unique/absent", label, src.count(old))
        body = src.replace(old, new)
    with open(os.path.join(d, os.path.basename(SRC)), "w", encoding="utf-8") as f:
        f.write(body)
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(d, "tests"), ignore=shutil.ignore_patterns("__pycache__"))
    try:
        r = subprocess.run(command(), cwd=d, capture_output=True, text=True, timeout=600)
        rc = r.returncode
        last = [l for l in r.stderr.strip().splitlines() if l.startswith(("OK", "FAILED"))]
        verdict = last[-1] if last else "?"
    except subprocess.TimeoutExpired:
        rc, verdict = -1, "TIMEOUT (hung) = red"
    good = (rc == 0) == (old is None)
    ok = ok and good
    print("%-36s rc=%d %-28s %s" % (label, rc, verdict, "as expected" if good else "*** UNEXPECTED ***"), flush=True)
    shutil.rmtree(d, ignore_errors=True)
sys.exit(0 if ok else 1)