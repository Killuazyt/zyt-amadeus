# Amadeus Desktop

P7 adds reproducible Windows packaging, legacy-architecture auditing, and a clean-machine installer
acceptance workflow on top of the complete P6 settings, tray, proactive-interaction, and local-data-management
closure and the tested P1 application foundation, P2 desktop-pet engine, P3 attached chat flow, P4 secure
text-model integration, and P5 auditable hybrid memory. P7C-P7H extend that same conversation state
machine with managed image/document attachments, sentence-level low-latency voice, opt-in screen,
window, camera, and manual-region visual input. The application includes safe local pet
import/switch/removal, fixed-clock animation, transparent hit testing, manual drag placement,
multi-screen recovery, the pinned P7B high-resolution built-in Kurisu spritesheet, cancellable chat states, and an exact
eight-item system tray.

P4 provides DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom HTTPS provider. API
credentials are stored only in Windows Credential Manager. P5 stores conversations, summaries,
immutable memory versions, provenance, jobs, independent user/persona vector generations, recall
events, decay, and safe FTS fallback. P7C-P7E upgrade to SQLite schema v5 and settings schema v8, add
proactive message origin and a body-free interaction ledger, and exposes an eight-page single
settings window, verified HKCU startup control, restrained greetings, diagnostics, versioned JSON
exports, attachment-inclusive backup/restore, and fail-closed factory reset. Capture is always opt-in
and off at application startup; privacy mode stops and releases active media sources. Tool execution,
autonomous desktop actions, WebRTC, and continuous background listening remain outside scope.

P7H freezes a request-scoped capability snapshot for text, completed voice transcripts, attachments,
and explicitly shared visual frames. It also adds user-reviewed companion cues bound to exact memory
versions, an off-by-default contextual-follow-up switch, one-shot privacy-safe bubbles, local-only cue
opening, and a cue-management view. SQLite is schema v7, settings are schema v11, chat and memory
exports are v3, and the backup remains v2. See [P7H-ACCEPTANCE.md](P7H-ACCEPTANCE.md).

## Development commands

Run these commands from PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\scripts\run.ps1
.\scripts\check.ps1
.\scripts\build.ps1
```

Import an additional local pet while Amadeus is fully stopped:

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
directory; the source package is never modified. SQLite is stored at `data\amadeus.sqlite3`, and
managed attachment originals are stored under `data\attachments`. Provider settings contain only
credential references; API credentials are stored by Windows Credential Manager and are never
written to JSON, SQLite, exports, or backups.

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
They include the byte-identical P7B built-in 4x high-resolution lossless WebP at SHA-256
`cca259ac33ffc7c8170b401a315f9a177a865eb063ba44a4da87c3ab13fa90b7`. Its source frames are
768x832 while the desktop pet remains 192x208 logical pixels at 100% application scale.
Degraded mode proves packaged FTS recall and a model-missing lifecycle. Bundled mode accepts only the
fixed verified public model, proves offline 512-D inference plus a model-ready full lifecycle, and is
the sole onedir input accepted by the P7 installer build. Both modes reject child processes, TCP
connections/listeners, media outside the exact built-in/icon hash allowlist, secrets, databases,
logs, and unexpected model files.

The built-in Kurisu source manifest did not declare an author, source, or license. Its packaged
status is `NOASSERTION`; user approval to include it is recorded separately from third-party rights,
which were not independently verified. See `resources\licenses\KURISU-ASSET-NOTICE.txt`.

The application, settings windows, tray, EXE, and installer use the user-selected Kurisu portrait
icon at SHA-256 `ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5`.
It is an AI-generated character derivative with `NOASSERTION` status; third-party character and
redistribution rights were not independently verified. See
`resources\licenses\KURISU-ICON-NOTICE.txt`. CC0 applies only to the retained generic robot
fallback/test fixture.

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
prompts are intentionally not migrated. P7 is an unsigned test package. P8 is reserved as the one-time
final acceptance after all remaining user-directed feature and task adjustments are complete; it retains
signing decisions, physical mixed-DPI/multi-monitor regression, three-day daily use, and eight-hour
stability acceptance instead of repeating those full checks during intermediate iterations.
