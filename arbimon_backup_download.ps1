<#
Arbimon project backup: bulk download of recordings, templates and soundscapes (Windows).

A project backup is a ZIP of CSV files. The audio is not inside the ZIP: the
recordings, templates and soundscapes CSVs each have a "url" column that links
to the file. This script reads those CSVs and downloads every linked file.

  powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1                 (inside the extracted backup folder)
  powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1 -BackupDir C:\Users\me\Downloads\my-backup -Out E:\my-project
  powershell -ExecutionPolicy Bypass -File arbimon_backup_download.ps1 -DryRun          (count and estimate, download nothing)

Needs only what Windows 10 and 11 already have: PowerShell and curl.exe. Nothing to install.
Safe to stop (Ctrl+C) and re-run: finished files are skipped and a half-finished
file continues where it stopped.

To keep the load on Arbimon's servers low, it starts at most one request every
30 seconds (about 2,880 files a day). A large project takes days.

Help:    https://arbimon.org/support/arbimon/settings/downloading-recordings-from-a-project-backup/
Source:  https://github.com/rfcx/arbimon-project-backup
Support: contact@arbimon.org
#>
param(
    [string]$BackupDir = ".",
    [string]$Out = "downloads",
    [ValidateSet("recordings", "templates", "soundscapes")][string[]]$Only,
    [int]$Limit = -1,
    [switch]$DryRun,
    [int]$Sample = 3,
    [double]$Interval = -1,
    [int]$Timeout = 120,
    [int]$Retries = 5,
    [switch]$Version
)

$ScriptVersion = "2.0.0"
$MinInterval = 30          # seconds between the START of two requests; -Interval may only raise it
$BackoffBase = 2
$BackoffCap = 60
$UA = "arbimon-backup-download/$ScriptVersion (+https://arbimon.org)"
$StateName = ".arbimon-download-state.tsv"
$OldStateName = ".arbimon-download-state.jsonl"
$RateName = ".arbimon-download-rate"
$ExpiredMessage = @"
The download links in this backup have expired (they work for about 7 days after the backup is made).
Please request a new project backup in Arbimon (Project Settings > Project backup) and run this
script on the new backup. Files you already downloaded are kept and will be skipped.
"@
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Inv = [Globalization.CultureInfo]::InvariantCulture

function Say([string]$m = "") { [Console]::Out.WriteLine($m) }

if ($args.Count) {
    Say "Unknown option: $($args -join ' ')"
    Say "Options: -BackupDir PATH -Out PATH -Only recordings|templates|soundscapes -Limit N -DryRun -Sample N -Interval S -Timeout S -Retries N -Version"
    exit 2
}
if ($Version) { Say "arbimon_backup_download.ps1 $ScriptVersion"; exit 0 }
if ($Interval -lt 0) { $Interval = $MinInterval }
if ([double]::IsNaN($Interval) -or $Interval -lt $MinInterval) {
    Say "-Interval must be at least $MinInterval seconds (to keep the load on Arbimon's servers low)."
    exit 2
}
$Kinds = if ($Only) { @($Only) } else { @("recordings", "templates", "soundscapes") }
$Curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $Curl) { Say "This script needs curl.exe, which comes with Windows 10 (version 1803) and later. It was not found."; exit 2 }
$Curl = $Curl.Source

