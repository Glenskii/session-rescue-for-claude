# Verification

What was actually tested, how, and what it proved. Written 2026-07-18, reflects commit history through the SHA-256 backup verification change.

This exists separately from the README because the safety claims in this repo (auto-backups, atomic writes, the IndexedDB restore requirement) are not assertions — they were proven against a real Claude Desktop install with real session files, and that proof deserves its own record rather than being buried in usage instructions.

---

## 1. The archive bug is real, not a misunderstanding

Before writing a line of code, this was checked against Anthropic's own issue tracker rather than assumed. Confirmed live:

- [anthropics/claude-code#22931](https://github.com/anthropics/claude-code/issues/22931) — `[BUG] I archived my Claude Cowork chats and they are nowhere to be found`, labeled `bug`, status noted as "this never worked"
- Multiple related open issues covering the same gap across Code and Cowork, and a family of IndexedDB cache/sync bugs

This is a documented defect on Anthropic's tracker, not a feature gap or a misunderstanding of intended behavior. The public messaging for this project says "bug" because that word is earned, not because it reads better.

## 2. Discovering the IndexedDB cache problem

**The claim being tested:** does flipping `isArchived` in the session JSON file actually restore a session?

**Method:** a controlled diff experiment, not a guess.

1. Snapshotted every file under `%APPDATA%\Claude` (path, size, modified time) — 21,079 files
2. Archived one sacrificial session ("LinkedIn carousel PDF") through the normal restore mechanism
3. Diffed the file list against the snapshot to see exactly what changed

**Result:** the JSON file changed as expected, but so did files under `IndexedDB\https_claude.ai_0.indexeddb.leveldb` and `.blob`, `Local Storage\leveldb`, and `Network\Cookies`. The app was writing session state to more than one place.

**Follow-up test:** restore the JSON flag directly, relaunch Claude Desktop, check the sidebar. Session stayed archived. The JSON edit alone did not work — the IndexedDB cache was overriding it at startup.

**Fix verified:** quit Claude Desktop, restore the JSON, rename (not delete) the IndexedDB cache folders so the app rebuilds them from the JSON files on next launch, relaunch. Confirmed working: 8 previously-stuck sessions came back with no data loss, no logout, nothing broken elsewhere in the app.

## 3. Discovering that a full process kill matters, window-close does not

A second round of live testing (archiving new sessions through the app's own UI, not this tool) surfaced a further wrinkle: closing the app window, or using the tray-icon Quit, does not reliably flush IndexedDB state to the JSON files. Confirmed by:

1. Archiving a session in the app
2. Using tray-icon Quit, relaunching
3. Checking the JSON file's `isArchived` flag directly — still `false`, the archive never landed on disk
4. Fully killing the process via Task Manager instead, relaunching
5. Checking again — `isArchived: true`, correctly written through

This changed the primary guidance in the README and the in-app help panel: try a full process kill and relaunch first, since it resolves most cases on its own with zero file editing. The IndexedDB-rename fallback is for sessions still stuck after that.

## 4. Does Claude Code expose a native archive/restore API instead of file editing?

Before accepting file-level editing as the final approach, this was checked rather than assumed, prompted by comparing notes with a parallel project (`session-rescue-for-codex`, which found and used a native `thread/archive`/`thread/unarchive` protocol call for a different agent's session store).

**Checked:** `claude project --help` (only exposes `purge`, a destructive delete of all project state — not what's needed) and `claude agents --help` (manages background subagent tasks, a different concept from sidebar sessions, no archive/restore verbs).

**Result:** no equivalent protocol-level command exists in the Claude Code CLI surface as of v2.1.209. File-level editing is the only mechanism available for this, not a shortcut taken instead of a proper API that was overlooked.

## 5. Backup integrity: hash-verified, not just copied

Originally, `backup_session_file()` did a plain `shutil.copy2()` with no verification that the copy actually matched the source. Fixed after reviewing a parallel Codex-focused rebuild of this tool, which included SHA-256 verification on its backup path.

**What changed:** every backup now hashes the source before copying, hashes the copy after, and raises an error (aborting the write, not silently proceeding) if they don't match. A `.sha256` sidecar file is written next to each backup so integrity can be re-checked later without needing the original.

**Tested:**
1. Created an isolated test session file outside any real data
2. Ran `archive_session()` against it, confirmed the backup and its `.sha256` sidecar were written correctly and the flag flipped
3. Deliberately corrupted the backup file by appending garbage bytes
4. Ran `verify_backup_integrity()`, confirmed it correctly flagged the corrupted file and did not flag anything else

The "Integrity Check" button in the GUI (formerly "Check Orphans") now surfaces both orphaned transcripts/JSONs and any backup that fails its hash check.

## What has not been independently verified

- macOS and Linux behavior. The IndexedDB-rename fallback and the "full kill required" finding were tested on Windows only. The same principle is documented for macOS/Linux based on the shared Electron architecture, not from direct testing on those platforms. Treat that guidance as reasoned-but-unverified until someone confirms it on those OSes.
- Whether Anthropic's own eventual fix will change any of this. This document reflects behavior as of Claude Desktop's build in use during testing (July 2026). If Anthropic changes the caching architecture, some of this may go stale — check the date at the top of this file against your installed version before trusting it blindly.
