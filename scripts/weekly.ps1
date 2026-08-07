# Weekly end-to-end refresh, for Task Scheduler.
#
# Spans two repositories on purpose. The metadata sync belongs to
# danbooru_metadata (it owns the 46 GB database); the index rebuild and publish
# belong here. Neither repo reaches into the other's scripts -- this driver is
# the only thing that knows both exist.
#
# Credentials come from danbooru_metadata\.env (copy .env.example), never from
# arguments: anything on the command line lands in the task definition, the
# process list, and shell history. Without them the sync still runs, anonymously
# and much slower. Each sync script reports which path it took on its first line.

param(
    [string]$MetadataRepo = 'E:\danbooru_metadata',
    [string]$IndexRepo    = 'E:\danbooru-tag-index',
    [string]$LogDir       = 'E:\danbooru-tag-index\logs',
    [switch]$SkipSync
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$log = Join-Path $LogDir "weekly_$stamp.log"

function Log($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $message
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# Windows PowerShell 5.1's Tee-Object has no -Encoding and writes UTF-16LE, which
# against a UTF-8 log produces a file that is half readable and half spaced-out
# nulls. Route child output through Add-Content instead so one encoding wins.
function Tee-Log {
    process {
        Write-Output $_
        Add-Content -Path $log -Value $_ -Encoding utf8
    }
}

# The scripts print Chinese and CJK names; a cp932/gbk console would throw on
# them mid-run and kill an otherwise healthy rebuild.
$env:PYTHONIOENCODING = 'utf-8'

$started = Get-Date
Log "start  metadata=$MetadataRepo  index=$IndexRepo"

try {
    if (-not $SkipSync) {
        Log 'sync: posts'
        Push-Location $MetadataRepo
        uv run python scripts/update_danbooru.py 2>&1 | Tee-Log
        Log 'sync: wiki, tags, aliases, artists'
        uv run python scripts/update_wiki.py 2>&1 | Tee-Log
        Pop-Location
    } else {
        Log 'sync: skipped'
    }

    Log 'rebuild + publish'
    Push-Location $IndexRepo
    uv run python scripts/refresh.py --publish 2>&1 | Tee-Log
    Pop-Location

    Log ("done in {0:n1} min -- {1}" -f ((Get-Date) - $started).TotalMinutes, $log)
}
catch {
    Log "FAILED: $_"
    Log ("aborted after {0:n1} min -- {1}" -f ((Get-Date) - $started).TotalMinutes, $log)
    exit 1
}