# ------------------------------------------------------------------ helpers
function HumanBytes([double]$n) {
    $u = @("B", "KB", "MB", "GB", "TB"); $i = 0
    while ($n -ge 1000 -and $i -lt 4) { $n /= 1000; $i++ }
    if ($i -eq 0) { return "{0} {1}" -f [int]$n, $u[$i] }
    return [string]::Format($Inv, "{0:0.0} {1}", $n, $u[$i])
}
function HumanDuration([double]$s) {
    $s = [math]::Round($s)
    if ($s -lt 120) { return "$s seconds" }
    if ($s -lt 7200) { return "{0} minutes" -f [math]::Round($s / 60) }
    if ($s -lt 172800) { return [string]::Format($Inv, "{0:0.0} hours", $s / 3600) }
    return [string]::Format($Inv, "{0:0.0} days", $s / 86400)
}
function Fmt([double]$x) { return $x.ToString("0.###", $Inv) }
function QueryParam([string]$url, [string]$key) {
    $q = $url.IndexOf("?"); if ($q -lt 0) { return "" }
    foreach ($kv in $url.Substring($q + 1).Split("&")) {
        $p = $kv.Split("=", 2)
        if ($p[0] -eq $key) { if ($p.Count -lt 2) { return "" }; return [Uri]::UnescapeDataString($p[1].Replace("+", " ")) }
    }
    return ""
}
$Reserved = @("CON", "PRN", "AUX", "NUL") + (1..9 | ForEach-Object { "COM$_"; "LPT$_" })
function SafeName([string]$name) {
    if (-not $name) { return "" }
    $name = ($name -split '[\\/]')[-1]
    $name = [regex]::Replace($name, '[\x00-\x1f\x7f<>:"/\\|?*]', "_").Trim().TrimEnd(". ".ToCharArray())
    if (-not $name -or $name -eq "." -or $name -eq "..") { return "" }
    $dot = $name.LastIndexOf(".")
    if ($dot -gt 0) { $stem = $name.Substring(0, $dot); $ext = $name.Substring($dot + 1) } else { $stem = $name; $ext = "" }
    if ($Reserved -contains $stem.Split(".")[0].ToUpperInvariant()) { $stem = "_" + $stem }
    if ($stem.Length -gt 150) { $stem = $stem.Substring(0, 150) }
    if ($ext.Length -gt 12) { $ext = $ext.Substring(0, 12) }
    if ($ext) { return "$stem.$ext" } else { return $stem }
}
function DatePrefix([string]$v) {
    $v = "$v".Trim(); if (-not $v) { return "" }
    $v = $v -replace '\.\d+$', ''
    foreach ($f in @("yyyy-MM-dd HH:mm:ss", "M/d/yyyy HH:mm:ss", "M/d/yy HH:mm:ss", "MM/dd/yyyy HH:mm:ss", "MM/dd/yy HH:mm:ss")) {
        $d = [datetime]::MinValue
        if ([datetime]::TryParseExact($v, $f, $Inv, [Globalization.DateTimeStyles]::None, [ref]$d)) { return $d.ToString("yyyyMMdd_HHmmss_") }
    }
    return ""
}
function UrlKind([string]$u) {
    if (-not $u) { return "empty" }
    $path = $u -replace '[?#].*$', ''
    if ($path -match '/legacy-api/' -and $path -match '/recordings/download/') { return "legacy" }
    if ($u -match '[?&]stream-token=') { return "media" }
    return "link"
}
function UrlPath([string]$u) { try { return ([Uri]$u).AbsolutePath } catch { return "" } }

# Backup CSVs of one kind, in order: kind.csv, kind.0001.csv, kind.0002.csv, kind_1.csv ...
function CsvFiles([string]$dir, [string]$kind) {
    Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue | ForEach-Object {
        $n = $_.Name.ToLowerInvariant()
        if ($n -eq "$kind.csv") { [pscustomobject]@{ N = 0; P = $_.FullName } }
        elseif ($n -match ("^" + [regex]::Escape($kind) + '[._](\d+)\.csv$')) { [pscustomobject]@{ N = [int64]$Matches[1]; P = $_.FullName } }
    } | Sort-Object N | ForEach-Object { $_.P }
}
function HasBackupCsvs([string]$dir) {
    foreach ($k in @("recordings", "templates", "soundscapes", "sites")) { if (@(CsvFiles $dir $k).Count) { return $true } }
    return $false
}

# ------------------------------------------------------------------ find the backup
$OutAbs = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
$Src = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BackupDir)
if ((Test-Path -LiteralPath $Src -PathType Leaf) -and $Src.ToLowerInvariant().EndsWith(".zip")) {
    $dest = Join-Path $OutAbs ".backup-csv"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    try { Expand-Archive -LiteralPath $Src -DestinationPath $dest -Force } catch { Say "Could not read $BackupDir."; exit 2 }
    $Src = $dest
}
if ((Test-Path -LiteralPath $Src -PathType Container) -and -not (HasBackupCsvs $Src)) {
    foreach ($d in Get-ChildItem -LiteralPath $Src -Directory -ErrorAction SilentlyContinue) { if (HasBackupCsvs $d.FullName) { $Src = $d.FullName; break } }
}
if (-not (Test-Path -LiteralPath $Src -PathType Container) -or -not (HasBackupCsvs $Src)) {
    Say "No backup CSV files found in $BackupDir."
    Say "Extract the backup ZIP first (right-click > Extract All), then run this script inside that folder (or pass -BackupDir)."
    exit 2
}
New-Item -ItemType Directory -Force -Path $OutAbs | Out-Null
$State = Join-Path $OutAbs $StateName
$RateFile = Join-Path $OutAbs $RateName
$Work = Join-Path $OutAbs ".arbimon-work"
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Hdr = Join-Path $Work "headers"
$Utf8 = New-Object Text.UTF8Encoding($false)

