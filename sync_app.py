#!/usr/bin/env python3
"""Laptop <-> External SSD Sync Tool (Ubuntu, Tkinter).

Runnable via:  python3 sync_app.py

- Manages folder pairs (laptop <-> SSD).
- Manifest-based two-way sync: newest wins, deletions never propagate.
- Preview-before-write: nothing is copied without explicit Confirm.
- Conflict backups: older version kept as `name.conflict-bak-YYYYMMDD-HHMMSS`.
- SSD availability polling with "SSD detected - Sync now?" prompt.
- Config + manifests + log in ~/.config/laptop-ssd-sync/
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

APP_NAME = "SSD Sync"
CONFIG_DIR = Path.home() / ".config" / "laptop-ssd-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
MANIFEST_DIR = CONFIG_DIR / "manifests"
LOG_FILE = CONFIG_DIR / "sync.log"

MTIME_EPS = 0.002  # tolerance when comparing mtimes
SSD_POLL_MS = 3000

ACCENT = "#E95420"       # Ubuntu Yaru orange
ACCENT_DARK = "#C7441A"
BG = "#232430"
CARD = "#2D2E3A"
CARD2 = "#363748"
FG = "#F2F2F2"
MUTED = "#AEA79F"
BORDER = "#454545"
GREEN = "#46A656"
BLUE = "#3CA3E8"
ORANGE = "#E95420"
GREY = "#8A8A8A"
RED = "#E05B5B"

# Box accents for the two-folder picker
LAPTOP_BLUE = "#4A90D9"
LAPTOP_BG = "#28334A"
SSD_ORANGE = "#E95420"
SSD_BG = "#40302A"
SUCCESS_BG = "#2A4033"


# --------------------------------------------------------------------------
# Helpers: config / manifest / log
# --------------------------------------------------------------------------

def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("", encoding="utf-8")


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_FILE.exists():
        return {"pairs": [], "keep_backups": True}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "pairs" not in data:
            return {"pairs": [], "keep_backups": True}
        # sanitize
        pairs = []
        for p in data.get("pairs", []):
            if isinstance(p, dict) and p.get("laptop") and p.get("ssd"):
                pairs.append({
                    "id": p.get("id") or uuid.uuid4().hex[:12],
                    "laptop": p["laptop"],
                    "ssd": p["ssd"],
                    "name": p.get("name") or f"{Path(p['laptop']).name} ↔ {Path(p['ssd']).name}",
                })
        return {"pairs": pairs, "keep_backups": bool(data.get("keep_backups", True))}
    except Exception:
        return {"pairs": [], "keep_backups": True}


def save_config(cfg: dict) -> None:
    ensure_dirs()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def manifest_path(pair_id: str) -> Path:
    return MANIFEST_DIR / f"{pair_id}.json"


def load_manifest(pair_id: str) -> dict:
    p = manifest_path(pair_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_manifest(pair_id: str, manifest: dict) -> None:
    ensure_dirs()
    manifest_path(pair_id).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def log_to_file(message: str) -> None:
    ensure_dirs()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


# --------------------------------------------------------------------------
# Sync engine (pure logic, no tkinter)
# --------------------------------------------------------------------------

@dataclass
class FileInfo:
    abspath: Path
    mtime: float
    size: int


@dataclass
class Action:
    kind: str  # NEW_TO_SSD, NEW_TO_LAPTOP, UPDATE_TO_SSD, UPDATE_TO_LAPTOP,
               # CONFLICT_TO_SSD, CONFLICT_TO_LAPTOP, ORPHANED, ERROR, INFO
    relpath: str
    pair_name: str = ""
    detail: str = ""
    src: Path | None = None
    dst: Path | None = None
    # display helpers
    label: str = ""
    direction: str = ""  # →SSD, →Laptop, —

    def display_row(self):
        return (self.label, self.relpath, self.detail)


KINDS_WRITE = {
    "NEW_TO_SSD", "NEW_TO_LAPTOP",
    "UPDATE_TO_SSD", "UPDATE_TO_LAPTOP",
    "CONFLICT_TO_SSD", "CONFLICT_TO_LAPTOP",
}

KIND_LABEL = {
    "NEW_TO_SSD": "NEW →SSD",
    "NEW_TO_LAPTOP": "NEW →Laptop",
    "UPDATE_TO_SSD": "UPDATE →SSD",
    "UPDATE_TO_LAPTOP": "UPDATE →Laptop",
    "CONFLICT_TO_SSD": "CONFLICT RESOLVED →SSD",
    "CONFLICT_TO_LAPTOP": "CONFLICT RESOLVED →Laptop",
    "ORPHANED": "ORPHANED",
    "ERROR": "ERROR",
    "INFO": "INFO",
}


def _should_skip(rel: str) -> bool:
    # Never sync our own conflict backups (avoids backup ping-pong).
    return ".conflict-bak-" in Path(rel).name


def scan_files(root: Path) -> dict[str, FileInfo]:
    """Walk root, return {relpath_posix: FileInfo} for regular files."""
    out: dict[str, FileInfo] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # skip symlinked dirs
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            ap = Path(dirpath) / fn
            try:
                if ap.is_symlink():
                    continue
                st = ap.stat()
                rel = ap.relative_to(root).as_posix()
                if _should_skip(rel):
                    continue
                out[rel] = FileInfo(abspath=ap, mtime=st.st_mtime, size=st.st_size)
            except OSError:
                continue
    return out


def _stored(entry: dict | None, side: str) -> tuple[float, int] | None:
    """Return the (mtime, size) remembered for one side, or None if unknown.

    Understands the current per-side format (``laptop_mtime``/``ssd_mtime``…)
    and the legacy single-value format (``last_synced_mtime``…). Per-side
    history matters: SSDs formatted exFAT/FAT round timestamps to 2 seconds
    while ext4 keeps nanoseconds, so one shared timestamp makes the laptop
    side look "changed" forever and turns every one-sided edit into a fake
    conflict + backup.
    """
    if not isinstance(entry, dict):
        return None
    m = entry.get(f"{side}_mtime")
    s = entry.get(f"{side}_size")
    if m is None or s is None:
        # legacy format: one timestamp shared by both sides
        m = entry.get("last_synced_mtime")
        s = entry.get("last_synced_size")
    if m is None or s is None:
        return None
    try:
        return float(m), int(s)
    except (TypeError, ValueError):
        return None


def _changed_side(current: FileInfo | None, entry: dict | None, side: str) -> bool:
    """Did this side change since the last sync? Each side is compared only
    against its own remembered state, so filesystem timestamp rounding on the
    *other* side can never flag this side as changed."""
    if current is None or entry is None:
        return False
    stored = _stored(entry, side)
    if stored is None:
        return True
    old_m, old_s = stored
    if current.size != old_s:
        return True
    return abs(current.mtime - old_m) > MTIME_EPS


def _manifest_entry(lap: FileInfo | None, ssd: FileInfo | None) -> dict:
    """Build a per-side manifest entry from current scan results."""
    e: dict = {}
    if lap is not None:
        e["laptop_mtime"] = lap.mtime
        e["laptop_size"] = lap.size
    if ssd is not None:
        e["ssd_mtime"] = ssd.mtime
        e["ssd_size"] = ssd.size
    return e


def plan_pair(pair: dict, manifest: dict,
             keep_backups: bool = True) -> tuple[list[Action], dict]:
    """Compute planned actions for one pair. Returns (actions, manifest_updates_noop).

    manifest_updates_noop: entries to add for identical-on-both-sides first sync
    (no copy needed but manifest must learn the file).
    """
    laptop_root = Path(pair["laptop"])
    ssd_root = Path(pair["ssd"])
    pname = pair.get("name", "")
    actions: list[Action] = []
    noop_updates: dict[str, dict] = {}

    laptop_missing = not laptop_root.is_dir()
    ssd_missing = not ssd_root.is_dir()
    if laptop_missing or ssd_missing:
        which = []
        if laptop_missing:
            which.append(f"laptop path missing: {laptop_root}")
        if ssd_missing:
            which.append(f"SSD path missing: {ssd_root}")
        actions.append(Action(kind="ERROR", relpath="—", pair_name=pname,
                              label="ERROR", detail="; ".join(which) + " — skipped"))
        return actions, noop_updates

    lap = scan_files(laptop_root)
    ssd = scan_files(ssd_root)
    all_rels = set(lap) | set(ssd) | set(manifest)

    for rel in sorted(all_rels):
        L = lap.get(rel)
        S = ssd.get(rel)
        entry = manifest.get(rel)

        lap_p = laptop_root / rel
        ssd_p = ssd_root / rel

        if entry is not None:
            in_l, in_s = L is not None, S is not None
            if not in_l and not in_s:
                continue  # shouldn't happen; ignore
            if in_l and in_s:
                ch_l = _changed_side(L, entry, "laptop")
                ch_s = _changed_side(S, entry, "ssd")
                if not ch_l and not ch_s:
                    continue  # unchanged
                if ch_l and not ch_s:
                    actions.append(Action(
                        kind="UPDATE_TO_SSD", relpath=rel, pair_name=pname,
                        label=KIND_LABEL["UPDATE_TO_SSD"],
                        detail="laptop newer", src=lap_p, dst=ssd_p))
                elif ch_s and not ch_l:
                    actions.append(Action(
                        kind="UPDATE_TO_LAPTOP", relpath=rel, pair_name=pname,
                        label=KIND_LABEL["UPDATE_TO_LAPTOP"],
                        detail="SSD newer", src=ssd_p, dst=lap_p))
                else:
                    # both sides really changed -> conflict, newest wins
                    bak = " — older version backed up" if keep_backups else " — overwrites older"
                    assert L is not None and S is not None
                    if L.mtime >= S.mtime:
                        actions.append(Action(
                            kind="CONFLICT_TO_SSD", relpath=rel, pair_name=pname,
                            label=KIND_LABEL["CONFLICT_TO_SSD"],
                            detail=f"both changed, laptop newer{bak}",
                            src=lap_p, dst=ssd_p))
                    else:
                        actions.append(Action(
                            kind="CONFLICT_TO_LAPTOP", relpath=rel, pair_name=pname,
                            label=KIND_LABEL["CONFLICT_TO_LAPTOP"],
                            detail=f"both changed, SSD newer{bak}",
                            src=ssd_p, dst=lap_p))
            else:
                # present on only one side but was known -> deleted elsewhere.
                # Hard rule: never propagate, never recreate. Flag only.
                if in_l and not in_s:
                    actions.append(Action(
                        kind="ORPHANED", relpath=rel, pair_name=pname,
                        label=KIND_LABEL["ORPHANED"],
                        detail="missing on SSD, left on laptop only"))
                else:
                    actions.append(Action(
                        kind="ORPHANED", relpath=rel, pair_name=pname,
                        label=KIND_LABEL["ORPHANED"],
                        detail="missing on laptop, left on SSD only"))
        else:
            # never seen before
            if L is not None and S is None:
                actions.append(Action(
                    kind="NEW_TO_SSD", relpath=rel, pair_name=pname,
                    label=KIND_LABEL["NEW_TO_SSD"],
                    detail="new file on laptop", src=lap_p, dst=ssd_p))
            elif S is not None and L is None:
                actions.append(Action(
                    kind="NEW_TO_LAPTOP", relpath=rel, pair_name=pname,
                    label=KIND_LABEL["NEW_TO_LAPTOP"],
                    detail="new file on SSD", src=ssd_p, dst=lap_p))
            elif L is not None and S is not None:
                # on both sides with no history -> conflict on first sync
                try:
                    same = (L.size == S.size) and filecmp.cmp(
                        str(L.abspath), str(S.abspath), shallow=False)
                except OSError:
                    same = False
                if same:
                    # identical content: nothing to copy, just learn each side as-is
                    noop_updates[rel] = _manifest_entry(L, S)
                else:
                    bak = " — older version backed up" if keep_backups else " — overwrites older"
                    if L.mtime >= S.mtime:
                        actions.append(Action(
                            kind="CONFLICT_TO_SSD", relpath=rel, pair_name=pname,
                            label=KIND_LABEL["CONFLICT_TO_SSD"],
                            detail=f"on both sides (first sync), laptop newer{bak}",
                            src=lap_p, dst=ssd_p))
                    else:
                        actions.append(Action(
                            kind="CONFLICT_TO_LAPTOP", relpath=rel, pair_name=pname,
                            label=KIND_LABEL["CONFLICT_TO_LAPTOP"],
                            detail=f"on both sides (first sync), SSD newer{bak}",
                            src=ssd_p, dst=lap_p))
    return actions, noop_updates


def execute_actions(pair: dict, actions: list[Action],
                    manifest: dict, noop_updates: dict | None = None,
                    keep_backups: bool = True) -> tuple[dict, list[str]]:
    """Copy files for write-actions, update manifest. Returns (new_manifest, errors).

    The manifest remembers each side separately (laptop vs SSD timestamps), so
    filesystem timestamp rounding on one side can never fake a change on the
    other side on the next scan.
    """
    manifest = dict(manifest)
    if noop_updates:
        manifest.update(noop_updates)
    errors: list[str] = []
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    for a in actions:
        if a.kind not in KINDS_WRITE:
            continue
        assert a.src is not None and a.dst is not None
        try:
            if not a.src.is_file():
                errors.append(f"{a.relpath}: source vanished, skipped")
                continue
            # True conflict: back up the older (destination) version first.
            if a.kind.startswith("CONFLICT") and a.dst.exists():
                if keep_backups:
                    backup = a.dst.parent / f"{a.dst.name}.conflict-bak-{ts}"
                    try:
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(a.dst), str(backup))
                        log_to_file(f"BACKUP {pair.get('name','')} :: {a.relpath} -> {backup.name}")
                    except OSError as e:
                        errors.append(f"{a.relpath}: backup failed ({e}), skipped overwrite")
                        continue
                else:
                    log_to_file(f"BACKUP-SKIPPED (disabled) {pair.get('name','')} :: {a.relpath}")
            a.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(a.src), str(a.dst))
            # Remember each side's own post-copy state.
            try:
                st_src = a.src.stat()
            except OSError:
                st_src = None
            st_dst = a.dst.stat()
            if a.kind.endswith("_TO_SSD"):
                lap_m, lap_s = (st_src.st_mtime, st_src.st_size) if st_src else (st_dst.st_mtime, st_dst.st_size)
                ssd_m, ssd_s = st_dst.st_mtime, st_dst.st_size
            else:  # _TO_LAPTOP
                lap_m, lap_s = st_dst.st_mtime, st_dst.st_size
                ssd_m, ssd_s = (st_src.st_mtime, st_src.st_size) if st_src else (st_dst.st_mtime, st_dst.st_size)
            manifest[a.relpath] = {"laptop_mtime": lap_m, "laptop_size": lap_s,
                                   "ssd_mtime": ssd_m, "ssd_size": ssd_s}
            log_to_file(f"{a.label} {pair.get('name','')} :: {a.relpath} ({a.detail})")
        except OSError as e:
            errors.append(f"{a.relpath}: {e}")
            log_to_file(f"ERROR {pair.get('name','')} :: {a.relpath}: {e}")

    # Log orphans / info too (no writes).
    for a in actions:
        if a.kind == "ORPHANED":
            log_to_file(f"ORPHANED {pair.get('name','')} :: {a.relpath} ({a.detail})")
    return manifest, errors


# --------------------------------------------------------------------------
# Add / Edit Pair dialog — two separate path boxes
# --------------------------------------------------------------------------

class AddPairDialog(tk.Toplevel):
    """Modal dialog with two clearly separated folder pickers.

    Box 1 (blue):  Laptop folder on this computer.
    Box 2 (orange): SSD folder on the external drive.
    Live validation + file counts so it's obvious what is chosen.
    Returns dict {laptop, ssd, name} via self.result, or None on cancel.
    """

    def __init__(self, parent: tk.Tk, initial_laptop: str = "",
                 initial_ssd: str = "", initial_name: str = "",
                 title: str = "Add Folder Pair") -> None:
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result: dict | None = None
        self._parent = parent

        self.laptop_var = tk.StringVar(value=initial_laptop)
        self.ssd_var = tk.StringVar(value=initial_ssd)
        self.name_var = tk.StringVar(value=initial_name)
        self._name_touched = bool(initial_name)

        # modal
        self.transient(parent)
        self.grab_set()

        outer = ttk.Frame(self, padding=20, style="Card.TFrame")
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="🔗  " + title, style="Card.TLabel",
                  font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Pick the two folders that should stay in sync. Nothing is copied yet.",
                  style="CardMuted.TLabel", font=("TkDefaultFont", 9)).pack(anchor="w", pady=(2, 12))

        # ---- Box 1: laptop ----
        box1 = tk.Frame(outer, bg=LAPTOP_BG, highlightbackground=LAPTOP_BLUE,
                        highlightthickness=1, bd=0)
        box1.pack(fill="x", pady=(0, 8))
        inner1 = tk.Frame(box1, bg=LAPTOP_BG)
        inner1.pack(fill="x", padx=12, pady=10)
        tk.Label(inner1, text="💻  ①  LAPTOP  —  folder on this computer",
                 bg=LAPTOP_BG, fg=LAPTOP_BLUE, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        tk.Label(inner1, text="e.g.  /home/you/Projects/BEU-Vision",
                 bg=LAPTOP_BG, fg="#9AB6D8", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(0, 6))
        row1 = tk.Frame(inner1, bg=LAPTOP_BG)
        row1.pack(fill="x")
        self.laptop_entry = tk.Entry(row1, textvariable=self.laptop_var,
                                     bg="#1E2433", fg=FG, insertbackground=FG,
                                     relief="flat", font=("TkDefaultFont", 9))
        self.laptop_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        tk.Button(row1, text="Browse…", bg=LAPTOP_BLUE, fg="white", relief="flat",
                  activebackground="#3A7DC4", activeforeground="white",
                  font=("TkDefaultFont", 9, "bold"), padx=14, pady=4,
                  command=self._browse_laptop).pack(side="right")
        self.laptop_info = tk.Label(inner1, text="", bg=LAPTOP_BG, fg=MUTED,
                                    font=("TkDefaultFont", 8), anchor="w")
        self.laptop_info.pack(anchor="w", pady=(6, 0))

        # connector
        conn = tk.Frame(outer, bg=CARD)
        conn.pack(fill="x", pady=2)
        tk.Label(conn, text="↕   two-way sync   ↕", bg=CARD, fg=MUTED,
                 font=("TkDefaultFont", 9, "bold")).pack()

        # ---- Box 2: SSD ----
        box2 = tk.Frame(outer, bg=SSD_BG, highlightbackground=SSD_ORANGE,
                        highlightthickness=1, bd=0)
        box2.pack(fill="x", pady=(8, 0))
        inner2 = tk.Frame(box2, bg=SSD_BG)
        inner2.pack(fill="x", padx=12, pady=10)
        tk.Label(inner2, text="💾  ②  SSD  —  folder on the external drive",
                 bg=SSD_BG, fg=SSD_ORANGE, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        tk.Label(inner2, text="e.g.  /media/you/SSD/BEU-Vision",
                 bg=SSD_BG, fg="#D8A88F", font=("TkDefaultFont", 8)).pack(anchor="w", pady=(0, 6))
        row2 = tk.Frame(inner2, bg=SSD_BG)
        row2.pack(fill="x")
        self.ssd_entry = tk.Entry(row2, textvariable=self.ssd_var,
                                  bg="#33231D", fg=FG, insertbackground=FG,
                                  relief="flat", font=("TkDefaultFont", 9))
        self.ssd_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        tk.Button(row2, text="Browse…", bg=SSD_ORANGE, fg="white", relief="flat",
                  activebackground=ACCENT_DARK, activeforeground="white",
                  font=("TkDefaultFont", 9, "bold"), padx=14, pady=4,
                  command=self._browse_ssd).pack(side="right")
        self.ssd_info = tk.Label(inner2, text="", bg=SSD_BG, fg=MUTED,
                                 font=("TkDefaultFont", 8), anchor="w")
        self.ssd_info.pack(anchor="w", pady=(6, 0))

        # ---- Name ----
        name_row = ttk.Frame(outer, style="Card.TFrame")
        name_row.pack(fill="x", pady=(12, 0))
        ttk.Label(name_row, text="Pair name", style="Card.TLabel",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        self.name_entry = tk.Entry(name_row, textvariable=self.name_var,
                                   bg=CARD2, fg=FG, insertbackground=FG,
                                   relief="flat", font=("TkDefaultFont", 10))
        self.name_entry.pack(fill="x", ipady=7, pady=(4, 0))
        self.name_entry.bind("<KeyRelease>", lambda _e: self._on_name_typed())

        self.error_label = ttk.Label(outer, text="", style="Card.TLabel", foreground=RED,
                                     font=("TkDefaultFont", 9), wraplength=480)
        self.error_label.pack(anchor="w", pady=(8, 0))

        # ---- Buttons ----
        btns = ttk.Frame(outer, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self._on_cancel).pack(side="right")
        self.save_btn = ttk.Button(btns, text="✓  Save Pair", style="Accent.TButton",
                                   command=self._on_save)
        self.save_btn.pack(side="right", padx=(0, 8))

        self.laptop_var.trace_add("write", lambda *_: self._on_change())
        self.ssd_var.trace_add("write", lambda *_: self._on_change())

        self._on_change()
        self.update_idletasks()
        w, h = 540, self.winfo_reqheight()
        px = parent.winfo_x() + (parent.winfo_width() - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 3
        self.geometry(f"{w}x{h}+{max(px, 0)}+{max(py, 0)}")
        self.laptop_entry.focus_set()
        self.wait_window(self)

    # -- events --
    def _on_name_typed(self) -> None:
        self._name_touched = True

    def _browse_laptop(self) -> None:
        d = filedialog.askdirectory(parent=self, title="① Pick LAPTOP folder (this computer)",
                                    initialdir=self.laptop_var.get() or str(Path.home()))
        if d:
            self.laptop_var.set(d)

    def _browse_ssd(self) -> None:
        start = self.ssd_var.get() or "/media"
        if not Path(start).exists():
            start = str(Path.home())
        d = filedialog.askdirectory(parent=self, title="② Pick SSD folder (external drive)",
                                    initialdir=start)
        if d:
            self.ssd_var.set(d)

    def _file_count(self, path: str) -> str:
        p = Path(path)
        if not path:
            return "No folder chosen yet."
        if not p.exists():
            return "⚠ Path does not exist."
        if not p.is_dir():
            return "⚠ Not a folder."
        try:
            n = sum(1 for _ in p.rglob("*") if Path(_).is_file())
            # cap display cost on huge trees
            return f"✓ Exists — {n} file(s) found."
        except OSError:
            return "✓ Exists."

    def _on_change(self) -> None:
        lap, ssd = self.laptop_var.get().strip(), self.ssd_var.get().strip()
        self.laptop_info.configure(text=self._file_count(lap))
        self.ssd_info.configure(text=self._file_count(ssd))
        if not self._name_touched:
            if lap or ssd:
                self.name_var.set(f"{Path(lap).name if lap else '?'} ↔ {Path(ssd).name if ssd else '?'}")
            else:
                self.name_var.set("")
        ok, msg = self._validate(silent=True)
        self.error_label.configure(text="" if ok else msg)
        self.save_btn.configure(state="normal" if ok else "disabled")

    def _validate(self, silent: bool = False) -> tuple[bool, str]:
        lap = self.laptop_var.get().strip()
        ssd = self.ssd_var.get().strip()
        name = self.name_var.get().strip()
        if not lap or not ssd:
            return False, "Choose both folders above (① Laptop and ② SSD)."
        if lap == ssd:
            return False, "Both paths are identical — pick two different folders."
        if not Path(lap).is_dir():
            return False, "① Laptop path is not an existing folder."
        if not Path(ssd).is_dir():
            return False, "② SSD path is not available — is the drive mounted?"
        try:
            if Path(ssd).resolve() == Path(lap).resolve():
                return False, "Both paths resolve to the same folder."
            # warn on nesting (same drive copy loops)
            if Path(ssd).resolve().is_relative_to(Path(lap).resolve()) or \
               Path(lap).resolve().is_relative_to(Path(ssd).resolve()):
                return False, "One folder is inside the other — pick two separate folders."
        except (OSError, RuntimeError):
            pass
        if not name and not silent:
            return False, "Give this pair a name."
        if not name:
            return False, ""
        return True, ""

    def _on_save(self) -> None:
        ok, msg = self._validate()
        if not ok:
            self.error_label.configure(text=msg)
            return
        self.result = {"laptop": self.laptop_var.get().strip(),
                       "ssd": self.ssd_var.get().strip(),
                       "name": self.name_var.get().strip()}
        self.grab_release()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.grab_release()
        self.destroy()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class SyncApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — Laptop ↔ SSD")
        self.geometry("1120x720")
        self.minsize(980, 620)
        self.configure(bg=BG)

        self.cfg = load_config()
        self.manifests: dict[str, dict] = {}
        self.pending: list[tuple[dict, Action]] = []  # (pair, action) awaiting confirm
        self.pending_noops: dict[str, dict] = {}      # pair_id -> noop updates
        self.skipped_keys: set[tuple[str, str, str]] = set()  # (pair_id, relpath, kind) user chose to skip
        self._row_map: dict[str, tuple[dict, Action]] = {}    # preview iid -> (pair, action)
        self.pair_by_id: dict[str, dict] = {p["id"]: p for p in self.cfg["pairs"]}
        self._ssd_prev: dict[str, bool] = {}
        self._working = False

        self._setup_style()
        self._build_widgets()
        self._refresh_pairs()
        self._update_ssd_status(initial=True)
        self._append_log(f"Ready. Config: {CONFIG_DIR}  •  Log: {LOG_FILE}")
        if not self.cfg["pairs"]:
            self._append_log("No folder pairs yet — click “+ Add Pair” to configure your first sync.")
        self.after(SSD_POLL_MS, self._poll_ssd)

    # -- style ----------------------------------------------------------
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=CARD,
                        font=("TkDefaultFont", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=CARD, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG,
                        font=("TkDefaultFont", 16, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=("TkDefaultFont", 9))
        style.configure("Section.TLabel", background=BG, foreground=FG,
                        font=("TkDefaultFont", 11, "bold"))
        style.configure("CardSection.TLabel", background=CARD, foreground=FG,
                        font=("TkDefaultFont", 11, "bold"))
        style.configure("TButton", background=CARD2, foreground=FG,
                        borderwidth=0, relief="flat", padding=(12, 8))
        style.map("TButton",
                  background=[("active", "#4A4B5E"), ("disabled", "#2a2b36")],
                  foreground=[("disabled", "#777777")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white",
                        borderwidth=0, padding=(16, 9),
                        font=("TkDefaultFont", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DARK), ("disabled", "#5a5a5a")],
                  foreground=[("disabled", "#cccccc")])
        style.configure("Success.TButton", background="#2E7D32", foreground="white",
                        borderwidth=0, padding=(16, 9),
                        font=("TkDefaultFont", 10, "bold"))
        style.map("Success.TButton", background=[("active", "#256728")])
        style.configure("Treeview", background="#262732", fieldbackground="#262732",
                        foreground=FG, borderwidth=0, rowheight=28)
        style.configure("Treeview.Heading", background=CARD2, foreground=FG,
                        relief="flat", font=("TkDefaultFont", 9, "bold"))
        style.map("Treeview", background=[("selected", "#4A4B5E")],
                  foreground=[("selected", "white")])
        style.configure("Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=CARD2, borderwidth=0, thickness=5)
        # summary chips
        style.configure("Chip.TLabel", background=CARD2, foreground=FG,
                        font=("TkDefaultFont", 9, "bold"), padding=(10, 4))

    # -- layout ---------------------------------------------------------
    def _build_widgets(self) -> None:
        # Header
        header = ttk.Frame(self, padding=(20, 16, 20, 8))
        header.pack(fill="x")
        title_box = ttk.Frame(header)
        title_box.pack(side="left")
        ttk.Label(title_box, text="◉ " + APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="Two-way folder sync for your external SSD — preview first, nothing writes without Confirm.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        head_right = ttk.Frame(header)
        head_right.pack(side="right", anchor="e")
        self.ssd_status = ttk.Label(head_right, text="● SSD: —", style="Muted.TLabel",
                                    font=("TkDefaultFont", 10, "bold"))
        self.ssd_status.pack(anchor="e", pady=(0, 8))
        btn_row = ttk.Frame(head_right)
        btn_row.pack(anchor="e")
        self.check_btn = ttk.Button(btn_row, text="🔍  Check for Changes",
                                    command=self.on_check, style="Accent.TButton")
        self.check_btn.pack(side="left", padx=(0, 8))
        self.confirm_btn = ttk.Button(btn_row, text="✅  Confirm & Sync",
                                      command=self.on_confirm, state="disabled")
        self.confirm_btn.pack(side="left")

        # Main split
        main = ttk.Frame(self, padding=(20, 8, 20, 6))
        main.pack(fill="both", expand=True)
        left = ttk.Frame(main, style="Card.TFrame", padding=14)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.configure(width=350)
        left.pack_propagate(False)
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="📁  Folder Pairs", style="CardSection.TLabel").pack(anchor="w")
        ttk.Label(left, text="Each pair syncs both ways.", style="CardMuted.TLabel",
                  font=("TkDefaultFont", 9)).pack(anchor="w", pady=(0, 8))

        self.pairs_tree = ttk.Treeview(left, columns=("status",), show="tree headings",
                                       height=7, selectmode="browse")
        self.pairs_tree.heading("#0", text="Pair")
        self.pairs_tree.heading("status", text="SSD")
        self.pairs_tree.column("#0", width=200, anchor="w")
        self.pairs_tree.column("status", width=90, anchor="center")
        self.pairs_tree.pack(fill="x")
        self.pairs_tree.bind("<<TreeviewSelect>>", lambda _e: self._show_pair_detail())
        self.pairs_tree.bind("<Button-3>", self._on_pair_right_click)
        self.pairs_tree.bind("<Delete>", lambda _e: self.on_remove_pair())

        pair_btns = ttk.Frame(left, style="Card.TFrame")
        pair_btns.pack(fill="x", pady=(10, 0))
        ttk.Button(pair_btns, text="+ Add", command=self.on_add_pair).pack(
            side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(pair_btns, text="✎ Edit", command=self.on_edit_pair).pack(
            side="left", fill="x", expand=True)

        # Unmissable full-width Remove for the selected pair
        self.remove_btn = ttk.Button(left, text="🗑  Remove selected pair",
                                     command=self.on_remove_pair)
        self.remove_btn.pack(fill="x", pady=(6, 0))

        # Selected-pair detail: two separate boxes (no more guessing)
        detail_wrap = ttk.Frame(left, style="Card.TFrame")
        detail_wrap.pack(fill="both", expand=True, pady=(12, 0))

        self.detail_laptop_box = tk.Frame(detail_wrap, bg=LAPTOP_BG,
                                          highlightbackground=LAPTOP_BLUE,
                                          highlightthickness=1)
        self.detail_laptop_box.pack(fill="x", pady=(0, 8))
        dl = tk.Frame(self.detail_laptop_box, bg=LAPTOP_BG)
        dl.pack(fill="x", padx=10, pady=8)
        tk.Label(dl, text="💻  LAPTOP", bg=LAPTOP_BG, fg=LAPTOP_BLUE,
                 font=("TkDefaultFont", 8, "bold")).pack(anchor="w")
        self.detail_laptop_path = tk.Label(dl, text="—", bg=LAPTOP_BG, fg=FG,
                                           font=("TkDefaultFont", 8),
                                           wraplength=290, justify="left", anchor="w")
        self.detail_laptop_path.pack(anchor="w", fill="x")

        self.detail_ssd_box = tk.Frame(detail_wrap, bg=SSD_BG,
                                       highlightbackground=SSD_ORANGE,
                                       highlightthickness=1)
        self.detail_ssd_box.pack(fill="x")
        ds = tk.Frame(self.detail_ssd_box, bg=SSD_BG)
        ds.pack(fill="x", padx=10, pady=8)
        tk.Label(ds, text="💾  SSD", bg=SSD_BG, fg=SSD_ORANGE,
                 font=("TkDefaultFont", 8, "bold")).pack(anchor="w")
        self.detail_ssd_path = tk.Label(ds, text="—", bg=SSD_BG, fg=FG,
                                        font=("TkDefaultFont", 8),
                                        wraplength=290, justify="left", anchor="w")
        self.detail_ssd_path.pack(anchor="w", fill="x")

        # Preview panel
        prev_head = ttk.Frame(right)
        prev_head.pack(fill="x")
        ttk.Label(prev_head, text="✨ Preview of changes",
                  style="Section.TLabel").pack(side="left")
        self.preview_count = ttk.Label(prev_head, text="No scan yet", style="Muted.TLabel")
        self.preview_count.pack(side="right")

        # summary chips
        chips = ttk.Frame(right)
        chips.pack(fill="x", pady=(8, 0))
        self.chip_new = ttk.Label(chips, text="🆕 New: 0", style="Chip.TLabel")
        self.chip_new.pack(side="left", padx=(0, 6))
        self.chip_upd = ttk.Label(chips, text="🔄 Updates: 0", style="Chip.TLabel")
        self.chip_upd.pack(side="left", padx=(0, 6))
        self.chip_conf = ttk.Label(chips, text="⚔ Conflicts: 0", style="Chip.TLabel")
        self.chip_conf.pack(side="left", padx=(0, 6))
        self.chip_orph = ttk.Label(chips, text="📦 Orphaned: 0", style="Chip.TLabel")
        self.chip_orph.pack(side="left")

        # per-file skip toolbar: exclude files you DON'T want from this sync
        skipbar = ttk.Frame(right)
        skipbar.pack(fill="x", pady=(8, 0))
        ttk.Button(skipbar, text="⏭  Skip selected", command=self.on_skip_selected).pack(side="left", padx=(0, 6))
        ttk.Button(skipbar, text="↩  Include all", command=self.on_include_all).pack(side="left")
        ttk.Label(skipbar, text="Tip: select file(s) → Skip. Double-click toggles. Del key skips too.",
                  style="Muted.TLabel", font=("TkDefaultFont", 8)).pack(side="right")

        tree_frame = ttk.Frame(right, style="Card.TFrame", padding=6)
        tree_frame.pack(fill="both", expand=True, pady=(8, 8))
        cols = ("action", "file", "detail", "pair")
        self.preview = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                    height=11, selectmode="extended")
        self.preview.heading("action", text="Action")
        self.preview.heading("file", text="File")
        self.preview.heading("detail", text="Detail")
        self.preview.heading("pair", text="Pair")
        self.preview.column("action", width=180, anchor="w")
        self.preview.column("file", width=250, anchor="w")
        self.preview.column("detail", width=300, anchor="w")
        self.preview.column("pair", width=140, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.preview.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.preview.xview)
        self.preview.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.preview.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.preview.bind("<Double-1>", lambda _e: self.on_toggle_skip_event())
        self.preview.bind("<Delete>", lambda _e: self.on_skip_selected())

        for tag, color in [("NEW", "#7BD88F"), ("UPDATE", "#6CB8EE"),
                           ("CONFLICT", "#F0A35E"), ("ORPHANED", "#9A9A9A"),
                           ("ERROR", "#F08080"), ("INFO", MUTED),
                           ("SKIPPED", "#5A5A5A")]:
            self.preview.tag_configure(tag, foreground=color)

        # Log panel
        log_head = ttk.Frame(right)
        log_head.pack(fill="x")
        ttk.Label(log_head, text="🧾 Log", style="Section.TLabel").pack(side="left")
        ttk.Button(log_head, text="Open log file", command=self.on_open_log).pack(side="right", padx=(6, 0))
        ttk.Button(log_head, text="Clear view", command=self.on_clear_log_view).pack(side="right")
        log_frame = ttk.Frame(right, style="Card.TFrame", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(log_frame, height=6, wrap="word", bg="#262732", fg="#E8E8E8",
                                insertbackground=FG, relief="flat", bd=0,
                                padx=8, pady=8,
                                font=("TkMonoFont", 9) if "TkMonoFont" in self.tk.call("font", "names") else ("Monospace", 9))
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        # Footer
        footer = ttk.Frame(self, padding=(20, 6, 20, 14))
        footer.pack(fill="x")
        self.progress = ttk.Progressbar(footer, mode="indeterminate",
                                        style="Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(0, 6))
        self.status = ttk.Label(footer, text="Idle.", style="Muted.TLabel")
        self.status.pack(side="left")
        self.backup_var = tk.BooleanVar(value=bool(self.cfg.get("keep_backups", True)))
        self.backup_check = tk.Checkbutton(
            footer, text="Keep .conflict-bak backups",
            variable=self.backup_var, command=self.on_toggle_backups,
            bg=BG, fg=MUTED, selectcolor=CARD2, activebackground=BG,
            activeforeground=FG, relief="flat", font=("TkDefaultFont", 8))
        self.backup_check.pack(side="right", padx=(12, 0))
        ttk.Label(footer, text="Deletions are never propagated.",
                  style="Muted.TLabel").pack(side="right")

    def on_toggle_backups(self) -> None:
        self.cfg["keep_backups"] = bool(self.backup_var.get())
        save_config(self.cfg)
        state = "ON — older versions are kept as .conflict-bak" if self.cfg["keep_backups"] \
            else "OFF — newer file simply overwrites, no backup copies"
        log_to_file(f"CONFIG keep_backups = {self.cfg['keep_backups']}")
        self._append_log(f"Conflict backups {state}.")
        # detail lines in the current preview were computed with the old setting
        if self.pending:
            self._clear_preview()
            self._set_status("Setting changed — click Check for Changes to re-scan.")

    # -- pairs ----------------------------------------------------------
    def _refresh_pairs(self) -> None:
        self.pair_by_id = {p["id"]: p for p in self.cfg["pairs"]}
        for i in self.pairs_tree.get_children():
            self.pairs_tree.delete(i)
        for p in self.cfg["pairs"]:
            avail = Path(p["ssd"]).is_dir()
            dot = "🟢 available" if avail else "🔴 missing"
            self.pairs_tree.insert("", "end", iid=p["id"], text=p.get("name", "?"),
                                   values=(dot,))
        self._show_pair_detail()

    def _selected_pair(self) -> dict | None:
        sel = self.pairs_tree.selection()
        if not sel:
            return None
        return self.pair_by_id.get(sel[0])

    def _show_pair_detail(self) -> None:
        p = self._selected_pair()
        if not p:
            self.detail_laptop_path.configure(text="Select a pair to see its folders.")
            self.detail_ssd_path.configure(text="—")
            return
        self.detail_laptop_path.configure(text=p["laptop"])
        self.detail_ssd_path.configure(text=p["ssd"])

    def on_add_pair(self) -> None:
        dlg = AddPairDialog(self, title="Add Folder Pair")
        if not dlg.result:
            return
        # duplicate check
        for p in self.cfg["pairs"]:
            if p["laptop"] == dlg.result["laptop"] and p["ssd"] == dlg.result["ssd"]:
                messagebox.showinfo(APP_NAME, "That exact pair already exists.")
                return
        pair = {"id": uuid.uuid4().hex[:12], **dlg.result}
        self.cfg["pairs"].append(pair)
        save_config(self.cfg)
        log_to_file(f"CONFIG add pair: {pair['name']} ({pair['laptop']} <-> {pair['ssd']})")
        self._refresh_pairs()
        try:
            self.pairs_tree.selection_set(pair["id"])
        except tk.TclError:
            pass
        self._append_log(f"Added pair: {pair['name']}")
        self._append_log(f"   💻 {pair['laptop']}")
        self._append_log(f"   💾 {pair['ssd']}")
        self._update_ssd_status()
        self._set_status(f"Added “{pair['name']}”. Click Check for Changes to preview.")

    def on_edit_pair(self) -> None:
        p = self._selected_pair()
        if not p:
            messagebox.showinfo(APP_NAME, "Select a pair first.")
            return
        dlg = AddPairDialog(self, initial_laptop=p["laptop"], initial_ssd=p["ssd"],
                            initial_name=p.get("name", ""), title="Edit Folder Pair")
        if not dlg.result:
            return
        old = dict(p)
        p.update(dlg.result)
        # if paths changed, drop old manifest so history restarts cleanly
        if old["laptop"] != p["laptop"] or old["ssd"] != p["ssd"]:
            try:
                manifest_path(p["id"]).unlink(missing_ok=True)  # type: ignore[arg-type]
            except OSError:
                pass
            self.manifests.pop(p["id"], None)
            self._clear_preview()
        save_config(self.cfg)
        log_to_file(f"CONFIG edit pair: {p['name']} ({p['laptop']} <-> {p['ssd']})")
        self._refresh_pairs()
        try:
            self.pairs_tree.selection_set(p["id"])
        except tk.TclError:
            pass
        self._append_log(f"Updated pair: {p['name']}")
        self._update_ssd_status()

    def _on_pair_right_click(self, event) -> None:
        iid = self.pairs_tree.identify_row(event.y)
        if iid and self.pairs_tree.exists(iid):
            self.pairs_tree.selection_set(iid)
        p = self._selected_pair()
        menu = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG, activebackground=ACCENT,
                       activeforeground="white")
        menu.add_command(label="✎  Edit pair…", command=self.on_edit_pair,
                         state="normal" if p else "disabled")
        menu.add_command(label="🗑  Remove pair…", command=self.on_remove_pair,
                         state="normal" if p else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def on_remove_pair(self) -> None:
        p = self._selected_pair()
        if not p:
            # be helpful: if there's exactly one pair, pick it
            if len(self.cfg["pairs"]) == 1:
                p = self.cfg["pairs"][0]
                try:
                    self.pairs_tree.selection_set(p["id"])
                except tk.TclError:
                    pass
            else:
                messagebox.showinfo(APP_NAME, "Select a pair on the left first,\nthen click “Remove selected pair”.")
                return
        if not messagebox.askyesno(APP_NAME, f"Remove sync pair “{p['name']}”?\n\n💻 {p['laptop']}\n💾 {p['ssd']}\n\nYour files are NOT touched — only this sync entry and its history are removed."):
            return
        self.cfg["pairs"] = [x for x in self.cfg["pairs"] if x["id"] != p["id"]]
        save_config(self.cfg)
        try:
            manifest_path(p["id"]).unlink(missing_ok=True)  # type: ignore[arg-type]
        except OSError:
            pass
        log_to_file(f"CONFIG remove pair: {p['name']}")
        self._refresh_pairs()
        self._append_log(f"Removed pair: {p['name']}")
        self._clear_preview()
        self._update_ssd_status()

    # -- scan / sync ----------------------------------------------------
    def _set_status(self, msg: str) -> None:
        self.status.configure(text=msg)

    def _set_working(self, working: bool) -> None:
        self._working = working
        if working:
            self.progress.start(12)
            self.check_btn.configure(state="disabled")
        else:
            self.progress.stop()
            self.check_btn.configure(state="normal")

    def on_check(self) -> None:
        if self._working:
            return
        if not self.cfg["pairs"]:
            messagebox.showinfo(APP_NAME, "Add a folder pair first.")
            return
        self._set_working(True)
        self._set_status("Scanning all pairs…")
        self._append_log("—— Scanning for changes… ——")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            pending: list[tuple[dict, Action]] = []
            noops: dict[str, dict] = {}
            keep = bool(self.cfg.get("keep_backups", True))
            for pair in self.cfg["pairs"]:
                manifest = load_manifest(pair["id"])
                self.manifests[pair["id"]] = manifest
                actions, noop = plan_pair(pair, manifest, keep_backups=keep)
                noops[pair["id"]] = noop
                for a in actions:
                    pending.append((pair, a))
            self.after(0, lambda: self._scan_done(pending, noops))
        except Exception:
            err = traceback.format_exc()
            self.after(0, lambda: self._scan_failed(err))

    def _scan_done(self, pending: list[tuple[dict, Action]], noops: dict[str, dict]) -> None:
        self._set_working(False)
        self.pending = pending
        self.pending_noops = noops
        self.skipped_keys.clear()
        self._render_preview()
        self._refresh_confirm_state(status_msg=True)

    def _refresh_confirm_state(self, status_msg: bool = False) -> None:
        active = self._active_pending()
        writes = [a for _, a in active if a.kind in KINDS_WRITE]
        orphans = [a for _, a in active if a.kind == "ORPHANED"]
        errors = [a for _, a in active if a.kind == "ERROR"]
        n_skip = len(self.skipped_keys)
        if status_msg:
            if errors and not writes:
                self._set_status("Scan done — a path is missing. See preview.")
            elif not writes and not orphans:
                extra = f" ({n_skip} skipped)" if n_skip else ""
                self._set_status(f"Scan done — everything is up to date. ✅{extra}")
            else:
                extra = f", {n_skip} skipped" if n_skip else ""
                self._set_status(f"Scan done — {len(writes)} change(s), {len(orphans)} orphaned{extra}. Review, then Confirm & Sync.")
            self._append_log(f"Scan complete: {len(writes)} to copy, {len(orphans)} orphaned, {len(errors)} errors."
                             + (f" ({n_skip} skipped)" if n_skip else ""))
        if writes:
            self.confirm_btn.configure(state="normal", style="Success.TButton")
        else:
            self.confirm_btn.configure(state="disabled", style="TButton")

    def _scan_failed(self, err: str) -> None:
        self._set_working(False)
        self._set_status("Scan failed — see log.")
        self._append_log("SCAN FAILED:\n" + err)
        log_to_file("SCAN FAILED: " + err.splitlines()[-1] if err else "SCAN FAILED")

    def _update_chips(self) -> None:
        active = self._active_pending()
        n_new = sum(1 for _, a in active if a.kind.startswith("NEW"))
        n_upd = sum(1 for _, a in active if a.kind.startswith("UPDATE"))
        n_conf = sum(1 for _, a in active if a.kind.startswith("CONFLICT"))
        n_orph = sum(1 for _, a in active if a.kind == "ORPHANED")
        self.chip_new.configure(text=f"🆕 New: {n_new}")
        self.chip_upd.configure(text=f"🔄 Updates: {n_upd}")
        self.chip_conf.configure(text=f"⚔ Conflicts: {n_conf}")
        self.chip_orph.configure(text=f"📦 Orphaned: {n_orph}")

    @staticmethod
    def _action_key(pair: dict, a: Action) -> tuple[str, str, str]:
        return (pair.get("id", ""), a.relpath, a.kind)

    def _active_pending(self) -> list[tuple[dict, Action]]:
        return [(p, a) for p, a in self.pending
                if self._action_key(p, a) not in self.skipped_keys]

    def on_skip_selected(self) -> None:
        sel = self.preview.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select file(s) in the preview first.")
            return
        n = 0
        for iid in sel:
            row = self._row_map.get(iid)
            if not row:
                continue
            pair, a = row
            key = self._action_key(pair, a)
            if key not in self.skipped_keys and a.kind in KINDS_WRITE:
                self.skipped_keys.add(key)
                n += 1
        if n == 0:
            messagebox.showinfo(APP_NAME, "Only copy actions (New / Update / Conflict) can be skipped.\nOrphaned rows are already never copied.")
            return
        self._append_log(f"Skipped {n} file(s) for this sync — they stay untouched.")
        self._render_preview()
        self._refresh_confirm_state()

    def on_include_all(self) -> None:
        if not self.skipped_keys:
            return
        n = len(self.skipped_keys)
        self.skipped_keys.clear()
        self._append_log(f"Included all back ({n} file(s)) — full preview will sync on Confirm.")
        self._render_preview()
        self._refresh_confirm_state()

    def on_toggle_skip_event(self) -> None:
        sel = self.preview.selection()
        if not sel:
            return
        for iid in sel:
            row = self._row_map.get(iid)
            if not row:
                continue
            pair, a = row
            if a.kind not in KINDS_WRITE:
                continue
            key = self._action_key(pair, a)
            if key in self.skipped_keys:
                self.skipped_keys.discard(key)
            else:
                self.skipped_keys.add(key)
        self._render_preview()
        self._refresh_confirm_state()

    def _render_preview(self) -> None:
        for i in self.preview.get_children():
            self.preview.delete(i)
        self._row_map.clear()
        order = {"CONFLICT_TO_SSD": 0, "CONFLICT_TO_LAPTOP": 0, "ERROR": 1,
                 "UPDATE_TO_SSD": 2, "UPDATE_TO_LAPTOP": 2,
                 "NEW_TO_SSD": 3, "NEW_TO_LAPTOP": 3, "ORPHANED": 4, "INFO": 5}
        rows = sorted(self.pending, key=lambda pa: (order.get(pa[1].kind, 9), pa[1].relpath))
        for idx, (pair, a) in enumerate(rows):
            key = self._action_key(pair, a)
            skipped = key in self.skipped_keys
            if skipped:
                tag = "SKIPPED"
                label = "⏭ SKIPPED"
                detail = (a.detail + " — skipped, won't copy") if a.detail else "skipped, won't copy"
            else:
                tag = ("CONFLICT" if a.kind.startswith("CONFLICT")
                       else "NEW" if a.kind.startswith("NEW")
                       else "UPDATE" if a.kind.startswith("UPDATE")
                       else a.kind)
                label, detail = a.label, a.detail
            iid = str(idx)
            self.preview.insert("", "end", iid=iid,
                                values=(label, a.relpath, detail, pair.get("name", "")), tags=(tag,))
            self._row_map[iid] = (pair, a)
        self._update_chips()
        active = self._active_pending()
        n_write = sum(1 for _, a in active if a.kind in KINDS_WRITE)
        n_orph = sum(1 for _, a in active if a.kind == "ORPHANED")
        n_err = sum(1 for _, a in active if a.kind == "ERROR")
        n_skip = len(self.skipped_keys)
        if not self.pending:
            self.preview_count.configure(text="Up to date — nothing to do ✅")
        else:
            skip_txt = f"  •  {n_skip} skipped" if n_skip else ""
            self.preview_count.configure(
                text=f"{n_write} to copy  •  {n_orph} orphaned  •  {n_err} errors  •  {len(self.pending)} rows{skip_txt}")

    def _clear_preview(self) -> None:
        self.pending = []
        self.skipped_keys.clear()
        self._row_map.clear()
        for i in self.preview.get_children():
            self.preview.delete(i)
        self.preview_count.configure(text="No scan yet")
        self._update_chips()
        self.confirm_btn.configure(state="disabled", style="TButton")

    def on_confirm(self) -> None:
        active = self._active_pending()
        writes = [(p, a) for p, a in active if a.kind in KINDS_WRITE]
        if not writes:
            if self.skipped_keys:
                messagebox.showinfo(APP_NAME, "Everything is skipped — click “Include all” to sync.")
            else:
                messagebox.showinfo(APP_NAME, "Nothing to sync.")
            return
        n_conflict = sum(1 for _, a in writes if a.kind.startswith("CONFLICT"))
        n_skip = len(self.skipped_keys)
        keep = bool(self.cfg.get("keep_backups", True))
        msg = f"Copy {len(writes)} file(s)?"
        if n_skip:
            msg += f"\n({n_skip} skipped file(s) will stay untouched.)"
        if n_conflict:
            if keep:
                msg += f"\n\nIncluding {n_conflict} conflict(s) — the older version will be kept as .conflict-bak-TIMESTAMP first."
            else:
                msg += f"\n\nIncluding {n_conflict} conflict(s) — newer overwrites older with NO backup (you turned backups off)."
        msg += "\n\nDeletions are NEVER propagated."
        if not messagebox.askyesno(APP_NAME, msg):
            return
        self._set_working(True)
        self.check_btn.configure(state="disabled")
        self.confirm_btn.configure(state="disabled", style="TButton")
        self._set_status("Syncing…")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self) -> None:
        try:
            # group ACTIVE (non-skipped) actions per pair
            active = self._active_pending()
            by_pair: dict[str, tuple[dict, list[Action]]] = {}
            for pair, a in active:
                by_pair.setdefault(pair["id"], (pair, []))[1].append(a)
            total_copied = 0
            total_errors: list[str] = []
            for pid, (pair, acts) in by_pair.items():
                manifest = self.manifests.get(pid, load_manifest(pid))
                new_manifest, errs = execute_actions(
                    pair, acts, manifest, self.pending_noops.get(pid, {}),
                    keep_backups=bool(self.cfg.get("keep_backups", True)))
                # persist noop-only manifests too (identical first-sync files)
                if not [a for a in acts if a.kind in KINDS_WRITE] and self.pending_noops.get(pid):
                    new_manifest = {**manifest, **self.pending_noops[pid]}
                save_manifest(pid, new_manifest)
                self.manifests[pid] = new_manifest
                total_copied += sum(1 for a in acts if a.kind in KINDS_WRITE and not any(a.relpath in e for e in errs))
                total_errors.extend([f"{pair.get('name')}: {e}" for e in errs])
            # manifests for pairs with zero actions still need noop updates
            for pid, noop in self.pending_noops.items():
                if pid not in by_pair and noop:
                    m = self.manifests.get(pid, load_manifest(pid))
                    m.update(noop)
                    save_manifest(pid, m)
            summary = (total_copied, list(total_errors))
            self.after(0, lambda: self._sync_done(summary[0], summary[1]))
        except Exception:
            err = traceback.format_exc()
            self.after(0, lambda: self._sync_failed(err))

    def _sync_done(self, copied: int, errors: list[str]) -> None:
        self._set_working(False)
        self.check_btn.configure(state="normal")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if errors:
            self._set_status(f"Sync finished with {len(errors)} error(s) — {copied} copied.")
            self._append_log(f"[{ts}] Sync done: {copied} file(s) copied, {len(errors)} error(s):")
            for e in errors:
                self._append_log(f"   • {e}")
            log_to_file(f"SYNC DONE: {copied} copied, {len(errors)} errors")
            messagebox.showwarning(APP_NAME, f"Sync finished with {len(errors)} error(s).\n{copied} file(s) copied.\nSee log for details.")
        else:
            self._set_status(f"Sync complete — {copied} file(s) copied. ✅")
            self._append_log(f"[{ts}] Sync complete: {copied} file(s) copied.")
            log_to_file(f"SYNC DONE: {copied} copied, no errors")
            messagebox.showinfo(APP_NAME, f"Sync complete ✅\n{copied} file(s) copied.")
        # re-scan to confirm clean state
        self.on_check()

    def _sync_failed(self, err: str) -> None:
        self._set_working(False)
        self.check_btn.configure(state="normal")
        self._set_status("Sync failed — see log.")
        self._append_log("SYNC FAILED:\n" + err)

    # -- log ------------------------------------------------------------
    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_clear_log_view(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def on_open_log(self) -> None:
        ensure_dirs()
        try:
            import subprocess
            subprocess.Popen(["xdg-open", str(LOG_FILE)])
        except OSError:
            messagebox.showinfo(APP_NAME, f"Log file:\n{LOG_FILE}")

    # -- SSD detection --------------------------------------------------
    def _ssd_states(self) -> dict[str, bool]:
        return {p["id"]: Path(p["ssd"]).is_dir() for p in self.cfg["pairs"]}

    def _update_ssd_status(self, initial: bool = False) -> None:
        states = self._ssd_states()
        self._ssd_prev = states
        self._paint_ssd_status(states)

    def _paint_ssd_status(self, states: dict[str, bool]) -> None:
        if not states:
            self.ssd_status.configure(text="● SSD: no pairs", foreground=MUTED)
            return
        ok = sum(1 for v in states.values() if v)
        if ok == len(states):
            self.ssd_status.configure(text=f"● SSD: connected ({ok}/{len(states)})", foreground=GREEN)
        elif ok == 0:
            self.ssd_status.configure(text="● SSD: not detected", foreground=RED)
        else:
            self.ssd_status.configure(text=f"● SSD: partial ({ok}/{len(states)})", foreground=ORANGE)
        self._refresh_pairs_badges(states)

    def _refresh_pairs_badges(self, states: dict[str, bool]) -> None:
        for pid, avail in states.items():
            if self.pairs_tree.exists(pid):
                self.pairs_tree.set(pid, "status", "🟢 available" if avail else "🔴 missing")

    def _poll_ssd(self) -> None:
        try:
            states = self._ssd_states()
            self._paint_ssd_status(states)
            # newly-appeared SSD path? prompt once per transition.
            for pid, now in states.items():
                was = self._ssd_prev.get(pid, now)
                if now and not was and not self._working:
                    pair = self.pair_by_id.get(pid, {})
                    name = pair.get("name", "SSD")
                    log_to_file(f"SSD detected for pair: {name}")
                    self._append_log(f"SSD detected for “{name}” — scan now?")
                    if messagebox.askyesno(APP_NAME, f"SSD detected — Sync now?\n\n“{name}” is available again.\nScan for changes?"):
                        self.on_check()
                        break
            self._ssd_prev = states
        finally:
            self.after(SSD_POLL_MS, self._poll_ssd)


def main() -> None:
    ensure_dirs()
    app = SyncApp()
    app.mainloop()


if __name__ == "__main__":
    main()
