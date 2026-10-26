"""Graphical user interface (tkinter)."""
from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, legacy
from .config import DEFAULTS_FILE, Config, Profiles
from .context import Cancelled, Declined, RunContext
from .deps import check
from .i18n import catalog, language, t
from .jobs import (DupParams, JobResult, SortParams, SyncParams, default_dup_folder, params_from_dict,
                   params_to_dict, run_job, summary_items)
from .journal import is_undone, list_journals, read_entries, undo
from .lightroom import affected_catalogs
from .metadata import Meta, assign_names
from .patterns import PLACEHOLDERS, PatternError, PatternRenderer, canonical, localize, validate
from .scan import FileEntry

# Choice lists for settings (key -> allowed values); shown as t("choice.<value>")
CHOICES = {
    "compare.method": ["hash", "name_size", "name_size_date"],
    "compare.hash_algorithm": ["blake2b", "sha256", "sha1", "md5"],
    "actions.collision": ["suffix", "skip", "overwrite"],
    "sync.missing_placement": ["structure", "pattern"],
    "sync.changed_sidecar": ["newer_replaces", "report_only"],
    "duplicates.keep": ["oldest", "newest", "shortest_path", "longest_path", "first"],
    "duplicates.action": ["report", "move", "trash"],
    "ui.language": ["auto", "en", "de"],
    "ui.theme": ["system", "light", "dark", "sand", "sage", "night", "graphite"],
}
# Settings holding patterns: shown with placeholder names in the app language, stored in English
PATTERN_SETTINGS = {"patterns.presets", "patterns.default", "patterns.filename"}
STATE_FILE = "last_inputs.json"

# Colour themes. Every theme keeps the safety meaning of the accents
# (blue = dry run, amber like a darkroom safelight = real run); "on_*" = text on an accent.
# tests.ThemeTests checks that all themes have the same keys and readable contrast.
THEMES = {
    "light": {
        "paper": "#F5F6F4", "side": "#E8EAE7", "line": "#D3D7D4", "field": "#FFFFFF",
        "text": "#23272B", "muted": "#5E666F",
        "dry": "#2D6784", "dry_dark": "#24546C", "dry_tint": "#E2ECF1", "on_dry": "#FFFFFF",
        "real": "#E39A2D", "real_dark": "#CC8620", "real_tint": "#FBEBD2", "on_real": "#23272B",
        "error": "#B3261E", "thumb": "#7A828A",
    },
    "dark": {
        "paper": "#1F2123", "side": "#17191B", "line": "#3A3E42", "field": "#2B2E31",
        "text": "#E8EAEC", "muted": "#A3AAB1",
        "dry": "#5AA3CC", "dry_dark": "#4A92BB", "dry_tint": "#1E313C", "on_dry": "#10171C",
        "real": "#E39A2D", "real_dark": "#CC8620", "real_tint": "#3A2D18", "on_real": "#1F2123",
        "error": "#F28B82", "thumb": "#6E767E",
    },
    "sand": {
        "paper": "#F3EEE4", "side": "#E7DFD1", "line": "#D3C8B4", "field": "#FFFDF8",
        "text": "#2B2620", "muted": "#655B4E",
        "dry": "#2D6784", "dry_dark": "#24546C", "dry_tint": "#E0E8EA", "on_dry": "#FFFFFF",
        "real": "#E39A2D", "real_dark": "#CC8620", "real_tint": "#F8E4C4", "on_real": "#2B2620",
        "error": "#A8241C", "thumb": "#857A6B",
    },
    "sage": {
        "paper": "#EEF2EE", "side": "#DCE5DD", "line": "#C3D0C6", "field": "#FFFFFF",
        "text": "#1F2A23", "muted": "#55645A",
        "dry": "#2D6784", "dry_dark": "#24546C", "dry_tint": "#DDE9EE", "on_dry": "#FFFFFF",
        "real": "#E39A2D", "real_dark": "#CC8620", "real_tint": "#FAEBD3", "on_real": "#1F2A23",
        "error": "#A8241C", "thumb": "#6E7F73",
    },
    "night": {
        "paper": "#1B2430", "side": "#141B24", "line": "#33404F", "field": "#243041",
        "text": "#E4E9EF", "muted": "#9EACBC",
        "dry": "#5AA3D6", "dry_dark": "#4A91C4", "dry_tint": "#1D3347", "on_dry": "#0F1720",
        "real": "#E39A2D", "real_dark": "#CC8620", "real_tint": "#3A2E1C", "on_real": "#1B2430",
        "error": "#F28B82", "thumb": "#5E6E82",
    },
    "graphite": {
        "paper": "#3A3A3A", "side": "#2E2E2E", "line": "#505050", "field": "#464646",
        "text": "#ECECEC", "muted": "#C2C2C2",
        "dry": "#6FB2DD", "dry_dark": "#5EA1CC", "dry_tint": "#2F4553", "on_dry": "#15202A",
        "real": "#E39A2D", "real_dark": "#CC8620", "real_tint": "#54442C", "on_real": "#2A2A2A",
        "error": "#FFA198", "thumb": "#7A7A7A",
    },
}
DARK_THEMES = {"dark", "night", "graphite"}
P = dict(THEMES["light"])  # the active theme; set by apply_theme() before any widget is built


def system_dark() -> bool:
    """True when macOS currently uses the dark appearance."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True,
                             timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.strip().lower() == "dark"


def apply_theme(name: str) -> str:
    """'system' | one of THEMES. Fills P and returns the theme now in use."""
    if name not in THEMES:
        name = "dark" if system_dark() else "light"
    P.clear()
    P.update(THEMES[name])
    return name


# Sidebar: (job or page key, index in the notebook); None = spacer
NAV = [("sync", 0), ("duplicates", 1), ("sort", 2), None, ("history", 4), ("settings", 3)]
JOBS = ["sync", "duplicates", "sort"]


def choice_label(value: str) -> str:
    return t(f"choice.{value}")


def parse_comments(file: Path) -> dict[str, str]:
    """Comments in defaults.toml as English help texts: {"section.key": "text"}."""
    helps: dict[str, str] = {}
    section, buf = "", []
    for line in file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            buf.append(s.lstrip("# ").rstrip())
        elif m := re.match(r"^\[\[?([^\]]+)\]\]?$", s):
            section = m.group(1)
            if buf:
                helps.setdefault(section, " ".join(buf))
            buf = []
        elif m := re.match(r"^([A-Za-z0-9_]+)\s*=", s):
            if buf:
                helps[f"{section}.{m.group(1)}" if section else m.group(1)] = " ".join(buf)
            buf = []
        elif not s:
            buf = []
    return helps


def open_path(p: Path) -> None:
    r = subprocess.run(["open", str(p)], check=False, capture_output=True, text=True)
    if r.returncode:
        messagebox.showerror(t("gui.open_failed"), f"{p}\n\n{r.stderr.strip()}")


def text_box(parent, **kw) -> tk.Text:
    """tk.Text styled like the other input fields."""
    return tk.Text(parent, bg=P["field"], fg=P["text"], insertbackground=P["text"], relief="flat",
                   highlightthickness=1, highlightbackground=P["line"], highlightcolor=P["dry"],
                   selectbackground=P["dry"], selectforeground=P["on_dry"], padx=8, pady=6, **kw)


class ChoiceVar:
    """Combobox showing readable texts, returning the internal value."""

    def __init__(self, parent, values: list[str], value: str, width=28):
        self.values = values
        self.var = tk.StringVar(value=choice_label(value))
        self.widget = ttk.Combobox(parent, textvariable=self.var, state="readonly", width=width,
                                   values=[choice_label(v) for v in values])

    def get(self) -> str:
        shown = self.var.get()
        return next((v for v in self.values if choice_label(v) == shown), shown)

    def set(self, value: str) -> None:
        self.var.set(choice_label(value))


class ScrollPage:
    """Vertically scrollable area: content goes into .body. The bar only appears when the content
    doesn't fit. App._wheel passes mouse wheel/trackpad scrolling to the area under the pointer."""

    def __init__(self, parent, padding=0):
        self.outer = ttk.Frame(parent)
        self.outer.rowconfigure(0, weight=1)
        self.outer.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.outer, highlightthickness=0, bg=P["paper"], yscrollincrement=1)
        self.canvas.scroll_page = True
        self.bar = ttk.Scrollbar(self.outer, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=padding)
        self.win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self._set_bar)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar.grid(row=0, column=1, sticky="ns")
        self.body.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._sync)

    def _sync(self, _=None):
        self.canvas.itemconfigure(self.win, width=self.canvas.winfo_width())
        self.canvas.configure(scrollregion=(0, 0, self.body.winfo_reqwidth(), self.body.winfo_reqheight()))

    def _set_bar(self, lo, hi):
        self.bar.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.bar.grid_remove()
        else:
            self.bar.grid()


