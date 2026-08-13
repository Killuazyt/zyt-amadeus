# Amadeus Desktop Pet

Amadeus is being rebuilt as a Windows 10/11 x64 desktop pet using Python 3.11 and PySide6. The active desktop application uses one local process and does not require Electron, Node, a browser UI, a local HTTP service, or WebRTC.

The canonical `main` branch contains the P1 application foundation, the P2 desktop-pet engine, the P3 attached chat flow, the P4 secure text-model integration, P5 local conversations plus auditable hybrid memory, the P6 settings/tray/proactive-interaction closure, the P7 Windows packaging and migration-audit infrastructure, the user-approved P7B high-resolution built-in appearance replacement, and the pre-P8 P7C-P7F multimodal and five-layer-memory extensions. The pet supports safe local resource import and switching, fixed-clock sprite animation, transparent hit testing, drag placement, DPI-aware screen recovery, complete tray controls, and a compact cancellable streaming chat panel.

P4 established DeepSeek pay-as-you-go, MiMo pay-as-you-go, and a strict custom OpenAI-compatible HTTPS endpoint. P7G expands this into a bundled static provider catalog, OpenAI Chat Completions and Anthropic Messages adapters, reusable Profiles, and explicit conversation/summary/memory/vision task assignments without cross-Profile fallback. API credentials are stored only in Windows Credential Manager. P5/P6 persist chat history, proactive-message origin, immutable user-memory versions, provenance, recall events, isolated local persona knowledge, and an interaction event ledger. Recall combines injection-safe Chinese FTS5 with an explicitly prepared, CPU-only `BAAI/bge-small-zh-v1.5` model; a missing or invalid model degrades immediately to FTS5 without networking. P6 adds a single-instance settings center, tray controls, verified HKCU startup control, restrained local greetings, versioned exports, consistent backup/restore, diagnostics, and fail-closed factory reset.

P7C adds managed image/document attachments, background text extraction, multimodal routing, attachment-aware history, and backup/restore. P7D adds explicitly started push-to-talk or hands-free sentence-level MiMo ASR, cancellable streaming text, and bounded sentence-level MiMo TTS playback. P7E adds manual region capture and opt-in screen, window, or camera sources with a capacity-one latest-frame slot, visible capture indicators, event-driven sampling, and unified privacy shutdown. Media capture is off at startup; raw microphone audio and transient proactive-analysis frames are not retained. Tool execution and autonomous desktop actions remain outside scope.

P7F adds N.E.K.O.-inspired working, recent, fact/event, reflection, and persona-impression layers while retaining Amadeus's stricter immutable versions and user-message provenance. Reflections and persona impressions use physically separate SQLite tables, FTS5 indexes, and local-vector generations; static Kurisu knowledge remains a read-only fourth retrieval corpus and can never be rewritten by the derived-memory pipeline. Evidence, conflicts, lineage, promotion, suppression, rollback, and metadata-only audit events are user-visible. The deep-memory switch pauses derived processing and recall without disabling facts, recent context, or static character knowledge.

## P7G validation status

P7G local acceptance completed on 2026-08-13. The focused fake-credential/mock-transport, migration, credential, routing, settings, backup, and UI suite passed `212` tests; the final complete desktop suite passed `952` tests with `3` environment-specific skips. Ruff, format verification, `compileall`, `pip check`, Python wheel/sdist asset verification, tracked-source and artifact privacy scans, FTS-degraded onedir, fixed-model bundled onedir, WinCred probe, and the bundled `20/20` lifecycle matrix all passed. The unsigned Inno Setup 6.7.3 installer built successfully from the clean code candidate and has SHA-256 `853f31a44b5b7766c2587c82d7fa6941f7418b0266a44575877328892fee41e1`.

