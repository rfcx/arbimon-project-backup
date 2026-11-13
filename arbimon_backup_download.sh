#!/usr/bin/env bash
# Arbimon project backup: bulk download of recordings, templates and soundscapes (macOS and Linux).
#
# A project backup is a ZIP of CSV files. The audio is not inside the ZIP: the
# recordings, templates and soundscapes CSVs each have a "url" column that links
# to the file. This script reads those CSVs and downloads every linked file.
#
#   bash arbimon_backup_download.sh                 # run inside the extracted backup folder
#   bash arbimon_backup_download.sh --backup-dir ~/Downloads/my-backup --out ~/Music/my-project
#   bash arbimon_backup_download.sh --dry-run       # count and estimate, download nothing
#
# Needs only what macOS and Linux already have: bash, curl, awk. Nothing to install.
# Safe to stop (Ctrl+C) and re-run: finished files are skipped and a half-finished
# file continues where it stopped.
#
# To keep the load on Arbimon's servers low, it starts at most one request every
# 30 seconds (about 2,800 files a day). A large project takes days.
#
# Help:    https://arbimon.org/support/arbimon/settings/downloading-recordings-from-a-project-backup/
# Source:  https://github.com/rfcx/arbimon-project-backup
# Support: contact@arbimon.org

VERSION="2.0.0"
MIN_INTERVAL=30          # seconds between the START of two requests; --interval may only raise it
RETRIES=5
TIMEOUT=120              # seconds without any data before a request is retried
SAMPLE=3
BACKOFF_BASE=2
BACKOFF_CAP=60
UA="arbimon-backup-download/$VERSION (+https://arbimon.org)"
STATE_NAME=".arbimon-download-state.tsv"
OLD_STATE_NAME=".arbimon-download-state.jsonl"   # written by the 1.0.0 Python script
RATE_NAME=".arbimon-download-rate"
US=$(printf '\037')
TAB=$(printf '\t')
EXPIRED_MESSAGE="The download links in this backup have expired (they work for about 7 days after the backup is made).
Please request a new project backup in Arbimon (Project Settings > Project backup) and run this
script on the new backup. Files you already downloaded are kept and will be skipped."

say() { printf '%s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: bash arbimon_backup_download.sh [options]

Download the recordings, templates and soundscapes linked from an Arbimon project backup.

  --backup-dir PATH   the extracted backup folder, or the backup .zip (default: current folder)
  --out PATH          where to save files (default: ./downloads)
  --only KIND         recordings, templates or soundscapes (repeatable)
  --limit N           stop after N files (for a quick test)
  --dry-run           count files and estimate size and time; download nothing
  --sample N          files to check for the --dry-run size estimate (default 3)
  --interval S        seconds between the start of two downloads; at least 30 (default 30)
  --timeout S         seconds without data before a request is retried (default 120)
  --retries N         retries per file on temporary errors (default 5)
  --version           print the version

Exit codes: 0 done, 1 some files failed, 2 bad arguments / nothing to do, 3 links expired, 130 stopped.
EOF
}

# ------------------------------------------------------------------ arguments
BACKUP_DIR="."
OUT="downloads"
ONLY=""
LIMIT=""
DRY_RUN=0
INTERVAL=""
need_value() { if [ $# -lt 2 ] || [ -z "$2" ]; then say "Missing value for $1"; exit 2; fi; }
while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir) need_value "$@"; BACKUP_DIR="$2"; shift 2 ;;
    --out) need_value "$@"; OUT="$2"; shift 2 ;;
    --only) need_value "$@"
      case "$2" in recordings|templates|soundscapes) ONLY="$ONLY $2" ;; *) say "--only must be recordings, templates or soundscapes"; exit 2 ;; esac
      shift 2 ;;
    --limit) need_value "$@"; LIMIT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --sample) need_value "$@"; SAMPLE="$2"; shift 2 ;;
    --interval) need_value "$@"; INTERVAL="$2"; shift 2 ;;
    --timeout) need_value "$@"; TIMEOUT="$2"; shift 2 ;;
    --retries) need_value "$@"; RETRIES="$2"; shift 2 ;;
    --version) say "arbimon_backup_download.sh $VERSION"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) say "Unknown option: $1"; usage; exit 2 ;;
  esac
