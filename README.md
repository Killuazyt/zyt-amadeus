# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active MVP uses one local desktop process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The current `rewrite/desktop-pet-mvp` branch contains the P1 application foundation, the P2 desktop-pet engine, and the P3 attached text-chat simulation. The pet supports safe local resource import, fixed-clock sprite animation, transparent hit testing, drag placement, DPI-aware screen recovery, tray visibility controls, and a compact cancellable streaming chat panel backed by a deterministic local provider.

P3 does not contact a real model service or persist conversations. Real provider access, Windows Credential Manager integration, SQLite chat history, long-term memory, speech, and screen observation belong to later phases.

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

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`. P3 does not accept or store API credentials, and its simulated chat messages remain in memory for the current process only.

## Repository history

The legacy browser/Electron/voice implementation remains available on the preserved `main` and `codex/fix-project-handoff` branches. The rewrite branch is intentionally clean and Python-only; rollback branches must not be force-pushed or deleted.
