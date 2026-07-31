# Amadeus Desktop

P2 provides the Python 3.11 + PySide6 desktop-pet window and sprite engine on top of the tested P1
application foundation. It includes safe local pet import, a versioned pet manifest, legacy
8×9 sprite compatibility, fixed-clock animation, transparent hit testing, manual drag placement,
multi-screen recovery, a bundled CC0 placeholder pet, and the system tray lifecycle.

It does not contain chat, model providers, memory, speech, screen observation, or automation.

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
directory; the source package is never modified. No API credentials are accepted or stored by P2.
