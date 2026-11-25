$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$configPath = Join-Path $env:USERPROFILE ".codex\config.toml"
$profilesPath = Join-Path $scriptDir "config\profiles.json"

if (-not (Test-Path $configPath)) {
    Write-Host "File config.toml non trovato." -ForegroundColor Red
    exit
}

$content = Get-Content $configPath -Raw
$isDeepSeek = $content -match 'model_provider\s*=\s*"deepseek"'

if ($isDeepSeek) {
    Write-Host "CODEX STATUS: DEEPSEEK ACTIVE" -ForegroundColor Cyan
} else {
    Write-Host "CODEX STATUS: OPENAI TOKEN-MINIMAL ACTIVE" -ForegroundColor Green
    if (Test-Path $profilesPath) {
        $profiles = Get-Content $profilesPath -Raw | ConvertFrom-Json
        $p = $profiles.openai
        
        Write-Host "`nDefault root:`n  $($p.root_model) / $($p.root_reasoning)" -ForegroundColor Cyan
        Write-Host "`nDynamic routing (low/medium/high/xhigh):" -ForegroundColor Cyan
        foreach ($model in @($p.fast_worker_model, $p.mid_worker_model, $p.strong_worker_model)) {
            if ($model) {
                foreach ($effort in @("low", "medium", "high", "xhigh")) {
                    $execution = if ($model -eq $p.fast_worker_model -and $effort -in @("low", "medium")) { "ROOT DIRECT" } elseif ($model -eq $p.fast_worker_model -or ($model -eq $p.mid_worker_model -and $effort -in @("low", "medium"))) { "worker" } else { "strong_worker" }
                    Write-Host "  $model $effort ($execution)" -ForegroundColor Cyan
                }
            }
        }
        Write-Host "`nAutomatic max effort:`n  $($p.auto_max_reasoning)" -ForegroundColor Cyan
        Write-Host "`nAutomatic Astra:`n  $($p.auto_astra)" -ForegroundColor Cyan
        Write-Host "`nStrong worker:`n  $($p.strong_worker_model) / $($p.strong_worker_reasoning) default`n  dynamic high override supported" -ForegroundColor Cyan
        Write-Host "`nReviewer:`n  $($p.reviewer_model) / $($p.reviewer_reasoning) default`n  high only for critical review" -ForegroundColor Cyan
    }
}
Write-Host ""
