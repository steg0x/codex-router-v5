param(
    [string]$CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [switch]$PolicyOnly,
    [switch]$NoTelemetry
)

$ErrorActionPreference = "Stop"
$repoDir = $PSScriptRoot
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Set-TomlKeyInBlock {
    param(
        [string]$BlockText,
        [string]$Key,
        [string]$Line
    )
    $pattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*$'
    if ([regex]::IsMatch($BlockText, $pattern)) {
        return [regex]::Replace($BlockText, $pattern, $Line)
    }
    $trimmed = $BlockText.TrimEnd("`r", "`n")
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        return $Line + "`r`n"
    }
    return $trimmed + "`r`n" + $Line + "`r`n"
}

function Set-TomlSectionKeys {
    param(
        [string]$TomlText,
        [string]$SectionName,
        [hashtable]$KeyLines
    )
    $pattern = '(?ms)^(\[' + [regex]::Escape($SectionName) + '\]\s*\r?\n)(.*?)(?=^\[|\z)'
    $m = [regex]::Match($TomlText, $pattern)
    if ($m.Success) {
        $header = $m.Groups[1].Value
        $body = $m.Groups[2].Value
        foreach ($k in $KeyLines.Keys) {
            $body = Set-TomlKeyInBlock -BlockText $body -Key $k -Line $KeyLines[$k]
        }
        $replacement = $header + $body.TrimEnd("`r", "`n") + "`r`n`r`n"
        return $TomlText.Remove($m.Index, $m.Length).Insert($m.Index, $replacement)
    }
    $newSection = "`r`n[" + $SectionName + "]`r`n"
    foreach ($k in $KeyLines.Keys) {
        $newSection += $KeyLines[$k] + "`r`n"
    }
    return $TomlText.TrimEnd("`r", "`n") + "`r`n" + $newSection
}

function Update-CodexHooksJson {
    param(
        [string]$HooksJsonPath,
        [string]$HookCommand,
        [bool]$InstallHook
    )
    $pyCmd = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pyCmd) { $pyCmd = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if ($pyCmd) {
        $env:ROUTER_HOOKS_JSON = $HooksJsonPath
        $env:ROUTER_HOOK_CMD = $HookCommand
        $env:ROUTER_HOOK_INSTALL = if ($InstallHook) { "1" } else { "0" }
        $pyScript = @'
import json, os
from pathlib import Path

path = Path(os.environ["ROUTER_HOOKS_JSON"])
cmd = os.environ.get("ROUTER_HOOK_CMD", "")
install = os.environ["ROUTER_HOOK_INSTALL"] == "1"

if path.is_file():
    raw = path.read_text(encoding="utf-8").strip()
    data = json.loads(raw) if raw else {}
else:
    data = {}

if not isinstance(data, dict):
    data = {}
hooks = data.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
    data["hooks"] = hooks

stop_entries = hooks.get("Stop")
if not isinstance(stop_entries, list):
    stop_entries = []

# Remove any existing router_v5_stop_telemetry.py entries while keeping other Stop hooks
cleaned_stop = []
for group in stop_entries:
    if not isinstance(group, dict):
        continue
    inner = group.get("hooks")
    if isinstance(inner, list):
        filtered = [h for h in inner if not (isinstance(h, dict) and "router_v5_stop_telemetry.py" in str(h.get("command", "")))]
        if filtered:
            new_group = dict(group)
            new_group["hooks"] = filtered
            cleaned_stop.append(new_group)
    else:
        cleaned_stop.append(group)

if install:
    cleaned_stop.append({
        "hooks": [
            {
                "type": "command",
                "command": cmd,
                "timeout": 10
            }
        ]
    })

if cleaned_stop:
    hooks["Stop"] = cleaned_stop
elif "Stop" in hooks:
    del hooks["Stop"]

if hooks or path.is_file():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
'@
        $pyScript | & $pyCmd -
        if ($LASTEXITCODE -ne 0) { throw "Failed to update hooks.json via Python" }
        return
    }
    if (-not $InstallHook) { return }
    throw "Python is required to register the zero-token Stop hook (or re-run with -NoTelemetry)."
}

