# Arbimon project backup: bulk download

`arbimon_backup_download.py` downloads the audio linked from an
[Arbimon](https://arbimon.org) project backup: every recording, plus template
and soundscape files when the backup has them. A project backup is a ZIP of
CSV files. The audio is not inside the ZIP: the `recordings`, `templates` and
`soundscapes` CSVs each have a `url` column with a download link. This script
reads those CSVs and downloads every linked file into one tidy folder.

**It is deliberately slow:** it starts one download every 30 seconds (about
2,880 files a day), to keep the load on Arbimon's servers low. A large project
takes days. Leave it running, or stop it and run it again whenever you like:
it carries on where it stopped. See [How long it takes](#how-long-it-takes).

Full guide: [Downloading Recordings from a Project Backup](https://arbimon.org/support/arbimon/settings/downloading-recordings-from-a-project-backup/)

## Requirements

- **Python 3.9 or newer.** Nothing else: no `pip install`, no extra packages.
  - macOS / Linux: check with `python3 --version`.
  - Windows: install from [python.org](https://www.python.org/downloads/) and tick
    **Add python.exe to PATH**. Check with `python --version` (or `py --version`).
- Enough free disk space (recordings are typically 1–2 MB per minute of audio as FLAC).
  Use `--dry-run` to get an estimate first.
- A computer that stays on and awake while it runs (or run it again after a break).

## Usage

1. Download the script: open [`arbimon_backup_download.py`](arbimon_backup_download.py) and click the
   **Download raw file** button (↓) at the top right of the file, or download the latest
   [release](https://github.com/rfcx/arbimon-project-backup/releases/latest).
2. Extract your backup ZIP. Put the script in that folder (or point to the folder with `--backup-dir`).
3. Open a terminal in that folder and run:

macOS / Linux:

```
python3 arbimon_backup_download.py --dry-run     # count the files, estimate the size and the time
python3 arbimon_backup_download.py               # download everything into ./downloads
```

Windows (Command Prompt or PowerShell):

```
python arbimon_backup_download.py --dry-run
python arbimon_backup_download.py
```

(If `python` is not found on Windows, try `py` instead.)

More examples:

```
# the backup ZIP can be read directly, no need to extract it
python3 arbimon_backup_download.py --backup-dir ~/Downloads/my-project_2026-11-12_export.zip

# save to an external drive
python3 arbimon_backup_download.py --out /Volumes/MyDrive/my-project

# only recordings, and only the first 20 (a quick test)
python3 arbimon_backup_download.py --only recordings --limit 20
```

## How long it takes

One download starts every 30 seconds, whatever the speed of your connection:

| Recordings | Time |
|---|---|
| 1,000 | about 8 hours |
| 5,000 | about 42 hours |
| 10,000 | about 3.5 days |
| 20,000 | about 7 days |
| 100,000 | about 35 days |

The links in a backup expire after 7 days (see below), so one backup covers
about **20,000 files**. For a bigger project, let the script run until the links
expire, then request a new project backup and run the script on it with the same
`--out` folder: everything you already have is skipped, and it continues with the
rest. `--dry-run` prints the estimate for your backup and warns you when the
links will expire before it can finish.

Files you already have cost nothing: they are skipped without contacting the
server. If you stop the script and start it again straight away, it first waits
out the rest of the 30 seconds since its last request.

## Options

| Option | What it does |
|---|---|
| `--backup-dir PATH` | The extracted backup folder, or the backup `.zip` itself. Default: the current folder. |
| `--out PATH` | Where to save the files. Default: `./downloads`. |
| `--only KIND` | Only `recordings`, `templates` or `soundscapes` (can be repeated). |
| `--dry-run` | Count the files and estimate the total size and time (checks 3 sample files, about a minute). Downloads nothing. |
| `--limit N` | Stop after N files (useful for a test). |
| `--interval S` | Seconds between the start of two downloads. Default and minimum: 30. You can make it slower, not faster. |
| `--workers N` | How many downloads may overlap when one file is slow to arrive, 1–4. Default 2. This does not make the script faster: the 30-second spacing sets the pace. |
| `--sample N` | Files to check for the `--dry-run` size estimate. Default 3. |
| `--retries N` | Retries per file on temporary errors. Default 5. |
| `--timeout S` | Seconds before a stalled request is retried. Default 120. |
| `--version` | Print the version. |

## What you get

```
downloads/
├── recordings/
│   ├── 90326/                      one folder per site (site_id)
│   │   ├── 2MM09808_20260327_080400.flac
│   │   └── ...
│   ├── 90327/
│   └── sites.csv                   site_id → site name, and how many files each has
├── templates/
├── soundscapes/
├── download-report-<time>.csv      only if something needs attention
└── .arbimon-download-state.jsonl   progress record (keep it until you are done)
```

- Recordings keep the **original file name** they were uploaded with. Long uploads
  that Arbimon split into parts get a part number, e.g. `name.003.flac`.
- If two recordings in the same site folder have the same name, the second gets
  its recording id added: `name__313708070.flac`. Nothing is overwritten.
- Folders are named by site id because site names can repeat and can contain
  characters that are not allowed in file names; `recordings/sites.csv` maps ids to names.

## Stopping and resuming

Press **Ctrl+C** at any time. Run the same command again to continue: finished
files are skipped and a half-downloaded file continues from where it stopped.
Running it again after it has finished downloads nothing new, so it is also a
quick way to check that everything is there.

## Links expire after about 7 days

The links in a backup work for about **7 days** after the backup is made (the
script prints the exact time when it starts). After that the server answers
"Unauthorized" and the script stops with:

> The download links in this backup have expired … Please request a new project backup …

Request a new backup in Arbimon (**Project Settings → Project backup**) and run
the script on the new backup, with the same `--out` folder: files you already
have are kept and skipped.

## "Need an arbimon.org sign-in"

A small number of recordings in older projects (mostly uploaded 2015–2022) are
stored in a legacy format. Their links only work in a browser while you are
signed in to arbimon.org, so the script cannot download them. It lists them in
`download-report-<time>.csv` with their links: open each link in a browser while
signed in, or contact us if there are many.

## Exit codes

`0` everything downloaded (or already there) · `1` some files failed (run again to
retry) · `2` no backup files found / bad option · `3` links expired · `130` stopped with Ctrl+C.

## Tests

`python3 -m unittest -v` runs the test suite against a local fake server (no network, no
install). It runs on Linux, macOS and Windows with Python 3.9 and 3.13 on every push.

## Help

Email **contact@arbimon.org** with the script's last lines of output and the
`download-report-*.csv` file if there is one.

## Changelog

- **1.0.0 (2026-11-12)** — First release. Replaces the older backup download scripts
  (`download_files_v1.py`, `download_files_v2.py` and `download_only_detected_part.py`).
  Starts at most one request every 30 seconds (`--interval` can only raise it), across
  parallel downloads, retries and restarts; `--dry-run` estimates the time and warns when
  the links will expire first. Standard library only (no `requests`). Reads `.0001.csv` / `.0002.csv` … files or the ZIP
  directly; all backup date formats; original file names; per-site folders without
  overwriting; safe resume of partial files; parallel downloads with retry and back-off;
  clear messages for expired links and legacy (sign-in) links; `--dry-run` size estimate.

## License

[Apache License 2.0](LICENSE), the same as [Arbimon](https://github.com/rfcx/arbimon).
