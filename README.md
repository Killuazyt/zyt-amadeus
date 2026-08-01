# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active MVP uses one local desktop process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The current `rewrite/desktop-pet-mvp` branch contains the P1 application foundation, the P2 desktop-pet engine, the P3 attached chat flow, the P4 secure text-model integration, and P5A local conversations plus auditable keyword memory. The pet supports safe local resource import, fixed-clock sprite animation, transparent hit testing, drag placement, DPI-aware screen recovery, tray visibility controls, and a compact cancellable streaming chat panel.

P4 supports DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom OpenAI-compatible HTTPS endpoint. API credentials are stored only in Windows Credential Manager. P5A persists chat history and versioned user memories in a local SQLite database, provides safe Chinese FTS5 recall, and exposes model, history, and memory management pages. Offline embeddings, hybrid/vector recall, role knowledge, speech, screen observation, tool execution, and autonomous desktop actions are not included yet.

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

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`; P5A creates `data\amadeus.sqlite3` and keeps migration backups under the local data boundary. Settings contain only a fixed credential reference; API credentials remain in Windows Credential Manager. Deterministic local simulation is available only through the explicit `--mock-chat` development flag or test injection.

## Repository history

The legacy browser/Electron/voice implementation remains available on the preserved `main` and `codex/fix-project-handoff` branches. The rewrite branch is intentionally clean and Python-only; rollback branches must not be force-pushed or deleted.