# 1. Ensure Codex directories exist
if (-not (Test-Path $CodexHome)) {
    New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
}
$agentsDir = Join-Path $CodexHome "agents"
if (-not (Test-Path $agentsDir)) {
    New-Item -ItemType Directory -Force -Path $agentsDir | Out-Null
}
$hooksDir = Join-Path $CodexHome "hooks"
if (-not (Test-Path $hooksDir)) {
    New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
}

# 2. Install subagent definitions (keep worker and strong-worker dynamic)
$copiedAgents = @()
foreach ($agentFile in @("worker.toml", "strong-worker.toml", "reviewer.toml")) {
    $src = Join-Path $repoDir "config\agents\$agentFile"
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $agentsDir $agentFile) -Force
        $copiedAgents += $agentFile
    }
}
$legacyRouterToml = Join-Path $agentsDir "router.toml"
if (Test-Path $legacyRouterToml) {
    Remove-Item $legacyRouterToml -Force
}

# 3. Install or remove zero-token Stop hook telemetry
$hooksJsonPath = Join-Path $CodexHome "hooks.json"
$hookTarget = Join-Path $hooksDir "router_v5_stop_telemetry.py"
$hookStatus = "disabilitato (-NoTelemetry)"
if (-not $NoTelemetry) {
    $hookSrc = Join-Path $repoDir "config\hooks\router_v5_stop_telemetry.py"
    if (-not (Test-Path $hookSrc)) {
        throw "Stop hook script not found: $hookSrc"
    }
    Copy-Item $hookSrc $hookTarget -Force
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pythonExe) { $pythonExe = "python" }
    $hookCmd = "$pythonExe `"$hookTarget`""
    Update-CodexHooksJson -HooksJsonPath $hooksJsonPath -HookCommand $hookCmd -InstallHook $true
    $hookStatus = "attivo (Stop hook a 0 token -> state_5.sqlite)"
} else {
    Update-CodexHooksJson -HooksJsonPath $hooksJsonPath -HookCommand "" -InstallHook $false
    if (Test-Path $hookTarget) { Remove-Item $hookTarget -Force }
}

# 4. Render and install AGENTS.md routing policy block
$policySrc = Join-Path $repoDir "config\openai-delegation-v5.md"
if (-not (Test-Path $policySrc)) {
    throw "Policy template not found: $policySrc"
}
$policyText = [System.IO.File]::ReadAllText($policySrc, $utf8NoBom)
$liveStateDir = (Join-Path $CodexHome "router_v5_live") + "\"
$hooksDirWin = $hooksDir + "\"
$policyText = $policyText.Replace("{{ROUTER_DIR}}", $repoDir).Replace("{{CODEX_HOME}}/router_v5_live/", $liveStateDir).Replace("{{CODEX_HOME}}/hooks/", $hooksDirWin).Replace("{{CODEX_HOME}}", $CodexHome)

if ($NoTelemetry) {
    $telemetryIdx = $policyText.IndexOf("## Zero-token automatic telemetry")
    if ($telemetryIdx -ge 0) {
        $policyText = $policyText.Substring(0, $telemetryIdx).TrimEnd("`r", "`n", " ", "`t")
    }
    $policyText += "`r`n`r`n- **Do NOT run shell commands to log routing telemetry.**"
}

$policyText = $policyText.TrimEnd("`r", "`n")
$block = "<!-- CODEX-TOKEN-MINIMAL START -->`r`n$policyText`r`n<!-- CODEX-TOKEN-MINIMAL END -->"

$agentsMdPath = Join-Path $CodexHome "AGENTS.md"
if (Test-Path $agentsMdPath) {
    $existingAgentsMd = [System.IO.File]::ReadAllText($agentsMdPath, $utf8NoBom)
} else {
    $existingAgentsMd = ""
}

