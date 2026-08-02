# Amadeus Desktop

P5 adds persistent local conversations and auditable hybrid memory on top of the tested P1
application foundation, P2 desktop-pet engine, P3 attached chat flow, and P4 secure text-model
integration. The application includes safe local pet import, fixed-clock animation, transparent
hit testing, manual drag placement, multi-screen recovery, a bundled CC0 placeholder pet,
cancellable chat states, and the system tray lifecycle.

P4 provides DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom HTTPS provider. API
credentials are stored only in Windows Credential Manager. P5A stores conversations, summaries,
immutable memory versions, provenance, jobs, and keyword indexes in SQLite; it also exposes model,
history, and memory management pages. P5B adds SQLite schema v2, independent user/persona
generations, Chinese FTS5 plus CPU-only local vector recall, recall events, event decay, and safe
FTS fallback. Speech, screen observation, tool execution, and automation remain outside the MVP.

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
directory; the source package is never modified. P5A stores its SQLite database at
`data\amadeus.sqlite3`. Provider settings contain only a fixed credential reference; API credentials
are stored by Windows Credential Manager and are never written to JSON or SQLite.

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
fixed verified public model, proves offline 512-D inference plus a model-ready full lifecycle, and
remains a P5B smoke artifact rather than the P7 installer. Both modes reject child processes, TCP
connections/listeners, unsafe assets, secrets, databases, logs, and unexpected model files.