The provider center also passed a real Windows display smoke on a `2048 x 1152`, DPR `1.25` screen: all 21 catalog entries loaded without degraded recovery, the Profile CRUD/test/save controls and four task assignments were visible, no connection test or credential access started, and visual inspection found no overlap or clipping. No real remote or local provider endpoint was contacted; such connectivity checks still require separate explicit authorization. P8 remains unstarted and separate from this work.

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

Runtime data is stored under `%LOCALAPPDATA%\Amadeus`; the application keeps `data\amadeus.sqlite3`, managed attachments, settings, imported resources, local persona material, model cache, backups, and redacted logs in separate validated regions. Settings schema v10 and SQLite schema v6 contain only non-secret state and credential references; `amadeus-memory-export/v2` includes persistent semantic versions, provenance, evidence, conflicts, lineage, and audit metadata while excluding working snapshots, vectors, chat-body copies, and secrets. `amadeus-backup/v2` remains the complete-restoration format. Model Profile, legacy model, ASR, and TTS credentials remain in Windows Credential Manager. Deterministic local simulation is available only through the explicit `--mock-chat` development flag or test injection.

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

Wheels, source distributions, and the Degraded onedir contain neither the embedding model nor local persona/user data. They do include the P7B built-in Kurisu 4x high-resolution lossless WebP at its pinned SHA-256; source frames are 768x832 and render as 192x208 logical pixels at 100% application scale. The P7 installer is built only from the Bundled PyInstaller onedir after the fixed public model, privacy scan, archive tags, and release-notice closure pass verification.

The built-in Kurisu source manifest declared no author, source, or license. Its packaged status is `NOASSERTION`: the repository owner explicitly approved the exact 4x derivative for inclusion on 2026-08-07, but third-party redistribution rights were not independently verified. See `desktop/src/amadeus_desktop/resources/licenses/KURISU-ASSET-NOTICE.txt`.

The Kurisu portrait application icon is separately pinned at SHA-256 `ded30eeb568f26e3df64998131698e472603bf48d531885b27a7c04293d3b0b5`. It is an AI-generated character derivative with `NOASSERTION` status; third-party character and redistribution rights were not independently verified. See `desktop/src/amadeus_desktop/resources/licenses/KURISU-ICON-NOTICE.txt`. The project MIT license and the CC0 generic fallback/test notice do not cover either Kurisu asset.

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

P7 deliberately does not import the legacy Chromium `localStorage`, old `.env` secrets, Token Plan endpoints, or old character prompts. Default uninstall removes the program, shortcuts, uninstall entry, and HKCU startup value while preserving `%LOCALAPPDATA%\Amadeus` and Windows Credential Manager; `/DELETEUSERDATA=1` explicitly requests the fail-closed seven-region cleanup. P7 artifacts are unsigned test builds, so SmartScreen reputation warnings remain expected. P8 is reserved as the one-time final acceptance after the user finishes the remaining feature and task adjustments. It still owns code signing decisions, the physical 100%/150% and mixed-DPI multi-monitor matrix, three days of daily use, and the eight-hour stability run; those full checks are intentionally deferred until the final candidate is declared ready.

P5B acceptance distinguishes the lower-level vector-cache benchmark from the complete production
retrieval chain. Run `amadeus-embedding-acceptance production-benchmark` for the 10,000-memory,
five-warmup, 100-query SQLite + vector + fusion + prompt gate. Both onedir build modes execute their
own isolated packaged probes and privacy scan; Bundled additionally proves fixed-model inference and
the full model-ready application lifecycle without network or shared caches.

## Repository history

`main` is the only supported development and release branch. After each explicitly requested phase passes its local acceptance gate, its reviewable commit is pushed directly to `main` and the matching `Desktop CI` run is verified before work proceeds.

The immutable annotated tags `archive/legacy-main-44d1270` and `archive/legacy-web-voice-handoff-3ff9486` preserve the two legacy browser/Electron/voice rollback points. Restore work starts from one of those tags and returns through a normal commit on `main`; repository history and archive tags must not be rewritten or force-pushed.