$markerPattern = '(?ms)<!-- CODEX-TOKEN-MINIMAL START -->.*?<!-- CODEX-TOKEN-MINIMAL END -->'
if ([regex]::IsMatch($existingAgentsMd, $markerPattern)) {
    $newAgentsMd = [regex]::Replace($existingAgentsMd, $markerPattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $block })
} elseif ([string]::IsNullOrWhiteSpace($existingAgentsMd)) {
    $newAgentsMd = $block
} else {
    $newAgentsMd = $existingAgentsMd.TrimEnd("`r", "`n") + "`r`n`r`n" + $block
}
[System.IO.File]::WriteAllText($agentsMdPath, $newAgentsMd.TrimEnd("`r", "`n") + "`r`n", $utf8NoBom)

# 5. Idempotently patch config.toml (unless -PolicyOnly)
$configPatched = $false
if (-not $PolicyOnly) {
    $configPath = Join-Path $CodexHome "config.toml"
    $existingConfig = ""
    if (Test-Path $configPath) {
        Copy-Item $configPath ($configPath + ".bak") -Force
        $existingConfig = [System.IO.File]::ReadAllText($configPath, $utf8NoBom)
    }

    $sectionMatch = [regex]::Match($existingConfig, '(?m)^\[')
    if ($sectionMatch.Success) {
        $preamble = $existingConfig.Substring(0, $sectionMatch.Index)
        $rest = $existingConfig.Substring($sectionMatch.Index)
    } else {
        $preamble = $existingConfig
        $rest = ""
    }

    $preamble = Set-TomlKeyInBlock -BlockText $preamble -Key "model_provider" -Line 'model_provider = "openai"'
    $preamble = Set-TomlKeyInBlock -BlockText $preamble -Key "model" -Line 'model = "gpt-6-luna"'
    $preamble = Set-TomlKeyInBlock -BlockText $preamble -Key "model_reasoning_effort" -Line 'model_reasoning_effort = "medium"'

    $combined = $preamble.TrimEnd("`r", "`n") + "`r`n`r`n" + $rest.TrimStart("`r", "`n")
    if ([string]::IsNullOrWhiteSpace($rest)) {
        $combined = $preamble.TrimEnd("`r", "`n") + "`r`n"
    }

    $combined = Set-TomlSectionKeys -TomlText $combined -SectionName "features" -KeyLines @{
        "multi_agent" = "multi_agent = true"
    }
    $combined = Set-TomlSectionKeys -TomlText $combined -SectionName "agents" -KeyLines @{
        "default_subagent_model" = 'default_subagent_model = "gpt-6-luna"'
        "default_subagent_reasoning_effort" = 'default_subagent_reasoning_effort = "medium"'
    }

    [System.IO.File]::WriteAllText($configPath, $combined.TrimStart("`r", "`n").TrimEnd("`r", "`n") + "`r`n", $utf8NoBom)
    $configPatched = $true
}

Write-Host ""
Write-Host "Codex Router V5.1 installato con successo!" -ForegroundColor Green
Write-Host "  Codex Home   : $CodexHome" -ForegroundColor Cyan
Write-Host "  Sub-agenti   : $($copiedAgents -join ', ')" -ForegroundColor Cyan
Write-Host "  Telemetria   : $hookStatus" -ForegroundColor Cyan
Write-Host "  AGENTS.md    : aggiornato" -ForegroundColor Cyan
Write-Host "  config.toml  : $(if ($configPatched) { 'aggiornato (backup .bak salvato se preesistente)' } else { 'saltato (-PolicyOnly)' })" -ForegroundColor Cyan
Write-Host ""
Write-Host "Prossimo passo: riavvia Codex e apri una NUOVA chat (consigliato: permessi 'Accesso completo')." -ForegroundColor Yellow
