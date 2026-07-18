# Session Rescue for Claude — Feature & Safety Spec

Reference doc for building a parity tool against another agent's session store (e.g. Codex). Everything below is implemented and tested in `session-rescue-for-claude`, not aspirational.

Repo: https://github.com/Glenskii/session-rescue-for-claude
Author: Glen E. Grant (https://profile.glenegrant.com)
Written: 2026-07-18
Reflects: session-rescue-for-claude as of commit ae8188e

---

## The problem this solves

The host app (Claude Desktop) lets you archive a session with one click but ships no UI to browse or restore archived sessions. Archive by accident, or archive on purpose and change your mind, and the session is gone from the sidebar with no recovery path in the app itself.

A second, independent problem: the host app caches session state (including the archived flag) in a local cache (IndexedDB) and only writes that cache through to the on-disk session files when the process is fully terminated — not on window close, not on a graceful tray-icon quit. Any tool built against this needs to account for that write-through behavior or its fixes will appear to silently fail.

---

## Core features

1. **Cross-store discovery.** Scans every known session store location for the OS (Windows/macOS/Linux), not just one. In our case that meant Claude Code's store AND Cowork's store, which use an identical file schema but live in separate directories that the original single-store tool never looked at.
2. **Restore (single or bulk).** Flip the archived flag back, one session or all archived sessions at once.
3. **Archive (single or bulk).** The reverse direction. Native app UI only does this one at a time; the tool does it in bulk.
4. **Trash (single or bulk), never hard delete.** See Safety section.
5. **Search and filter.** By title, by project folder/working directory, by store, by archived/active status.
6. **Group by project.** Cluster sessions by their working directory instead of a flat chronological list — matters once the list is in the dozens.
7. **Orphan detection.** Finds transcript folders with no matching metadata file, and metadata files with no matching transcript folder. Surfaces silent corruption before it becomes a support ticket.
8. **Two interfaces.** A local browser GUI (zero external network calls, stdlib HTTP server only) and a headless CLI (`--list`, `--restore-all-archived`, `--path` for a custom store location) for scripting.
9. **In-app help panel.** A `?` button opens a modal with the operational guidance (the "fully kill, don't just quit" rule, the safety model, filters) so a user never has to leave the tool to find the fix.
10. **Build-freshness signal.** Help panel shows a "last updated" timestamp read from the script's own file modification time, in the maintainer's local timezone. Zero manual upkeep.
11. **Conversational skill wrapper.** A packaged skill (`skills/session-rescue/SKILL.md`) lets the agent itself list/restore/archive/trash sessions through natural conversation, following the same safety rules as the standalone tool.

## Safety net (non-negotiable, do not water down)

1. **Automatic backup before every write.** Any file the tool is about to modify gets copied first to a `*-backups` folder inside the same store, timestamped. This happens unconditionally, not as an opt-in flag.
2. **Trash, not delete.** The tool's "delete" action never actually deletes. It moves the session file and its transcript folder into a `*-trash` folder inside the same store. Recoverable by moving it back, indefinitely, until the user manually empties it themselves.
3. **Atomic writes.** Every write goes to a temp file first, then an OS-level rename over the target. A crash or power loss mid-write cannot leave a half-written, corrupted session file.
4. **Field preservation.** Only the specific flag being changed is touched. Every other field in the session's metadata round-trips untouched — no reformatting, no dropped unknown fields, no silent schema migration.
5. **Path validation before any write.** Every write path is checked to confirm it resolves inside a discovered, legitimate session store before the tool touches it. No writing outside the known store boundaries, ever.
6. **Refuses to run destructively while the host app is live**, where applicable (the cache-rebuild fallback script explicitly checks the host process is not running before proceeding).
7. **Cache-desync fallback is reversible, not destructive.** When a deeper fix is needed (forcing a stale local cache to rebuild from the on-disk files), the fix renames the cache folders with a timestamped suffix rather than deleting them. Full rollback is always one rename away.
8. **No network calls, no telemetry, no external dependencies.** Standard library only, everything stays on the local machine. Nothing to audit for data exfiltration because there is no outbound path.

## Documentation shape that matters

- **Lead with the simple fix, not the heavy tool.** Testing showed most "stuck" cases resolve with a full process kill and relaunch alone. Docs and in-app messaging say this first; the deeper cache-rebuild script is explicitly framed as the fallback for sessions that are still stuck after that.
- **One linear "Recovery workflow" section**, numbered start to finish, for the person who just noticed something is missing and wants steps, not concepts scattered across five headings.
- **Screenshots that show real state**, not mockups — actual archived lists, the actual help panel, the actual restore confirmation.
- **Credit the terrain you're building on.** If a similar prior-art tool exists for the target agent's session format, credit it explicitly and extend rather than silently reinvent.

## What to adapt for a different agent's session store

Everything above is store-agnostic except the concrete paths and schema. To port this to another agent (e.g. Codex), the porting work is:

1. Find every location that agent persists session/conversation state to disk (check for more than one store — assume it is not single-location until proven otherwise).
2. Confirm the exact field name and type used for archived/deleted/hidden state.
3. Confirm whether that agent's app also caches state in a layer separate from the on-disk files (a local database, an IndexedDB-equivalent, a SQLite file) that would need the same "does a plain flag flip actually take effect" testing this project did. Do not assume it is a simple flag flip until verified by a real before/after diff test.
4. Confirm what "fully quit" actually means for that specific app (window close vs. tray quit vs. process kill can all behave differently) before writing that into the docs as fact.
5. Reuse the safety net wholesale. It is not specific to Claude Desktop's schema.
