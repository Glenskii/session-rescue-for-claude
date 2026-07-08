# ============================================================
# Claude Session Rescue: rebuild session state
# ============================================================
# Claude Desktop caches session state (including archived flags)
# in its IndexedDB, and reads THAT at startup instead of the
# session JSON files. This script:
#   1. Verifies Claude Desktop is fully closed
#   2. Restores every archived session in the JSON files
#   3. Renames the IndexedDB cache so the app rebuilds it from JSON
#   4. Nothing is deleted: full rollback is possible
#
# Run AFTER fully quitting Claude Desktop (tray icon included):
#   powershell -ExecutionPolicy Bypass -File rebuild_session_state.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$claudeData = Join-Path $env:APPDATA "Claude"
$idbDir = Join-Path $claudeData "IndexedDB"
$rescueScript = Join-Path $PSScriptRoot "claude_session_rescue.py"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

# ---- Step 1: confirm Claude Desktop is not running ----
$procs = Get-Process -Name "Claude*" -ErrorAction SilentlyContinue
if ($procs) {
    Write-Host "Claude Desktop is still running (found: $($procs.Name -join ', '))." -ForegroundColor Red
    Write-Host "Fully quit it first: right-click the tray icon and choose Quit."
    exit 1
}
Write-Host "[1/3] Claude Desktop is closed." -ForegroundColor Green

# ---- Step 2: restore all archived sessions in the JSON files ----
Write-Host "[2/3] Restoring archived sessions in JSON files..."
python $rescueScript --restore-all-archived
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python restore failed. Aborting before touching IndexedDB." -ForegroundColor Red
    exit 1
}

# ---- Step 3: rename the IndexedDB cache (reversible) ----
$originDir = Join-Path $idbDir "https_claude.ai_0.indexeddb.leveldb"
$blobDir = Join-Path $idbDir "https_claude.ai_0.indexeddb.blob"
$renamed = @()
foreach ($dir in @($originDir, $blobDir)) {
    if (Test-Path $dir) {
        $bak = "$dir.bak-$stamp"
        Rename-Item -Path $dir -NewName (Split-Path $bak -Leaf)
        $renamed += $bak
    }
}
if ($renamed.Count -gt 0) {
    Write-Host "[3/3] IndexedDB cache renamed (rollback available):" -ForegroundColor Green
    $renamed | ForEach-Object { Write-Host "      $_" }
} else {
    Write-Host "[3/3] No IndexedDB cache found to rename. Nothing changed there." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Launch Claude Desktop and check the sidebar." -ForegroundColor Cyan
Write-Host "If anything looks wrong: quit the app, delete the newly created"
Write-Host "IndexedDB folders, and remove '.bak-$stamp' from the renamed ones"
Write-Host "to restore the previous state exactly."
