# aitrack paigaldus Windowsi jaoks.
# Loob 'aitrack.cmd' käivitaja ja käivitab seadistusnõustaja.
# Kasuta:  powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# leia python (py launcher või python)
$Py = $null
foreach ($c in @("py", "python")) {
  $cmd = Get-Command $c -ErrorAction SilentlyContinue
  if ($cmd) { $Py = $cmd.Source; break }
}
if (-not $Py) {
  Write-Host "VIGA: Pythonit (3.9+) ei leitud. Paigalda: https://www.python.org/downloads/"
  Write-Host "(Paigaldamisel margi 'Add Python to PATH'.)"
  exit 1
}
Write-Host "==> Python: $Py"

# luba luhike 'aitrack' kask: aitrack.cmd kasutaja WindowsApps-kausta (tavaliselt PATH-is)
$BinDir = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
if (-not (Test-Path $BinDir)) { $BinDir = $Dir }
$Shim = Join-Path $BinDir "aitrack.cmd"
Set-Content -Path $Shim -Encoding ASCII -Value "@echo off`r`n`"$Py`" `"$Dir\aitrack.py`" %*"
Write-Host "==> Loodud kask: $Shim"
Write-Host "    Abi: aitrack help"

# kaivita seadistusnoustaja
Write-Host ""
& $Py "$Dir\aitrack.py" setup