# What is already downloaded (this script's state file, and the 1.0.0 Python script's).
$Done = @{}
$Claimed = @{}
if (Test-Path -LiteralPath $State) {
    foreach ($line in [IO.File]::ReadAllLines($State, $Utf8)) {
        $p = $line.Split("`t"); if ($p.Count -lt 4) { continue }
        $Done["$($p[0])`t$($p[1])"] = $p[2]; $Claimed[$p[2].ToLowerInvariant()] = "$($p[0])`t$($p[1])"
    }
}
$old = Join-Path $OutAbs $OldStateName
if (Test-Path -LiteralPath $old) {
    foreach ($line in [IO.File]::ReadAllLines($old, $Utf8)) {
        try { $r = $line | ConvertFrom-Json } catch { continue }
        $k = "$($r.kind)`t$($r.id)"; $pth = "$($r.path)".Replace("\", "/")
        if (-not $Done.ContainsKey($k)) { $Done[$k] = $pth; $Claimed[$pth.ToLowerInvariant()] = $k }
    }
}

# Items, streamed in order (Import-Csv is a real CSV parser: quotes, commas and line breaks inside fields).
$IdCol = @{ recordings = "recording_id"; templates = "template_id"; soundscapes = "soundscape_id" }
$Items = New-Object System.Collections.Generic.List[object]
$Present = @{}
foreach ($kind in $Kinds) {
    $files = @(CsvFiles $Src $kind)
    $Present[$kind] = $files.Count -gt 0
    $n = 0
    foreach ($f in $files) {
        if ($Limit -ge 0 -and $Items.Count -ge $Limit) { break }
        foreach ($row in Import-Csv -LiteralPath $f -Encoding UTF8) {
            if ($Limit -ge 0 -and $Items.Count -ge $Limit) { break }
            $n++
            $id = "$($row.($IdCol[$kind]))".Trim(); if (-not $id) { $id = "row$n" }
            $url = "$($row.url)".Trim()
            $site = "$($row.site_id)".Trim()
            $when = if ($row.PSObject.Properties["datetime"]) { $row.datetime } else { $row.date_created }
            $leaf = ($url -replace '[?#].*$', '') -replace '^.*/', ''
            $ext = ""; $di = $leaf.LastIndexOf("."); if ($di -ge 0) { $ext = $leaf.Substring($di); if ($ext.Length -gt 8) { $ext = $ext.Substring(0, 8) } }
            $Items.Add([pscustomobject]@{ Kind = $kind; Id = $id; Site = $site; Url = $url; Fallback = (DatePrefix $when) + $id + $ext; UK = (UrlKind $url) })
        }
    }
}

Say "arbimon_backup_download.ps1 $ScriptVersion - backup: $Src"
foreach ($kind in $Kinds) {
    if (-not $Present[$kind]) { Say ("  {0,-12} none in this backup" -f $kind); continue }
    $mine = @($Items | Where-Object { $_.Kind -eq $kind })
    $l = @($mine | Where-Object { $_.UK -eq "legacy" }).Count; $e = @($mine | Where-Object { $_.UK -eq "empty" }).Count
    $x = ""; if ($l) { $x += ", $l need an arbimon.org sign-in" }; if ($e) { $x += ", $e without a link" }
    Say ("  {0,-12} {1} files{2}" -f $kind, $mine.Count, $x)
}
if ($Items.Count -eq 0) { Say "Nothing to download."; Remove-Item -Recurse -Force $Work; exit 0 }
$Downloadable = @($Items | Where-Object { $_.UK -eq "media" -or $_.UK -eq "link" })
$Total = $Downloadable.Count
$Already = @($Downloadable | Where-Object { $Done.ContainsKey("$($_.Kind)`t$($_.Id)") }).Count
$Exp = $null
$firstMedia = $Items | Where-Object { $_.UK -eq "media" } | Select-Object -First 1
if ($firstMedia) { $e = QueryParam $firstMedia.Url "exp"; if ($e -match '^\d+$') { $Exp = [DateTimeOffset]::FromUnixTimeSeconds([int64]$e) } }
if ($Exp) {
    $left = ($Exp - [DateTimeOffset]::UtcNow).TotalSeconds
    $local = $Exp.ToLocalTime().ToString("yyyy-MM-dd HH:mm")
    if ($left -gt 0) { Say ([string]::Format($Inv, "Links valid until {0} ({1:0.0} days left).", $local, $left / 86400)) }
    else { Say "Warning: the links look expired (valid until $local by this computer's clock). Trying anyway." }
}
function TimeEstimate([int]$todo) {
    if ($todo -le 0) { return }
    $need = $todo * $Interval
    Say ("Estimated time: about {0} for {1} files (one file every {2} seconds, about {3} a day)." -f (HumanDuration $need), $todo, (Fmt $Interval), [math]::Floor(86400 / $Interval))
    if ($Exp) {
        $left = ($Exp - [DateTimeOffset]::UtcNow).TotalSeconds
        if ($left -gt 0 -and $left -lt $need) {
            Say ("Warning: the links expire in {0}, before all files can be downloaded (about {1} of {2} will fit)." -f (HumanDuration $left), [math]::Floor($left / $Interval), $todo)
            Say "  Let it run until the links expire, then request a new project backup and run the script"
            Say "  on the new backup with the same -Out folder: files you already have are skipped."
        }
    }
}

# ------------------------------------------------------------------ the 30-second rule
$Last = $null
if (Test-Path -LiteralPath $RateFile) {
    $t = 0.0
    if ([double]::TryParse(([IO.File]::ReadAllText($RateFile)).Trim(), [Globalization.NumberStyles]::Float, $Inv, [ref]$t)) {
        $nowE = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        if ($t -gt $nowE) { $t = $nowE }   # a saved time in the future: the clock moved; wait one interval at most
        $Last = $t
    }
}
function NowEpoch { return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }
function GateWait {
    if ($null -ne $script:Last) {
        while ($true) {
            $left = $script:Last + $Interval - (NowEpoch)
            if ($left -le 0) { break }
            Start-Sleep -Milliseconds ([int][math]::Ceiling([math]::Min($left, 1.0) * 1000))
        }
    }
    $script:Last = NowEpoch
    $tmp = "$RateFile.tmp"
    [IO.File]::WriteAllText($tmp, $script:Last.ToString("0.000", $Inv), $Utf8)
    Move-Item -LiteralPath $tmp -Destination $RateFile -Force
}

# Run curl.exe; returns @{ Code; Rc; Headers }.
function Invoke-Curl([string[]]$extra, [string]$url) {
    if (Test-Path -LiteralPath $Hdr) { Remove-Item -LiteralPath $Hdr -Force }
    $cargs = @("-sS", "-A", $UA, "-H", "Accept: */*", "--connect-timeout", "30", "--speed-time", "$Timeout", "--speed-limit", "1", "-D", $Hdr) + $extra + @($url)
    $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $Curl @cargs 2>$null | Out-Null; $rc = $LASTEXITCODE } finally { $ErrorActionPreference = $eap }
    $h = @{}; $code = 0
    if (Test-Path -LiteralPath $Hdr) {
        foreach ($line in [IO.File]::ReadAllLines($Hdr)) {
            if ($line -match '^HTTP/\S+\s+(\d+)') { $code = [int]$Matches[1]; $h = @{} }
            elseif ($line -match '^([^:]+):\s*(.*)$') { $h[$Matches[1].ToLowerInvariant()] = $Matches[2].Trim() }
        }
    }
    return @{ Code = $code; Rc = $rc; Headers = $h }
}
function DispositionName($h) {
    $cd = $h["content-disposition"]; if (-not $cd) { return "" }
    if ($cd -match "filename\*=UTF-8''([^;]+)") { return [Uri]::UnescapeDataString($Matches[1]) }
    if ($cd -match 'filename="([^"]*)"') { return $Matches[1] }
    if ($cd -match 'filename=([^;]+)') { return $Matches[1].Trim() }
    return ""
}

