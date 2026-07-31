# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active MVP uses one local desktop process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The current `rewrite/desktop-pet-mvp` branch contains the completed P1 application foundation only: application lifecycle, local single-instance IPC, a minimal system tray, versioned local settings, local paths, and redacted rotating logs. Desktop-pet rendering, chat, model access, memory, and speech are not part of this foundation yet.

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

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`. P1 does not accept or store API credentials.

## Repository history

The legacy browser/Electron/voice implementation remains available on the preserved `main` and `codex/fix-project-handoff` branches. The rewrite branch is intentionally clean and Python-only; rollback branches must not be force-pushed or deleted.
