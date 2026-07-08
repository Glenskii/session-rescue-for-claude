#!/usr/bin/env python3
"""
Session Rescue for Claude
=========================
Browser-based GUI for managing archived Claude Desktop sessions, covering
BOTH Claude Code ("claude-code-sessions") and Cowork ("local-agent-mode-sessions").

NOTE: on current Claude Desktop builds, flipping isArchived in the JSON is
necessary but not sufficient: the app caches session state in IndexedDB and
reads the cache at startup. Use rebuild_session_state.ps1 (Windows) for the
full restore, or rename the IndexedDB cache folders manually after flipping.

Why this exists: Claude Desktop currently has no UI to browse or restore
archived sessions. One misclick and your project session is trapped.
This tool finds every session JSON on disk and lets you restore, trash,
or inspect them safely.

Safety model (differences from naive approaches):
  - Every write is preceded by a timestamped backup copy
  - "Delete" moves sessions to a trash folder, never destroys them
  - Writes are atomic (temp file + rename), a crash cannot corrupt a session
  - JSON round-trips preserve every field the app stored

Usage:
  python claude_session_rescue.py             # auto-detect, open browser UI
  python claude_session_rescue.py --path DIR  # point at a custom sessions dir
  python claude_session_rescue.py --list      # print sessions to stdout (no GUI)
  python claude_session_rescue.py --restore-all-archived   # headless bulk restore

Requires Python 3.8+, standard library only.

Core restore mechanism (isArchived flag flip) discovered by SugaCrypto's
cowork-archive-manager: https://github.com/SugaCrypto/cowork-archive-manager
License: MIT
"""

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ============================================================
# SECTION 1: Constants and configuration
# ============================================================

VERSION = "1.0.0"
PORT = 52850  # distinct from upstream cowork-archive-manager (52849)
LOCK_FILE = Path.home() / ".claude_session_rescue.lock"

# Both known session stores. Claude Code and Cowork use an identical
# local_*.json schema with an isArchived boolean.
SESSION_DIR_NAMES = ["claude-code-sessions", "local-agent-mode-sessions"]
SOURCE_LABELS = {
    "claude-code-sessions": "Code",
    "local-agent-mode-sessions": "Cowork",
}

# Backups and trash live NEXT TO the session dirs, inside the Claude data
# folder, so they are easy to find and excluded from session scanning.
BACKUP_DIR_NAME = "session-rescue-backups"
TRASH_DIR_NAME = "session-rescue-trash"

custom_sessions_path = None  # set via --path


# ============================================================
# SECTION 2: Path discovery
# ============================================================

def get_claude_roots():
    """Return candidate Claude data root directories for the current OS."""
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        roots = [
            home / "Library" / "Application Support" / "Claude",
            home / "Library" / "Application Support" / "claude-desktop",
            home / ".claude",
        ]
    elif system == "Windows":
        appdata = Path(os.environ.get("APPDATA", ""))
        localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
        roots = [appdata / "Claude"]
        msix_base = localappdata / "Packages"
        if msix_base.exists():
            try:
                for pkg in msix_base.iterdir():
                    if pkg.is_dir() and pkg.name.startswith("Claude_"):
                        roots.append(pkg / "LocalCache" / "Roaming" / "Claude")
            except PermissionError:
                pass
        roots += [
            localappdata / "Claude",
            appdata / "claude-desktop",
            localappdata / "claude-desktop",
            home / ".claude",
        ]
    else:
        roots = [
            home / ".config" / "Claude",
            home / ".config" / "claude-desktop",
            home / ".claude",
        ]
    return roots


def get_candidate_bases():
    """Return (dir_name, path) pairs for every root x session-dir combination."""
    bases = []
    for root in get_claude_roots():
        for dir_name in SESSION_DIR_NAMES:
            bases.append((dir_name, root / dir_name))
    return bases


