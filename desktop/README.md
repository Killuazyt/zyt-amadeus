# Amadeus Desktop

P3 adds an attached text-chat panel and deterministic local streaming simulation on top of the
tested P1 application foundation and P2 desktop-pet engine. The application includes safe local
pet import, fixed-clock animation, transparent hit testing, manual drag placement, multi-screen
recovery, a bundled CC0 placeholder pet, cancellable chat states, and the system tray lifecycle.

The P3 provider is local and synthetic: it makes no network request and stores messages only for
the current process. Real model providers, credentials, SQLite history, long-term memory, speech,
screen observation, and automation are not included.

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

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`. Imported pets are copied into its `pets`
directory; the source package is never modified. No API credentials are accepted or stored by P3.
