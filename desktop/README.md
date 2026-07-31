# Amadeus Desktop

P1 provides the Python 3.11 + PySide6 desktop application foundation. It intentionally contains
only application lifecycle, single-instance IPC, a minimal system tray, local paths, versioned
settings, and redacted rotating logs.

It does not contain the desktop pet, chat, model providers, memory, speech, screen observation, or
automation features.

## Development commands

Run these commands from PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\scripts\run.ps1
.\scripts\check.ps1
.\scripts\build.ps1
```

To regenerate committed dependency locks after intentionally changing `pyproject.toml`:

```powershell
.\scripts\lock.ps1
```

The application entry point is:

```powershell
.\.venv\Scripts\python.exe -m amadeus_desktop
```

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`. No API credentials are accepted or stored
by the P1 application.