def iter_session_json_files(base_path):
    """Yield local_*.json files under base_path, tolerating vanished subdirs."""
    try:
        base_path = Path(base_path)
    except TypeError:
        return
    if not base_path.exists():
        return

    def _on_walk_error(_err):
        return

    for root, dirs, files in os.walk(base_path, onerror=_on_walk_error):
        # Never descend into our own backup or trash folders
        dirs[:] = [d for d in dirs if d not in (BACKUP_DIR_NAME, TRASH_DIR_NAME)]
        for name in files:
            if name.startswith("local_") and name.endswith(".json"):
                yield Path(root) / name


def find_sessions_dirs():
    """Locate every session base dir containing session files. Returns (list, diagnostics)."""
    diag = {
        "os": platform.system(),
        "custom_path": str(custom_sessions_path) if custom_sessions_path else None,
        "searched_paths": [],
        "base_exists": False,
        "found_bases": [],
        "reason": None,
    }

    if custom_sessions_path is not None:
        p = Path(custom_sessions_path)
        diag["searched_paths"] = [str(p)]
        if not p.exists():
            diag["reason"] = "custom_path_not_found"
            return [], diag
        if not p.is_dir():
            diag["reason"] = "custom_path_not_dir"
            return [], diag
        diag["base_exists"] = True
        if any(iter_session_json_files(p)):
            diag["found_bases"] = [str(p)]
            return [("custom", p)], diag
        diag["reason"] = "no_session_files_in_custom_path"
        return [], diag

    found = []
    for dir_name, base in get_candidate_bases():
        diag["searched_paths"].append(str(base))
        if not base.exists():
            continue
        diag["base_exists"] = True
        if any(iter_session_json_files(base)):
            found.append((dir_name, base))
            diag["found_bases"].append(str(base))

    if not found:
        diag["reason"] = "base_dir_not_found" if not diag["base_exists"] else "no_session_files"
    return found, diag


# ============================================================
# SECTION 3: Safety layer (backup, trash, atomic writes)
# ============================================================

def _rescue_dir(session_json_path, kind):
    """Return the backup or trash directory for a session file, creating it."""
    base = Path(session_json_path).parent
    # Walk up to the session-store root (the dir named in SESSION_DIR_NAMES)
    # so backups pool in one predictable place per store.
    for parent in [base] + list(base.parents):
        if parent.name in SESSION_DIR_NAMES:
            target = parent / kind
            target.mkdir(exist_ok=True)
            return target
    # Custom path fallback: keep rescue dirs beside the file
    target = base / kind
    target.mkdir(exist_ok=True)
    return target