class App:
    def __init__(self, root: tk.Tk, cfg: Config):
        self.root = root
        self.cfg = cfg
        self.profiles = Profiles(cfg)
        self.state_file = cfg.data_dir / STATE_FILE
        self.q: queue.Queue = queue.Queue()
        self.ctx: RunContext | None = None
        self.worker: threading.Thread | None = None
        self.last_result: JobResult | None = None
        self.drive_hints: list = []

        self.theme = apply_theme(cfg.get("ui.theme", "system"))
        root.title(f"MediaTidy {__version__}")
        self._window_appearance(root)
        root.geometry(cfg.get("ui.window", "1100x800"))
        root.minsize(900, 640)
        root.configure(bg=P["paper"])
        size = int(cfg.get("ui.font_size", 13))
        for f in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tk.font.nametofont(f).configure(size=size)
            except (tk.TclError, AttributeError):
                pass
        self._style(size)

        shell = tk.Frame(root, bg=P["paper"])
        shell.pack(fill="both", expand=True)
        self._build_sidebar(shell)
        tk.Frame(shell, bg=P["line"], width=1).pack(side="left", fill="y")
        self.main = ttk.Frame(shell)
        self.main.pack(side="left", fill="both", expand=True)

        self._build_profile_bar()
        self.nb = ttk.Notebook(self.main, style="Pages.TNotebook")
        self._build_sync_tab()
        self._build_dup_tab()
        self._build_sort_tab()
        self._build_settings_tab()
        self._build_history_tab()
        self._build_bottom()
        self.nb.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._page_changed())
        for i, (_, idx) in enumerate(n for n in NAV if n):
            root.bind(f"<Command-Key-{i + 1}>", lambda e, idx=idx: self.nb.select(idx))
        root.bind("<FocusIn>", lambda e: self._refresh_drives())
        root.bind_all("<MouseWheel>", lambda e: self._wheel(e, touchpad=False))
        try:
            root.bind_all("<TouchpadScroll>", lambda e: self._wheel(e, touchpad=True))  # Tk 9 and later
        except tk.TclError:
            pass
        self._load_state()
        self._page_changed()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        hints = check(cfg)
        if hints:
            self.log(t("gui.hints") + "\n" + "\n".join("• " + h for h in hints))

    # --- Look ----------------------------------------------------------------------
    def _window_appearance(self, win: tk.Misc):
        """Light or dark title bar to match the theme (macOS Tk only)."""
        try:
            win.update_idletasks()  # Tk 8.6 cannot set the appearance of a window that does not exist yet
            win.tk.call("::tk::unsupported::MacWindowStyle", "appearance", win,
                        "darkaqua" if self.theme in DARK_THEMES else "aqua")
        except tk.TclError:
            pass

    def _style(self, size: int):
        base = tk.font.nametofont("TkDefaultFont").actual("family")
        head = "Futura" if "Futura" in tk.font.families() else base
        self.fonts = {
            "h1": (head, round(size * 1.85), "bold"), "h2": (head, round(size * 1.25), "bold"),
            "nav": (head, round(size * 1.1)), "bold": (base, size, "bold"),
            "small": (base, size - 1), "mono": ("Menlo", size - 1),
        }
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=P["paper"], foreground=P["text"], bordercolor=P["line"],
                    lightcolor=P["paper"], darkcolor=P["paper"], troughcolor=P["side"],
                    focuscolor=P["dry"], selectbackground=P["dry"], selectforeground=P["on_dry"],
                    fieldbackground=P["field"], insertcolor=P["text"], arrowcolor=P["text"])
        s.configure("Hint.TLabel", foreground=P["muted"], font=self.fonts["small"])
        s.configure("Error.TLabel", foreground=P["error"], font=self.fonts["small"])
        s.configure("H1.TLabel", font=self.fonts["h1"])
        s.configure("H2.TLabel", font=self.fonts["h2"])
        s.configure("TButton", padding=(12, 5), background=P["field"], bordercolor=P["line"],
                    lightcolor=P["field"], darkcolor=P["field"])
        s.map("TButton", background=[("disabled", P["paper"]), ("pressed", P["line"]), ("active", P["side"])],
              foreground=[("disabled", P["muted"])], bordercolor=[("focus", P["dry"])],
              lightcolor=[("pressed", P["line"]), ("active", P["side"])],
              darkcolor=[("pressed", P["line"]), ("active", P["side"])])
        for name, bg, dark, fg in (("Dry", P["dry"], P["dry_dark"], P["on_dry"]),
                                   ("Real", P["real"], P["real_dark"], P["on_real"])):
            s.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=dark, lightcolor=bg,
                        darkcolor=bg, font=self.fonts["bold"], padding=(18, 7))
            s.map(f"{name}.TButton",
                  background=[("disabled", P["line"]), ("pressed", dark), ("active", dark)],
                  lightcolor=[("disabled", P["line"]), ("pressed", dark), ("active", dark)],
                  darkcolor=[("disabled", P["line"]), ("pressed", dark), ("active", dark)],
                  foreground=[("disabled", P["muted"])], bordercolor=[("focus", P["text"])])
        s.configure("TEntry", padding=(6, 4), lightcolor=P["field"], darkcolor=P["field"])
        s.map("TEntry", bordercolor=[("focus", P["dry"])], lightcolor=[("focus", P["dry"])])
        s.configure("TCombobox", padding=(6, 3), background=P["field"], lightcolor=P["field"], darkcolor=P["field"])
        s.map("TCombobox", fieldbackground=[("disabled", P["paper"]), ("readonly", P["field"])],
              foreground=[("disabled", P["muted"])], background=[("active", P["side"])],
              selectbackground=[("readonly", P["field"])], selectforeground=[("readonly", P["text"])],
              bordercolor=[("focus", P["dry"])])
        self.root.option_add("*TCombobox*Listbox.background", P["field"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", P["dry"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", P["on_dry"])
        s.configure("TCheckbutton", indicatorbackground=P["field"], indicatorforeground=P["dry"],
                    upperbordercolor=P["muted"], lowerbordercolor=P["muted"])
        s.map("TCheckbutton", background=[("active", P["paper"])],
              indicatorbackground=[("selected", P["field"])])
        s.configure("Pages.TNotebook", borderwidth=0, tabmargins=0, padding=0)
        s.layout("Pages.TNotebook.Tab", [])  # hide the tabs – navigation is in the sidebar
        # Scroll bar without arrows, strong thumb on a light trough
        s.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Vertical.TScrollbar", troughcolor=P["side"], background=P["thumb"], bordercolor=P["side"],
                    lightcolor=P["thumb"], darkcolor=P["thumb"], gripcount=0, arrowsize=5)  # Tk 9 scales: ~17 px
        s.map("Vertical.TScrollbar", background=[("pressed", P["text"]), ("active", P["muted"])],
              lightcolor=[("pressed", P["text"]), ("active", P["muted"])],
              darkcolor=[("pressed", P["text"]), ("active", P["muted"])])
        s.configure("Run.Horizontal.TProgressbar", background=P["dry"], troughcolor=P["side"],
                    bordercolor=P["side"], lightcolor=P["dry"], darkcolor=P["dry"], thickness=6)
        s.configure("Mode.Toolbutton", padding=(12, 5), font=self.fonts["bold"])

    # --- Sidebar ---------------------------------------------------------------------
    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=P["side"], width=200)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="MediaTidy", font=self.fonts["h2"], bg=P["side"], fg=P["text"]
                 ).pack(anchor="w", padx=22, pady=(26, 0))
        tk.Label(side, text=t("gui.version", version=__version__), font=self.fonts["small"], bg=P["side"],
                 fg=P["muted"]).pack(anchor="w", padx=22, pady=(0, 22))
        self.nav_items: dict[int, tuple] = {}
        for item in NAV:
            if item is None:
                tk.Frame(side, bg=P["line"], height=1).pack(fill="x", padx=22, pady=12)
                continue
            key, idx = item
            row = tk.Frame(side, bg=P["side"])
            row.pack(fill="x")
            mark = tk.Frame(row, bg=P["side"], width=4)
            mark.pack(side="left", fill="y")
            lbl = tk.Label(row, text=t(f"nav.{key}"), font=self.fonts["nav"], bg=P["side"], fg=P["muted"],
                           anchor="w", padx=18, pady=7, takefocus=1, highlightthickness=1,
                           highlightbackground=P["side"], highlightcolor=P["dry"])
            lbl.pack(side="left", fill="x", expand=True)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, idx=idx: self.nb.select(idx))
            for k in ("<Return>", "<space>"):
                lbl.bind(k, lambda e, idx=idx: self.nb.select(idx))
            self.nav_items[idx] = (row, mark, lbl)
        # Wrap instead of clipping: the sidebar has a fixed width and Tk 9 renders text wider
        tk.Label(side, text=t("gui.shortcuts"), font=self.fonts["small"], bg=P["side"], fg=P["muted"],
                 wraplength=int(side.cget("width")) - 2 * 22, justify="left"
                 ).pack(side="bottom", anchor="w", padx=22, pady=18)

    def _page_changed(self):
        cur = self.nb.index(self.nb.select())
        for idx, (row, mark, lbl) in self.nav_items.items():
            on = idx == cur
            bg = P["paper"] if on else P["side"]
            row.configure(bg=bg)
            mark.configure(bg=P["text"] if on else bg)
            lbl.configure(bg=bg, fg=P["text"] if on else P["muted"], highlightbackground=bg)
        if hasattr(self, "run_frame"):  # settings: own footer instead of start/log
            if cur == 3:
                self.run_frame.pack_forget()
            elif not self.run_frame.winfo_manager():
                self.run_frame.pack(side="bottom", fill="x", before=self.nb)
        self._update_mode()

    # --- Building blocks ----------------------------------------------------------------
    def _folder_row(self, parent, row, label, var):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", pady=(9, 4), padx=(0, 16))
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=1, sticky="ew", pady=4)
        cell.columnconfigure(0, weight=1)
        ttk.Entry(cell, textvariable=var, width=10).grid(row=0, column=0, sticky="ew")
        hint = ttk.Label(cell, style="Hint.TLabel")
        hint.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(parent, text=t("gui.choose"), command=lambda: self._choose(var)).grid(
            row=row, column=2, sticky="nw", padx=(8, 0), pady=4)
        self._drive_hint(var, hint)

    def _drive_hint(self, var: tk.StringVar, label: ttk.Label):
        """Shows below paths on /Volumes whether the drive is connected."""
        def update(*_):
            parts = Path(var.get().strip()).parts
            if len(parts) >= 3 and parts[1] == "Volumes":
                if Path("/Volumes", parts[2]).is_dir():
                    label.configure(text=t("gui.drive_connected", drive=parts[2]), style="Hint.TLabel")
                else:
                    label.configure(text=t("gui.drive_not_connected", drive=parts[2]), style="Error.TLabel")
                label.grid()
            else:
                label.grid_remove()
        var.trace_add("write", update)
        self.drive_hints.append(update)
        update()

    def _refresh_drives(self):
        for update in self.drive_hints:
            update()

    def _choose(self, var: tk.StringVar):
        p = filedialog.askdirectory(initialdir=var.get() or str(Path.home()), mustexist=False)
        if p:
            var.set(p)

    def _label(self, parent, row, text, top=4):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=(top, 4), padx=(0, 16))

    def _pattern_row(self, parent, row, label, var):
        self._label(parent, row, label)
        cb = ttk.Combobox(parent, textvariable=var, values=[localize(p) for p in self.cfg.get("patterns.presets")])
        cb.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text=t("gui.placeholders"), command=self._show_placeholders).grid(
            row=row, column=2, sticky="w", padx=(8, 0))
        return cb

    def _show_placeholders(self):
        messagebox.showinfo(t("gui.placeholders"), t("gui.placeholders_intro") + "\n\n" +
                            "\n".join(f"{{{t(f'ph.{p}')}}}  –  {t(f'ph_help.{p}')}" for p in PLACEHOLDERS))

    def _header(self, parent, title, desc="", **grid):
        ttk.Label(parent, text=title, style="H1.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", **grid)
        if desc:
            ttk.Label(parent, text=desc, style="Hint.TLabel", wraplength=620).grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(2, 14), **grid)

    def _tab(self, title, desc):
        page = ScrollPage(self.nb, padding=(32, 16, 32, 16))
        f = page.body
        f.columnconfigure(1, weight=1)
        self.nb.add(page.outer, text=title)
        self._header(f, title, desc)
        return f

    def _wheel(self, e, touchpad: bool):
        """Mouse wheel/trackpad: scrolls the ScrollPage area under the pointer.
        Text and list fields scroll themselves (own Tk binding)."""
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):  # e.g. the drop-down list of a combobox
            return
        while w is not None and not getattr(w, "scroll_page", False):
            if isinstance(w, (tk.Text, tk.Listbox)):
                return
            w = w.master
        if w is None:
            return
        if touchpad:
            dy = int(self.root.tk.splitlist(self.root.tk.call("tk::PreciseScrollDeltas", e.delta))[1])
            w.yview_scroll(-dy, "units")  # yscrollincrement=1 -> pixels
        else:  # Tk 9 reports 120 per notch, older versions small values
            notches = e.delta / 120 if abs(e.delta) >= 120 else e.delta
            w.yview_scroll(int(-notches * 36) or (-1 if e.delta > 0 else 1), "units")

    # --- Profiles -------------------------------------------------------------------------
    def _build_profile_bar(self):
        bar = ttk.Frame(self.main, padding=(32, 14, 32, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text=t("gui.delete"), command=self._profile_delete).pack(side="right")
        ttk.Button(bar, text=t("gui.save"), command=self._profile_save).pack(side="right", padx=4)
        ttk.Button(bar, text=t("gui.load"), command=self._profile_load).pack(side="right")
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(bar, textvariable=self.profile_var, width=20, values=self.profiles.names())
        self.profile_cb.pack(side="right", padx=8)
        ttk.Label(bar, text=t("gui.profile")).pack(side="right")

    def _current_job(self) -> str | None:
        idx = self.nb.index(self.nb.select())
        return JOBS[idx] if idx < 3 else None

    def _profile_save(self):
        job = self._current_job()
        name = self.profile_var.get().strip()
        if not job:
            messagebox.showinfo(t("gui.profile"), t("gui.profile_pick_job"))
            return
        if not name:
            messagebox.showinfo(t("gui.profile"), t("gui.profile_enter_name"))
            return
        self.profiles.save(name, {"job": job, "params": params_to_dict(self._params(job))})
        self.profile_cb["values"] = self.profiles.names()
        self.log(t("gui.profile_saved", name=name))

    def _profile_load(self):
        name = self.profile_var.get().strip()
        try:
            prof = self.profiles.load(name)
        except (OSError, json.JSONDecodeError):
            messagebox.showerror(t("gui.profile"), t("gui.profile_not_found", name=name))
            return
        job = prof.get("job", "sync")
        self._apply_params(job, prof.get("params", {}))
        self.nb.select(JOBS.index(job))
        self.log(t("gui.profile_loaded", name=name))

    def _profile_delete(self):
        name = self.profile_var.get().strip()
        if name and messagebox.askyesno(t("gui.profile_delete_title"), t("gui.profile_delete_ask", name=name)):
            self.profiles.delete(name)
            self.profile_cb["values"] = self.profiles.names()
            self.profile_var.set("")

    # --- Sync ------------------------------------------------------------------------------
    def _build_sync_tab(self):
        f = self._tab(t("nav.sync"), t("gui.sync_desc"))
        ttk.Label(f, text=t("gui.source_folders")).grid(row=2, column=0, sticky="nw", pady=(4, 4), padx=(0, 16))
        self.sync_sources = tk.Listbox(f, height=4, width=10, selectmode="extended", bg=P["field"], fg=P["text"],
                                       relief="flat", highlightthickness=1, highlightbackground=P["line"],
                                       highlightcolor=P["dry"], selectbackground=P["dry"],
                                       selectforeground=P["on_dry"], activestyle="none")
        self.sync_sources.grid(row=2, column=1, sticky="nsew", pady=4)
        bf = ttk.Frame(f)
        bf.grid(row=2, column=2, sticky="n", padx=(8, 0), pady=4)
        ttk.Button(bf, text=t("gui.add"), command=self._add_source).pack(fill="x")
        ttk.Button(bf, text=t("gui.remove"), command=lambda: [self.sync_sources.delete(i) for i in
                                                              reversed(self.sync_sources.curselection())]
                   ).pack(fill="x", pady=4)
        self.sync_target = tk.StringVar()
        self._folder_row(f, 3, t("gui.target_folder"), self.sync_target)
        self.sync_copy = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text=t("gui.copy_missing"), variable=self.sync_copy).grid(
            row=4, column=1, sticky="w", pady=(16, 4))
        self._label(f, 5, t("gui.placement"))
        self.sync_placement = ChoiceVar(f, CHOICES["sync.missing_placement"], self.cfg.get("sync.missing_placement"))
        self.sync_placement.widget.grid(row=5, column=1, sticky="w", pady=4)
        self.sync_pattern = tk.StringVar(value=localize(self.cfg.get("patterns.default")))
        pattern_cb = self._pattern_row(f, 6, t("gui.folder_pattern"), self.sync_pattern)
        toggle = lambda *_: pattern_cb.configure(  # noqa: E731
            state="normal" if self.sync_placement.get() == "pattern" else "disabled")
        self.sync_placement.var.trace_add("write", toggle)
        toggle()
        self.sync_fname = tk.StringVar(value=localize(self.cfg.get("patterns.filename")))
        self._label(f, 7, t("gui.filename_pattern"))
        ttk.Entry(f, textvariable=self.sync_fname).grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Label(f, text=t("gui.empty_original_name"), style="Hint.TLabel").grid(
            row=7, column=2, sticky="w", padx=(8, 0))

    def _add_source(self):
        p = filedialog.askdirectory(initialdir=str(Path.home() / "Pictures"))
        if p and p not in self.sync_sources.get(0, "end"):
            self.sync_sources.insert("end", p)

    # --- Duplicates --------------------------------------------------------------------------
    def _build_dup_tab(self):
        f = self._tab(t("nav.duplicates"), t("gui.dup_desc"))
        self.dup_folder = tk.StringVar()
        self._folder_row(f, 2, t("gui.folder"), self.dup_folder)
        self._label(f, 3, t("gui.keep"), top=16)
        self.dup_keep = ChoiceVar(f, CHOICES["duplicates.keep"], self.cfg.get("duplicates.keep"))
        self.dup_keep.widget.grid(row=3, column=1, sticky="w", pady=(16, 4))
        self._label(f, 4, t("gui.with_duplicates"))
        self.dup_action = ChoiceVar(f, CHOICES["duplicates.action"], self.cfg.get("duplicates.action"))
        self.dup_action.widget.grid(row=4, column=1, sticky="w", pady=4)
        self._label(f, 5, t("gui.dup_folder"))
        self.dup_dir = tk.StringVar(value=default_dup_folder(self.cfg))
        ttk.Entry(f, textvariable=self.dup_dir, width=30).grid(row=5, column=1, sticky="w", pady=4)
        ttk.Label(f, text=t("gui.libraries_safe"), style="Hint.TLabel", wraplength=560).grid(
            row=6, column=1, columnspan=2, sticky="w", pady=(14, 0))

    # --- Sort ----------------------------------------------------------------------------------
    def _build_sort_tab(self):
        f = self._tab(t("nav.sort"), t("gui.sort_desc"))
        self.sort_src = tk.StringVar()
        self._folder_row(f, 2, t("gui.source_folder"), self.sort_src)
        self.sort_dst = tk.StringVar()
        self._folder_row(f, 3, t("gui.target_folder"), self.sort_dst)
        self.sort_pattern = tk.StringVar(value=localize(self.cfg.get("patterns.default")))
        self._pattern_row(f, 4, t("gui.folder_pattern"), self.sort_pattern)
        self.sort_fname = tk.StringVar(value=localize(self.cfg.get("patterns.filename")))
        self._label(f, 5, t("gui.filename_pattern"))
        ttk.Entry(f, textvariable=self.sort_fname).grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Label(f, text=t("gui.empty_original_name"), style="Hint.TLabel").grid(
            row=5, column=2, sticky="w", padx=(8, 0))
        self._label(f, 6, t("gui.files"))
        self.sort_mode = ChoiceVar(f, ["copy", "move"], "copy")
        self.sort_mode.widget.grid(row=6, column=1, sticky="w", pady=4)
        # Example path: shows live what the pattern looks like
        ex = tk.Frame(f, bg=P["side"])
        ex.grid(row=7, column=1, columnspan=2, sticky="ew", pady=(16, 0))
        tk.Label(ex, text=t("gui.example"), font=self.fonts["small"], bg=P["side"], fg=P["muted"]
                 ).pack(anchor="w", padx=12, pady=(8, 0))
        self.sort_example = tk.Label(ex, font=self.fonts["mono"], bg=P["side"], fg=P["text"], anchor="w",
                                     justify="left", wraplength=540)
        self.sort_example.pack(fill="x", padx=12, pady=(2, 10))
        self.sort_pattern.trace_add("write", lambda *_: self._update_example())
        self.sort_fname.trace_add("write", lambda *_: self._update_example())
        self._update_example()

    def _update_example(self):
        e = FileEntry(Path("/Source/Holiday/IMG_4711.CR3"), 0, 0, Path("/Source"))
        m = Meta(datetime(2025, 8, 9, 14, 5, 59), "DateTimeOriginal", "Canon EOS R6m2", "Canon", "RF24-105mm F4 L IS USM")
        try:
            r = PatternRenderer(self.cfg)
            path = r.folder(self.sort_pattern.get(), e, m) / r.filename(self.sort_fname.get(), e, m)
            self.sort_example.configure(text=t("gui.example_line", path=str(path)), fg=P["text"])
        except (PatternError, KeyError, ValueError, IndexError) as err:
            self.sort_example.configure(text=str(err).splitlines()[0], fg=P["error"])

    # --- Settings (generated from defaults.toml) ----------------------------------------------
    def _build_settings_tab(self):
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text=t("nav.settings"))
        top = ttk.Frame(outer, padding=(32, 16, 32, 6))
        top.pack(side="top", fill="x")
        ttk.Label(top, text=t("nav.settings"), style="H1.TLabel").pack(anchor="w")
        ttk.Label(top, text=t("gui.settings_saved_in", path=str(self.cfg.user_file)), style="Hint.TLabel"
                  ).pack(anchor="w", pady=(2, 0))

        # The footer stays visible while scrolling
        foot = ttk.Frame(outer, padding=(32, 12, 32, 16))
        foot.pack(side="bottom", fill="x")
        tk.Frame(outer, bg=P["line"], height=1).pack(side="bottom", fill="x")
        ttk.Button(foot, text=t("gui.settings_save"), style="Dry.TButton", command=self._settings_save
                   ).pack(side="left")
        ttk.Button(foot, text=t("gui.settings_reset"), command=self._settings_reset).pack(side="left", padx=8)
        self.settings_status = ttk.Label(foot, style="Hint.TLabel")
        self.settings_status.pack(side="left", padx=8)
        ttk.Button(foot, text=t("gui.open_config"), command=lambda: (self.cfg.save(), open_path(self.cfg.user_file))
                   ).pack(side="right")

        page = ScrollPage(outer, padding=(32, 4, 32, 24))
        page.outer.pack(side="top", fill="both", expand=True)
        self.settings_frame = page.body
        self.helps = parse_comments(DEFAULTS_FILE)
        self._fill_settings()

    def _help(self, path: str) -> str:
        """Help text in the app language: German texts come from the locale, English ones from defaults.toml."""
        return catalog(language()).get(f"help.{path}") or self.helps.get(path, "")

    def _fill_settings(self):
        for w in self.settings_frame.winfo_children():
            w.destroy()
        self.setting_getters: dict[str, callable] = {}
        f = self.settings_frame
        f.columnconfigure(1, weight=1)
        row = 0
        for section, values in self.cfg.defaults.items():
            if not isinstance(values, dict):
                continue
            ttk.Label(f, text=t(f"section.{section}"), style="H2.TLabel"
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(22, 6))
            row += 1
            for key, default in values.items():
                path = f"{section}.{key}"
                current = self.cfg.get(path, default)
                label = catalog(language()).get(f"setting.{path}") or key.replace("_", " ")
                ttk.Label(f, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 16), pady=(7, 3))
                getter, widget = self._setting_widget(f, path, default, current)
                widget.grid(row=row, column=1, sticky="ew" if not isinstance(widget, ttk.Checkbutton) else "w",
                            pady=3)
                self.setting_getters[path] = getter
                row += 1
                if help_text := self._help(path):
                    ttk.Label(f, text=help_text, style="Hint.TLabel", wraplength=620
                              ).grid(row=row, column=1, sticky="w", pady=(0, 4))
                    row += 1

    def _setting_widget(self, parent, path, default, current):
        if path in PATTERN_SETTINGS:
            current = [localize(p) for p in current] if isinstance(current, list) else localize(current)
        if isinstance(default, bool):
            var = tk.BooleanVar(value=current)
            return var.get, ttk.Checkbutton(parent, variable=var)
        if path in CHOICES:
            cv = ChoiceVar(parent, CHOICES[path], current)
            return cv.get, cv.widget
        if isinstance(default, (int, float)):
            var = tk.StringVar(value=str(current))
            typ = type(default)

            def get_num(var=var, typ=typ, path=path):
                try:
                    return typ(var.get().replace(",", "."))
                except ValueError:
                    raise ValueError(t("gui.number_expected", key=path))
            return get_num, ttk.Entry(parent, textvariable=var, width=12)
        if isinstance(default, str):
            var = tk.StringVar(value=current)
            get = (lambda var=var: canonical(var.get())) if path in PATTERN_SETTINGS else var.get
            return get, ttk.Entry(parent, textvariable=var)
        # Lists/tables: as text, one line per entry
        if isinstance(default, list) and (not default or not isinstance(default[0], dict)):
            text = "\n".join(map(str, current))
            if path in PATTERN_SETTINGS:
                parse = lambda s: [canonical(x.strip()) for x in s.splitlines() if x.strip()]  # noqa: E731
            else:
                parse = lambda s: [x.strip() for x in s.splitlines() if x.strip()]  # noqa: E731
        elif isinstance(default, list):  # list of tables (libraries)
            text = "\n".join(f"{d.get('extension', '')}: {', '.join(d.get('subfolders', []))}" for d in current)
            parse = lambda s: [{"extension": a.strip(), "subfolders": [x.strip() for x in b.split(",") if x.strip()]}  # noqa: E731
                               for a, _, b in (x.partition(":") for x in s.splitlines() if x.strip())]
        else:  # dict (e.g. camera names)
            text = "\n".join(f"{k} = {v}" for k, v in current.items())
            parse = lambda s: {a.strip(): b.strip() for a, _, b in  # noqa: E731
                               (x.partition("=") for x in s.splitlines() if "=" in x)}
        box = text_box(parent, height=min(8, max(2, text.count("\n") + 2)), width=1, wrap="none", font=self.fonts["mono"])
        box.insert("1.0", text)
        return (lambda box=box, parse=parse: parse(box.get("1.0", "end"))), box

    def _settings_save(self):
        try:
            values = {p: g() for p, g in self.setting_getters.items()}
            for p in ("patterns.default", "patterns.filename"):
                if values.get(p):
                    validate(values[p])
            for v in values.get("patterns.presets", []):
                validate(v)
        except (ValueError, PatternError) as e:
            messagebox.showerror(t("nav.settings"), str(e))
            return
        old_look = (self.cfg.get("ui.language"), self.cfg.get("ui.theme"))
        for p, v in values.items():
            self.cfg.set(p, v)
        self.cfg.save()
        self.cfg.reload()
        self.log(t("gui.settings_saved") + ("\n" + "\n".join(self.cfg.warnings) if self.cfg.warnings else ""))
        self._settings_note(t("gui.saved") + (" – " + t("gui.with_warnings") if self.cfg.warnings else ""))
        self._update_example()
        if self.cfg.get("ui.language") != old_look[0]:
            messagebox.showinfo(t("nav.settings"), t("gui.restart_for_language"))
        elif self.cfg.get("ui.theme") != old_look[1]:
            messagebox.showinfo(t("nav.settings"), t("gui.restart_for_theme"))

    def _settings_reset(self):
        if messagebox.askyesno(t("gui.reset_title"), t("gui.reset_ask")):
            self.cfg.reset()
            self._fill_settings()
            self.log(t("gui.settings_reset_done"))
            self._settings_note(t("gui.settings_reset_done"))

    def _settings_note(self, text: str):
        self.settings_status.configure(text=f"{text} ({time.strftime('%H:%M')})")

    # --- History / undo -------------------------------------------------------------------------
    def _build_history_tab(self):
        f = ttk.Frame(self.nb, padding=(32, 16, 32, 8))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        self.nb.add(f, text=t("nav.history"))
        self._header(f, t("nav.history"), t("gui.history_desc"))
        self.hist = tk.Listbox(f, height=8, bg=P["field"], fg=P["text"], relief="flat", highlightthickness=1,
                               highlightbackground=P["line"], highlightcolor=P["dry"], activestyle="none",
                               selectbackground=P["dry"], selectforeground=P["on_dry"], font=self.fonts["mono"])
        self.hist.grid(row=2, column=0, sticky="nsew")
        hist_bar = ttk.Scrollbar(f, orient="vertical", command=self.hist.yview)
        hist_bar.grid(row=2, column=1, sticky="ns")
        self.hist.configure(yscrollcommand=hist_bar.set)
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(bf, text=t("gui.undo"), command=self._hist_undo).pack(side="left")
        ttk.Button(bf, text=t("gui.refresh"), command=self._hist_refresh).pack(side="left", padx=6)
        ttk.Button(bf, text=t("gui.log_file"), command=lambda: open_path(self.cfg.data_dir / "logs" / "mediatidy.log")
                   ).pack(side="right")
        ttk.Button(bf, text=t("gui.reports_folder"), command=lambda: open_path(self.cfg.data_dir / "reports")
                   ).pack(side="right", padx=6)
        self._hist_refresh()

    def _hist_refresh(self):
        self.hist_files = list_journals(self.cfg.data_dir)
        self.hist.delete(0, "end")
        for p in self.hist_files:
            n = sum(1 for e in read_entries(p) if e.get("action") not in ("run_undone", "folder_created"))
            self.hist.insert("end", "  " + t("gui.history_line", name=p.stem, n=n))
        if not self.hist_files:
            self.hist.insert("end", "  " + t("gui.history_empty"))

    def _hist_undo(self):
        sel = self.hist.curselection()
        if not sel or sel[0] >= len(self.hist_files):
            return
        file = self.hist_files[sel[0]]
        if is_undone(file):
            messagebox.showinfo(t("gui.undo"), t("gui.already_undone"))
            return
        if not messagebox.askyesno(t("gui.undo"), t("gui.undo_ask", name=file.stem)):
            return

        def work(ctx):
            done, problems = undo(file, ctx)
            ctx.message(t("gui.undo_done", n=done) + "".join(f"\n  ⚠️ {p}" for p in problems))
            return None
        self._start(work)

    # --- Bottom: mode, start, progress, log -------------------------------------------------------
    def _build_bottom(self):
        f = ttk.Frame(self.main, padding=(32, 4, 32, 16))
        f.pack(side="bottom", fill="x")
        f.columnconfigure(0, weight=1)
        self.run_frame = f

        # Mode bar: blue = dry run, amber = real run
        self.banner = tk.Frame(f)
        self.banner.grid(row=0, column=0, sticky="ew")
        self.banner_mark = tk.Frame(self.banner, width=5)
        self.banner_mark.pack(side="left", fill="y")
        inner = tk.Frame(self.banner)
        inner.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        inner.columnconfigure(1, weight=1)
        self.dry_var = tk.BooleanVar(value=self.cfg.get("actions.dry_run", True))
        modes = tk.Frame(inner)
        modes.grid(row=0, column=0, sticky="w")
        for text, val in ((t("gui.mode_dry"), True), (t("gui.mode_real"), False)):
            ttk.Radiobutton(modes, text=text, variable=self.dry_var, value=val, style="Mode.Toolbutton"
                            ).pack(side="left")
        info = tk.Frame(inner)
        info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.banner_title = tk.Label(info, font=self.fonts["bold"], anchor="w")
        self.banner_title.pack(side="left")
        self.banner_text = tk.Label(info, anchor="w", justify="left", wraplength=560)
        self.banner_text.pack(side="left", padx=(8, 0))
        self.banner_parts = [self.banner, inner, modes, info, self.banner_title, self.banner_text]
        btns = tk.Frame(inner)
        btns.grid(row=0, column=2, sticky="e")
        self.banner_parts.append(btns)
        self.report_btn = ttk.Button(btns, text=t("gui.open_report"), state="disabled", command=self._open_report)
        self.report_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text=t("gui.cancel"), state="disabled",
                                     command=lambda: self.ctx and self.ctx.cancel())
        self.cancel_btn.pack(side="left", padx=6)
        self.start_btn = ttk.Button(btns, text=t("gui.start"), command=self._run, style="Dry.TButton")
        self.start_btn.pack(side="left")

        self.prog = ttk.Progressbar(f, mode="determinate", style="Run.Horizontal.TProgressbar")
        self.prog.grid(row=1, column=0, sticky="ew", pady=(12, 2))
        self.prog_label = ttk.Label(f, text=t("gui.ready"), style="Hint.TLabel")
        self.prog_label.grid(row=2, column=0, sticky="w")
        self.logbox = text_box(f, height=5, width=1, wrap="word", font=self.fonts["mono"], state="disabled")
        self.logbox.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.logbox.tag_configure("error", foreground=P["error"])
        self.logbox.tag_configure("head", font=(self.fonts["mono"][0], self.fonts["mono"][1], "bold"))

        for var in (self.dry_var, self.sync_copy, self.dup_action.var, self.dup_dir, self.sort_mode.var):
            var.trace_add("write", lambda *_: self._update_mode())

    def _mode_info(self) -> tuple[str, str, str, str]:
        """(kind, button text, title, explanation) for the current page and mode."""
        job = self._current_job()
        if not job:
            return "off", t("gui.start"), "", ""
        if self.dry_var.get():
            return "dry", t("gui.start_dry"), t("gui.mode_dry"), t("gui.dry_explain")
        if job == "sync" and not self.sync_copy.get() or job == "duplicates" and self.dup_action.get() == "report":
            return "dry", t("gui.create_report"), t("gui.report_only"), t("gui.report_only_explain")
        undo_hint = " " + t("gui.undo_hint")
        real = t("gui.real_run")
        if job == "sync":
            return "real", t("gui.copy_missing_button"), real, t("gui.sync_explain") + undo_hint
        if job == "duplicates":
            if self.dup_action.get() == "trash":
                return "real", t("gui.trash_button"), real, t("gui.trash_explain")
            return "real", t("gui.move_dups_button"), real, \
                t("gui.move_dups_explain", folder=self.dup_dir.get() or default_dup_folder(self.cfg)) + undo_hint
        if self.sort_mode.get() == "move":
            return "real", t("gui.move_button"), real, t("gui.sort_move_explain") + undo_hint
        return "real", t("gui.copy_button"), real, t("gui.sort_copy_explain") + undo_hint

    def _update_mode(self):
        if not hasattr(self, "banner"):
            return
        kind, button, title, text = self._mode_info()
        if kind == "off":
            self.banner.grid_remove()
            return
        self.banner.grid()
        real = kind == "real"
        accent, light = (P["real"], P["real_tint"]) if real else (P["dry"], P["dry_tint"])
        for w in self.banner_parts:
            w.configure(bg=light)
        self.banner_mark.configure(bg=accent)
        self.banner_title.configure(text=title, fg=P["text"])
        self.banner_text.configure(text=text, fg=P["text"])
        s = ttk.Style(self.root)
        s.configure("Mode.Toolbutton", background=light, foreground=P["muted"], bordercolor=accent,
                    lightcolor=light, darkcolor=light)
        on_fg = P["on_real"] if self.dry_var.get() is False else P["on_dry"]
        sel = P["real"] if not self.dry_var.get() else P["dry"]
        s.map("Mode.Toolbutton", background=[("selected", sel), ("active", P["field"])],
              lightcolor=[("selected", sel)], darkcolor=[("selected", sel)],
              foreground=[("selected", on_fg)])
        s.configure("Run.Horizontal.TProgressbar", background=accent, lightcolor=accent, darkcolor=accent)
        self.start_btn.configure(text=button, style="Real.TButton" if real else "Dry.TButton")

    def log(self, text: str):
        self.logbox.configure(state="normal")
        for line in text.split("\n"):
            tag = "error" if ("❌" in line or "⚠️" in line) else "head" if line.startswith("===") else ()
            self.logbox.insert("end", line + "\n", tag)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _open_report(self):
        if self.last_result and self.last_result.reports:
            html = [r for r in self.last_result.reports if r.suffix == ".html"]
            open_path((html or self.last_result.reports)[0])

    # --- Parameters <-> UI ---------------------------------------------------------------------------
    def _params(self, job: str):
        dry = self.dry_var.get()
        if job == "sync":
            return SyncParams(list(self.sync_sources.get(0, "end")), self.sync_target.get().strip(),
                              self.sync_copy.get(), self.sync_placement.get(), canonical(self.sync_pattern.get().strip()),
                              canonical(self.sync_fname.get().strip()), dry)
        if job == "duplicates":
            return DupParams(self.dup_folder.get().strip(), self.dup_keep.get(), self.dup_action.get(),
                             self.dup_dir.get().strip() or default_dup_folder(self.cfg), dry)
        return SortParams(self.sort_src.get().strip(), self.sort_dst.get().strip(),
                          canonical(self.sort_pattern.get().strip()), canonical(self.sort_fname.get().strip()),
                          self.sort_mode.get(), dry)

    def _apply_params(self, job: str, d: dict):
        p = params_from_dict(job, {**params_to_dict(self._params(job)), **legacy.params(job, d)})
        if job == "sync":
            self.sync_sources.delete(0, "end")
            for s in p.sources:
                self.sync_sources.insert("end", s)
            self.sync_target.set(p.target)
            self.sync_copy.set(p.copy_missing)
            self.sync_placement.set(p.placement)
            self.sync_pattern.set(localize(p.pattern))
            self.sync_fname.set(localize(p.filename))
        elif job == "duplicates":
            self.dup_folder.set(p.folder)
            self.dup_keep.set(p.keep)
            self.dup_action.set(p.action)
            self.dup_dir.set(p.dup_folder)
        else:
            self.sort_src.set(p.source)
            self.sort_dst.set(p.target)
            self.sort_pattern.set(localize(p.pattern))
            self.sort_fname.set(localize(p.filename))
            self.sort_mode.set(p.mode)

    def _save_state(self):
        try:
            state = {"tab": self.nb.index(self.nb.select()), "dry_run": self.dry_var.get(),
                     **{j: params_to_dict(self._params(j)) for j in JOBS}}
            self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, tk.TclError):
            pass

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            try:
                old = self.state_file.with_name(legacy.LAST_INPUTS_FILE)
                state = legacy.last_inputs(json.loads(old.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                return
        for j in JOBS:
            if j in state:
                try:
                    self._apply_params(j, state[j])
                except (TypeError, ValueError):
                    pass
        try:
            self.nb.select(int(state.get("tab", 0)))
        except (tk.TclError, ValueError):
            pass

    # --- Run ------------------------------------------------------------------------------------------
    def _validate(self, job: str, p) -> str | None:
        if job == "sync":
            if not p.sources:
                return t("gui.need_source")
            if not p.target:
                return t("gui.need_target")
            if p.placement == "pattern":
                validate(p.pattern)
        elif job == "duplicates":
            if not p.folder:
                return t("gui.need_folder")
        else:
            if not p.source or not p.target:
                return t("gui.need_source_target")
            validate(p.pattern)
        if p.__dict__.get("filename"):
            validate(p.filename)
        return None

    def _confirm(self, job: str, p) -> bool:
        """Ask before changing actions. Sets p.confirmed."""
        if p.dry_run:
            return True
        ask = self.cfg.get("actions.confirm", True)
        overwrite = self.cfg.get("actions.collision") == "overwrite"
        msgs, warnings = [], []
        if job == "sync" and p.copy_missing:
            msgs.append(t("gui.confirm_sync", target=p.target))
        if job == "duplicates" and p.action == "move":
            msgs.append(t("gui.confirm_dup_move", folder=p.dup_folder))
        if job == "duplicates" and p.action == "trash":
            msgs.append(t("gui.confirm_dup_trash"))
        if job == "sort":
            msgs.append(t("gui.confirm_sort_move" if p.mode == "move" else "gui.confirm_sort_copy", target=p.target))
        if job == "duplicates" and p.action != "report" and self.cfg.get("compare.method", "hash") != "hash":
            warnings.append(t("gui.warn_unsafe_method"))
        if overwrite and job != "duplicates":
            warnings.append(t("gui.warn_overwrite"))
        moving_from = p.source if job == "sort" and p.mode == "move" else (
            p.folder if job == "duplicates" and p.action != "report" else None)
        if moving_from:
            cats = affected_catalogs(self.cfg, Path(moving_from))
            if cats:
                warnings.append(t("gui.warn_lightroom") + "\n" + "\n".join(cats))
        if warnings or (msgs and ask):
            if not messagebox.askyesno(t("gui.confirm_title"), "\n\n".join(msgs + warnings) + "\n\n" + t("gui.continue")):
                return False
        p.confirmed = True
        return True

    def _run(self):
        job = self._current_job()
        if not job:
            messagebox.showinfo(t("gui.start"), t("gui.profile_pick_job"))
            return
        p = self._params(job)
        try:
            err = self._validate(job, p)
        except PatternError as e:
            err = str(e)
        if err:
            messagebox.showwarning(t("gui.inputs"), err)
            return
        if not self._confirm(job, p):
            return
        self._save_state()
        self.log(f"\n=== {t(f'nav.{job}')} {'(' + t('gui.mode_dry') + ')' if p.dry_run else ''} ===")
        self._start(lambda ctx: run_job(self.cfg, ctx, job, p))

    def _start(self, work):
        if self.worker and self.worker.is_alive():
            return
        self.ctx = RunContext(on_message=lambda m: self.q.put(("msg", m)),
                              on_progress=lambda ph, d, n: self.q.put(("prog", (ph, d, n))),
                              on_names=lambda todo: self._names_from_worker(todo))
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        ctx = self.ctx

        def target():
            try:
                self.q.put(("done", work(ctx)))
            except Cancelled:
                self.q.put(("msg", t("msg.cancelled")))
                self.q.put(("done", None))
            except (OSError, ValueError) as e:  # no access, folder missing, wrong pattern
                self.q.put(("msg", f"❌ {e}"))
                self.q.put(("alert", str(e)))
                self.q.put(("done", None))
            except Exception as e:  # noqa: BLE001 – show everything instead of crashing
                logging.getLogger("mediatidy").exception("Error during run")
                self.q.put(("msg", "❌ " + t("gui.error", error=str(e))))
                self.q.put(("done", None))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _names_from_worker(self, todo: dict) -> dict:
        """Runs in the worker thread: open the dialog in the main thread and wait for the answer."""
        answer, ev = {}, threading.Event()
        self.q.put(("names", (todo, answer, ev)))
        while not ev.wait(0.2):
            self.ctx.check()
        if answer["names"] is None:
            raise Declined()
        return answer["names"]

    def _names_dialog(self, todo: dict) -> dict | None:
        """Show new names from the metadata; returns {placeholder: {name in file: target name}} or None."""
        win = tk.Toplevel(self.root, bg=P["paper"], padx=20, pady=16)
        win.title(t("gui.names_title"))
        self._window_appearance(win)
        win.transient(self.root)
        win.columnconfigure(0, weight=1)
        ttk.Label(win, text=t("gui.names_heading"), style="H2.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(win, style="Hint.TLabel", wraplength=560, justify="left", text=t("gui.names_explain")
                  ).grid(row=1, column=0, sticky="w", pady=(4, 12))
        rows = sum(len(v) for v in todo.values()) + len(todo)
        canvas = tk.Canvas(win, bg=P["paper"], highlightthickness=0, height=min(420, 34 * rows + 30), width=600,
                           yscrollincrement=1)
        canvas.scroll_page = True
        bar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.grid(row=2, column=0, sticky="nsew")
        if 34 * rows + 30 > 420:
            bar.grid(row=2, column=1, sticky="ns")
        grid = ttk.Frame(canvas)
        canvas.create_window(0, 0, window=grid, anchor="nw")
        grid.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        grid.columnconfigure(2, weight=1)
        entries: dict[str, dict[str, tk.StringVar]] = {}
        r = 0
        for ph, found in todo.items():
            ttk.Label(grid, text=f"{{{t(f'ph.{ph}')}}}", font=self.fonts["bold"]).grid(
                row=r, column=0, sticky="w", pady=(8, 2))
            ttk.Label(grid, text=t("gui.files"), style="Hint.TLabel").grid(row=r, column=1, sticky="e", padx=8)
            ttk.Label(grid, text=t("gui.becomes"), style="Hint.TLabel").grid(row=r, column=2, sticky="w")
            r += 1
            for raw, (proposal, n) in found.items():
                ttk.Label(grid, text=raw, font=self.fonts["mono"]).grid(row=r, column=0, sticky="w", padx=(0, 8))
                ttk.Label(grid, text=str(n), style="Hint.TLabel").grid(row=r, column=1, sticky="e", padx=8)
                var = tk.StringVar(value=proposal)
                ttk.Entry(grid, textvariable=var, width=28).grid(row=r, column=2, sticky="ew", pady=2)
                entries.setdefault(ph, {})[raw] = var
                r += 1
        result: dict = {}

        def accept():
            result["names"] = {ph: {raw: v.get().strip() or todo[ph][raw][0] for raw, v in names.items()}
                               for ph, names in entries.items()}
            win.destroy()

        btns = ttk.Frame(win)
        btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text=t("gui.cancel"), command=win.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text=t("gui.names_accept"), style="Dry.TButton", command=accept).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.grab_set()
        win.focus_set()
        self.root.wait_window(win)
        if "names" in result:  # otherwise the settings page shows the old mapping and would write it back
            assign_names(self.cfg, [], result["names"])  # the run saves it afterwards
            self._fill_settings()
        return result.get("names")

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "msg":
                    self.log(data)
                elif kind == "prog":
                    phase, done, total = data
                    label = t(f"phase.{phase}")
                    if total:
                        self.prog.configure(mode="determinate", maximum=total, value=done)
                        self.prog_label.configure(text=f"{label}: {done} / {total}{self._eta(phase, done, total)}")
                    else:
                        self.prog.configure(mode="indeterminate")
                        self.prog.step(5)
                        self.prog_label.configure(text=t("gui.progress_files", phase=label, n=done))
                elif kind == "alert":
                    messagebox.showerror("MediaTidy", data)
                elif kind == "names":
                    todo, answer, ev = data
                    answer["names"] = self._names_dialog(todo)
                    ev.set()
                elif kind == "done":
                    self._finished(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _eta(self, phase: str, done: int, total: int) -> str:
        """Remaining time estimate per phase."""
        now = time.monotonic()
        if getattr(self, "_eta_phase", None) != phase or done < getattr(self, "_eta_done0", 0):
            self._eta_phase, self._eta_t0, self._eta_done0 = phase, now, done
            return ""
        elapsed, step = now - self._eta_t0, done - self._eta_done0
        if elapsed < 5 or step <= 0:
            return ""
        rest = elapsed / step * (total - done)
        text = f"{rest / 60:.0f} min" if rest >= 90 else f"{rest:.0f} s"
        return "  –  " + t("gui.eta", time=text)

    def _finished(self, res: JobResult | None):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.prog.configure(mode="determinate", value=0)
        self.prog_label.configure(text=t("gui.done"))
        self._hist_refresh()
        if res is None:
            return
        self.last_result = res
        if res.cancelled:
            self.log(t("gui.cancelled_partial"))
        self.log("\n".join(f"  {k}: {v}" for k, v in summary_items(res)))
        for r in res.reports:
            self.log("  " + t("msg.report", path=str(r)))
        self.report_btn.configure(state="normal" if res.reports else "disabled")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(t("gui.quit_title"), t("gui.quit_ask")):
                return
            self.ctx.cancel()
        self._save_state()
        self.root.destroy()


def run_gui(cfg: Config) -> None:
    root = tk.Tk()
    App(root, cfg)
    root.mainloop()
