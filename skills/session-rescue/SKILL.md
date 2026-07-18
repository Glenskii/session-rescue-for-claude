---
name: session-rescue
description: Manage archived Claude Desktop sessions (Claude Code and Cowork) from inside any session. Use when the user says a session is archived, trapped, missing, lost, or asks to unarchive, restore, list archived sessions, or clean up old sessions. Also use when the user cannot find a project session in the sidebar.
---

# Session Rescue for Claude

Claude Desktop stores sessions as `local_*.json` files in two sibling folders. Archiving flips an `isArchived` boolean in the JSON. There is no in-app UI to browse or restore archived sessions, so this skill operates on the files directly.

FIRST, TRY THE SIMPLE FIX: tell the user to fully KILL the Claude Desktop process (Task Manager > End Task on Windows, `kill`/Force Quit on macOS/Linux) and relaunch. Closing the window or using tray-icon Quit does not reliably flush state. Confirmed by testing: a real process kill followed by relaunch correctly syncs archive and restore actions made through the app's own UI, no file editing needed.

CRITICAL (fallback only): on current Claude Desktop builds, flipping the JSON alone does NOT restore a session that is still stuck archived after a full kill and relaunch. The app caches session state in `IndexedDB/https_claude.ai_0.indexeddb.*` under the Claude data folder and trusts the cache at startup. A reliable restore = flip the JSONs while the app is closed, then rename both IndexedDB folders (`.leveldb` and `.blob`) with a `.bak-<timestamp>` suffix so the app rebuilds the cache from the JSONs on relaunch. The bundled `rebuild_session_state.ps1` does all of this on Windows. Never delete the cache folders, always rename.

## Session store locations

Windows:
- Claude Code: `%APPDATA%\Claude\claude-code-sessions\`
- Cowork: `%APPDATA%\Claude\local-agent-mode-sessions\`

macOS:
- Claude Code: `~/Library/Application Support/Claude/claude-code-sessions/`
- Cowork: `~/Library/Application Support/Claude/local-agent-mode-sessions/`

Linux:
- Claude Code: `~/.config/Claude/claude-code-sessions/`
- Cowork: `~/.config/Claude/local-agent-mode-sessions/`

Layout inside each store: `<account-uuid>/<workspace-uuid>/local_<session-uuid>.json` plus a sibling transcript folder named `local_<session-uuid>`.

## Rules (non-negotiable)

1. ALWAYS back up a session JSON before modifying it. Copy it to a `session-rescue-backups` folder inside the same store, suffixed with a timestamp. Verify the copy's SHA-256 matches the source before proceeding; abort the write if it does not.
2. NEVER hard-delete a session. Move it to a `session-rescue-trash` folder inside the same store instead.
3. Write atomically: write to a temp file, then rename over the original.
4. Preserve every JSON field. Only change `isArchived`. Do not reformat or drop unknown fields.
5. Changes only appear after the user fully KILLS the Claude Desktop process (not just closes the window or uses tray Quit) and relaunches. Always tell them this.
6. Skip the `session-rescue-backups` and `session-rescue-trash` folders when scanning.

## Workflows

### List archived sessions
If the bundled tool is available, prefer it:
```
python claude_session_rescue.py --list
```
Otherwise scan both stores for `local_*.json`, parse each, and report title, `isArchived`, `cwd`, and `lastActivityAt` (epoch ms). Sort by last activity, newest first.

### Restore one or more sessions
The user must fully quit Claude Desktop first (tray icon included). Then either run the bundled script (Windows):
```
powershell -ExecutionPolicy Bypass -File rebuild_session_state.ps1
```
Or manually, for each target JSON:
1. Copy to `session-rescue-backups/<name>.<timestamp>.json`
2. Load JSON, set `isArchived` to `false`
3. Atomic write back
4. Rename `IndexedDB/https_claude.ai_0.indexeddb.leveldb` and `...indexeddb.blob` to `.bak-<timestamp>` names
5. Tell the user to relaunch Claude Desktop

Bulk flip only (still requires the IndexedDB rename to take effect):
```
python claude_session_rescue.py --restore-all-archived
```

### Archive sessions
Same as restore but set `isArchived` to `true`. Confirm with the user before bulk archiving.

### Trash a session
Move both the JSON and its transcript folder into `session-rescue-trash`, suffixed with a timestamp. Confirm with the user first. Explain it is recoverable from that folder.

### Launch the GUI
```
python claude_session_rescue.py
```
Opens a local browser UI at 127.0.0.1:52850 with filters (Code, Cowork, archived, active), search, group-by-project, and bulk operations. Local only, no network access, stdlib only.

## Gotchas

- Claude Desktop may hold session files open while running. If a write fails, ask the user to quit the app first.
- PowerShell `ConvertFrom-Json` chokes on session files containing an empty-string key. Use `-AsHashtable`, or use Python.
- Session JSONs can exceed 300 KB. Read specific fields rather than dumping whole files into context.
- The `title` field is the human-readable name. `processName` may not exist in Code sessions.
