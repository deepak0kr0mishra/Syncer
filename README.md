# SSD Sync — Laptop ↔ External SSD (Ubuntu)

A small Ubuntu desktop app that keeps chosen folder pairs in sync between your
laptop and an external SSD — **both directions, with your explicit confirmation
before anything is written.**

No cloud, no network, no daemons. Pick folders, preview changes, confirm.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![GUI](https://img.shields.io/badge/GUI-Tkinter-orange)
![Platform](https://img.shields.io/badge/platform-Ubuntu-lightgrey)

---

## Features

- **Folder pairs** — e.g. `~/Projects/BEU-Vision ↔ /media/you/SSD/BEU-Vision`. Add, edit, remove.
- **Preview before write** — every planned copy is listed first; nothing is
  copied until you click **Confirm & Sync**.
- **Newest-wins conflicts** — if both sides changed, the newer file wins and the
  older version is backed up as `name.conflict-bak-YYYYMMDD-HHMMSS`.
- **Deletions never propagate** — a file deleted on one side is flagged
  `ORPHANED` and left alone on the other side. This is a hard rule.
- **Skip files per sync** — uncheck files you don't want this time
  (`Skip selected` / double-click / `Del`, `Include all` to restore).
- **SSD detection** — when a configured SSD path reappears, the app asks
  “SSD detected — Sync now?”.
- **Persistent log** — every scan/sync is appended to `sync.log`.

---

## Requirements

- Ubuntu (tested on 24.04 / 26.04) with Python 3.10+
- Tkinter (`python3-tk`) — ships with most Ubuntu desktops, zero pip packages needed

### Check Python is installed

```bash
python3 --version          # want 3.10 or newer
python3 -c "import tkinter; print('tkinter OK')"
```

If either fails, install them:

```bash
sudo apt update
sudo apt install python3 python3-tk
```

---

## Quick start

```bash
git clone https://github.com/deepak0kr0mishra/Syncer.git
cd Syncer
python3 sync_app.py
```

That's it — no `pip install`, no build step.

---

## Usage

1. **Add a pair** — click **+ Add**, then:
   - **Box ① (blue):** pick the **Laptop** folder on this computer
   - **Box ② (orange):** pick the **SSD** folder on the external drive
   - Give it a name, click **Save Pair**.
2. **Check for Changes** — scans all pairs and fills the preview table:
   - `NEW →SSD / →Laptop` — file exists on one side only, will be copied over
   - `UPDATE →SSD / →Laptop` — changed on one side, will be copied over
   - `CONFLICT RESOLVED →…` — changed on both sides, newer wins, older is backed up first
   - `ORPHANED` — deleted on one side; **never copied, never deleted**, just flagged
3. **(Optional) skip files** — select rows you don't want this time → **Skip selected**.
4. **Confirm & Sync** — copies everything in the preview (minus skipped), updates
   history, shows a summary. Re-scans afterwards so you see a clean state.
5. **Remove a pair** — select it on the left → **Remove selected pair**
   (or right-click → Remove). Your files are untouched; only the sync entry and
   its history are deleted.

### Where your data lives

| What | Location |
|---|---|
| Folder pairs | `~/.config/laptop-ssd-sync/config.json` |
| Sync history (one file per pair) | `~/.config/laptop-ssd-sync/manifests/` |
| Log | `~/.config/laptop-ssd-sync/sync.log` |

> The history files are how the app tells “new file” apart from “deleted
> elsewhere”. If a pair ever behaves oddly, remove and re-add it to restart its
> history from scratch.

---

## Desktop icon (app launcher)

To launch SSD Sync from the dock / activities overview like a real app:

**Automatic (recommended):**

```bash
cd Syncer
./install-desktop-icon.sh
```

This creates `~/.local/share/applications/ssd-sync.desktop` with the correct
paths, puts a copy on your `~/Desktop` if you have one, and marks it trusted.
Then:

- Press `Super`, type **SSD Sync**, right-click → **Add to Favorites** to pin it,
- or on the Desktop copy: right-click → **Allow Launching**.

**Manual:**

1. Copy `ssd-sync.desktop` to `~/.local/share/applications/`
2. Edit its `Exec=` and `Path=` lines to point at your `sync_app.py`
3. Run `chmod +x ~/.local/share/applications/ssd-sync.desktop`
4. Log out/in (or run `update-desktop-database ~/.local/share/applications`)

**Remove the icon later:**

```bash
rm ~/.local/share/applications/ssd-sync.desktop ~/Desktop/ssd-sync.desktop
```

---

## Troubleshooting

**`python3: command not found`**
Python isn't installed. Run `sudo apt install python3`, then check with
`python3 --version`.

**`ModuleNotFoundError: No module named 'tkinter'`**
You have Python but not its GUI toolkit. Run `sudo apt install python3-tk`,
then verify with `python3 -c "import tkinter"`.

**App window doesn't open / TclError about display**
You're likely SSH'd in without a display, or on Wayland with a broken
`DISPLAY`. Run it from a terminal inside the desktop session:
`echo $DISPLAY` should print something like `:0` or `:1`.

**“SSD path missing / not detected”**
The drive isn't mounted where the pair expects it. Check:
```bash
lsblk -o NAME,LABEL,MOUNTPOINT   # is the SSD listed and mounted?
ls "/media/$USER"                # your mount path should be here
```
If Ubuntu mounted it somewhere new, **Edit** the pair and re-pick Box ②.

**“Permission denied” when syncing**
Your user can't write to the SSD (common with NTFS/exFAT drives mounted
read-only or owned by root). Check `ls -la /media/$USER/SSD` and remount with
write access.

**Sync says “everything is up to date” but I expected changes**
The history may have learned a state you didn't intend (e.g. you copied files
manually behind its back). Fix: remove and re-add the pair to restart its
history, then scan again.

**I confirmed a conflict and regret it**
Look next to the overwritten file for `*.conflict-bak-YYYYMMDD-HHMMSS` — that's
the losing version, kept automatically. Copy it back manually if needed.

**Where's the detailed log?**
`~/.config/laptop-ssd-sync/sync.log` (also: **Open log file** button in the app).
Attach the last ~50 lines if you file an issue:
```bash
tail -n 50 ~/.config/laptop-ssd-sync/sync.log
```

**App looks tiny/huge on HiDPI**
Ubuntu Settings → Displays → Scale. Tkinter follows the system text-scaling
factor.

---

## Project layout

```
Syncer/
├── sync_app.py              # the whole app (GUI + sync engine)
├── ssd-sync.desktop         # launcher template (paths filled by installer)
├── install-desktop-icon.sh  # one-command desktop icon setup
└── README.md
```

## Sync rules (summary)

1. Changed on one side only → copy that side over.
2. Changed on both sides → newer wins, older kept as `.conflict-bak`.
3. Deleted on one side → do nothing, flag `ORPHANED`.
4. Never seen before, one side only → copy over.
5. Never seen before, both sides differ → newer wins (first-sync conflict).
6. Nothing is ever deleted by this app. Ever.
