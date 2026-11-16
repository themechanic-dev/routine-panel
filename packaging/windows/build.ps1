# Builds Routine Panel for Windows: RoutinePanel.exe with PyInstaller, then the Setup with Inno Setup.
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Python <python.exe> -Version 0.2.0
# Run from the folder that holds routine_panel\ and packaging\. Writes out\routine-panel-setup-<version>.exe,
# and build.log; build.done (or build.failed) when it ends, for whoever waits on it.
param(
    [string]$Python = "python",
    [string]$Version = "0.0.0",
    [string]$Iscc = ""
)
$ErrorActionPreference = "Stop"
$root = (Get-Location).Path
Remove-Item -Force -ErrorAction SilentlyContinue "$root\build.done", "$root\build.failed"
Start-Transcript -Path "$root\build.log" -Force | Out-Null
try {
    & $Python -c "import sys; print(sys.version)"
    & $Python -m PyInstaller --noconfirm --clean --windowed --name RoutinePanel `
        --icon "$root\packaging\windows\routine-panel.ico" `
        --add-data "$root\routine_panel\web;routine_panel\web" `
        --paths "$root" --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" `
        "$root\packaging\windows\launcher.py"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    if (-not $Iscc) {
        $Iscc = @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            "C:\Users\*\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | ForEach-Object { Get-Item $_ -ErrorAction SilentlyContinue } | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) was not found" }
    & $Iscc "/DVersion=$Version" "/DSource=$root\dist\RoutinePanel" "/O$root\out" "$root\packaging\windows\routine-panel.iss"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
    Get-FileHash "$root\out\routine-panel-setup-$Version.exe" -Algorithm SHA256 | Format-List
    "done" | Out-File "$root\build.done"
} catch {
    "$_" | Out-File "$root\build.failed"
    Write-Output "FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
}
