$backupDir = Join-Path $PSScriptRoot 'backup'
$configPath = Join-Path $env:USERPROFILE '.codex\config.toml'
if (-not (Test-Path $backupDir)) { Write-Host 'Nessun backup trovato.'; exit }

$latest = Get-ChildItem -Path $backupDir -Filter '*_config.toml.bak' | Sort-Object CreationTime -Descending | Select-Object -First 1
if (-not $latest) { Write-Host 'Nessun backup trovato.'; exit }

Write-Host 'Trovato ultimo backup:' $latest.Name -ForegroundColor Yellow
$choice = Read-Host 'Vuoi ripristinare questo backup? (s/n)'
if ($choice -match '^s') {
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'Codex' -or $_.ProcessName -match 'codex-windows-sandbox' } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Copy-Item $latest.FullName $configPath -Force
    Write-Host 'Backup ripristinato con successo.' -ForegroundColor Green
    
    $app = Get-StartApps | Where-Object { $_.Name -match 'Codex' } | Select-Object -First 1
    if ($app) {
        Start-Process explorer.exe "shell:AppsFolder\$($app.AppID)"
    }
}
