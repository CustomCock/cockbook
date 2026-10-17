# CockBook unter Windows starten: Backend und Oberflaeche in je einem Fenster, dann Browser.
# Doppelklick auf start.bat (ruft dieses Skript auf). Nur ASCII im Text: Windows PowerShell 5.1
# liest UTF-8 ohne BOM sonst falsch.
#
# Bitunix-Konto (optional): Keys NICHT hier eintragen. Stattdessen in einer PowerShell setzen
# und das Skript von dort starten (die Fenster erben die Variablen):
#   $env:BITUNIX_API_KEY="..."; $env:BITUNIX_API_SECRET="..."; .\start.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Fail($text) {
    Write-Host $text -ForegroundColor Red
    Read-Host "Enter zum Beenden"
    exit 1
}

function Need($cmd, $wingetId) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Fail ("'$cmd' fehlt. In PowerShell installieren mit:`n  winget install --id $wingetId -e`n" +
              "Danach alle PowerShell-Fenster schliessen und start.bat erneut starten.")
    }
}

Need "uv" "astral-sh.uv"
Need "node" "OpenJS.NodeJS.LTS"

Write-Host "Backend-Abhaengigkeiten pruefen (uv) ..." -ForegroundColor Cyan
uv sync --project backend
if ($LASTEXITCODE -ne 0) { Fail "uv sync ist fehlgeschlagen (siehe Meldung oben)." }

Write-Host "Frontend-Abhaengigkeiten pruefen (npm) ..." -ForegroundColor Cyan
Push-Location frontend
# npm.cmd statt npm: umgeht die Windows-Sperre fuer npm.ps1 (Ausfuehrungsrichtlinie)
npm.cmd install --no-fund --no-audit
$npmExit = $LASTEXITCODE
Pop-Location
if ($npmExit -ne 0) { Fail "npm install ist fehlgeschlagen (siehe Meldung oben)." }

$backend = "`$host.UI.RawUI.WindowTitle = 'CockBook Backend'; Set-Location '$root'; " +
           "uv run --project backend python -m cockbook --config config.yaml"
$frontend = "`$host.UI.RawUI.WindowTitle = 'CockBook Oberflaeche'; Set-Location '$root\frontend'; " +
            "npm.cmd run dev"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $backend
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $frontend

Write-Host "Starte ... der Browser oeffnet sich gleich." -ForegroundColor Green
Start-Sleep -Seconds 8
Start-Process "http://127.0.0.1:5173"
Write-Host "Beenden: in beiden CockBook-Fenstern Strg+C druecken bzw. sie schliessen."
