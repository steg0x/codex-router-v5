param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("openai","deepseek")]
    [string]$Mode,
    [switch]$NoRestart,
    [switch]$PolicyOnly,
    [ValidateSet("v4_1","v5")]
    [string]$RoutingPolicy
)

$scriptDir = $PSScriptRoot
$configPath = Join-Path $env:USERPROFILE ".codex\config.toml"
$catalogPath = Join-Path $env:USERPROFILE ".codex\models.json"
$profilesPath = Join-Path $scriptDir "config\profiles.json"
$logPath = Join-Path $scriptDir "switcher.log"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$routingPolicyPath = Join-Path $scriptDir "config\live-routing-policy.txt"
$activeRoutingPolicy = if ($RoutingPolicy) { $RoutingPolicy } elseif (Test-Path $routingPolicyPath) {
    [System.IO.File]::ReadAllText($routingPolicyPath).Trim()
} else { "v4_1" }
if ($activeRoutingPolicy -notin @("v4_1", "v5")) { throw "Invalid saved routing policy" }
$delegationName = if ($activeRoutingPolicy -eq "v5") { "openai-delegation-v5.md" } else { "openai-delegation.md" }

function Write-Log($message) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "$ts - $message"
}

if (-not (Test-Path $configPath)) {
    Write-Host "Config Codex non trovato: $configPath" -ForegroundColor Red
    Write-Log "FAIL: Config not found"
    if (-not $NoRestart) { Read-Host "Premi Invio per chiudere" }
    exit 1
}
if (-not (Test-Path $profilesPath)) {
    Write-Host "Profili non trovati: $profilesPath" -ForegroundColor Red
    Write-Log "FAIL: Profiles not found"
    if (-not $NoRestart) { Read-Host "Premi Invio per chiudere" }
    exit 1
}

$profiles = Get-Content $profilesPath | ConvertFrom-Json
$profile = if ($Mode -eq "openai") { $profiles.openai } else { $profiles.deepseek }