def backup_session_file(json_path):
    """Copy the session JSON to the backup folder with a timestamp. Returns backup path."""
    src = Path(json_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _rescue_dir(src, BACKUP_DIR_NAME) / f"{src.stem}.{stamp}.json"
    shutil.copy2(src, dest)
    return dest


def atomic_write_json(json_path, data):
    """Write JSON via temp file + os.replace so a crash cannot corrupt the target."""
    target = Path(json_path)
    tmp = target.with_suffix(".rescue-tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, target)


# ============================================================
# SECTION 4: Session operations
# ============================================================

_cached_sessions_dirs = None


def _validate_session_path(json_path):
    """Only operate on files inside a discovered session store."""
    global _cached_sessions_dirs
    p = Path(json_path).resolve()
    if _cached_sessions_dirs is None:
        bases, _ = find_sessions_dirs()
        _cached_sessions_dirs = [b.resolve() for _, b in bases]
    for base in _cached_sessions_dirs:
        try:
            p.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def load_sessions():
    """Load all sessions from every store, tagged with source and path."""
    bases, diag = find_sessions_dirs()
    sessions = []
    for dir_name, base in bases:
        label = SOURCE_LABELS.get(dir_name, dir_name)
        for json_file in iter_session_json_files(base):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
                data["_path"] = str(json_file)
                data["_source"] = label
                sessions.append(data)
            except (json.JSONDecodeError, OSError):
                continue
    sessions.sort(key=lambda s: s.get("lastActivityAt", 0), reverse=True)
    return sessions, diag


def set_archived(json_path, archived):
    """Flip the isArchived flag with backup + atomic write. Returns True on success."""
    if not _validate_session_path(json_path):
        return False
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        backup_session_file(json_path)
        data["isArchived"] = archived
        atomic_write_json(json_path, data)
        return True
    except (json.JSONDecodeError, OSError):
        return False


def restore_session(json_path):
    return set_archived(json_path, False)


def archive_session(json_path):
    return set_archived(json_path, True)


def trash_session(json_path):
    """Move the session JSON and its transcript folder to the trash dir. Recoverable."""
    if not _validate_session_path(json_path):
        return False
    try:
        src = Path(json_path)
        trash = _rescue_dir(src, TRASH_DIR_NAME)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(str(src), str(trash / f"{src.stem}.{stamp}.json"))
        transcript_dir = src.with_suffix("")
        if transcript_dir.is_dir():
            shutil.move(str(transcript_dir), str(trash / f"{src.stem}.{stamp}"))
        return True
    except OSError:
        return False


def find_orphans():
    """Report transcript folders without a JSON, and JSONs without transcripts."""
    bases, _ = find_sessions_dirs()
    orphan_dirs, orphan_jsons = [], []
    for _, base in bases:
        for json_file in iter_session_json_files(base):
            if not json_file.with_suffix("").is_dir():
                orphan_jsons.append(str(json_file))
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (BACKUP_DIR_NAME, TRASH_DIR_NAME)]
            for d in list(dirs):
                if d.startswith("local_") and not (Path(root) / f"{d}.json").exists():
                    orphan_dirs.append(str(Path(root) / d))
    return {"transcript_without_json": orphan_dirs, "json_without_transcript": orphan_jsons}


# ============================================================
# SECTION 5: Headless CLI modes
# ============================================================

def cli_list():
    sessions, diag = load_sessions()
    if not sessions:
        print("No sessions found.")
        print(f"Searched: {diag['searched_paths']}")
        return
    for s in sessions:
        state = "ARCHIVED" if s.get("isArchived") else "active  "
        title = s.get("title") or s.get("processName") or "(untitled)"
        print(f"[{state}] [{s.get('_source','?'):6}] {title:50.50} {s.get('cwd','')}")
    archived = sum(1 for s in sessions if s.get("isArchived"))
    print(f"\n{len(sessions)} sessions total, {archived} archived.")


def cli_restore_all_archived():
    sessions, _ = load_sessions()
    archived = [s for s in sessions if s.get("isArchived")]
    if not archived:
        print("No archived sessions found.")
        return
    count = sum(1 for s in archived if restore_session(s["_path"]))
    print(f"Restored {count}/{len(archived)} archived sessions.")
    print("Fully quit and relaunch Claude Desktop to see them.")


# ============================================================
# SECTION 6: Server process management
# ============================================================

def is_server_running():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", PORT))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def kill_existing_server():
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
        except (ValueError, ProcessLookupError, OSError):
            pass
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


# ============================================================
# SECTION 7: Web UI
# ============================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Rescue for Claude</title>
<style>
  :root {
    --bg: #0c0e14; --surface: #14171f; --surface2: #1c2029; --surface3: #242833;
    --accent: #e8643a; --accent-soft: rgba(232,100,58,0.12); --accent-hover: #d4572f;
    --emerald: #34d399; --emerald-soft: rgba(52,211,153,0.12);
    --amber: #fbbf24; --amber-soft: rgba(251,191,36,0.12);
    --rose: #f43f5e; --rose-soft: rgba(244,63,94,0.12);
    --blue: #60a5fa; --blue-soft: rgba(96,165,250,0.12);
    --text: #e8eaed; --text2: #9aa2b5; --text3: #6a7188;
    --border: rgba(255,255,255,0.06); --border-hover: rgba(255,255,255,0.12);
    --radius: 10px; --radius-lg: 14px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; font-size: 16px; }
  .header { padding: 20px 32px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; background: var(--bg); border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 10px; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header-version { font-size: 14px; color: var(--text3); margin-left: 6px; }
  .header-actions { display: flex; gap: 8px; }
  .btn { padding: 8px 15px; border: none; border-radius: var(--radius); cursor: pointer; font-size: 14px; font-weight: 500; transition: 0.15s; display: inline-flex; align-items: center; gap: 6px; }
  .btn:hover { transform: translateY(-1px); }
  .btn-accent { background: var(--accent); color: white; }
  .btn-emerald { background: var(--emerald); color: #0c0e14; }
  .btn-amber { background: var(--amber); color: #0c0e14; }
  .btn-rose { background: var(--rose); color: white; }
  .btn-ghost { background: var(--surface2); color: var(--text2); border: 1px solid var(--border); }
  .btn-ghost:hover { color: var(--text); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
  .toolbar { padding: 16px 32px 8px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .filter-group { display: flex; gap: 2px; background: var(--surface); border-radius: var(--radius); padding: 3px; border: 1px solid var(--border); }
  .filter-btn { padding: 7px 16px; border: none; border-radius: 7px; background: transparent; color: var(--text2); cursor: pointer; font-size: 14px; font-weight: 500; }
  .filter-btn.active { background: var(--surface3); color: var(--text); }
  .count-badge { font-size: 14px; color: var(--text3); }
  .search-box { flex: 1; min-width: 200px; max-width: 420px; padding: 8px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); color: var(--text); font-size: 14px; }
  .search-box:focus { outline: none; border-color: var(--accent); }
  .bulk-actions { padding: 8px 32px 12px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .bulk-divider { width: 1px; height: 20px; background: var(--border); margin: 0 4px; }
  .select-all-area { padding: 0 32px 8px; display: flex; align-items: center; gap: 10px; font-size: 14px; color: var(--text2); }
  .ck { appearance: none; -webkit-appearance: none; width: 18px; height: 18px; border: 2px solid var(--text3); border-radius: 5px; cursor: pointer; position: relative; flex-shrink: 0; background: transparent; }
  .ck:checked { background: var(--accent); border-color: var(--accent); }
  .ck:checked::after { content: ''; position: absolute; left: 4px; top: 1px; width: 5px; height: 9px; border: solid white; border-width: 0 2px 2px 0; transform: rotate(45deg); }
  .group-header { padding: 18px 32px 4px; font-size: 14px; color: var(--text2); font-weight: 600; }
  .session-list { padding: 0 32px 32px; display: flex; flex-direction: column; gap: 6px; }
  .session-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 14px 18px; display: grid; grid-template-columns: 28px 1fr auto; gap: 14px; align-items: center; cursor: pointer; }
  .session-card:hover { border-color: var(--border-hover); background: var(--surface2); }
  .session-card.selected { border-color: var(--accent); background: var(--accent-soft); }
  .session-name { font-size: 15px; font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .session-meta { font-size: 14px; color: var(--text2); display: flex; gap: 16px; flex-wrap: wrap; }
  .badge { font-size: 12px; padding: 2px 8px; border-radius: 5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px; }
  .badge-archived { background: var(--amber-soft); color: var(--amber); }
  .badge-active { background: var(--emerald-soft); color: var(--emerald); }
  .badge-code { background: var(--blue-soft); color: var(--blue); }
  .badge-cowork { background: var(--accent-soft); color: var(--accent); }
  .session-actions { display: flex; gap: 4px; flex-shrink: 0; opacity: 0.6; }
  .session-card:hover .session-actions { opacity: 1; }
  .toast { position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%) translateY(80px); padding: 12px 22px; border-radius: var(--radius); font-size: 14px; font-weight: 500; z-index: 1000; transition: transform 0.25s; }
  .toast.show { transform: translateX(-50%) translateY(0); }
  .toast-success { background: rgba(52,211,153,0.92); color: #0c0e14; }
  .toast-error { background: rgba(244,63,94,0.92); color: white; }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.65); display: flex; align-items: center; justify-content: center; z-index: 200; }
  .modal { background: var(--surface2); border: 1px solid var(--border-hover); border-radius: var(--radius-lg); padding: 24px; max-width: 460px; width: 90%; }
  .modal h2 { font-size: 17px; margin-bottom: 10px; }
  .modal p { color: var(--text2); margin-bottom: 18px; line-height: 1.7; font-size: 14px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
  .empty-state { text-align: center; padding: 80px 32px; color: var(--text2); }
  .empty-state p { font-size: 15px; line-height: 1.8; }
  .empty-state code { background: var(--surface2); padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 14px; }
  .restart-banner { margin: 0 32px 8px; padding: 12px 18px; border-radius: var(--radius); background: var(--amber-soft); color: var(--amber); font-size: 14px; font-weight: 500; display: none; }
  .restart-banner.show { display: block; }
</style>
</head>
<body>

<div class="header">
  <div><h1>Session Rescue for Claude <span class="header-version">v__VERSION__</span></h1></div>
  <div class="header-actions">
    <button class="btn btn-ghost" onclick="checkOrphans()">Check Orphans</button>
    <button class="btn btn-ghost" onclick="openFolder()">Open Folder</button>
    <button class="btn btn-accent" onclick="refresh()">Refresh</button>
  </div>
</div>

<div class="toolbar">
  <div class="filter-group">
    <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
    <button class="filter-btn" data-filter="archived" onclick="setFilter('archived')">Archived</button>
    <button class="filter-btn" data-filter="active" onclick="setFilter('active')">Active</button>
    <button class="filter-btn" data-filter="Code" onclick="setFilter('Code')">Code</button>
    <button class="filter-btn" data-filter="Cowork" onclick="setFilter('Cowork')">Cowork</button>
  </div>
  <input class="search-box" id="search" placeholder="Search title or folder..." oninput="renderSessions()">
  <label style="display:flex;align-items:center;gap:8px;font-size:14px;color:var(--text2);cursor:pointer">
    <input type="checkbox" class="ck" id="group-by-project" onchange="renderSessions()"> Group by project
  </label>
  <span class="count-badge" id="count"></span>
</div>

<div class="bulk-actions">
  <button class="btn btn-emerald" onclick="restoreSelected()" id="btn-restore-sel" disabled>Restore Selected</button>
  <button class="btn btn-amber" onclick="archiveSelected()" id="btn-archive-sel" disabled>Archive Selected</button>
  <button class="btn btn-rose" onclick="trashSelected()" id="btn-trash-sel" disabled>Trash Selected</button>
  <span class="bulk-divider"></span>
  <button class="btn btn-ghost" onclick="restoreAllArchived()">Restore All Archived</button>
</div>

<div class="select-all-area">
  <input type="checkbox" id="select-all" class="ck" onchange="toggleSelectAll(this.checked)">
  <label for="select-all">Select all shown</label>
</div>

<div class="restart-banner" id="restart-banner">
  Changes saved. Fully quit Claude Desktop (including the system tray icon) and relaunch to see them.
</div>

<div class="session-list" id="session-list"></div>
<div class="toast" id="toast"></div>

<script>
let sessions = [];
let currentFilter = 'all';
let selectedPaths = new Set();
let lastDiagnostic = null;

setInterval(() => { fetch('/api/heartbeat', { method: 'POST' }).catch(() => {}); }, 2000);

async function api(endpoint, data) {
  const res = await fetch('/api/' + endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });
  return res.json();
}

function formatDate(ms) {
  if (!ms) return 'Unknown';
  const d = new Date(ms);
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0') + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  renderSessions();
}

function getFiltered() {
  const q = (document.getElementById('search').value || '').toLowerCase();
  return sessions.filter(s => {
    if (currentFilter === 'archived' && !s.isArchived) return false;
    if (currentFilter === 'active' && s.isArchived) return false;
    if ((currentFilter === 'Code' || currentFilter === 'Cowork') && s._source !== currentFilter) return false;
    if (q) {
      const hay = ((s.title || '') + ' ' + (s.processName || '') + ' ' + (s.cwd || '')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function cardHtml(s) {
  const isSelected = selectedPaths.has(s._path);
  const stateBadge = s.isArchived ? `<span class="badge badge-archived">Archived</span>` : `<span class="badge badge-active">Active</span>`;
  const srcBadge = s._source === 'Code' ? `<span class="badge badge-code">Code</span>` : `<span class="badge badge-cowork">Cowork</span>`;
  const title = s.title || s.processName || '(untitled)';
  return `
    <div class="session-card ${isSelected ? 'selected' : ''}" onclick="toggleSelect('${escapeHtml(s._path).replace(/\\/g,'\\\\')}')">
      <input type="checkbox" class="ck" ${isSelected ? 'checked' : ''} onclick="event.stopPropagation(); toggleSelect('${escapeHtml(s._path).replace(/\\/g,'\\\\')}')">
      <div>
        <div class="session-name">${stateBadge} ${srcBadge} ${escapeHtml(title)}</div>
        <div class="session-meta">
          <span>Model: ${escapeHtml(s.model || 'Unknown')}</span>
          <span>Last active: ${formatDate(s.lastActivityAt)}</span>
          <span>${escapeHtml(s.cwd || '')}</span>
        </div>
      </div>
      <div class="session-actions">
        ${s.isArchived
          ? `<button class="btn btn-emerald" onclick="event.stopPropagation(); actOne('restore', '${escapeHtml(s._path).replace(/\\/g,'\\\\')}')">Restore</button>`
          : `<button class="btn btn-amber" onclick="event.stopPropagation(); actOne('archive', '${escapeHtml(s._path).replace(/\\/g,'\\\\')}')">Archive</button>`}
        <button class="btn btn-rose" onclick="event.stopPropagation(); trashOne('${escapeHtml(s._path).replace(/\\/g,'\\\\')}')">Trash</button>
      </div>
    </div>`;
}

function renderSessions() {
  const list = document.getElementById('session-list');
  const filtered = getFiltered();
  const archivedCount = sessions.filter(s => s.isArchived).length;
  document.getElementById('count').textContent = `${filtered.length} shown, ${archivedCount} archived total`;

  if (filtered.length === 0) {
    let msg = 'No sessions match.';
    if (sessions.length === 0 && lastDiagnostic) {
      msg = `No session stores found.<br><br>Searched:<br>${(lastDiagnostic.searched_paths||[]).map(p=>`<code>${escapeHtml(p)}</code>`).join('<br>')}<br><br>Run with <code>--path DIR</code> to point at a custom location.`;
    }
    list.innerHTML = `<div class="empty-state"><p>${msg}</p></div>`;
    updateBulkButtons();
    return;
  }

  const grouped = document.getElementById('group-by-project').checked;
  if (grouped) {
    const groups = {};
    filtered.forEach(s => {
      const key = s.cwd || '(no folder)';
      (groups[key] = groups[key] || []).push(s);
    });
    list.innerHTML = Object.keys(groups).sort().map(key =>
      `<div class="group-header">${escapeHtml(key)} (${groups[key].length})</div>` +
      groups[key].map(cardHtml).join('')
    ).join('');
  } else {
    list.innerHTML = filtered.map(cardHtml).join('');
  }
  updateBulkButtons();
}

function toggleSelect(path) {
  if (selectedPaths.has(path)) selectedPaths.delete(path); else selectedPaths.add(path);
  renderSessions();
}

function toggleSelectAll(checked) {
  const filtered = getFiltered();
  if (checked) filtered.forEach(s => selectedPaths.add(s._path));
  else filtered.forEach(s => selectedPaths.delete(s._path));
  renderSessions();
}

function updateBulkButtons() {
  const n = selectedPaths.size;
  document.getElementById('btn-restore-sel').disabled = n === 0;
  document.getElementById('btn-archive-sel').disabled = n === 0;
  document.getElementById('btn-trash-sel').disabled = n === 0;
}

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast toast-' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 3500);
}

function showRestartBanner() {
  document.getElementById('restart-banner').classList.add('show');
}

function showModal(title, message, onConfirm, danger) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `<div class="modal"><h2>${title}</h2><p>${message}</p><div class="modal-actions"><button class="btn btn-ghost" id="m-cancel">Cancel</button><button class="btn ${danger ? 'btn-rose' : 'btn-emerald'}" id="m-ok">Confirm</button></div></div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#m-cancel').onclick = () => overlay.remove();
  overlay.querySelector('#m-ok').onclick = () => { overlay.remove(); onConfirm(); };
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
}

async function refresh() {
  const res = await api('list');
  sessions = res.sessions || [];
  lastDiagnostic = res.diagnostic || null;
  selectedPaths.clear();
  document.getElementById('select-all').checked = false;
  renderSessions();
}

async function actOne(action, path) {
  const res = await api(action, { paths: [path] });
  if (res.count > 0) { showToast(action === 'restore' ? 'Restored (backup saved)' : 'Archived (backup saved)', 'success'); showRestartBanner(); refresh(); }
  else showToast('Operation failed. Is Claude Desktop holding the file?', 'error');
}

async function trashOne(path) {
  const s = sessions.find(x => x._path === path);
  showModal('Move to Trash', `Move "${escapeHtml(s.title || '(untitled)')}" to the rescue trash folder?<br>Recoverable from the trash folder any time.`,
    async () => {
      const res = await api('trash', { paths: [path] });
      if (res.count > 0) { showToast('Moved to trash', 'success'); refresh(); }
      else showToast('Trash failed', 'error');
    }, true);
}

async function restoreSelected() {
  const paths = [...selectedPaths].filter(p => sessions.find(s => s._path === p && s.isArchived));
  if (!paths.length) { showToast('Nothing archived in selection', 'error'); return; }
  const res = await api('restore', { paths });
  showToast(`Restored ${res.count} session(s), backups saved`, 'success');
  showRestartBanner();
  refresh();
}

async function archiveSelected() {
  const paths = [...selectedPaths].filter(p => sessions.find(s => s._path === p && !s.isArchived));
  if (!paths.length) { showToast('Nothing active in selection', 'error'); return; }
  showModal('Archive Selected', `Archive ${paths.length} session(s)?`, async () => {
    const res = await api('archive', { paths });
    showToast(`Archived ${res.count} session(s)`, 'success');
    showRestartBanner();
    refresh();
  });
}

async function trashSelected() {
  const paths = [...selectedPaths];
  showModal('Move to Trash', `Move ${paths.length} session(s) to the rescue trash folder?<br>Recoverable any time from the trash folder.`, async () => {
    const res = await api('trash', { paths });
    showToast(`Moved ${res.count} to trash`, 'success');
    refresh();
  }, true);
}

async function restoreAllArchived() {
  const archived = sessions.filter(s => s.isArchived);
  if (!archived.length) { showToast('No archived sessions', 'error'); return; }
  showModal('Restore All', `Restore all ${archived.length} archived session(s) across Code and Cowork?`, async () => {
    const res = await api('restore', { paths: archived.map(s => s._path) });
    showToast(`Restored ${res.count} session(s), backups saved`, 'success');
    showRestartBanner();
    refresh();
  });
}

async function checkOrphans() {
  const res = await api('orphans');
  const nd = (res.transcript_without_json || []).length;
  const nj = (res.json_without_transcript || []).length;
  if (nd + nj === 0) { showToast('No orphans found. All sessions are consistent.', 'success'); return; }
  showModal('Orphan Report', `Transcript folders missing their JSON: <strong>${nd}</strong><br>JSON files missing transcripts: <strong>${nj}</strong><br><br>Details printed to the terminal running this tool.`, () => {});
}

async function openFolder() { await api('open_folder'); }

refresh();
</script>
</body>
</html>
""".replace("__VERSION__", VERSION)


class Handler(BaseHTTPRequestHandler):
    last_heartbeat = time.time()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}
        except (json.JSONDecodeError, ValueError):
            body = {}

        result = {}

        if path == "/api/heartbeat":
            Handler.last_heartbeat = time.time()
            result = {"ok": True}

        elif path == "/api/list":
            sessions, diag = load_sessions()
            # Send only the fields the UI needs. Full session files can be
            # hundreds of KB each; 80+ of them would be a 30MB response.
            slim = [
                {
                    "_path": s.get("_path"),
                    "_source": s.get("_source"),
                    "title": s.get("title"),
                    "processName": s.get("processName"),
                    "model": s.get("model"),
                    "cwd": s.get("cwd"),
                    "isArchived": bool(s.get("isArchived")),
                    "createdAt": s.get("createdAt"),
                    "lastActivityAt": s.get("lastActivityAt"),
                }
                for s in sessions
            ]
            result = {"sessions": slim, "diagnostic": diag}

        elif path == "/api/restore":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if restore_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/archive":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if archive_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/trash":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if trash_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/orphans":
            result = find_orphans()
            for key, items in result.items():
                if items:
                    print(f"\n{key}:")
                    for item in items:
                        print(f"  {item}")

        elif path == "/api/open_folder":
            bases, _ = find_sessions_dirs()
            if bases:
                system = platform.system()
                target = str(bases[0][1])
                if system == "Darwin":
                    subprocess.run(["open", target])
                elif system == "Windows":
                    subprocess.run(["explorer", target])
                else:
                    subprocess.run(["xdg-open", target])
            result = {"success": True}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def watchdog():
    # Give the browser 30s of grace: a slow request or tab reload must not
    # kill the server. Heartbeats arrive every 2s when the tab is open.
    while True:
        time.sleep(5)
        if time.time() - Handler.last_heartbeat > 30:
            print("Browser closed. Shutting down.")
            try:
                LOCK_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            os._exit(0)


# ============================================================
# SECTION 8: Entry point
# ============================================================

def main():
    global custom_sessions_path

    parser = argparse.ArgumentParser(description="Session Rescue for Claude: manage archived Claude Code and Cowork sessions")
    parser.add_argument("--path", type=str, default=None, help="Manually specify a session directory")
    parser.add_argument("--list", action="store_true", help="Print all sessions to stdout and exit")
    parser.add_argument("--restore-all-archived", action="store_true", help="Restore every archived session, no GUI")
    args = parser.parse_args()

    if args.path:
        custom_sessions_path = args.path

    if args.list:
        cli_list()
        return
    if args.restore_all_archived:
        cli_restore_all_archived()
        return

    if is_server_running():
        print("Server already running. Opening browser.")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return

    kill_existing_server()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} in use. Retrying after cleanup.")
        kill_existing_server()
        time.sleep(1)
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    LOCK_FILE.write_text(str(os.getpid()))
    url = f"http://127.0.0.1:{PORT}"

    print(f"Session Rescue for Claude v{VERSION}")
    print(f"Server: {url}")
    print("Close the browser tab to shut down automatically.")

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    def shutdown(sig, frame):
        print("\nStopping.")
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