done
is_int() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
for v in "$SAMPLE" "$TIMEOUT" "$RETRIES"; do is_int "$v" || { say "Options --sample, --timeout and --retries take whole numbers."; exit 2; }; done
if [ -n "$LIMIT" ] && ! is_int "$LIMIT"; then say "--limit takes a whole number."; exit 2; fi
[ -z "$INTERVAL" ] && INTERVAL=$MIN_INTERVAL
if ! is_int "$INTERVAL" || [ "$INTERVAL" -lt "$MIN_INTERVAL" ]; then
  say "--interval must be a whole number of seconds, at least $MIN_INTERVAL (to keep the load on Arbimon's servers low)."
  exit 2
fi
KINDS="${ONLY:- recordings templates soundscapes}"
command -v curl >/dev/null 2>&1 || { say "This script needs curl, which was not found. Install curl and try again."; exit 2; }

# ------------------------------------------------------------------ helpers (awk does the text work: it is everywhere)
human_bytes() { awk -v n="$1" 'BEGIN{split("B KB MB GB TB",u," ");i=1;while(n>=1000&&i<5){n/=1000;i++};printf (i==1?"%d %s":"%.1f %s"), n, u[i]}'; }
human_duration() { awk -v s="$1" 'BEGIN{s=int(s+0.5);if(s<120)printf "%d seconds",s;else if(s<7200)printf "%d minutes",int(s/60+0.5);else if(s<172800)printf "%.1f hours",s/3600;else printf "%.1f days",s/86400}'; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
file_size() { wc -c < "$1" | tr -d ' '; }
epoch_to_local() { date -d "@$1" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || date -r "$1" '+%Y-%m-%d %H:%M %Z'; }
urldecode() { local s="${1//+/ }"; printf '%b' "${s//%/\\x}"; }
query_param() { printf '%s' "$1" | sed -n "s/^[^?]*?\(.*&\)\{0,1\}$2=\([^&#]*\).*/\2/p"; }

# A file name that is valid on Windows, macOS and Linux (no folders, no reserved names).
safe_name() {
  printf '%s' "$1" | LC_ALL=C tr '\001-\037\177' '_' | awk '
    { n = $0; sub(/.*[\/\\]/, "", n); gsub(/[<>:"|?*]/, "_", n)
      sub(/^[ ]+/, "", n); sub(/[. ]+$/, "", n)
      if (n == "" || n == "." || n == "..") { exit }
      stem = n; ext = ""
      if (match(n, /\.[^.]*$/) && RSTART > 1) { stem = substr(n, 1, RSTART - 1); ext = substr(n, RSTART + 1) }
      first = toupper(stem); sub(/\..*/, "", first)
      if (first ~ /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/) stem = "_" stem
      if (length(stem) > 150) stem = substr(stem, 1, 150)
      if (length(ext) > 12) ext = substr(ext, 1, 12)
      printf "%s", stem (ext != "" ? "." ext : "") }'
}

# CSV -> one line per item (fields separated by \037): kind id site url fallback-name url-kind.
# A real CSV reader: quoted fields, "" escapes, and line breaks inside quotes.
read -r -d '' AWK_ITEMS <<'AWK'
function csvsplit(rec, F,    n, s, i, val) {
  n = 0; s = rec
  while (1) {
    n++
    if (substr(s, 1, 1) == "\"") {
      val = ""; s = substr(s, 2)
      while (1) {
        i = index(s, "\"")
        if (i == 0) { val = val s; s = ""; break }
        val = val substr(s, 1, i - 1); s = substr(s, i + 1)
        if (substr(s, 1, 1) == "\"") { val = val "\""; s = substr(s, 2) } else break
      }
      F[n] = val
      i = index(s, ",")
      if (i == 0) break
      s = substr(s, i + 1)
    } else {
      i = index(s, ",")
      if (i == 0) { F[n] = s; break }
      F[n] = substr(s, 1, i - 1); s = substr(s, i + 1)
    }
  }
  return n
}
function trim(x) { gsub(/^[ \t]+|[ \t]+$/, "", x); return x }
function clean(x) { gsub(/[\t\r\n]/, " ", x); gsub(US, " ", x); return x }
function isnum(x) { return x ~ /^[0-9]+$/ }
function datefmt(w,    a, b, c, d, t, y, m, dd) {
  if (split(trim(w), a, " ") < 2) return ""
  d = a[1]; t = a[2]; sub(/\..*/, "", t)
  if (split(t, b, ":") != 3) return ""
  if (index(d, "-")) { if (split(d, c, "-") != 3) return ""; y = c[1]; m = c[2]; dd = c[3] }
  else if (index(d, "/")) { if (split(d, c, "/") != 3) return ""; m = c[1]; dd = c[2]; y = c[3]; if (length(y) == 2) y = ((y + 0) < 69 ? "20" : "19") y }
  else return ""
  if (!isnum(y) || !isnum(m) || !isnum(dd) || !isnum(b[1]) || !isnum(b[2]) || !isnum(b[3])) return ""
  if (m < 1 || m > 12 || dd < 1 || dd > 31 || b[1] > 23 || b[2] > 59 || b[3] > 61) return ""
  return sprintf("%04d%02d%02d_%02d%02d%02d_", y, m, dd, b[1], b[2], b[3])
}
BEGIN { US = sprintf("%c", 31) }
FNR == 1 { header = 1; rec = "" }
{
  if (rec != "") rec = rec "\n" $0; else rec = $0
  q = rec; nq = gsub(/"/, "", q)
  if (nq % 2 == 1) next                      # a quoted field continues on the next line
  r = rec; rec = ""; sub(/\r$/, "", r)
  if (header) {
    n = csvsplit(r, H)
    for (k in col) delete col[k]
    for (i = 1; i <= n; i++) { h = H[i]; sub(/^[^A-Za-z_]+/, "", h); col[tolower(trim(h))] = i }
    header = 0; next
  }
  if (r == "") next
  for (k in F) delete F[k]
  n = csvsplit(r, F)
  rown++
  id = (idcol in col) ? trim(F[col[idcol]]) : ""
  if (id == "") id = "row" rown
  if (kind == "sites") { print clean(id) US clean(("name" in col) ? trim(F[col["name"]]) : ""); next }
  url = ("url" in col) ? trim(F[col["url"]]) : ""
  site = ("site_id" in col) ? trim(F[col["site_id"]]) : ""
  when = ("datetime" in col) ? F[col["datetime"]] : (("date_created" in col) ? F[col["date_created"]] : "")
  if (url == "") uk = "empty"
  else if (url ~ /\/legacy-api\// && url ~ /\/recordings\/download\//) uk = "legacy"
  else if (url ~ /[?&]stream-token=/) uk = "media"
  else uk = "link"
  p = url; sub(/[?#].*/, "", p); sub(/.*\//, "", p); ext = ""
  if (match(p, /\.[^.]*$/)) ext = substr(p, RSTART, 8)
  print kind US clean(id) US clean(site) US clean(url) US clean(datefmt(when) id ext) US uk
}
AWK

# Backup CSVs of one kind, in order: kind.csv, kind.0001.csv, kind.0002.csv, kind_1.csv ...
csv_files() {
  local dir="$1" kind="$2" f b lb num
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    b="${f##*/}"; lb=$(lower "$b")
    case "$lb" in
      "$kind.csv") num=0 ;;
      "$kind".[0-9]*.csv|"$kind"_[0-9]*.csv) num="${lb#"$kind"?}"; num="${num%.csv}"; is_int "$num" || continue ;;
      *) continue ;;
    esac
    printf '%s\t%s\n' "$((10#$num))" "$f"
  done | sort -n -k1,1 | cut -f2-
}
has_backup_csvs() {
  local k
  for k in recordings templates soundscapes sites; do [ -n "$(csv_files "$1" "$k")" ] && return 0; done
  return 1
}

# ------------------------------------------------------------------ find the backup
OUT_ABS="$OUT"
case "$OUT_ABS" in /*) ;; *) OUT_ABS="$(pwd)/$OUT" ;; esac
SRC="$BACKUP_DIR"
case "$(lower "$SRC")" in
  *.zip)
    if [ -f "$SRC" ]; then
      command -v unzip >/dev/null 2>&1 || { say "Extract the backup ZIP first (double-click it), then run this script in that folder or pass --backup-dir."; exit 2; }
      mkdir -p "$OUT_ABS/.backup-csv" || exit 2
      unzip -o -q "$SRC" -d "$OUT_ABS/.backup-csv" || { say "Could not read $SRC."; exit 2; }
      SRC="$OUT_ABS/.backup-csv"
    fi ;;
esac
if [ -d "$SRC" ] && ! has_backup_csvs "$SRC"; then
  for d in "$SRC"/*/; do [ -d "$d" ] && has_backup_csvs "${d%/}" && { SRC="${d%/}"; break; }; done
fi
if [ ! -d "$SRC" ] || ! has_backup_csvs "$SRC"; then
  say "No backup CSV files found in $BACKUP_DIR."
  say "Extract the backup ZIP first, then run this script inside that folder (or pass --backup-dir)."
  exit 2
fi

mkdir -p "$OUT_ABS" || { say "Cannot create the output folder $OUT_ABS."; exit 2; }
STATE="$OUT_ABS/$STATE_NAME"
RATE="$OUT_ABS/$RATE_NAME"
WORK="$OUT_ABS/.arbimon-work"
mkdir -p "$WORK"
ITEMS="$WORK/items"
: > "$ITEMS"
for kind in $KINDS; do
  case $kind in recordings) idcol=recording_id ;; templates) idcol=template_id ;; soundscapes) idcol=soundscape_id ;; esac
  files=$(csv_files "$SRC" "$kind")
  [ -z "$files" ] && continue
  printf '%s\n' "$files" > "$WORK/files"
  set --
  while IFS= read -r f; do set -- "$@" "$f"; done < "$WORK/files"
  LC_ALL=C awk -v kind="$kind" -v idcol="$idcol" "$AWK_ITEMS" "$@" >> "$ITEMS" || { say "Could not read the $kind CSV files."; exit 2; }
done
if [ -n "$LIMIT" ]; then head -n "$LIMIT" "$ITEMS" > "$ITEMS.l"; mv -f "$ITEMS.l" "$ITEMS"; fi

# Join with what is already downloaded (this script's state file, and the 1.0.0 Python script's).
[ -f "$STATE" ] || : > "$STATE"
OLD_STATE="$OUT_ABS/$OLD_STATE_NAME"
[ -f "$OLD_STATE" ] || OLD_STATE=/dev/null
LC_ALL=C awk -v US="$US" '
  FILENAME == ARGV[1] { split($0, a, "\t"); done[a[1] SUBSEP a[2]] = a[3]; next }
  FILENAME == ARGV[2] {
    k = $0; i = $0; p = $0
    if (sub(/.*"kind": "/, "", k) && sub(/".*/, "", k) && sub(/.*"id": "/, "", i) && sub(/".*/, "", i) && sub(/.*"path": "/, "", p) && sub(/".*/, "", p)) {
      gsub(/\\\\/, "/", p); if (!((k SUBSEP i) in done)) done[k SUBSEP i] = p }
    next }
  { split($0, f, US); print $0 US done[f[1] SUBSEP f[2]] }' "$STATE" "$OLD_STATE" "$ITEMS" > "$ITEMS.j" && mv -f "$ITEMS.j" "$ITEMS"

say "arbimon_backup_download.sh $VERSION - backup: $SRC"
for kind in $KINDS; do
  if [ -z "$(csv_files "$SRC" "$kind")" ]; then printf '  %-12s none in this backup\n' "$kind"; continue; fi
  awk -F"$US" -v k="$kind" '$1==k{n++; if($6=="legacy")l++; if($6=="empty")e++}
    END{x=""; if(l)x=x sprintf(", %d need an arbimon.org sign-in", l); if(e)x=x sprintf(", %d without a link", e); printf "  %-12s %d files%s\n", k, n, x}' "$ITEMS"
done
TOTAL_ROWS=$(awk 'END{print NR}' "$ITEMS")
if [ "$TOTAL_ROWS" -eq 0 ]; then say "Nothing to download."; rm -rf "$WORK"; exit 0; fi
TOTAL=$(awk -F"$US" '$6=="media"||$6=="link"{n++}END{print n+0}' "$ITEMS")
ALREADY=$(awk -F"$US" '($6=="media"||$6=="link")&&$7!=""{n++}END{print n+0}' "$ITEMS")
FIRST_MEDIA=$(awk -F"$US" '$6=="media"{print $4; exit}' "$ITEMS")
EXP=$(query_param "$FIRST_MEDIA" exp)
is_int "$EXP" || EXP=""
NOW=$(date +%s)
if [ -n "$EXP" ]; then
  if [ "$EXP" -gt "$NOW" ]; then
    say "Links valid until $(epoch_to_local "$EXP") ($(awk -v s=$((EXP - NOW)) 'BEGIN{printf "%.1f", s/86400}') days left)."
  else
    say "Warning: the links look expired (valid until $(epoch_to_local "$EXP") by this computer's clock). Trying anyway."
  fi
fi

time_estimate() {   # $1 = files to download
  [ "$1" -gt 0 ] || return 0
  local need=$(( $1 * INTERVAL )) left
  say "Estimated time: about $(human_duration "$need") for $1 files (one file every $INTERVAL seconds, about $((86400 / INTERVAL)) a day)."
  if [ -n "$EXP" ]; then
    left=$(( EXP - $(date +%s) ))
    if [ "$left" -gt 0 ] && [ "$left" -lt "$need" ]; then
      say "Warning: the links expire in $(human_duration "$left"), before all files can be downloaded (about $((left / INTERVAL)) of $1 will fit)."
      say "  Let it run until the links expire, then request a new project backup and run the script"
      say "  on the new backup with the same --out folder: files you already have are skipped."
    fi
  fi
}

# ------------------------------------------------------------------ the 30-second rule
INTERRUPTED=0
trap 'INTERRUPTED=1' INT TERM
LAST=""
if [ -f "$RATE" ]; then
  LAST=$(head -n 1 "$RATE" 2>/dev/null); LAST="${LAST%%.*}"
  is_int "$LAST" || LAST=""
  [ -n "$LAST" ] && [ "$LAST" -gt "$NOW" ] && LAST=$NOW   # a saved time in the future: the clock moved; wait one interval at most
fi
first_wait() { if [ -n "$LAST" ]; then local w=$(( LAST + INTERVAL + 1 - $(date +%s) )); [ "$w" -gt 0 ] && echo "$w" && return; fi; echo 0; }
# Block until a request may start: more than INTERVAL whole seconds after the previous start, in this run or the last one.
gate_wait() {
  local now target
  if [ -n "$LAST" ]; then
    target=$(( LAST + INTERVAL ))
    while now=$(date +%s); [ "$now" -le "$target" ]; do
      [ "$INTERRUPTED" = 1 ] && return 1
      if [ $(( target - now )) -gt 1 ]; then sleep $(( target - now - 1 )); else sleep 0.2 2>/dev/null || sleep 1; fi
    done
  fi
  [ "$INTERRUPTED" = 1 ] && return 1
  LAST=$(date +%s)
  printf '%s\n' "$LAST" > "$RATE.tmp" 2>/dev/null && mv -f "$RATE.tmp" "$RATE" 2>/dev/null
  return 0
}

# ------------------------------------------------------------------ dry run
if [ "$DRY_RUN" = 1 ]; then
  TODO=$(( TOTAL - ALREADY ))
  awk -F"$US" -v s="$SAMPLE" '$6=="media"&&$7==""{u[++n]=$4} END{if(s>n)s=n; for(i=1;i<=s;i++){j=int((i-0.5)*n/s)+1; print u[j]}}' "$ITEMS" > "$WORK/sample"
  SN=$(awk 'END{print NR}' "$WORK/sample")
  [ "$SN" -gt 0 ] && say "Checking the size of $SN sample files (one request every $INTERVAL seconds)..."
  SUM=0; GOT=0
  while IFS= read -r u <&3; do
    gate_wait || break
    h=$(curl -sS -I -A "$UA" --connect-timeout 30 --max-time "$TIMEOUT" "$u" 2>/dev/null | tr -d '\r')
    code=$(printf '%s\n' "$h" | awk '/^HTTP\//{c=$2}END{print c}')
    if [ "$code" = 401 ]; then say "$EXPIRED_MESSAGE"; rm -rf "$WORK"; exit 3; fi
    len=$(printf '%s\n' "$h" | awk 'tolower($1)=="content-length:"{l=$2}END{print l}')
    if [ "$code" = 200 ] && is_int "$len"; then SUM=$(( SUM + len )); GOT=$(( GOT + 1 )); fi
  done 3< "$WORK/sample"
  [ "$INTERRUPTED" = 1 ] && { say ""; say "Stopped."; rm -rf "$WORK"; exit 130; }
  say "Dry run: $TODO to download, $ALREADY already downloaded, output folder $OUT_ABS"
  NMEDIA=$(awk -F"$US" '$6=="media"{n++}END{print n+0}' "$ITEMS")
  if [ "$GOT" -gt 0 ]; then
    AVG=$(( SUM / GOT ))
    say "Estimated size: about $(human_bytes $(( AVG * NMEDIA ))) for $NMEDIA audio files (average $(human_bytes "$AVG") each, from $GOT samples)."
  fi
  time_estimate "$TODO"
  rm -rf "$WORK"
  exit 0
fi

# ------------------------------------------------------------------ download
DOWNLOADED=0; SKIPPED=0; FAILED=0; LEGACY=0; NO_URL=0; BYTES=0; EXPIRED=0
SHORT_OK=0; SHORT_404=0; DONE_N=0
PROBLEMS="$WORK/problems"; : > "$PROBLEMS"
STARTED=$(date +%s)
csvq() { local s="${1//\"/\"\"}"; printf '"%s"' "$s"; }
problem() {   # kind id site status reason url
  printf '%s,%s,%s,%s,%s,%s\n' "$(csvq "$1")" "$(csvq "$2")" "$(csvq "$3")" "$4" "$(csvq "$5")" "$(csvq "$6")" >> "$PROBLEMS"
  case "$4" in
    legacy) LEGACY=$(( LEGACY + 1 )) ;;
    no_url) NO_URL=$(( NO_URL + 1 )) ;;
    failed) FAILED=$(( FAILED + 1 )); say "  failed: ${1%s} $2 ($5)" ;;
  esac
}
# Is this relative path already recorded for a different item? (case-insensitive, like Windows and macOS)
claimed_by_other() {
  LC_ALL=C awk -F'\t' -v p="$(lower "$1")" -v k="$2" -v i="$3" 'tolower($3)==p && !($1==k && $2==i){f=1} END{exit !f}' "$STATE"
}
header_value() { awk -v h="$(lower "$2"):" 'tolower($1)==h{sub(/^[^:]*:[ ]*/,""); v=$0} END{print v}' "$1"; }
disposition_name() {
  local cd v
  cd=$(header_value "$1" content-disposition)
  v=$(printf '%s' "$cd" | sed -n "s/.*filename\*=[Uu][Tt][Ff]-8''\([^;]*\).*/\1/p")
  if [ -n "$v" ]; then urldecode "$v"; return; fi
  printf '%s\n' "$cd" | awk '{ s = $0
    if (match(s, /filename="[^"]*"/)) { print substr(s, RSTART + 10, RLENGTH - 11); exit }
    if (match(s, /filename=[^;]*/)) { v = substr(s, RSTART + 9, RLENGTH - 9); gsub(/^[ ]+|[ ]+$/, "", v); print v; exit } }' 
}