# ------------------------------------------------------------------ dry run
if ($DryRun) {
    $todo = $Total - $Already
    $cands = @($Items | Where-Object { $_.UK -eq "media" -and -not $Done.ContainsKey("$($_.Kind)`t$($_.Id)") })
    $s = [math]::Min($Sample, $cands.Count)
    $picks = @(); for ($i = 1; $i -le $s; $i++) { $picks += $cands[[int][math]::Floor(($i - 0.5) * $cands.Count / $s)] }
    if ($picks.Count) { Say ("Checking the size of {0} sample files (one request every {1} seconds)..." -f $picks.Count, (Fmt $Interval)) }
    $sum = 0.0; $got = 0
    foreach ($it in $picks) {
        GateWait
        $r = Invoke-Curl @("-I", "-o", "NUL") $it.Url
        if ($r.Code -eq 401) { Say $ExpiredMessage; Remove-Item -Recurse -Force $Work; exit 3 }
        if ($r.Code -eq 200 -and $r.Headers["content-length"] -match '^\d+$') { $sum += [double]$r.Headers["content-length"]; $got++ }
    }
    Say "Dry run: $todo to download, $Already already downloaded, output folder $OutAbs"
    $nMedia = @($Items | Where-Object { $_.UK -eq "media" }).Count
    if ($got) { $avg = $sum / $got; Say ("Estimated size: about {0} for {1} audio files (average {2} each, from {3} samples)." -f (HumanBytes ($avg * $nMedia)), $nMedia, (HumanBytes $avg), $got) }
    TimeEstimate $todo
    Remove-Item -Recurse -Force $Work
    exit 0
}

