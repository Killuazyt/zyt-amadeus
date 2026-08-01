# Amadeus Desktop

P5A adds persistent local conversations and auditable FTS5 memory on top of the tested P1
application foundation, P2 desktop-pet engine, P3 attached chat flow, and P4 secure text-model
integration. The application includes safe local pet import, fixed-clock animation, transparent
hit testing, manual drag placement, multi-screen recovery, a bundled CC0 placeholder pet,
cancellable chat states, and the system tray lifecycle.

P4 provides DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom HTTPS provider. API
credentials are stored only in Windows Credential Manager. P5A stores conversations, summaries,
immutable memory versions, provenance, jobs, and keyword indexes in SQLite; it also exposes model,
history, and memory management pages. P5B offline vectors and role knowledge are not present.
Speech, screen observation, tool execution, and automation remain outside the MVP.

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
