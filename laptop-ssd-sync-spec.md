# Project: Laptop ↔ External SSD Sync Tool (Ubuntu)

## Problem
I carry an external SSD between my laptop and a lab. I edit files in both places. Right now I manually copy-paste files back and forth and it's error-prone. I need a small Ubuntu desktop app that syncs specific folder pairs between my laptop and the SSD, in both directions, with my explicit confirmation before anything is written.

## Platform
- OS: Ubuntu (desktop)
- Language/stack: Python 3 preferred (easy filesystem + GUI work). GUI via Tkinter (simplest, no extra deps) or PyQt5/PySide6 if a nicer look is wanted — pick whichever is faster to build reliably.
- No cloud, no network — purely local filesystem sync between two mounted paths.

## Core Concept
The app manages a list of **folder pairs**, e.g.:
```
Pair 1: /home/user/Projects/BEU-Vision  <->  /media/user/SSD/BEU-Vision
Pair 2: /home/user/Docs/Notes           <->  /media/user/SSD/Notes
```
Each pair is configured once by the user (manual folder picker for laptop side and SSD side) and saved. The app does **not** need to auto-detect the drive by UUID/label — the user manually sets the SSD's mount path when configuring, and it's expected to stay the same until manually changed.

## Sync Behavior (must-follow rules)

1. **Trigger**: Not fully automatic and not purely manual.
   - The app should detect when the configured SSD path becomes available (i.e., the folder exists / drive is mounted) and then show a notification/prompt: "SSD detected — Sync now?"
   - It should also have a manual "Sync Now" button in the GUI at all times.
   - **Nothing is written to disk without the user clicking Confirm** after reviewing a preview of changes.

2. **Conflict resolution — newest file wins**:
   - If a file exists on both sides and both have changed since the last sync, compare modification time (mtime). The newer file overwrites the older one.
   - If a file changed on only one side since the last sync, copy that change to the other side (this is a normal one-way update, not a "conflict").

3. **Deletions — never auto-propagate**:
   - If a file was deleted from one side since the last sync, do **not** delete it from the other side, and do **not** silently recreate it on the side it's missing from either.
   - Instead, treat it as "orphaned" — leave the existing copy alone and just log/flag it for the user to review manually. The app should never delete files on its own.

4. **New files**: A file that exists on one side only, and was never seen before (not in sync history), should simply be copied to the other side.

## Why a naive two-way rsync won't work here
Plain mtime comparison between two folders can't tell the difference between "this file is new" and "this file was deleted on the other side." To support rule 3 correctly, the app needs to remember what it last synced.

**Required: a sync state manifest per folder pair** — a local JSON file (e.g. stored in `~/.config/laptop-ssd-sync/`) recording, for every relative file path it has seen:
```json
{
  "relative/path/to/file.txt": {
    "last_synced_mtime": 1728931200,
    "last_synced_size": 4096
  }
}
```

### Per-file sync algorithm (for each folder pair, for each relative path found on either side)
- **In manifest, missing on both current sides**: shouldn't happen; ignore.
- **In manifest, present on both sides, same mtime as manifest on both**: unchanged, skip.
- **In manifest, present on both sides, mtime differs from manifest on one side only**: copy that side's version to the other (simple update).
- **In manifest, present on both sides, mtime differs from manifest on both sides (both changed)**: conflict → newer of the two wins, copy it over the older, log this as a resolved conflict.
- **In manifest, present on only one side now (was on both before)**: this file was deleted on the other side → do nothing, just log as "orphaned / deleted elsewhere, left as-is."
- **Not in manifest, present on only one side**: new file → copy to the other side, add to manifest.
- **Not in manifest, present on both sides with different content/mtime**: treat as conflict on first sync → newer wins (or ask user, since there's no history to trust).
- After any copy, update the manifest entry with the new mtime/size for both sides.

## GUI Requirements
- Simple window, Tkinter is fine.
- List of configured folder pairs, with "Add Pair" (pick laptop folder + SSD folder) and "Remove Pair" buttons.
- "Check for Changes" / "Sync Now" button that:
  1. Scans all pairs.
  2. Shows a **preview/log panel** listing every planned action before doing anything:
     - `[UPDATE →SSD] file.txt (laptop newer)`
     - `[UPDATE →Laptop] notes.md (SSD newer)`
     - `[CONFLICT RESOLVED →SSD] report.docx (laptop newer, SSD version kept as report.docx.conflict-bak)`
     - `[NEW →SSD] new_file.py`
     - `[ORPHANED] old_draft.txt (missing on SSD, left on laptop only)`
  3. Requires a **Confirm** click before actually copying anything.
  4. After sync, shows a completion summary and keeps a persistent log (append to a `sync.log` file with timestamps).
- On conflict resolution, before overwriting the older file, save a backup copy of the older version as `filename.conflict-bak-<timestamp>` (so nothing is ever truly lost, even on a "wrong" resolution).

## Non-requirements / explicitly out of scope
- No cloud sync, no network calls.
- No automatic deletion propagation, ever — this is a hard rule, not a default.
- No need for USB auto-mount detection by UUID; user sets paths manually and expects them to persist until changed.
- No real-time file-watching sync needed — it's an on-demand/prompted sync tool, not a live daemon.

## Deliverable
A single Python app (Tkinter GUI) runnable via `python3 sync_app.py`, storing its config (folder pairs) and manifest state in `~/.config/laptop-ssd-sync/`, with the algorithm and GUI behavior described above.
