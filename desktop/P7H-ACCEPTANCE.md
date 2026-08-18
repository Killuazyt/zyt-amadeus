# P7H acceptance evidence

P7H was locally accepted on Windows on 2026-08-18. The application version remains
`0.7.0.dev7`; SQLite is schema v7, settings are schema v11, chat and memory exports are v3,
and the complete-restoration backup remains `amadeus-backup/v2`.

## Verified behavior

- Immutable capability snapshots cover text, completed voice transcripts, attachments,
  screen/window/camera frames, proactive requests, and retries without inferring device presence.
- Replies are prohibited from claiming continuous sensing, unseen windows, tool use, or desktop
  control. Relationship warmth requires a user-confirmed, current relationship-memory version.
- The existing memory-extraction response may propose at most two high-confidence cues. Only three
  explicit user intents are accepted; assistant text, attachments, visual inference, sensitive data,
  and opt-out turns cannot become sources.
- Proposed cues require user editing and confirmation. Confirmed text is immutable, expires after
  30 days by default, can be retained until resolved, and is invalidated by exact-version changes,
  conflicts, denial, archival, or deletion.
- Contextual follow-ups are off by default. The desktop bubble contains only a fixed generic preview,
  is surfaced once, and opens the frozen text locally without any provider request. Chat source labels
  deep-link to the cue; static Kurisu knowledge can never authorize a cue.
- Cue deletion clears content and source relations while retaining only content-free audit metadata.
  Chat and memory v3 exports include only relevant authorization metadata and no vectors.

## Local gates

- Complete pytest: `985 passed, 3 skipped in 132.03s`. The skips are two host symlink-capability
  cases and the separately authorized real-HKCU smoke.
- Ruff, format verification, `compileall`, `pip check`, and `git diff --check`: passed.
- Source-candidate privacy/secret/asset scan: 246 files, 0 archive members, 0 violations.
- Existing build-output scan: 686 files, 487 archive members, 0 violations. No new build output was
  produced for P7H.
- Real DPR 1.25 GUI smoke: cue management `1500x1080` logical / `1875x1350` physical;
  proactive settings `900x760` / `1125x950`; chat source label `520x680` / `650x850`.
  Visual inspection found no clipping or private cue text in the desktop bubble.
- The default Windows temp location was temporarily blocked by an external sharing lock on the user
  profile root. The unchanged fail-closed uninstall anchor tests passed `12 passed, 1 skipped` when
  rerun with a workspace-local pytest temp root; the production safety gate was not weakened.

No real remote or local provider was contacted. No Python package, onedir, installer, release, P8
three-day use, P8 eight-hour stability run, or P8 physical-display matrix was executed.

## GitHub validation

Implementation commit `11e6b15ac66429f0f16f89b72b844306254850d2` was pushed normally to
`main` with `[skip ci]`. Manually dispatched test-only Desktop CI run `32155341664` precisely
matched that SHA and succeeded: `987 passed, 1 skipped in 244.86s`, 246 source files scanned with
zero violations, project installation and all Python package/isolated wheel/onedir steps skipped,
`package-installer` skipped, and zero artifacts.

This evidence update creates a documentation-only commit. A second manually dispatched test-only
run will verify the final documentation SHA; its run ID and exact SHA are recorded in the local Plan
evidence without creating a third recursive documentation commit.