# ------------------------------------------------------------------ download
$C = @{ downloaded = 0; skipped = 0; failed = 0; legacy = 0; no_url = 0 }
$Bytes = 0.0; $Expired = $false; $ShortOk = 0; $Short404 = 0; $DoneN = 0
$Problems = New-Object System.Collections.Generic.List[object]
$Started = Get-Date
function Problem($it, [string]$status, [string]$reason, [string]$url = "") {
    $C[$status]++
    $Problems.Add([pscustomobject]@{ type = $it.Kind; id = $it.Id; site_id = $it.Site; status = $status; reason = $reason; url = $url })
    if ($status -eq "failed") { Say ("  failed: {0} {1} ({2})" -f $it.Kind.TrimEnd("s"), $it.Id, $reason) }
}
function Fetch($it) {
    if ($it.Kind -eq "recordings") { $sf = SafeName $it.Site; if (-not $sf) { $sf = "no-site" }; $folder = "recordings/$sf" } else { $folder = $it.Kind }
    $dir = Join-Path $OutAbs $folder
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $idn = SafeName $it.Id; if (-not $idn) { $idn = "x" }
    $part = Join-Path $dir ("{0}-{1}.part" -f $it.Kind.TrimEnd("s"), $idn)
    $next = "$part.next"
    $path = UrlPath $it.Url
    $attempt = 0; $restarted = $false
    while ($true) {
        GateWait
        $offset = 0; if (Test-Path -LiteralPath $part) { $offset = (Get-Item -LiteralPath $part).Length }
        if (Test-Path -LiteralPath $next) { Remove-Item -LiteralPath $next -Force }
        if ($offset -gt 0) { $r = Invoke-Curl @("-r", "$offset-", "-o", $next) $it.Url } else { $r = Invoke-Curl @("-o", $next) $it.Url }
        $got = 0; if (Test-Path -LiteralPath $next) { $got = (Get-Item -LiteralPath $next).Length }
        $rc = $r.Rc
        if ($r.Code -eq 200 -and $got -gt 0) { Move-Item -LiteralPath $next -Destination $part -Force }
        elseif ($r.Code -eq 206) {
            $start = if ($r.Headers["content-range"] -match '^bytes (\d+)-') { [int64]$Matches[1] } else { -1 }
            if ($start -eq $offset) {
                $o = [IO.File]::Open($part, [IO.FileMode]::Append); $in = [IO.File]::OpenRead($next)
                try { $in.CopyTo($o) } finally { $in.Close(); $o.Close() }
            } else { Remove-Item -LiteralPath $part -Force; $rc = 99 }
        }
        if (Test-Path -LiteralPath $next) { Remove-Item -LiteralPath $next -Force }
        if ($r.Code -eq 200 -or $r.Code -eq 206) { $script:Bytes += $got }
        if ($rc -eq 0 -and ($r.Code -eq 200 -or $r.Code -eq 206)) { break }
        if ($r.Code -eq 416) {
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            if (-not $restarted) { $restarted = $true; continue }
            Problem $it "failed" "the server refused to resume the download (HTTP 416)"; return
        }
        switch ($r.Code) {
            401 { $script:Expired = $true; return }
            { $_ -in 301, 302, 303, 307, 308 } {
                  if ("$($r.Headers['location'])" -match 'login') { Problem $it "legacy" "needs an arbimon.org sign-in" $it.Url }
                  else { Problem $it "failed" "unexpected redirect (HTTP $($r.Code))" }
                  return }
            404 { if ($path.StartsWith("/dl/")) {
                      $script:Short404++
                      if ($script:ShortOk -eq 0 -and $script:Short404 -ge 3) { $script:Expired = $true; return }
                      Problem $it "failed" "link not found or expired (HTTP 404)"; return }
                  Problem $it "failed" "HTTP 404"; return }
            410 { Problem $it "failed" "the file no longer exists on the server (HTTP 410)"; return }
            403 { Problem $it "failed" "access refused (HTTP 403) - please report this to contact@arbimon.org"; return }
            { $_ -in 0, 200, 206, 408, 425, 429, 500, 502, 503, 504 } { }
            default { Problem $it "failed" "HTTP $($r.Code)"; return }
        }
        if ($attempt -ge $Retries) {
            if ($r.Code -in 0, 200, 206) { Problem $it "failed" "network error (curl $rc); run the script again to resume" } else { Problem $it "failed" "HTTP $($r.Code)" }
            return
        }
        $delay = [math]::Min($BackoffCap, $BackoffBase * [math]::Pow(2, $attempt)) * (0.5 + (Get-Random -Minimum 0.0 -Maximum 0.5))
        $ra = 0.0
        if ([double]::TryParse("$($r.Headers['retry-after'])", [Globalization.NumberStyles]::Float, $Inv, [ref]$ra)) { $delay = [math]::Max($delay, [math]::Min(300, $ra)) }
        $attempt++
        if ($delay -gt 0) { Start-Sleep -Milliseconds ([int]($delay * 1000)) }
    }
    if ($path.StartsWith("/dl/")) { $script:ShortOk++ }
    $name = SafeName (DispositionName $r.Headers)
    if (-not $name) { $name = SafeName (QueryParam $it.Url "download-name") }
    if (-not $name) { $name = SafeName $it.Fallback }
    if (-not $name) { $name = $idn }
    $fileSize = (Get-Item -LiteralPath $part).Length
    $dot = $name.LastIndexOf(".")
    if ($dot -gt 0) { $stem = $name.Substring(0, $dot); $ext = $name.Substring($dot) } else { $stem = $name; $ext = "" }
    $key = "$($it.Kind)`t$($it.Id)"
    $rel = $null
    foreach ($cand in @($name, "${stem}__$idn$ext")) {
        $relc = "$folder/$cand"; $owner = $Claimed[$relc.ToLowerInvariant()]
        if ($owner -and $owner -ne $key) { continue }
        $final = Join-Path $OutAbs $relc
        if ((Test-Path -LiteralPath $final) -and -not $owner) {
            $same = ((Get-Item -LiteralPath $final).Length -eq $fileSize) -and ((Get-FileHash -LiteralPath $final).Hash -eq (Get-FileHash -LiteralPath $part).Hash)
            if ($same) { Remove-Item -LiteralPath $part -Force; $rel = $relc; break }
            if ($cand -eq $name) { continue }
        }
        Move-Item -LiteralPath $part -Destination $final -Force
        $rel = $relc; break
    }
    if (-not $rel) { Problem $it "failed" "could not find a free file name"; return }
    $Claimed[$rel.ToLowerInvariant()] = $key; $Done[$key] = $rel
    [IO.File]::AppendAllText($State, "$($it.Kind)`t$($it.Id)`t$rel`t$fileSize`n", $Utf8)
    $C["downloaded"]++
    Say ("  {0} ({1}) - {2}/{3} - about {4} left" -f $rel, (HumanBytes $fileSize), ($script:DoneN + 1), $Total, (HumanDuration (($Total - $script:DoneN - 1) * $Interval)))
}

