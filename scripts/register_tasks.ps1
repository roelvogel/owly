# Register Windows Task Scheduler jobs for Owly (07:00 and 21:00)
# Run from an elevated PowerShell if task creation fails.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $PythonExe) {
        throw "Python not found. Create a venv at $ProjectRoot\.venv first."
    }
    Write-Warning "Using system Python: $PythonExe"
}

$RunCmd = "`"$PythonExe`" -m owly.run"
$WorkingDir = $ProjectRoot

function Register-OwlyTask {
    param(
        [string]$Name,
        [string]$Time
    )

    $existing = schtasks /Query /TN $Name 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removing existing task: $Name"
        schtasks /Delete /TN $Name /F | Out-Null
    }

    Write-Host "Registering task: $Name at $Time"
    schtasks /Create `
        /TN $Name `
        /TR $RunCmd `
        /SC DAILY `
        /ST $Time `
        /RL LIMITED `
        /F | Out-Null
}

Register-OwlyTask -Name "Owly Morning Edition" -Time "07:00"
Register-OwlyTask -Name "Owly Evening Edition" -Time "21:00"

Write-Host ""
Write-Host "Tasks registered successfully."
Write-Host "  - Owly Morning Edition: daily at 07:00"
Write-Host "  - Owly Evening Edition: daily at 21:00"
Write-Host ""
Write-Host "Optional: start dashboard at logon (uncomment below in this script)."
Write-Host "Dashboard: python -m owly.dashboard -> http://localhost:8741"

# Uncomment to register dashboard at logon:
# $DashCmd = "`"$PythonExe`" -m owly.dashboard"
# schtasks /Create /TN "Owly Dashboard" /TR $DashCmd /SC ONLOGON /RL LIMITED /F