if (-not $NoRestart) {
    # Chiude Codex per evitare conflitti
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -match "Codex" -or $_.ProcessName -match "codex-windows-sandbox" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

$backupDir = Join-Path $scriptDir "backup"
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$backupFile = Join-Path $backupDir "${stamp}_config.toml.bak"

try {
    if (-not $PolicyOnly) {
        # Backup
        Copy-Item $configPath $backupFile
        Write-Log "Created backup: $backupFile"

        # Rotazione backup (mantieni ultimi 10)
        $backups = Get-ChildItem -Path $backupDir -Filter "*_config.toml.bak" | Sort-Object CreationTime -Descending
        if ($backups.Count -gt 10) {
            $backups | Select-Object -Skip 10 | Remove-Item -Force
        }

        $content = [System.IO.File]::ReadAllText($configPath)

    # Separa preambolo e sezioni
    $sectionMatch = [regex]::Match($content, '(?m)^\[')
    if ($sectionMatch.Success) {
        $preamble = $content.Substring(0, $sectionMatch.Index)
        $rest = $content.Substring($sectionMatch.Index)
    } else {
        $preamble = $content
        $rest = ""
    }

    # Rimuovi vecchie chiavi globali
    $keys = @("model", "model_provider", "model_reasoning_effort", "default_subagent_model", "default_subagent_reasoning_effort", "model_catalog_json")
    foreach ($key in $keys) {
        $pattern = '(?m)^\s*' + [regex]::Escape($key) + '\s*=.*(?:\r?\n|$)'
        $preamble = [regex]::Replace($preamble, $pattern, '')
    }
    # Rimuovi eventuale blocco developer_instructions multiriga
    $preamble = [regex]::Replace($preamble, '(?s)developer_instructions\s*=\s*''''''(.*?)''''''\r?\n?', "")
    $preamble = [regex]::Replace($preamble, '(?s)developer_instructions\s*=\s*"""(.*?)"""\r?\n?', "")

    $prefix = ""

    if ($Mode -eq "openai") {
        $prefix += "model_provider = ""$($profile.provider)""`r`n"
        $prefix += "model = ""$($profile.root_model)""`r`n"
        $prefix += "model_reasoning_effort = ""$($profile.root_reasoning)""`r`n"
        
        $delegationFile = Join-Path $scriptDir "config\$delegationName"

        
        # Patch [agents] section robustly
        $pattern = '(?ms)^\[agents\]\s*\r?\n(.*?)(?=^\[|\z)'
        $match = [regex]::Match($rest, $pattern)
        
        if ($match.Success) {
            $body = $match.Groups[1].Value
            $body = [regex]::Replace($body, '(?m)^\s*default_subagent_model\s*=.*(?:\r?\n|$)', '')
            $body = [regex]::Replace($body, '(?m)^\s*default_subagent_reasoning_effort\s*=.*(?:\r?\n|$)', '')
            $body = $body.TrimEnd() + "`r`ndefault_subagent_model = ""$($profile.fast_worker_model)""`r`ndefault_subagent_reasoning_effort = ""$($profile.fast_worker_reasoning)""`r`n`r`n"
            $rest = $rest.Remove($match.Index, $match.Length).Insert($match.Index, "[agents]`r`n" + $body)
        } else {
            $rest = $rest.TrimEnd() + "`r`n`r`n[agents]`r`ndefault_subagent_model = ""$($profile.fast_worker_model)""`r`ndefault_subagent_reasoning_effort = ""$($profile.fast_worker_reasoning)""`r`n`r`n"
        }

        if ($rest -match '(?m)^\[features\]') {
            if ($rest -match '(?m)^multi_agent\s*=') {
                $rest = [regex]::Replace($rest, '(?m)^multi_agent\s*=.*$', "multi_agent = true")
            } else {
                $rest = [regex]::Replace($rest, '(?m)^\[features\]\s*\r?\n', "[features]`r`nmulti_agent = true`r`n")
            }
        }
        Write-Log "SWITCH TO OPENAI SUCCESS"
    } else {
        # DeepSeek
        $catalogNormalized = $catalogPath.Replace('\','/')
        $prefix += "model_provider = ""$($profile.provider)""`r`n"
        $prefix += "model = ""$($profile.model)""`r`n"
        $prefix += "model_catalog_json = ""$catalogNormalized""`r`n"
        
        # Disabilita agents
        if ($rest -match '(?m)^\[features\]') {
            if ($rest -match '(?m)^multi_agent\s*=') {
                $rest = [regex]::Replace($rest, '(?m)^multi_agent\s*=.*$', "multi_agent = false")
            } else {
                $rest = [regex]::Replace($rest, '(?m)^\[features\]\s*\r?\n', "[features]`r`nmulti_agent = false`r`n")
            }
        }

        # DeepSeek patch per modelli 1M
        if (Test-Path $catalogPath) {
            try {
                $catJson = Get-Content $catalogPath -Raw | ConvertFrom-Json
                $updatedCatalog = $false
                foreach ($m in $catJson.models) {
                    if ($m.slug -match "deepseek") {
                        if ($m.context_window -ne 1048576) {
                            $m.context_window = 1048576
                            $m.max_context_window = 1048576
                            $m.effective_context_window_percent = 95
                            $updatedCatalog = $true
                        }
                    }
                }
                if ($updatedCatalog) {
                    $utf8 = New-Object System.Text.UTF8Encoding($false)
                    [System.IO.File]::WriteAllText($catalogPath, ($catJson | ConvertTo-Json -Depth 15), $utf8)
                }
            } catch {
                Write-Log "Warning: models.json patch failed $_"
            }
        }

        # Auth patch per deepseek
        if ($rest -match '(?m)^\[model_providers\.deepseek\]\s*$') {
            $blockMatch = [regex]::Match($rest, '(?ms)^\[model_providers\.deepseek\]\s*\r?\n(?<body>.*?)(?=^\[|\z)')
            if ($blockMatch.Success -and $blockMatch.Groups["body"].Value -notmatch '(?m)^\s*requires_openai_auth\s*=') {
                $replacement = $blockMatch.Value.TrimEnd() + "`r`nrequires_openai_auth = false`r`n"
                $rest = $rest.Remove($blockMatch.Index, $blockMatch.Length).Insert($blockMatch.Index, $replacement)
            }
        }
        Write-Log "SWITCH TO DEEPSEEK SUCCESS"
    }

    $preamble = $prefix + "`r`n" + $preamble.TrimStart()
    $newContent = $preamble.TrimEnd() + "`r`n`r`n" + $rest.TrimStart()
    
    # Scrittura atomica
    $tmpFile = "$configPath.tmp"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmpFile, $newContent, $utf8NoBom)
    
    Remove-Item $configPath -Force
    Rename-Item $tmpFile (Split-Path $configPath -Leaf)
    } # end if (-not $PolicyOnly)

    # Patch AGENTS.md
    $agentsMdPath = Join-Path $env:USERPROFILE ".codex\AGENTS.md"
    if (Test-Path $agentsMdPath) {
        $agentsContent = [System.IO.File]::ReadAllText($agentsMdPath)
    } else {
        $agentsContent = ""
    }
    
    $agentsContent = [regex]::Replace($agentsContent, '(?ms)<!-- CODEX-TOKEN-MINIMAL START -->.*?<!-- CODEX-TOKEN-MINIMAL END -->\r?\n?', "")
    
    $targetRouterToml = Join-Path $env:USERPROFILE ".codex\agents\router.toml"
    $sourceRouterToml = Join-Path $scriptDir "config\agents\router.toml"

    if ($Mode -eq "openai") {
        $codexHome = Join-Path $env:USERPROFILE ".codex"
        $hooksDir = Join-Path $codexHome "hooks"
        if (-not (Test-Path $hooksDir)) { New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null }
        $srcStopHook = Join-Path $scriptDir "config\hooks\router_v5_stop_telemetry.py"
        if (Test-Path $srcStopHook) {
            Copy-Item $srcStopHook (Join-Path $hooksDir "router_v5_stop_telemetry.py") -Force
        }
        $delegationFile = Join-Path $scriptDir "config\$delegationName"
        if (Test-Path $delegationFile) {
            $delegationText = [System.IO.File]::ReadAllText($delegationFile)
            $liveStateDir = (Join-Path $codexHome "router_v5_live") + "\"
            $hooksDirWin = $hooksDir + "\"
            $delegationText = $delegationText.Replace("{{ROUTER_DIR}}", $scriptDir).Replace("{{CODEX_HOME}}/router_v5_live/", $liveStateDir).Replace("{{CODEX_HOME}}/hooks/", $hooksDirWin).Replace("{{CODEX_HOME}}", $codexHome)
            $block = "<!-- CODEX-TOKEN-MINIMAL START -->`r`n$delegationText`r`n<!-- CODEX-TOKEN-MINIMAL END -->`r`n"
            $agentsContent = $agentsContent.TrimEnd() + "`r`n`r`n" + $block
        }
        $agentsDir = Join-Path $codexHome "agents"
        if (-not (Test-Path $agentsDir)) { New-Item -ItemType Directory -Force -Path $agentsDir | Out-Null }
        foreach ($agentFile in @("worker.toml", "strong-worker.toml", "reviewer.toml")) {
            $srcAgent = Join-Path $scriptDir "config\agents\$agentFile"
            if (Test-Path $srcAgent) {
                Copy-Item $srcAgent (Join-Path $agentsDir $agentFile) -Force
            }
        }
        # Sincronizza router.toml solo per V4.1; in V5.1 il routing e' diretto
        if ($activeRoutingPolicy -eq "v4_1" -and (Test-Path $sourceRouterToml)) {
            Copy-Item $sourceRouterToml $targetRouterToml -Force
            Write-Log "Synced router.toml to $targetRouterToml"
        } elseif ($activeRoutingPolicy -eq "v5" -and (Test-Path $targetRouterToml)) {
            Remove-Item $targetRouterToml -Force
            Write-Log "Removed legacy V4.1 router.toml in V5 mode"
        }
    } else {
        # DeepSeek: rimuove router.toml per mantenere single-model pulito
        if (Test-Path $targetRouterToml) {
            Remove-Item $targetRouterToml -Force
            Write-Log "Removed router.toml in DeepSeek mode"
        }
    }
    
    if ($agentsContent.Trim() -eq "") {
        if (Test-Path $agentsMdPath) { Remove-Item $agentsMdPath -Force }
    } else {
        [System.IO.File]::WriteAllText($agentsMdPath, $agentsContent.TrimStart() + "`r`n", $utf8NoBom)
    }

    if ($Mode -eq "openai" -and $RoutingPolicy) {
        [System.IO.File]::WriteAllText($routingPolicyPath, $activeRoutingPolicy + "`n", $utf8NoBom)
        Write-Log "Routing policy set to $activeRoutingPolicy"
    }

    if (-not $NoRestart) {
        # Riapre Codex
        $app = Get-StartApps | Where-Object { $_.Name -match 'Codex' } | Select-Object -First 1
        if ($app) {
            Start-Process explorer.exe "shell:AppsFolder\$($app.AppID)"
        } else {
            Write-Host "Configurazione applicata. Apri Codex dal menu Start."
        }
    } else {
        Write-Host "Modalita NoRestart: Codex Desktop non e stato riavviato." -ForegroundColor Yellow
    }

    Write-Host ""
    if ($Mode -eq "openai") {
        Write-Host "CODEX PROFILE: OPENAI TOKEN-MINIMAL" -ForegroundColor Green
        Write-Host "Provider       : $($profile.provider)" -ForegroundColor Cyan
        Write-Host "Root model     : $($profile.root_model) / $($profile.root_reasoning)" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "Dynamic Routing (Model + Effort):" -ForegroundColor Cyan
        if ($activeRoutingPolicy -eq "v5") {
            foreach ($model in @($profile.fast_worker_model, $profile.mid_worker_model, $profile.strong_worker_model)) {
                foreach ($effort in @("low", "medium", "high", "xhigh")) {
                    $execution = if ($model -eq $profile.fast_worker_model -and $effort -in @("low", "medium")) { "ROOT DIRECT" } elseif ($model -eq $profile.fast_worker_model -or ($model -eq $profile.mid_worker_model -and $effort -in @("low", "medium"))) { "worker" } else { "strong_worker" }
                    Write-Host "  $model $effort ($execution)" -ForegroundColor Cyan
                }
            }
        } else {
        Write-Host "  $($profile.fast_worker_model) $($profile.fast_worker_reasoning)" -ForegroundColor Cyan
        Write-Host "  $($profile.fast_worker_model) $($profile.auto_max_reasoning)" -ForegroundColor Cyan
        Write-Host "  $($profile.strong_worker_model) $($profile.strong_worker_reasoning)" -ForegroundColor Cyan
        Write-Host "  $($profile.strong_worker_model) $($profile.auto_max_reasoning)" -ForegroundColor Cyan
        }
        Write-Host ""
        Write-Host "Auto Max Reason: $($profile.auto_max_reasoning)" -ForegroundColor Cyan
        Write-Host "Auto Astra     : disabled" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "Strong Worker  : $($profile.strong_worker_model) / $($profile.strong_worker_reasoning) default (dynamic effort override)" -ForegroundColor Cyan
        Write-Host "Reviewer       : $($profile.reviewer_model) / $($profile.reviewer_reasoning) default (high only for critical)" -ForegroundColor Cyan
        Write-Host "Subagents      : Enabled (multi_agent = true)" -ForegroundColor Cyan
        Write-Host "Delegation     : Active ($activeRoutingPolicy policy injected)" -ForegroundColor Cyan
    } else {
        Write-Host "CODEX PROFILE: DEEPSEEK" -ForegroundColor Green
        Write-Host "Provider       : $($profile.provider)" -ForegroundColor Cyan
        Write-Host "Model          : $($profile.model)" -ForegroundColor Cyan
        Write-Host "Subagents      : Disabled (multi_agent = false)" -ForegroundColor Cyan
        Write-Host "Routing        : Single model (No worker configured)" -ForegroundColor Cyan
    }
    Write-Host ""
    Write-Host "IMPORTANTE: Apri sempre una NUOVA chat dopo il cambio provider." -ForegroundColor Yellow
    if (-not $NoRestart) { Start-Sleep -Seconds 3 }

} catch {
    Write-Host "Errore durante lo switch: $_" -ForegroundColor Red
    Write-Log "FAIL: Error during switch - $_"
    # Rollback automatico
    if (Test-Path $backupFile) {
        Copy-Item $backupFile $configPath -Force
        Write-Host "Ripristinato backup precedente." -ForegroundColor Yellow
        Write-Log "Rolled back to $backupFile"
    }
    if (-not $NoRestart) {
        Read-Host "Premi Invio per chiudere"
    }
}
