# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active MVP uses one local desktop process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The current `rewrite/desktop-pet-mvp` branch contains the P1 application foundation and the P2 desktop-pet engine: safe local resource import, fixed-clock sprite animation, transparent hit testing, drag placement, DPI-aware screen recovery, a CC0 placeholder pet, and tray visibility controls. Chat, model access, memory, and speech are not implemented yet.

## Development

From PowerShell:

```powershell
Set-Location .\desktop
.\scripts\bootstrap.ps1
.\scripts\run.ps1
.\scripts\check.ps1
.\scripts\build.ps1
```

The module entry point is:

```powershell
.\desktop\.venv\Scripts\python.exe -m amadeus_desktop
```

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`. P2 does not accept or store API credentials.

## Repository history

The legacy browser/Electron/voice implementation remains available on the preserved `main` and `codex/fix-project-handoff` branches. The rewrite branch is intentionally clean and Python-only; rollback branches must not be force-pushed or deleted.
