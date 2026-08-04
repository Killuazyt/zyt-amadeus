# Amadeus Desktop

P7 adds reproducible Windows packaging, legacy-architecture auditing, and a clean-machine installer
acceptance workflow on top of the complete P6 settings, tray, proactive-interaction, and local-data-management
closure and the tested P1 application foundation, P2 desktop-pet engine, P3 attached chat flow, P4 secure
text-model integration, and P5 auditable hybrid memory. The application includes safe local pet
import/switch/removal, fixed-clock animation, transparent hit testing, manual drag placement,
multi-screen recovery, a bundled CC0 placeholder pet, cancellable chat states, and an exact
eight-item system tray.

P4 provides DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom HTTPS provider. API
credentials are stored only in Windows Credential Manager. P5 stores conversations, summaries,
immutable memory versions, provenance, jobs, independent user/persona vector generations, recall
events, decay, and safe FTS fallback. P6 upgrades to SQLite schema v3 and settings schema v5, adds
proactive message origin and a body-free interaction ledger, and exposes an eight-page single
settings window, verified HKCU startup control, restrained greetings, diagnostics, versioned JSON
exports, consistent backup/restore, and fail-closed factory reset. Speech, screen observation, tool
execution, and automation remain outside the MVP.

## Development commands

Run these commands from PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\scripts\run.ps1
.\scripts\check.ps1
.\scripts\build.ps1
```

Import the local transitional pet while Amadeus is fully stopped:

```powershell
.\scripts\import-pet.ps1 -Path 'D:\path\to\pet.codex-pet'
```

Preview every action or export private contact-sheet evidence:

```powershell
.\scripts\preview-pet.ps1 -Path 'D:\path\to\pet.codex-pet'
.\scripts\preview-pet.ps1 -Path 'D:\path\to\pet.codex-pet' -ExportDir 'D:\private-evidence'
```

Run the manual 50-click plus 50-drag acceptance counter:

```powershell
.\scripts\accept-pet-input.ps1 -Path 'D:\path\to\pet.codex-pet' -Output 'D:\private-evidence\input.json'
```

To regenerate committed dependency locks after intentionally changing `pyproject.toml`:

```powershell
.\scripts\lock.ps1
```

The application entry point is:

```powershell
.\.venv\Scripts\python.exe -m amadeus_desktop
```

The deterministic local provider is development-only and must be requested explicitly:

```powershell
.\.venv\Scripts\python.exe -m amadeus_desktop --mock-chat
```

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`. Imported pets are copied into its `pets`
directory; the source package is never modified. SQLite is stored at `data\amadeus.sqlite3`.
Provider settings contain only a fixed credential reference; API credentials are stored by Windows
Credential Manager and are never written to JSON, SQLite, exports, or backups.

The application never downloads an embedding model during startup or chat. Prepare and verify the
pinned model only through an explicit command:

```powershell
.\.venv\Scripts\amadeus-model.exe prepare
.\.venv\Scripts\amadeus-model.exe verify
```

Import local persona JSONL while the application is stopped; command output contains only a count
and a safe error category:

```powershell
.\.venv\Scripts\amadeus-persona.exe import
```

Run aggregate-only offline acceptance or build either onedir mode:

```powershell
.\.venv\Scripts\amadeus-embedding-acceptance.exe offline-smoke
.\.venv\Scripts\amadeus-embedding-acceptance.exe benchmark
.\.venv\Scripts\amadeus-embedding-acceptance.exe production-benchmark
.\scripts\build-onedir.ps1 -Mode Degraded
.\scripts\build-onedir.ps1 -Mode Bundled
```

`benchmark` measures query embedding plus the two immutable matrix scans. The required performance
gate is `production-benchmark`: it seeds 10,000 synthetic current user memories, warms five times,
then measures 100 distinct requests through SQLite FTS, the priority vector runtime, current-version
revalidation, RRF/quality/decay fusion, and final prompt budgeting. It fails on an incorrect expected
recall, corpus mixing, leaked runtime/SQLite handles, or p95 above 300 ms.

Wheel, sdist, and Degraded onedir outputs exclude the model, persona material, databases, and logs.
Degraded mode proves packaged FTS recall and a model-missing lifecycle. Bundled mode accepts only the
fixed verified public model, proves offline 512-D inference plus a model-ready full lifecycle, and is
the sole onedir input accepted by the P7 installer build. Both modes reject child processes, TCP
connections/listeners, unsafe assets, secrets, databases, logs, and unexpected model files.

## P7 installer

The release build requires official Inno Setup 6.7.3, a clean Git tree, and the verified pinned model.
Run from this directory:

```powershell
$modelPath = Join-Path $env:LOCALAPPDATA 'Amadeus\models\bge-small-zh-v1.5\46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
.\scripts\audit-legacy.ps1
.\scripts\check-release-licenses.ps1
.\scripts\build-installer.ps1 -ModelPath $modelPath
```

The formal output is `build\installer\Amadeus-0.7.0.dev7-win64-setup.exe`; `SHA256SUMS.txt`
is generated beside it. For the complete offline Windows Sandbox acceptance matrix, first create the
temporary lower-version package and then launch the generated `.wsb` configuration:

```powershell
.\scripts\build-installer.ps1 -ModelPath $modelPath -BuildAcceptanceBaseline
.\scripts\start-sandbox-acceptance.ps1 `
    -InstallerPath .\build\installer\Amadeus-0.7.0.dev7-win64-setup.exe `
    -BaselineInstallerPath .\build\installer\Amadeus-0.6.0.dev6-win64-acceptance-baseline-setup.exe
```

The installer is current-user, x64, and does not download runtime resources. Login startup is offered
only on a first install and is unchecked by default. Normal uninstall preserves local data and WinCred;
the explicit `/DELETEUSERDATA=1` acceptance switch exercises the seven-region fail-closed cleanup.
Legacy Chromium `localStorage`, legacy `.env` credentials, Token Plan configuration, and old character
prompts are intentionally not migrated. P7 is an unsigned test package; P8 retains signing decisions,
physical mixed-DPI/multi-monitor regression, three-day daily use, and eight-hour stability acceptance.
