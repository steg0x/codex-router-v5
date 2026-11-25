# Codex Router V5.1 (12-Tier Luna / Terra / Sol 6.2)

Sistema di routing cognitivo automatico a 12 tier per **Codex** (`gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.2-sol`), con sub-agenti dinamici, escalation progressiva e telemetria locale leggera.

## Matrice di routing a 12 tier

| Famiglia | Tier (`effort`) | Esecuzione | Ruolo |
| --- | --- | --- | --- |
| **`gpt-6-luna`** | `low`, `medium` | `ROOT DIRECT` | Letture rapide, comandi immediati, fix locali su 1–2 file |
| **`gpt-6-luna`** | `high`, `xhigh` | `spawn worker` | Implementazioni multi-file, test, automazioni e debug lineare |
| **`gpt-6.1-terra`** | `low`, `medium` | `spawn worker` | Sintesi multi-step, coordinamento multi-modulo, refactoring |
| **`gpt-6.1-terra`** | `high`, `xhigh` | `spawn strong_worker` | Ingegneria multi-componente complessa e vincoli incrociati |
| **`gpt-6.2-sol`** | `low`, `medium`, `high`, `xhigh` | `spawn strong_worker` | Design/architettura, grafica vettoriale, audit critici e task ad alta incertezza |

*(Nota: `max`, `ultra` e `gpt-6-astra` restano solo manuali).*

---

## Installazione su un nuovo PC

### Windows (PowerShell)

```powershell
git clone https://github.com/steg0x/codex-router-v5.git
cd codex-router-v5
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### macOS / Linux

```bash
git clone https://github.com/steg0x/codex-router-v5.git
cd codex-router-v5
bash ./install.sh
```

> [!TIP]
> **Telemetria automatica a ZERO token (Codex `Stop` hook)**:
> Il Root **non** esegue mai comandi shell per loggare la telemetria (risparmiando 1 turno completo di inferenza Root per ogni sub-agente). Al termine del turno, l'hook `Stop` (`~/.codex/hooks/router_v5_stop_telemetry.py`) legge automaticamente `state_5.sqlite` in sola lettura e registra modello reale, effort, `tokens_used` e durata in `router_v5_live/`.
> Se su un PC non hai Python o non vuoi nemmeno la telemetria via hook, aggiungi `-NoTelemetry` (Windows) o `--no-telemetry` (macOS/Linux).

### Altri flag disponibili
* `-PolicyOnly` / `--policy-only`: aggiorna `AGENTS.md`, i sub-agenti in `agents/` e il `Stop` hook senza modificare `config.toml`.
* `-CodexHome <percorso>` / `--codex-home <percorso>`: specifica una cartella `.codex` personalizzata.

---

## Cosa fa l'installer (in modo idempotente)

1. Copia `worker.toml`, `strong-worker.toml` e `reviewer.toml` in `~/.codex/agents/` (rimuovendo eventuale `router.toml` legacy).
2. Installa `~/.codex/hooks/router_v5_stop_telemetry.py` e registra l'hook `Stop` dentro `~/.codex/hooks.json` **preservando intatti eventuali altri hook esistenti** (es. `PreToolUse`).
3. Inserisce o aggiorna il blocco `<!-- CODEX-TOKEN-MINIMAL START --> ... <!-- CODEX-TOKEN-MINIMAL END -->` dentro `~/.codex/AGENTS.md` **senza cancellare** altre tue istruzioni personali presenti nel file.
4. Crea un backup `config.toml.bak` e imposta in `~/.codex/config.toml`:
   * `model_provider = "openai"`, `model = "gpt-6-luna"`, `model_reasoning_effort = "medium"`
   * `[features] multi_agent = true`
   * `[agents] default_subagent_model = "gpt-6-luna"` e `default_subagent_reasoning_effort = "medium"`
   *(preservando tutte le altre sezioni esistenti come `mcp_servers`, `projects`, `plugins`, ecc.)*.

---

## Impostazioni consigliate in Codex Desktop

1. **Riavvia Codex** e apri sempre una **nuova chat** dopo l'installazione o un aggiornamento.
2. Imposta i permessi della chat su **`Accesso completo`**, così il Root può spawnare i sub-agenti (`worker`, `strong_worker`) e scrivere la telemetria senza blocchi sandbox.

---

## Aggiornare il router su tutti i PC

Quando vengono aggiornati i modelli o le regole nella repo:

```bash
git pull
# su Windows:
powershell -ExecutionPolicy Bypass -File .\install.ps1
# su macOS/Linux:
bash ./install.sh
```

---

## Eseguire la test suite

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```
