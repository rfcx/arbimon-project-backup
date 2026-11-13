# Arbimon project backup: bulk download

Two small scripts that download the audio linked from an [Arbimon](https://arbimon.org)
project backup: every recording, plus template and soundscape files when the backup has
them. **Nothing to install** — they use only what your computer already has:

| Your computer | Script | Uses |
|---|---|---|
| **Windows 10 or 11** | [`arbimon_backup_download.ps1`](arbimon_backup_download.ps1) | PowerShell + `curl.exe` (both built in) |
| **Mac** or **Linux** | [`arbimon_backup_download.sh`](arbimon_backup_download.sh) | Terminal (`bash`) + `curl` (both built in) |

A project backup is a ZIP of CSV files. The audio is not inside the ZIP: the
`recordings`, `templates` and `soundscapes` CSVs each have a `url` column with a
download link. The script reads those CSVs and downloads every linked file into one
tidy folder.

**It is deliberately slow:** it starts one download every 30 seconds (about 2,880
files a day), to keep the load on Arbimon's servers low. A large project takes days.
Leave it running, or stop it and run it again whenever you like: it carries on where it
stopped. See [How long it takes](#how-long-it-takes).

Full guide: [Downloading Recordings from a Project Backup](https://arbimon.org/support/arbimon/settings/downloading-recordings-from-a-project-backup/)

## Usage

1. Download the script for your computer: click its name in the table above, then the
   **Download raw file** button (↓) at the top right of the file. Or download it from the
   latest [release](https://github.com/rfcx/arbimon-project-backup/releases/latest).
2. Extract your backup ZIP (Windows: right-click → **Extract All**; Mac: double-click).
   Put the script in that folder.
3. Open a terminal in that folder and run the commands below.

**Windows** — in the backup folder, click the address bar, type `powershell`, press Enter, then:

```
powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1
```

(`-ExecutionPolicy Bypass` lets Windows run a script you downloaded, for this one command only.)

**Mac / Linux** — open Terminal, type `cd ` (with a space), drag the backup folder onto the window, press Enter, then:

```
bash arbimon_backup_download.sh --dry-run
bash arbimon_backup_download.sh
```

The first command counts the files and estimates the size and time (about a minute);
the second downloads everything into a `downloads` folder.

More examples:

```
# save to an external drive
bash arbimon_backup_download.sh --out /Volumes/MyDrive/my-project
powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1 -Out E:\my-project

# only recordings, and only the first 20 (a quick test)
bash arbimon_backup_download.sh --only recordings --limit 20
powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1 -Only recordings -Limit 20
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

The links in a backup expire after 7 days (see below), so one backup covers about
**20,000 files**. For a bigger project, let the script run until the links expire, then
request a new project backup and run the script on it with the same output folder:
everything you already have is skipped, and it continues with the rest. The dry run
prints the estimate for your backup and warns you when the links will expire before it
can finish.

Files you already have cost nothing: they are skipped without contacting the server. If
you stop the script and start it again straight away, it first waits out the rest of the
30 seconds since its last request. Keep the computer awake while it runs.

## Options

| Mac / Linux | Windows | What it does |
|---|---|---|
| `--backup-dir PATH` | `-BackupDir PATH` | The extracted backup folder (or the `.zip`). Default: the current folder. |
| `--out PATH` | `-Out PATH` | Where to save the files. Default: `downloads`. |
| `--only KIND` | `-Only KIND` | Only `recordings`, `templates` or `soundscapes`. |
| `--dry-run` | `-DryRun` | Count the files and estimate size and time (checks 3 sample files). Downloads nothing. |
| `--limit N` | `-Limit N` | Stop after N files (useful for a test). |
| `--interval S` | `-Interval S` | Seconds between the start of two downloads. Default and minimum: 30. You can make it slower, not faster. |
| `--sample N` | `-Sample N` | Files to check for the dry-run size estimate. Default 3. |
| `--retries N` | `-Retries N` | Retries per file on temporary errors. Default 5. |
| `--timeout S` | `-Timeout S` | Seconds without any data before a download is retried. Default 120. |
| `--version` | `-Version` | Print the version. |

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
└── .arbimon-download-state.tsv     progress record (keep it until you are done)
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
quick way to check that everything is there. Both scripts (and the older 1.0.0 Python
script) keep the same progress record, so you can switch between them.

## Links expire after about 7 days

The links in a backup work for about **7 days** after the backup is made (the
script prints the exact time when it starts). After that the server answers
"Unauthorized" and the script stops with:

> The download links in this backup have expired … Please request a new project backup …

Request a new backup in Arbimon (**Project Settings → Project backup**) and run
the script on the new backup, with the same output folder: files you already
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

## Help

Email **contact@arbimon.org** with the script's last lines of output and the
`download-report-*.csv` file if there is one.

## Tests

`tests/` runs both scripts against a local fake of Arbimon's servers on every push
(bash on Linux and macOS; Windows PowerShell 5.1 and PowerShell 7 on Windows).
The test harness is written in Python; the scripts themselves do not use it.

## Changelog

- **2.0.0 (2026-11-13)** — No Python needed: `arbimon_backup_download.sh` (Mac/Linux:
  bash + curl) and `arbimon_backup_download.ps1` (Windows: PowerShell + curl.exe) replace
  the Python script. Same 30-second spacing, resume, folders, report and exit codes; one
  download at a time. Progress from 1.0.0 is picked up. The 1.0.0 Python script stays
  available in [release 1.0.0](https://github.com/rfcx/arbimon-project-backup/releases/tag/v1.0.0).
- **1.0.0 (2026-11-12)** — First release (Python). Replaces the older backup download scripts
  (`download_files_v1.py`, `download_files_v2.py` and `download_only_detected_part.py`).

## License

[Apache License 2.0](LICENSE), the same as [Arbimon](https://github.com/rfcx/arbimon).