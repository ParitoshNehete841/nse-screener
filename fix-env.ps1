<#
.SYNOPSIS
  Checks the screener's files and Python environment and fixes the known problems automatically.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\fix-env.ps1
#>
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$ok = $true

Write-Host "1) Checking app files in $root"
$files = "screener.py", "core.py", "strategies.py", "backtest.py", "replay.py", "paper.py", "glossary.py", "arrow_guard.py", "alert_bot.py", "news.py"
$missing = $files | Where-Object { -not (Test-Path (Join-Path $root $_)) }
if ($missing) {
    Write-Host "   MISSING: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "   Copy these files into $root (all files must be the latest version), then run this script again."
    exit 1
}
Write-Host "   all files present" -ForegroundColor Green

if (-not (Test-Path $py)) {
    Write-Host "No .venv found. Run setup-screener.ps1 first (or: py -3 -m venv .venv)." -ForegroundColor Red
    exit 1
}

function Test-Imports {
    $code = "import sys; sys.path.insert(0, r'$root'); import arrow_guard, pandas, numpy, yfinance, plotly, streamlit, core, strategies, backtest, replay, paper, glossary, news; print('IMPORTS_OK')"
    $text = (& $py -c $code 2>&1 | Out-String)
    return [pscustomobject]@{ Ok = ($text -match "IMPORTS_OK"); Text = $text }
}

Write-Host "2) Checking Python packages"
$r = Test-Imports
if (-not $r.Ok -and $r.Text -match "No module named") {
    Write-Host "   a package is missing - installing requirements..."
    & $py -m pip install -r (Join-Path $root "requirements.txt")
    $r = Test-Imports
}
if (-not $r.Ok -and $r.Text -match "pyarrow") {
    Write-Host "   Windows blocks pyarrow (or pandas cannot load it) - removing pyarrow, the app does not need it..."
    & $py -m pip uninstall -y pyarrow
    $r = Test-Imports
}
if ($r.Ok) {
    Write-Host "   imports OK" -ForegroundColor Green
} else {
    $ok = $false
    $line = ($r.Text -split "`n" | Where-Object { $_ -match "Error" } | Select-Object -Last 1)
    Write-Host "   STILL FAILING: $line" -ForegroundColor Red
    if ($r.Text -match "Application Control") {
        Write-Host "   Windows is blocking another compiled package. Without changing policy, the realistic fix is WSL2 (Ubuntu)."
    }
    Write-Host "   Send the full text below if you want help:"
    Write-Host $r.Text
}

Write-Host "3) Checking Yahoo Finance access"
$yf = (& $py -c "import yfinance as yf; d = yf.download('RELIANCE.NS', period='5d', progress=False); print('YAHOO_OK' if len(d) > 0 else 'YAHOO_EMPTY')" 2>&1 | Out-String)
if ($yf -match "YAHOO_OK") {
    Write-Host "   Yahoo reachable" -ForegroundColor Green
} else {
    Write-Host "   Yahoo returned nothing. Rate limiting (wait 10-15 min) or your network/VPN blocks it." -ForegroundColor Yellow
    Write-Host "   The app now shows this as a message inside the tab instead of crashing."
}

if ($ok) {
    Write-Host ""
    Write-Host "Ready. Start the app with:" -ForegroundColor Green
    Write-Host "  .\.venv\Scripts\python.exe -m streamlit run screener.py"
}
