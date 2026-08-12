# atlas_start.ps1 - launch the Propagation Observatory DETACHED.
# Daemons must outlive the session that started them (Start-Process,
# never a chat background task). Logs: lab\prop_atlas_log.txt
$py = "$env:USERPROFILE\radioconda\python.exe"
$repo = Split-Path -Parent $PSScriptRoot

# refuse to double-start: one metronome only
$already = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "prop_atlas\.py run" }
if ($already) {
    Write-Host "prop_atlas already running (PID $($already.ProcessId)) - not starting a second."
    exit 0
}

Start-Process -FilePath $py `
    -ArgumentList "$repo\tools\prop_atlas.py run" `
    -WorkingDirectory $repo -WindowStyle Hidden
Write-Host "Propagation Observatory started (30-min sweeps, warden priority 20)."
Write-Host "Watch:  Get-Content $repo\lab\prop_atlas_log.txt -Tail 20"
Write-Host "Status: $py $repo\tools\prop_atlas.py status"