Say "Saving to $OutAbs. Press Ctrl+C to stop; run again to resume."
Say ("One download starts every {0} seconds, to keep the load on Arbimon's servers low." -f (Fmt $Interval))
TimeEstimate ($Total - $Already)
if ($null -ne $Last -and ($Total - $Already) -gt 0) {
    $w0 = $Last + $Interval - (NowEpoch)
    if ($w0 -ge 1) { Say ("Waiting {0} seconds since the last run's final request..." -f [math]::Round($w0)) }
}
$Interrupted = $true
try {
    foreach ($it in $Items) {
        if ($Expired) { break }
        if ($it.UK -eq "empty") { Problem $it "no_url" "no link in the backup"; continue }
        if ($it.UK -eq "legacy") { Problem $it "legacy" "needs an arbimon.org sign-in" $it.Url; continue }
        $k = "$($it.Kind)`t$($it.Id)"
        if ($Done.ContainsKey($k) -and (Test-Path -LiteralPath (Join-Path $OutAbs $Done[$k]))) { $C["skipped"]++; $DoneN++; continue }
        Fetch $it
        $DoneN++
    }
    $Interrupted = $false
}
finally {
    if ($Interrupted) { Say ""; Say "Stopping... (partly downloaded files are kept and will resume next time)" }
    # ---------------------------------------------------------------- summary
    $report = $null
    if ($Problems.Count) {
        $report = Join-Path $OutAbs ("download-report-{0}.csv" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
        $Problems | Export-Csv -LiteralPath $report -NoTypeInformation -Encoding UTF8
    }
    Say ""
    Say ("Summary ({0}, {1} s)" -f $OutAbs, [math]::Round(((Get-Date) - $Started).TotalSeconds))
    Say ("  downloaded now:          {0} ({1})" -f $C["downloaded"], (HumanBytes $Bytes))
    Say ("  already downloaded:      {0}" -f $C["skipped"])
    if ($C["legacy"]) {
        Say ("  need an arbimon.org sign-in: {0} - older uploads that cannot be downloaded by a script." -f $C["legacy"])
        Say "      Open each link from the report in a browser while signed in to arbimon.org,"
        Say "      or ask contact@arbimon.org for help with a large number."
    }
    if ($C["no_url"]) { Say ("  no link in the backup:   {0}" -f $C["no_url"]) }
    if ($C["failed"]) { Say ("  failed:                  {0} - run the script again to retry them" -f $C["failed"]) } else { Say "  failed:                  0" }
    $recs = @($Done.GetEnumerator() | Where-Object { $_.Key.StartsWith("recordings`t") })
    if ($recs.Count) {
        $names = @{}
        foreach ($f in @(CsvFiles $Src "sites")) { foreach ($row in Import-Csv -LiteralPath $f -Encoding UTF8) { $names["$($row.site_id)".Trim()] = "$($row.name)".Trim() } }
        $counts = @{}
        foreach ($e in $recs) { $sid = $e.Value.Split("/")[1]; $counts[$sid] = 1 + [int]$counts[$sid] }
        Say "  recordings by site folder (recordings/<site_id>/):"
        $rows = foreach ($sid in ($counts.Keys | Sort-Object { $_.Length }, { $_ })) {
            Say ("      {0,-10} {1,6}  {2}" -f $sid, $counts[$sid], $names[$sid])
            [pscustomobject]@{ site_id = $sid; site_name = "$($names[$sid])"; files = $counts[$sid] }
        }
        $sitesCsv = Join-Path $OutAbs "recordings/sites.csv"
        $lines = @("site_id,site_name,files") + ($rows | ForEach-Object {
            $q = { param($s) if ($s -match '[",\r\n]') { '"' + $s.Replace('"', '""') + '"' } else { $s } }
            "{0},{1},{2}" -f (& $q $_.site_id), (& $q $_.site_name), $_.files })
        [IO.File]::WriteAllLines($sitesCsv, [string[]]$lines, $Utf8)
    }
    if ($report) { Say "  report: $report" }
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
if ($Expired) { Say ""; Say $ExpiredMessage; exit 3 }
if ($Interrupted) { exit 130 }
if ($C["failed"]) { exit 1 }
exit 0