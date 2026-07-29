# storm_start.ps1 - launch ionoTuna's storm watch DETACHED.
# Daemons must outlive the session that started them (Start-Process,
# never a chat background task). Logs: lab\storm_watch_log.txt
$py = "C:\Users\user\radioconda\python.exe"
$repo = Split-Path -Parent $PSScriptRoot

# refuse to double-start: one watcher only
$already = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "storm_watch\.py run" }
if ($already) {
    Write-Host "storm_watch already running (PID $($already.ProcessId)) - not starting a second."
    exit 0
}

Start-Process -FilePath $py `
    -ArgumentList "$repo\tools\storm_watch.py run" `
    -WorkingDirectory $repo -WindowStyle Hidden
Write-Host "ionoTuna storm watch started (hourly sniffs prio 20; storm sessions prio 90)."
Write-Host "Watch:  Get-Content $repo\lab\storm_watch_log.txt -Tail 20"
Write-Host "Status: $py $repo\tools\storm_watch.py status"
