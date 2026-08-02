# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active MVP uses one local desktop process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The canonical `main` branch contains the P1 application foundation, the P2 desktop-pet engine, the P3 attached chat flow, the P4 secure text-model integration, and P5 local conversations plus auditable hybrid memory. The pet supports safe local resource import, fixed-clock sprite animation, transparent hit testing, drag placement, DPI-aware screen recovery, tray visibility controls, and a compact cancellable streaming chat panel.

P4 supports DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom OpenAI-compatible HTTPS endpoint. API credentials are stored only in Windows Credential Manager. P5 persists chat history, immutable user-memory versions, provenance, recall events, and isolated local persona knowledge in SQLite schema v2. Recall combines injection-safe Chinese FTS5 with an explicitly prepared, CPU-only `BAAI/bge-small-zh-v1.5` model; a missing or invalid model degrades immediately to FTS5 without networking. Speech, screen observation, tool execution, and autonomous desktop actions are not included.

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

Prepare or verify the pinned offline model explicitly; normal application startup never downloads it:

```powershell
Set-Location .\desktop
.\.venv\Scripts\amadeus-model.exe prepare
.\.venv\Scripts\amadeus-model.exe verify
```

Local persona knowledge is imported from `%LOCALAPPDATA%\Amadeus\personas\kurisu\knowledge.jsonl` while the application is stopped:

```powershell
.\.venv\Scripts\amadeus-persona.exe import
```

Wheels, source distributions, and the default CI onedir contain neither the embedding model nor local persona/user data. `scripts\build-onedir.ps1 -Mode Bundled` is an explicit local-only smoke mode for the verified public model; installer work remains P7 scope.

P5B acceptance distinguishes the lower-level vector-cache benchmark from the complete production
retrieval chain. Run `amadeus-embedding-acceptance production-benchmark` for the 10,000-memory,
five-warmup, 100-query SQLite + vector + fusion + prompt gate. Both onedir build modes execute their
own isolated packaged probes and privacy scan; Bundled additionally proves fixed-model inference and
the full model-ready application lifecycle without network or shared caches.

## Repository history

`main` is the only supported development and release branch. After each explicitly requested phase passes its local acceptance gate, its reviewable commit is pushed directly to `main` and the matching `Desktop CI` run is verified before work proceeds.

The immutable annotated tags `archive/legacy-main-44d1270` and `archive/legacy-web-voice-handoff-3ff9486` preserve the two legacy browser/Electron/voice rollback points. Restore work starts from one of those tags and returns through a normal commit on `main`; repository history and archive tags must not be rewritten or force-pushed.
