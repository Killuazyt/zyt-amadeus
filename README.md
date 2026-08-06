# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active MVP uses one local desktop process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The canonical `main` branch contains the P1 application foundation, the P2 desktop-pet engine, the P3 attached chat flow, the P4 secure text-model integration, P5 local conversations plus auditable hybrid memory, the P6 settings/tray/proactive-interaction closure, the P7 Windows packaging and migration-audit infrastructure, and the user-approved P7A built-in appearance replacement. The pet supports safe local resource import and switching, fixed-clock sprite animation, transparent hit testing, drag placement, DPI-aware screen recovery, complete tray controls, and a compact cancellable streaming chat panel.

P4 supports DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom OpenAI-compatible HTTPS endpoint. API credentials are stored only in Windows Credential Manager. P5/P6 persist chat history, proactive-message origin, immutable user-memory versions, provenance, recall events, isolated local persona knowledge, and an interaction event ledger in SQLite schema v3. Recall combines injection-safe Chinese FTS5 with an explicitly prepared, CPU-only `BAAI/bge-small-zh-v1.5` model; a missing or invalid model degrades immediately to FTS5 without networking. P6 adds a single-instance eight-page settings center, exact eight-item tray, verified HKCU startup control, restrained local greetings, versioned JSON exports, consistent single-file backup/restore, diagnostics, and fail-closed factory reset. Speech, screen observation, tool execution, and autonomous desktop actions are not included.

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

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`; the application keeps `data\amadeus.sqlite3`, settings, imported resources, local persona material, model cache, backups, and redacted logs in separate validated regions. Settings schema v5 contains only non-secret state and a fixed credential reference; exports and backups never read Windows Credential Manager. API credentials remain in Windows Credential Manager. Deterministic local simulation is available only through the explicit `--mock-chat` development flag or test injection.

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

Wheels, source distributions, and the Degraded onedir contain neither the embedding model nor local persona/user data. They do include the P7A built-in Kurisu lossless WebP at its pinned SHA-256. The P7 installer is built only from the Bundled PyInstaller onedir after the fixed public model, privacy scan, archive tags, and release-notice closure pass verification.

The built-in Kurisu source manifest declared no author, source, or license. Its packaged status is `NOASSERTION`: the repository owner explicitly approved its inclusion on 2026-08-06, but third-party redistribution rights were not independently verified. See `desktop/src/amadeus_desktop/resources/licenses/KURISU-ASSET-NOTICE.txt`. The project MIT license and the CC0 application-icon notice do not cover that spritesheet.

## P7 packaging and clean-machine acceptance

Install the official Inno Setup 6.7.3 compiler, stop Amadeus, and run these commands from `desktop`. The installer build requires a clean Git tree and records the exact commit in the packaged diagnostics metadata.

```powershell
$modelPath = Join-Path $env:LOCALAPPDATA 'Amadeus\models\bge-small-zh-v1.5\46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
.\scripts\audit-legacy.ps1
.\scripts\check-release-licenses.ps1
.\scripts\build-installer.ps1 -ModelPath $modelPath
```

The unsigned P7 test installer and its SHA-256 manifest are written to `desktop\build\installer`. Build the lower-version acceptance baseline and launch the offline Windows Sandbox matrix with:

```powershell
.\scripts\build-installer.ps1 -ModelPath $modelPath -BuildAcceptanceBaseline
.\scripts\start-sandbox-acceptance.ps1 `
    -InstallerPath .\build\installer\Amadeus-0.7.0.dev7-win64-setup.exe `
    -BaselineInstallerPath .\build\installer\Amadeus-0.6.0.dev6-win64-acceptance-baseline-setup.exe
```

The Sandbox run covers install, lower-version upgrade, default data-preserving uninstall, explicit delete-data uninstall, lifecycle mutexes, and a user-local path containing Chinese characters and spaces. Its committed template disables networking and maps only read-only installers plus a writable aggregate-result folder. Evidence contains hashes, statuses, and counts only.

P7 deliberately does not import the legacy Chromium `localStorage`, old `.env` secrets, Token Plan endpoints, or old character prompts. Default uninstall removes the program, shortcuts, uninstall entry, and HKCU startup value while preserving `%LOCALAPPDATA%\Amadeus` and Windows Credential Manager; `/DELETEUSERDATA=1` explicitly requests the fail-closed seven-region cleanup. P7 artifacts are unsigned test builds, so SmartScreen reputation warnings remain expected. P8 still owns code signing decisions, the physical 100%/150% and mixed-DPI multi-monitor matrix, three days of daily use, and the eight-hour stability run.

P5B acceptance distinguishes the lower-level vector-cache benchmark from the complete production
retrieval chain. Run `amadeus-embedding-acceptance production-benchmark` for the 10,000-memory,
five-warmup, 100-query SQLite + vector + fusion + prompt gate. Both onedir build modes execute their
own isolated packaged probes and privacy scan; Bundled additionally proves fixed-model inference and
the full model-ready application lifecycle without network or shared caches.

## Repository history

`main` is the only supported development and release branch. After each explicitly requested phase passes its local acceptance gate, its reviewable commit is pushed directly to `main` and the matching `Desktop CI` run is verified before work proceeds.

The immutable annotated tags `archive/legacy-main-44d1270` and `archive/legacy-web-voice-handoff-3ff9486` preserve the two legacy browser/Electron/voice rollback points. Restore work starts from one of those tags and returns through a normal commit on `main`; repository history and archive tags must not be rewritten or force-pushed.