# One file: returns after success, a recorded problem, expiry, or Ctrl+C.
fetch_item() {
  local kind="$1" id="$2" site="$3" url="$4" fallback="$5"
  local folder part tmp hdr offset attempt=0 restarted=0 code rc status got start total name rel final stem ext cand i sz delay ra path
  if [ "$kind" = recordings ]; then folder="recordings/$(safe_name "$site")"; [ "$folder" = "recordings/" ] && folder="recordings/no-site"; else folder="$kind"; fi
  mkdir -p "$OUT_ABS/$folder" || { problem "$kind" "$id" "$site" failed "cannot create folder $folder" ""; return; }
  part="$OUT_ABS/$folder/${kind%s}-$(safe_name "$id").part"
  tmp="$part.next"; hdr="$WORK/headers"
  path="${url#*://}"; path="/${path#*/}"
  while :; do
    gate_wait || return
    offset=0; [ -f "$part" ] && offset=$(file_size "$part")
    rm -f "$tmp" "$hdr"
    if [ "$offset" -gt 0 ]; then
      curl -sS -A "$UA" -H 'Accept: */*' -r "$offset-" --connect-timeout 30 --speed-time "$TIMEOUT" --speed-limit 1 -D "$hdr" -o "$tmp" "$url" 2>/dev/null; rc=$?
    else
      curl -sS -A "$UA" -H 'Accept: */*' --connect-timeout 30 --speed-time "$TIMEOUT" --speed-limit 1 -D "$hdr" -o "$tmp" "$url" 2>/dev/null; rc=$?
    fi
    code=$(awk '/^HTTP\//{c=$2}END{print c+0}' "$hdr" 2>/dev/null); is_int "$code" || code=0
    tr -d '\r' < "$hdr" > "$hdr.c" 2>/dev/null && mv -f "$hdr.c" "$hdr"
    got=0; [ -f "$tmp" ] && got=$(file_size "$tmp")
    # Keep whatever arrived, so a stop or a broken connection resumes from there.
    if [ "$code" = 200 ] && [ "$got" -gt 0 ]; then mv -f "$tmp" "$part"
    elif [ "$code" = 206 ]; then
      start=$(header_value "$hdr" content-range | sed -n 's/^bytes \([0-9]*\)-.*/\1/p')
      if [ "$start" = "$offset" ]; then cat "$tmp" >> "$part"; else rm -f "$part"; rc=99; fi
    fi
    rm -f "$tmp"
    case "$code" in 200|206) [ "$got" -gt 0 ] && BYTES=$(( BYTES + got )) ;; esac
    if [ "$INTERRUPTED" = 1 ]; then return; fi
    if [ "$rc" = 0 ] && { [ "$code" = 200 ] || [ "$code" = 206 ]; }; then break; fi
    case "$code" in
      416) rm -f "$part"
           if [ "$restarted" = 0 ]; then restarted=1; continue; fi
           problem "$kind" "$id" "$site" failed "the server refused to resume the download (HTTP 416)" ""; return ;;
      401) EXPIRED=1; return ;;
      301|302|303|307|308)
           if header_value "$hdr" location | grep -qi login; then problem "$kind" "$id" "$site" legacy "needs an arbimon.org sign-in" "$url"
           else problem "$kind" "$id" "$site" failed "unexpected redirect (HTTP $code)" ""; fi
           return ;;
      404) case "$path" in
             /dl/*) SHORT_404=$(( SHORT_404 + 1 ))
                    if [ "$SHORT_OK" = 0 ] && [ "$SHORT_404" -ge 3 ]; then EXPIRED=1; return; fi
                    problem "$kind" "$id" "$site" failed "link not found or expired (HTTP 404)" ""; return ;;
           esac
           problem "$kind" "$id" "$site" failed "HTTP 404" ""; return ;;
      410) problem "$kind" "$id" "$site" failed "the file no longer exists on the server (HTTP 410)" ""; return ;;
      403) problem "$kind" "$id" "$site" failed "access refused (HTTP 403) - please report this to contact@arbimon.org" ""; return ;;
      0|200|206|408|425|429|500|502|503|504) ;;   # network trouble, a cut-off transfer, or a temporary server error: retry
      *) problem "$kind" "$id" "$site" failed "HTTP $code" ""; return ;;
    esac
    if [ "$attempt" -ge "$RETRIES" ]; then
      if [ "$code" = 0 ] || [ "$code" = 200 ] || [ "$code" = 206 ]; then problem "$kind" "$id" "$site" failed "network error (curl $rc); run the script again to resume" ""
      else problem "$kind" "$id" "$site" failed "HTTP $code" ""; fi
      return
    fi
    delay=$(( BACKOFF_BASE << attempt )); [ "$delay" -gt "$BACKOFF_CAP" ] && delay=$BACKOFF_CAP
    delay=$(( delay / 2 + (RANDOM % (delay / 2 + 1)) ))
    ra=$(header_value "$hdr" retry-after); ra="${ra%%.*}"
    if is_int "$ra"; then [ "$ra" -gt 300 ] && ra=300; [ "$ra" -gt "$delay" ] && delay=$ra; fi
    attempt=$(( attempt + 1 ))
    [ "$delay" -gt 0 ] && sleep "$delay"
    [ "$INTERRUPTED" = 1 ] && return
  done
  case "$path" in /dl/*) SHORT_OK=$(( SHORT_OK + 1 )) ;; esac
  # Name: the server's file name, else the link's download-name, else date + id.
  name=$(safe_name "$(disposition_name "$hdr")")
  [ -z "$name" ] && name=$(safe_name "$(urldecode "$(query_param "$url" download-name)")")
  [ -z "$name" ] && name=$(safe_name "$fallback")
  [ -z "$name" ] && name=$(safe_name "$id")
  total=$(file_size "$part")
  stem="$name"; ext=""
  case "$name" in ?*.*) stem="${name%.*}"; ext=".${name##*.}" ;; esac
  rel=""
  for i in 0 1; do
    if [ "$i" = 0 ]; then cand="$name"; else cand="${stem}__$(safe_name "$id")$ext"; fi
    claimed_by_other "$folder/$cand" "$kind" "$id" && continue
    final="$OUT_ABS/$folder/$cand"
    if [ -f "$final" ] && ! grep -q -F "$kind$TAB$id$TAB$folder/$cand$TAB" "$STATE"; then
      sz=$(file_size "$final")
      if [ "$sz" = "$total" ] && cmp -s "$final" "$part"; then rm -f "$part"; rel="$folder/$cand"; break; fi   # already there
      [ "$i" = 0 ] && continue          # someone else's file of the same name: do not overwrite it
    fi
    mv -f "$part" "$final" || { problem "$kind" "$id" "$site" failed "cannot save $folder/$cand" ""; return; }
    rel="$folder/$cand"; break
  done
  if [ -z "$rel" ]; then problem "$kind" "$id" "$site" failed "could not find a free file name" ""; return; fi
  printf '%s\t%s\t%s\t%s\n' "$kind" "$id" "$rel" "$total" >> "$STATE"
  DOWNLOADED=$(( DOWNLOADED + 1 ))
  say "  $rel ($(human_bytes "$total")) - $(( DONE_N + 1 ))/$TOTAL - about $(human_duration $(( (TOTAL - DONE_N - 1) * INTERVAL ))) left"
}

say "Saving to $OUT_ABS. Press Ctrl+C to stop; run again to resume."
say "One download starts every $INTERVAL seconds, to keep the load on Arbimon's servers low."
time_estimate $(( TOTAL - ALREADY ))
W0=$(first_wait)
[ "$W0" -ge 1 ] && [ $(( TOTAL - ALREADY )) -gt 0 ] && say "Waiting $W0 seconds since the last run's final request..."

while IFS="$US" read -r kind id site url fallback uk donepath <&3; do
  [ "$INTERRUPTED" = 1 ] && break
  [ "$EXPIRED" = 1 ] && break
  case "$uk" in
    empty) problem "$kind" "$id" "$site" no_url "no link in the backup" ""; continue ;;
    legacy) problem "$kind" "$id" "$site" legacy "needs an arbimon.org sign-in" "$url"; continue ;;
  esac
  if [ -n "$donepath" ] && [ -f "$OUT_ABS/$donepath" ]; then SKIPPED=$(( SKIPPED + 1 )); DONE_N=$(( DONE_N + 1 )); continue; fi
  fetch_item "$kind" "$id" "$site" "$url" "$fallback"
  DONE_N=$(( DONE_N + 1 ))
done 3< "$ITEMS"
[ "$INTERRUPTED" = 1 ] && { say ""; say "Stopping... (partly downloaded files are kept and will resume next time)"; }

# ------------------------------------------------------------------ summary
REPORT=""
if [ -s "$PROBLEMS" ]; then
  REPORT="$OUT_ABS/download-report-$(date +%Y%m%d-%H%M%S).csv"
  { printf 'type,id,site_id,status,reason,url\n'; cat "$PROBLEMS"; } > "$REPORT"
fi
say ""
say "Summary ($OUT_ABS, $(( $(date +%s) - STARTED )) s)"
say "  downloaded now:          $DOWNLOADED ($(human_bytes "$BYTES"))"
say "  already downloaded:      $SKIPPED"
if [ "$LEGACY" -gt 0 ]; then
  say "  need an arbimon.org sign-in: $LEGACY - older uploads that cannot be downloaded by a script."
  say "      Open each link from the report in a browser while signed in to arbimon.org,"
  say "      or ask contact@arbimon.org for help with a large number."
fi
[ "$NO_URL" -gt 0 ] && say "  no link in the backup:   $NO_URL"
if [ "$FAILED" -gt 0 ]; then say "  failed:                  $FAILED - run the script again to retry them"; else say "  failed:                  0"; fi
if grep -q "^recordings$TAB" "$STATE" 2>/dev/null; then
  csv_files "$SRC" sites > "$WORK/sitefiles"
  set --
  while IFS= read -r f; do set -- "$@" "$f"; done < "$WORK/sitefiles"
  : > "$WORK/sitenames"
  [ $# -gt 0 ] && LC_ALL=C awk -v kind=sites -v idcol=site_id "$AWK_ITEMS" "$@" > "$WORK/sitenames" 2>/dev/null
  say "  recordings by site folder (recordings/<site_id>/):"
  LC_ALL=C awk -v US="$US" -v out="$OUT_ABS/recordings/sites.csv" '
    function q(s) { gsub(/"/, "\"\"", s); return (s ~ /[",\n]/) ? "\"" s "\"" : s }
    FILENAME == ARGV[1] { split($0, a, US); nm[a[1]] = a[2]; next }
    { split($0, a, "\t"); if (a[1] != "recordings") next; split(a[3], p, "/"); if (!(p[2] in c)) order[++n] = p[2]; c[p[2]]++ }
    END {
      for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++) {
        x = order[i]; y = order[j]
        if (length(y) < length(x) || (length(y) == length(x) && y < x)) { order[i] = y; order[j] = x } }
      print "site_id,site_name,files" > out
      for (i = 1; i <= n; i++) { s = order[i]; print q(s) "," q(nm[s]) "," c[s] > out; printf "      %-10s %6d  %s\n", s, c[s], nm[s] } }' "$WORK/sitenames" "$STATE"
fi
[ -n "$REPORT" ] && say "  report: $REPORT"
rm -rf "$WORK"
if [ "$EXPIRED" = 1 ]; then say ""; say "$EXPIRED_MESSAGE"; exit 3; fi
[ "$INTERRUPTED" = 1 ] && exit 130
[ "$FAILED" -gt 0 ] && exit 1
exit 0
