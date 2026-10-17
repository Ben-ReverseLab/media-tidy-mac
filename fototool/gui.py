"""Grafische Oberfläche (tkinter)."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import tkinter as tk
import tkinter.font
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import DEFAULTS_FILE, Config, Profiles
from .context import Cancelled, RunContext
from .deps import check
from .jobs import DupParams, JobResult, SortParams, SyncParams, params_from_dict, params_to_dict, run_job
from .journal import list_journals, read_entries, undo
from .lightroom import affected_catalogs
from .patterns import PLACEHOLDERS, PatternError, validate

# Auswahllisten für Einstellungen (Schlüssel -> erlaubte Werte)
CHOICES = {
    "vergleich.methode": ["hash", "name_groesse", "name_groesse_datum"],
    "vergleich.hash_algorithmus": ["blake2b", "sha256", "sha1", "md5"],
    "aktionen.kollision": ["suffix", "ueberspringen", "ueberschreiben"],
    "abgleich.fehlende_ablage": ["struktur", "muster"],
    "abgleich.sidecar_geaendert": ["neuer_ersetzt", "nur_bericht"],
    "duplikate.behalten": ["aeltestes", "neuestes", "kuerzester_pfad", "laengster_pfad", "erster"],
    "duplikate.aktion": ["bericht", "verschieben", "papierkorb"],
}
LABELS = {
    "aeltestes": "ältestes", "neuestes": "neuestes", "kuerzester_pfad": "kürzester Pfad",
    "laengster_pfad": "längster Pfad", "erster": "erster (alphabetisch)",
    "bericht": "nur Bericht", "verschieben": "in Duplikat-Ordner verschieben", "papierkorb": "in Papierkorb",
    "struktur": "Ordnerstruktur beibehalten", "muster": "nach Muster sortieren",
    "copy": "kopieren", "move": "verschieben",
    "neuer_ersetzt": "ersetzen, wenn Quelle neuer", "nur_bericht": "nur im Bericht melden",
}
SECTION_TITLES = {
    "dateien": "Dateitypen", "scan": "Durchsuchen", "vergleich": "Vergleich", "metadaten": "Metadaten",
    "muster": "Muster", "aktionen": "Aktionen", "abgleich": "Abgleich", "duplikate": "Duplikate",
    "berichte": "Berichte", "pfade": "Pfade", "oberflaeche": "Oberfläche",
}

# Farben: ruhiges Papier-Grau; Farbe nur dort, wo sie Sicherheit bedeutet
# (Blau = Probelauf, Bernstein wie Dunkelkammerlicht = echter Lauf).
P = {
    "papier": "#F5F6F4", "seite": "#E8EAE7", "linie": "#D3D7D4", "weiss": "#FFFFFF",
    "graphit": "#23272B", "schiefer": "#646C75",
    "probe": "#2D6784", "probe_dunkel": "#24546C", "probe_hell": "#E2ECF1",
    "echt": "#E39A2D", "echt_dunkel": "#CC8620", "echt_hell": "#FBEBD2",
    "fehler": "#B3261E",
}
# Seitenleiste: (Titel, Index im Notebook); None = Abstand
NAV = [("Abgleich", 0), ("Duplikate", 1), ("Sortieren", 2), None, ("Verlauf", 4), ("Einstellungen", 3)]
JOBS = ["abgleich", "duplikate", "sortieren"]


def parse_comments(file: Path) -> dict[str, str]:
    """Kommentare aus defaults.toml als Hilfetexte: {"abschnitt.schluessel": "Text"}."""
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
            buf = [] if not section else buf
    return helps


def open_path(p: Path) -> None:
    subprocess.run(["open", str(p)], check=False)


def text_box(parent, **kw) -> tk.Text:
    """tk.Text im Stil der übrigen Eingabefelder."""
    return tk.Text(parent, bg=P["weiss"], fg=P["graphit"], insertbackground=P["graphit"], relief="flat",
                   highlightthickness=1, highlightbackground=P["linie"], highlightcolor=P["probe"],
                   selectbackground=P["probe"], selectforeground=P["weiss"], padx=8, pady=6, **kw)


class ChoiceVar:
    """Combobox mit lesbaren Texten, gibt internen Wert zurück."""

    def __init__(self, parent, values: list[str], value: str, width=28):
        self.values = values
        self.var = tk.StringVar(value=LABELS.get(value, value))
        self.widget = ttk.Combobox(parent, textvariable=self.var, state="readonly", width=width,
                                   values=[LABELS.get(v, v) for v in values])

    def get(self) -> str:
        shown = self.var.get()
        return next((v for v in self.values if LABELS.get(v, v) == shown), shown)

    def set(self, value: str) -> None:
        self.var.set(LABELS.get(value, value))


class App:
    def __init__(self, root: tk.Tk, cfg: Config):
        self.root = root
        self.cfg = cfg
        self.profiles = Profiles(cfg)
        self.state_file = cfg.data_dir / "zuletzt.json"
        self.q: queue.Queue = queue.Queue()
        self.ctx: RunContext | None = None
        self.worker: threading.Thread | None = None
        self.last_result: JobResult | None = None
        self.drive_hints: list = []

        root.title(f"FotoTool {__version__}")
        root.geometry(cfg.get("oberflaeche.fenster", "1100x800"))
        root.minsize(900, 640)
        root.configure(bg=P["papier"])
        size = int(cfg.get("oberflaeche.schriftgroesse", 13))
        for f in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tk.font.nametofont(f).configure(size=size)
            except (tk.TclError, AttributeError):
                pass
        self._style(size)

        shell = tk.Frame(root, bg=P["papier"])
        shell.pack(fill="both", expand=True)
        self._build_sidebar(shell)
        tk.Frame(shell, bg=P["linie"], width=1).pack(side="left", fill="y")
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
        self._load_state()
        self._page_changed()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        hints = check(cfg)
        if hints:
            self.log("Hinweise:\n" + "\n".join("• " + h for h in hints))

    # --- Aussehen ---------------------------------------------------------------
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
        s.configure(".", background=P["papier"], foreground=P["graphit"], bordercolor=P["linie"],
                    lightcolor=P["papier"], darkcolor=P["papier"], troughcolor=P["seite"],
                    focuscolor=P["probe"], selectbackground=P["probe"], selectforeground=P["weiss"],
                    fieldbackground=P["weiss"], insertcolor=P["graphit"], arrowcolor=P["graphit"])
        s.configure("Hint.TLabel", foreground=P["schiefer"], font=self.fonts["small"])
        s.configure("Error.TLabel", foreground=P["fehler"], font=self.fonts["small"])
        s.configure("H1.TLabel", font=self.fonts["h1"])
        s.configure("H2.TLabel", font=self.fonts["h2"])
        s.configure("TButton", padding=(12, 5), background=P["weiss"], bordercolor=P["linie"],
                    lightcolor=P["weiss"], darkcolor=P["weiss"])
        s.map("TButton", background=[("disabled", P["papier"]), ("pressed", P["linie"]), ("active", P["seite"])],
              foreground=[("disabled", P["schiefer"])], bordercolor=[("focus", P["probe"])],
              lightcolor=[("pressed", P["linie"]), ("active", P["seite"])],
              darkcolor=[("pressed", P["linie"]), ("active", P["seite"])])
        for name, bg, dark, fg in (("Probe", P["probe"], P["probe_dunkel"], P["weiss"]),
                                   ("Echt", P["echt"], P["echt_dunkel"], P["graphit"])):
            s.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=dark, lightcolor=bg,
                        darkcolor=bg, font=self.fonts["bold"], padding=(18, 7))
            s.map(f"{name}.TButton",
                  background=[("disabled", P["linie"]), ("pressed", dark), ("active", dark)],
                  lightcolor=[("disabled", P["linie"]), ("pressed", dark), ("active", dark)],
                  darkcolor=[("disabled", P["linie"]), ("pressed", dark), ("active", dark)],
                  foreground=[("disabled", P["schiefer"])], bordercolor=[("focus", P["graphit"])])
        s.configure("TEntry", padding=(6, 4), lightcolor=P["weiss"], darkcolor=P["weiss"])
        s.map("TEntry", bordercolor=[("focus", P["probe"])], lightcolor=[("focus", P["probe"])])
        s.configure("TCombobox", padding=(6, 3), background=P["weiss"], lightcolor=P["weiss"], darkcolor=P["weiss"])
        s.map("TCombobox", fieldbackground=[("disabled", P["papier"]), ("readonly", P["weiss"])],
              foreground=[("disabled", P["schiefer"])], background=[("active", P["seite"])],
              selectbackground=[("readonly", P["weiss"])], selectforeground=[("readonly", P["graphit"])],
              bordercolor=[("focus", P["probe"])])
        self.root.option_add("*TCombobox*Listbox.background", P["weiss"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", P["probe"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", P["weiss"])
        s.configure("TCheckbutton", indicatorbackground=P["weiss"], indicatorforeground=P["probe"],
                    upperbordercolor=P["schiefer"], lowerbordercolor=P["schiefer"])
        s.map("TCheckbutton", background=[("active", P["papier"])],
              indicatorbackground=[("selected", P["weiss"])])
        s.configure("Pages.TNotebook", borderwidth=0, tabmargins=0, padding=0)
        s.layout("Pages.TNotebook.Tab", [])  # Reiter ausblenden – Navigation über Seitenleiste
        s.configure("Vertical.TScrollbar", background=P["seite"], bordercolor=P["papier"], arrowcolor=P["schiefer"])
        s.configure("Run.Horizontal.TProgressbar", background=P["probe"], troughcolor=P["seite"],
                    bordercolor=P["seite"], lightcolor=P["probe"], darkcolor=P["probe"], thickness=6)
        s.configure("Mode.Toolbutton", padding=(12, 5), font=self.fonts["bold"])

    # --- Seitenleiste --------------------------------------------------------------
    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=P["seite"], width=200)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="FotoTool", font=self.fonts["h2"], bg=P["seite"], fg=P["graphit"]
                 ).pack(anchor="w", padx=22, pady=(26, 0))
        tk.Label(side, text=f"Version {__version__}", font=self.fonts["small"], bg=P["seite"], fg=P["schiefer"]
                 ).pack(anchor="w", padx=22, pady=(0, 22))
        self.nav_items: dict[int, tuple] = {}
        for n, item in enumerate(NAV):
            if item is None:
                tk.Frame(side, bg=P["linie"], height=1).pack(fill="x", padx=22, pady=12)
                continue
            title, idx = item
            row = tk.Frame(side, bg=P["seite"])
            row.pack(fill="x")
            mark = tk.Frame(row, bg=P["seite"], width=4)
            mark.pack(side="left", fill="y")
            lbl = tk.Label(row, text=title, font=self.fonts["nav"], bg=P["seite"], fg=P["schiefer"], anchor="w",
                           padx=18, pady=7, takefocus=1, highlightthickness=1,
                           highlightbackground=P["seite"], highlightcolor=P["probe"])
            lbl.pack(side="left", fill="x", expand=True)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, idx=idx: self.nb.select(idx))
            for key in ("<Return>", "<space>"):
                lbl.bind(key, lambda e, idx=idx: self.nb.select(idx))
            self.nav_items[idx] = (row, mark, lbl)
        tk.Label(side, text="⌘1 – ⌘5 wechselt den Bereich", font=self.fonts["small"], bg=P["seite"],
                 fg=P["schiefer"]).pack(side="bottom", anchor="w", padx=22, pady=18)

    def _page_changed(self):
        cur = self.nb.index(self.nb.select())
        for idx, (row, mark, lbl) in self.nav_items.items():
            on = idx == cur
            bg = P["papier"] if on else P["seite"]
            row.configure(bg=bg)
            mark.configure(bg=P["graphit"] if on else bg)
            lbl.configure(bg=bg, fg=P["graphit"] if on else P["schiefer"], highlightbackground=bg)
        self._update_mode()

    # --- Bausteine -------------------------------------------------------------
    def _folder_row(self, parent, row, label, var, multi=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", pady=(9, 4), padx=(0, 16))
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=1, sticky="ew", pady=4)
        cell.columnconfigure(0, weight=1)
        ttk.Entry(cell, textvariable=var, width=10).grid(row=0, column=0, sticky="ew")
        hint = ttk.Label(cell, style="Hint.TLabel")
        hint.grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(parent, text="Wählen…", command=lambda: self._choose(var)).grid(
            row=row, column=2, sticky="nw", padx=(8, 0), pady=4)
        self._drive_hint(var, hint)

    def _drive_hint(self, var: tk.StringVar, label: ttk.Label):
        """Zeigt unter Pfaden auf /Volumes, ob das Laufwerk angeschlossen ist."""
        def update(*_):
            parts = Path(var.get().strip()).parts
            if len(parts) >= 3 and parts[1] == "Volumes":
                if Path("/Volumes", parts[2]).is_dir():
                    label.configure(text=f"Laufwerk „{parts[2]}“ ist angeschlossen", style="Hint.TLabel")
                else:
                    label.configure(text=f"Laufwerk „{parts[2]}“ ist nicht angeschlossen", style="Error.TLabel")
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

    def _pattern_row(self, parent, row, label, var, values):
        self._label(parent, row, label)
        cb = ttk.Combobox(parent, textvariable=var, values=values)
        cb.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="Platzhalter", command=self._show_placeholders).grid(
            row=row, column=2, sticky="w", padx=(8, 0))
        return cb

    def _show_placeholders(self):
        messagebox.showinfo("Platzhalter", "Platzhalter für Muster (/ trennt Ordner):\n\n" +
                            "\n".join(f"{{{k}}}  –  {v}" for k, v in PLACEHOLDERS.items()))

    def _header(self, parent, title, desc="", **grid):
        ttk.Label(parent, text=title, style="H1.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", **grid)
        if desc:
            ttk.Label(parent, text=desc, style="Hint.TLabel", wraplength=620).grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(2, 14), **grid)

    def _tab(self, title, desc):
        f = ttk.Frame(self.nb, padding=(32, 16, 32, 8))
        f.columnconfigure(1, weight=1)
        self.nb.add(f, text=title)
        self._header(f, title, desc)
        return f

    # --- Profile ---------------------------------------------------------------
    def _build_profile_bar(self):
        bar = ttk.Frame(self.main, padding=(32, 14, 32, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Löschen", command=self._profile_delete).pack(side="right")
        ttk.Button(bar, text="Speichern", command=self._profile_save).pack(side="right", padx=4)
        ttk.Button(bar, text="Laden", command=self._profile_load).pack(side="right")
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(bar, textvariable=self.profile_var, width=20, values=self.profiles.names())
        self.profile_cb.pack(side="right", padx=8)
        ttk.Label(bar, text="Profil").pack(side="right")

    def _current_job(self) -> str | None:
        idx = self.nb.index(self.nb.select())
        return JOBS[idx] if idx < 3 else None

    def _profile_save(self):
        job = self._current_job()
        name = self.profile_var.get().strip()
        if not job:
            messagebox.showinfo("Profil", "Bitte zuerst Abgleich, Duplikate oder Sortieren wählen.")
            return
        if not name:
            messagebox.showinfo("Profil", "Bitte einen Profilnamen eingeben.")
            return
        self.profiles.save(name, {"aufgabe": job, "parameter": params_to_dict(self._params(job))})
        self.profile_cb["values"] = self.profiles.names()
        self.log(f"Profil gespeichert: {name}")

    def _profile_load(self):
        name = self.profile_var.get().strip()
        try:
            prof = self.profiles.load(name)
        except (OSError, json.JSONDecodeError):
            messagebox.showerror("Profil", f"Profil nicht gefunden: {name}")
            return
        job = prof.get("aufgabe", "abgleich")
        self._apply_params(job, prof.get("parameter", {}))
        self.nb.select(JOBS.index(job))
        self.log(f"Profil geladen: {name}")

    def _profile_delete(self):
        name = self.profile_var.get().strip()
        if name and messagebox.askyesno("Profil löschen", f"Profil „{name}“ löschen?"):
            self.profiles.delete(name)
            self.profile_cb["values"] = self.profiles.names()
            self.profile_var.set("")

    # --- Abgleich ----------------------------------------------------------------
    def _build_sync_tab(self):
        f = self._tab("Abgleich", "Prüft, welche Dateien der Quellordner schon im Ziel liegen – per Inhalt, "
                                  "unabhängig vom Namen – und kopiert fehlende.")
        ttk.Label(f, text="Quellordner").grid(row=2, column=0, sticky="nw", pady=(4, 4), padx=(0, 16))
        self.sync_sources = tk.Listbox(f, height=4, width=10, selectmode="extended", bg=P["weiss"], fg=P["graphit"],
                                       relief="flat", highlightthickness=1, highlightbackground=P["linie"],
                                       highlightcolor=P["probe"], selectbackground=P["probe"],
                                       selectforeground=P["weiss"], activestyle="none")
        self.sync_sources.grid(row=2, column=1, sticky="nsew", pady=4)
        bf = ttk.Frame(f)
        bf.grid(row=2, column=2, sticky="n", padx=(8, 0), pady=4)
        ttk.Button(bf, text="Hinzufügen…", command=self._add_source).pack(fill="x")
        ttk.Button(bf, text="Entfernen", command=lambda: [self.sync_sources.delete(i) for i in
                                                          reversed(self.sync_sources.curselection())]
                   ).pack(fill="x", pady=4)
        self.sync_target = tk.StringVar()
        self._folder_row(f, 3, "Zielordner", self.sync_target)
        self.sync_copy = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Fehlende Dateien ins Ziel kopieren (sonst nur Bericht)",
                        variable=self.sync_copy).grid(row=4, column=1, sticky="w", pady=(16, 4))
        self._label(f, 5, "Ablage im Ziel")
        self.sync_ablage = ChoiceVar(f, CHOICES["abgleich.fehlende_ablage"], self.cfg.get("abgleich.fehlende_ablage"))
        self.sync_ablage.widget.grid(row=5, column=1, sticky="w", pady=4)
        self.sync_pattern = tk.StringVar(value=self.cfg.get("muster.standard"))
        pattern_cb = self._pattern_row(f, 6, "Ordner-Muster", self.sync_pattern, self.cfg.get("muster.vorlagen"))
        toggle = lambda *_: pattern_cb.configure(state="normal" if self.sync_ablage.get() == "muster" else "disabled")
        self.sync_ablage.var.trace_add("write", toggle)
        toggle()
        self.sync_fname = tk.StringVar(value=self.cfg.get("muster.dateiname"))
        self._label(f, 7, "Dateiname-Muster")
        ttk.Entry(f, textvariable=self.sync_fname).grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="leer = Originalname", style="Hint.TLabel").grid(row=7, column=2, sticky="w", padx=(8, 0))

    def _add_source(self):
        p = filedialog.askdirectory(initialdir=str(Path.home() / "Pictures"))
        if p and p not in self.sync_sources.get(0, "end"):
            self.sync_sources.insert("end", p)

    # --- Duplikate ---------------------------------------------------------------
    def _build_dup_tab(self):
        f = self._tab("Duplikate", "Findet inhaltsgleiche Dateien in einem Ordner und allen Unterordnern.")
        self.dup_folder = tk.StringVar()
        self._folder_row(f, 2, "Ordner", self.dup_folder)
        self._label(f, 3, "Behalten wird", top=16)
        self.dup_keep = ChoiceVar(f, CHOICES["duplikate.behalten"], self.cfg.get("duplikate.behalten"))
        self.dup_keep.widget.grid(row=3, column=1, sticky="w", pady=(16, 4))
        self._label(f, 4, "Mit Duplikaten")
        self.dup_action = ChoiceVar(f, CHOICES["duplikate.aktion"], self.cfg.get("duplikate.aktion"))
        self.dup_action.widget.grid(row=4, column=1, sticky="w", pady=4)
        self._label(f, 5, "Duplikat-Ordner")
        self.dup_dir = tk.StringVar(value=self.cfg.get("duplikate.ordner"))
        ttk.Entry(f, textvariable=self.dup_dir, width=30).grid(row=5, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Dateien in Foto-Bibliotheken (Apple Fotos, Lightroom) werden nie verschoben "
                          "oder gelöscht – dort wird bevorzugt das Original behalten.",
                  style="Hint.TLabel", wraplength=560).grid(row=6, column=1, columnspan=2, sticky="w", pady=(14, 0))

    # --- Sortieren ---------------------------------------------------------------
    def _build_sort_tab(self):
        f = self._tab("Sortieren", "Legt Dateien anhand ihrer Metadaten – Aufnahmedatum, Kamera, Objektiv – "
                                   "in eine neue Ordnerstruktur.")
        self.sort_src = tk.StringVar()
        self._folder_row(f, 2, "Quellordner", self.sort_src)
        self.sort_dst = tk.StringVar()
        self._folder_row(f, 3, "Zielordner", self.sort_dst)
        self.sort_pattern = tk.StringVar(value=self.cfg.get("muster.standard"))
        self._pattern_row(f, 4, "Ordner-Muster", self.sort_pattern, self.cfg.get("muster.vorlagen"))
        self.sort_fname = tk.StringVar(value=self.cfg.get("muster.dateiname"))
        self._label(f, 5, "Dateiname-Muster")
        ttk.Entry(f, textvariable=self.sort_fname).grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="leer = Originalname", style="Hint.TLabel").grid(row=5, column=2, sticky="w", padx=(8, 0))
        self._label(f, 6, "Dateien")
        self.sort_mode = ChoiceVar(f, ["copy", "move"], "copy")
        self.sort_mode.widget.grid(row=6, column=1, sticky="w", pady=4)
        # Beispiel-Pfad: zeigt live, wie das Muster aussieht
        ex = tk.Frame(f, bg=P["seite"])
        ex.grid(row=7, column=1, columnspan=2, sticky="ew", pady=(16, 0))
        tk.Label(ex, text="So sieht es aus", font=self.fonts["small"], bg=P["seite"], fg=P["schiefer"]
                 ).pack(anchor="w", padx=12, pady=(8, 0))
        self.sort_example = tk.Label(ex, font=self.fonts["mono"], bg=P["seite"], fg=P["graphit"], anchor="w",
                                     justify="left", wraplength=540)
        self.sort_example.pack(fill="x", padx=12, pady=(2, 10))
        self.sort_pattern.trace_add("write", lambda *_: self._update_example())
        self.sort_fname.trace_add("write", lambda *_: self._update_example())
        self._update_example()

    def _update_example(self):
        from datetime import datetime
        from .metadata import Meta
        from .patterns import PatternRenderer
        from .scan import FileEntry
        e = FileEntry(Path("/Quelle/Urlaub/IMG_4711.CR3"), 0, 0, Path("/Quelle"))
        m = Meta(datetime(2025, 8, 9, 14, 5, 59), "DateTimeOriginal", "Canon EOS R6m2", "Canon", "RF24-105mm F4 L IS USM")
        try:
            r = PatternRenderer(self.cfg)
            path = r.folder(self.sort_pattern.get(), e, m) / r.filename(self.sort_fname.get(), e, m)
            self.sort_example.configure(text=f"IMG_4711.CR3  →  Ziel/{path}", fg=P["graphit"])
        except (PatternError, KeyError, ValueError, IndexError) as err:
            self.sort_example.configure(text=str(err).splitlines()[0], fg=P["fehler"])

    # --- Einstellungen (automatisch aus defaults.toml erzeugt) -------------------
    def _build_settings_tab(self):
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text="Einstellungen")
        top = ttk.Frame(outer, padding=(32, 22, 32, 8))
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="Einstellungen", style="H1.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, text=f"Gespeichert in {self.cfg.user_file}", style="Hint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 12))
        bf = ttk.Frame(top)
        bf.grid(row=2, column=0, sticky="w")
        ttk.Button(bf, text="Speichern", command=self._settings_save).pack(side="left")
        ttk.Button(bf, text="Auf Standard zurücksetzen", command=self._settings_reset).pack(side="left", padx=6)
        ttk.Button(bf, text="Config-Datei öffnen", command=lambda: (self.cfg.save(), open_path(self.cfg.user_file))
                   ).pack(side="left")

        canvas = tk.Canvas(outer, highlightthickness=0, bg=P["papier"])
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.settings_frame = ttk.Frame(canvas, padding=(32, 4, 32, 24))
        self.settings_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=self.settings_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta), "units")
                        if self.nb.index(self.nb.select()) == 3 else None)
        self.helps = parse_comments(DEFAULTS_FILE)
        self._fill_settings()

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
            ttk.Label(f, text=SECTION_TITLES.get(section, section), style="H2.TLabel"
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(22, 6))
            row += 1
            for key, default in values.items():
                path = f"{section}.{key}"
                current = self.cfg.get(path, default)
                ttk.Label(f, text=key.replace("_", " ")).grid(row=row, column=0, sticky="nw", padx=(0, 16), pady=(7, 3))
                getter, widget = self._setting_widget(f, path, default, current)
                widget.grid(row=row, column=1, sticky="ew" if not isinstance(widget, ttk.Checkbutton) else "w",
                            pady=3)
                self.setting_getters[path] = getter
                row += 1
                if help_text := self.helps.get(path):
                    ttk.Label(f, text=help_text, style="Hint.TLabel", wraplength=620
                              ).grid(row=row, column=1, sticky="w", pady=(0, 4))
                    row += 1

    def _setting_widget(self, parent, path, default, current):
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
                    raise ValueError(f"{path}: Zahl erwartet")
            return get_num, ttk.Entry(parent, textvariable=var, width=12)
        if isinstance(default, str):
            var = tk.StringVar(value=current)
            return var.get, ttk.Entry(parent, textvariable=var)
        # Listen/Tabellen: als Text, eine Zeile pro Eintrag
        if isinstance(default, list) and (not default or not isinstance(default[0], dict)):
            text = "\n".join(map(str, current))
            parse = lambda s: [l.strip() for l in s.splitlines() if l.strip()]
        elif isinstance(default, list):  # Liste von Tabellen (Bibliotheken)
            text = "\n".join(f"{d.get('endung', '')}: {', '.join(d.get('unterordner', []))}" for d in current)
            parse = lambda s: [{"endung": a.strip(), "unterordner": [x.strip() for x in b.split(",") if x.strip()]}
                               for a, _, b in (l.partition(":") for l in s.splitlines() if l.strip())]
        else:  # dict (z.B. Kameranamen)
            text = "\n".join(f"{k} = {v}" for k, v in current.items())
            parse = lambda s: {a.strip(): b.strip() for a, _, b in (l.partition("=") for l in s.splitlines() if "=" in l)}
        box = text_box(parent, height=min(8, max(2, text.count("\n") + 2)), width=1, wrap="none", font=self.fonts["mono"])
        box.insert("1.0", text)
        return (lambda box=box, parse=parse: parse(box.get("1.0", "end"))), box

    def _settings_save(self):
        try:
            values = {p: g() for p, g in self.setting_getters.items()}
            for p in ("muster.standard", "muster.dateiname"):
                if values.get(p):
                    validate(values[p])
            for v in values.get("muster.vorlagen", []):
                validate(v)
        except (ValueError, PatternError) as e:
            messagebox.showerror("Einstellungen", str(e))
            return
        for p, v in values.items():
            self.cfg.set(p, v)
        self.cfg.save()
        self.cfg.reload()
        self.log("Einstellungen gespeichert." + ("\n" + "\n".join(self.cfg.warnings) if self.cfg.warnings else ""))
        self._update_example()

    def _settings_reset(self):
        if messagebox.askyesno("Zurücksetzen", "Alle Einstellungen auf Standard zurücksetzen?"):
            self.cfg.reset()
            self._fill_settings()
            self.log("Einstellungen zurückgesetzt.")

    # --- Verlauf / Rückgängig ----------------------------------------------------
    def _build_history_tab(self):
        f = ttk.Frame(self.nb, padding=(32, 16, 32, 8))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        self.nb.add(f, text="Verlauf")
        self._header(f, "Verlauf", "Jeder echte Lauf wird protokolliert und kann rückgängig gemacht werden: "
                                   "Kopien werden entfernt, Verschobenes zurückgelegt.")
        self.hist = tk.Listbox(f, height=14, bg=P["weiss"], fg=P["graphit"], relief="flat", highlightthickness=1,
                               highlightbackground=P["linie"], highlightcolor=P["probe"], activestyle="none",
                               selectbackground=P["probe"], selectforeground=P["weiss"], font=self.fonts["mono"])
        self.hist.grid(row=2, column=0, sticky="nsew")
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(bf, text="Rückgängig machen", command=self._hist_undo).pack(side="left")
        ttk.Button(bf, text="Aktualisieren", command=self._hist_refresh).pack(side="left", padx=6)
        ttk.Button(bf, text="Logdatei", command=lambda: open_path(self.cfg.data_dir / "logs" / "fototool.log")
                   ).pack(side="right")
        ttk.Button(bf, text="Berichte-Ordner", command=lambda: open_path(self.cfg.data_dir / "berichte")
                   ).pack(side="right", padx=6)
        self._hist_refresh()

    def _hist_refresh(self):
        self.hist_files = list_journals(self.cfg.data_dir)
        self.hist.delete(0, "end")
        for p in self.hist_files:
            n = sum(1 for e in read_entries(p) if e.get("aktion") not in ("lauf_rueckgaengig", "ordner_angelegt"))
            self.hist.insert("end", f"  {p.stem}   ({n} Aktionen)")
        if not self.hist_files:
            self.hist.insert("end", "  Noch keine echten Läufe – Probeläufe erscheinen hier nicht.")

    def _hist_undo(self):
        sel = self.hist.curselection()
        if not sel or sel[0] >= len(self.hist_files):
            return
        file = self.hist_files[sel[0]]
        if "_RUECKGAENGIG" in file.name:
            messagebox.showinfo("Rückgängig", "Dieser Lauf wurde schon rückgängig gemacht.")
            return
        if not messagebox.askyesno("Rückgängig", f"Lauf „{file.stem}“ rückgängig machen?"):
            return

        def work(ctx):
            done, problems = undo(file, ctx)
            ctx.message(f"Rückgängig: {done} Dateien." + "".join(f"\n  ⚠️ {p}" for p in problems))
            return None
        self._start(work)

    # --- Unten: Modus, Start, Fortschritt, Log -------------------------------------
    def _build_bottom(self):
        f = ttk.Frame(self.main, padding=(32, 4, 32, 16))
        f.pack(side="bottom", fill="x")
        f.columnconfigure(0, weight=1)
        self.run_frame = f

        # Modus-Leiste: blau = Probelauf, Bernstein = echter Lauf
        self.banner = tk.Frame(f)
        self.banner.grid(row=0, column=0, sticky="ew")
        self.banner_mark = tk.Frame(self.banner, width=5)
        self.banner_mark.pack(side="left", fill="y")
        inner = tk.Frame(self.banner)
        inner.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        inner.columnconfigure(1, weight=1)
        self.dry_var = tk.BooleanVar(value=self.cfg.get("aktionen.probelauf", True))
        modes = tk.Frame(inner)
        modes.grid(row=0, column=0, sticky="w")
        for text, val in (("Probelauf", True), ("Echt ausführen", False)):
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
        self.report_btn = ttk.Button(btns, text="Bericht öffnen", state="disabled", command=self._open_report)
        self.report_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Abbrechen", state="disabled",
                                     command=lambda: self.ctx and self.ctx.cancel())
        self.cancel_btn.pack(side="left", padx=6)
        self.start_btn = ttk.Button(btns, text="Start", command=self._run, style="Probe.TButton")
        self.start_btn.pack(side="left")

        self.prog = ttk.Progressbar(f, mode="determinate", style="Run.Horizontal.TProgressbar")
        self.prog.grid(row=1, column=0, sticky="ew", pady=(12, 2))
        self.prog_label = ttk.Label(f, text="Bereit", style="Hint.TLabel")
        self.prog_label.grid(row=2, column=0, sticky="w")
        self.logbox = text_box(f, height=5, width=1, wrap="word", font=self.fonts["mono"], state="disabled")
        self.logbox.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.logbox.tag_configure("fehler", foreground=P["fehler"])
        self.logbox.tag_configure("kopf", font=(self.fonts["mono"][0], self.fonts["mono"][1], "bold"))

        for var in (self.dry_var, self.sync_copy, self.dup_action.var, self.dup_dir, self.sort_mode.var):
            var.trace_add("write", lambda *_: self._update_mode())

    def _mode_info(self) -> tuple[str, str, str, str]:
        """(Art, Knopftext, Titel, Erklärung) für den aktuellen Bereich und Modus."""
        job = self._current_job()
        if not job:
            return "aus", "Start", "", ""
        if self.dry_var.get():
            return "probe", "Probelauf starten", "Probelauf", "Nichts wird verändert – nur Vorschau und Bericht."
        if job == "abgleich" and not self.sync_copy.get() or job == "duplikate" and self.dup_action.get() == "bericht":
            return "probe", "Bericht erstellen", "Nur Bericht", "Mit diesen Eingaben werden keine Dateien verändert."
        undo_hint = " Rückgängig machen über „Verlauf“."
        if job == "abgleich":
            return "echt", "Fehlende kopieren", "Echter Lauf", "Fehlende Dateien werden ins Ziel kopiert." + undo_hint
        if job == "duplikate":
            if self.dup_action.get() == "papierkorb":
                return "echt", "In Papierkorb legen", "Echter Lauf", "Duplikate werden in den Papierkorb gelegt."
            return "echt", "Duplikate verschieben", "Echter Lauf", \
                f"Duplikate werden in „{self.dup_dir.get() or '_Duplikate'}“ verschoben." + undo_hint
        if self.sort_mode.get() == "move":
            return "echt", "Verschieben", "Echter Lauf", "Dateien werden aus dem Quellordner verschoben." + undo_hint
        return "echt", "Kopieren", "Echter Lauf", "Dateien werden sortiert ins Ziel kopiert." + undo_hint

    def _update_mode(self):
        if not hasattr(self, "banner"):
            return
        kind, button, title, text = self._mode_info()
        if kind == "aus":
            self.banner.grid_remove()
            return
        self.banner.grid()
        echt = kind == "echt"
        accent, light = (P["echt"], P["echt_hell"]) if echt else (P["probe"], P["probe_hell"])
        for w in self.banner_parts:
            w.configure(bg=light)
        self.banner_mark.configure(bg=accent)
        self.banner_title.configure(text=title, fg=P["graphit"])
        self.banner_text.configure(text=text, fg=P["graphit"])
        s = ttk.Style(self.root)
        s.configure("Mode.Toolbutton", background=light, foreground=P["schiefer"], bordercolor=accent,
                    lightcolor=light, darkcolor=light)
        on_fg = P["graphit"] if self.dry_var.get() is False else P["weiss"]
        sel = P["echt"] if not self.dry_var.get() else P["probe"]
        s.map("Mode.Toolbutton", background=[("selected", sel), ("active", P["weiss"])],
              lightcolor=[("selected", sel)], darkcolor=[("selected", sel)],
              foreground=[("selected", on_fg)])
        s.configure("Run.Horizontal.TProgressbar", background=accent, lightcolor=accent, darkcolor=accent)
        self.start_btn.configure(text=button, style="Echt.TButton" if echt else "Probe.TButton")

    def log(self, text: str):
        self.logbox.configure(state="normal")
        for line in text.split("\n"):
            tag = "fehler" if ("❌" in line or "⚠️" in line) else "kopf" if line.startswith("===") else ()
            self.logbox.insert("end", line + "\n", tag)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _open_report(self):
        if self.last_result and self.last_result.reports:
            html = [r for r in self.last_result.reports if r.suffix == ".html"]
            open_path((html or self.last_result.reports)[0])

    # --- Parameter <-> Oberfläche ------------------------------------------------
    def _params(self, job: str):
        dry = self.dry_var.get()
        if job == "abgleich":
            return SyncParams(list(self.sync_sources.get(0, "end")), self.sync_target.get().strip(),
                              self.sync_copy.get(), self.sync_ablage.get(), self.sync_pattern.get().strip(),
                              self.sync_fname.get().strip(), dry)
        if job == "duplikate":
            return DupParams(self.dup_folder.get().strip(), self.dup_keep.get(), self.dup_action.get(),
                             self.dup_dir.get().strip() or "_Duplikate", dry)
        return SortParams(self.sort_src.get().strip(), self.sort_dst.get().strip(), self.sort_pattern.get().strip(),
                          self.sort_fname.get().strip(), self.sort_mode.get(), dry)

    def _apply_params(self, job: str, d: dict):
        p = params_from_dict(job, {**params_to_dict(self._params(job)), **d})
        if job == "abgleich":
            self.sync_sources.delete(0, "end")
            for s in p.quellen:
                self.sync_sources.insert("end", s)
            self.sync_target.set(p.ziel)
            self.sync_copy.set(p.fehlende_kopieren)
            self.sync_ablage.set(p.ablage)
            self.sync_pattern.set(p.muster)
            self.sync_fname.set(p.dateiname)
        elif job == "duplikate":
            self.dup_folder.set(p.ordner)
            self.dup_keep.set(p.behalten)
            self.dup_action.set(p.aktion)
            self.dup_dir.set(p.duplikat_ordner)
        else:
            self.sort_src.set(p.quelle)
            self.sort_dst.set(p.ziel)
            self.sort_pattern.set(p.muster)
            self.sort_fname.set(p.dateiname)
            self.sort_mode.set(p.modus)

    def _save_state(self):
        try:
            state = {"tab": self.nb.index(self.nb.select()), "probelauf": self.dry_var.get(),
                     **{j: params_to_dict(self._params(j)) for j in JOBS}}
            self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, tk.TclError):
            pass

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
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

    # --- Ausführen ------------------------------------------------------------------
    def _validate(self, job: str, p) -> str | None:
        if job == "abgleich":
            if not p.quellen:
                return "Bitte mindestens einen Quellordner hinzufügen."
            if not p.ziel:
                return "Bitte einen Zielordner wählen."
            if p.ablage == "muster":
                validate(p.muster)
        elif job == "duplikate":
            if not p.ordner:
                return "Bitte einen Ordner wählen."
        else:
            if not p.quelle or not p.ziel:
                return "Bitte Quell- und Zielordner wählen."
            validate(p.muster)
        if p.__dict__.get("dateiname"):
            validate(p.dateiname)
        return None

    def _confirm(self, job: str, p) -> bool:
        """Rückfragen vor verändernden Aktionen. Setzt p.bestaetigt."""
        if p.probelauf:
            return True
        ask = self.cfg.get("aktionen.bestaetigen", True)
        overwrite = self.cfg.get("aktionen.kollision") == "ueberschreiben"
        msgs, warnings = [], []
        if job == "abgleich" and p.fehlende_kopieren:
            msgs.append(f"Fehlende Dateien werden nach\n{p.ziel}\nkopiert.")
        if job == "duplikate" and p.aktion == "verschieben":
            msgs.append(f"Duplikate werden nach „{p.duplikat_ordner}“ verschoben.")
        if job == "duplikate" and p.aktion == "papierkorb":
            msgs.append("Duplikate werden in den Papierkorb gelegt.")
        if job == "sortieren":
            msgs.append(f"Dateien werden nach\n{p.ziel}\n{'VERSCHOBEN' if p.modus == 'move' else 'kopiert'}.")
        if job == "duplikate" and p.aktion != "bericht" and self.cfg.get("vergleich.methode", "hash") != "hash":
            warnings.append("ACHTUNG: Vergleich nur nach Name/Größe – verschiedene Bilder können als "
                            "Duplikat gelten. Sicher ist nur die Methode „hash“.")
        if overwrite and job != "duplikate":
            warnings.append("ACHTUNG: Kollisionsstrategie „überschreiben“ ist aktiv.")
        moving_from = p.quelle if job == "sortieren" and p.modus == "move" else (
            p.ordner if job == "duplikate" and p.aktion != "bericht" else None)
        if moving_from:
            cats = affected_catalogs(self.cfg, Path(moving_from))
            if cats:
                warnings.append("Lightroom-Katalog betroffen – verschobene Dateien müssen dort neu verknüpft "
                            "werden:\n" + "\n".join(cats))
        if warnings or (msgs and ask):
            if not messagebox.askyesno("Wirklich ausführen?", "\n\n".join(msgs + warnings) + "\n\nFortfahren?"):
                return False
        p.bestaetigt = True
        return True

    def _run(self):
        job = self._current_job()
        if not job:
            messagebox.showinfo("Start", "Bitte Abgleich, Duplikate oder Sortieren wählen.")
            return
        p = self._params(job)
        try:
            err = self._validate(job, p)
        except PatternError as e:
            err = str(e)
        if err:
            messagebox.showwarning("Eingaben", err)
            return
        if not self._confirm(job, p):
            return
        self._save_state()
        self.log(f"\n=== {job.capitalize()} {'(Probelauf)' if p.probelauf else ''} ===")
        self._start(lambda ctx: run_job(self.cfg, ctx, job, p))

    def _start(self, work):
        if self.worker and self.worker.is_alive():
            return
        self.ctx = RunContext(on_message=lambda m: self.q.put(("msg", m)),
                              on_progress=lambda ph, d, t: self.q.put(("prog", (ph, d, t))))
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        ctx = self.ctx

        def target():
            try:
                self.q.put(("done", work(ctx)))
            except Cancelled:
                self.q.put(("msg", "Abgebrochen."))
                self.q.put(("done", None))
            except (OSError, ValueError) as e:  # Zugriff fehlt, Ordner fehlt, Muster falsch
                self.q.put(("msg", f"❌ {e}"))
                self.q.put(("alert", str(e)))
                self.q.put(("done", None))
            except Exception as e:  # noqa: BLE001 – alles anzeigen statt abstürzen
                import logging
                logging.getLogger("fototool").exception("Fehler im Lauf")
                self.q.put(("msg", f"❌ Fehler: {e}"))
                self.q.put(("done", None))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "msg":
                    self.log(data)
                elif kind == "prog":
                    phase, done, total = data
                    if total:
                        self.prog.configure(mode="determinate", maximum=total, value=done)
                        self.prog_label.configure(text=f"{phase}: {done} / {total}{self._eta(phase, done, total)}")
                    else:
                        self.prog.configure(mode="indeterminate")
                        self.prog.step(5)
                        self.prog_label.configure(text=f"{phase}: {done} Dateien")
                elif kind == "alert":
                    messagebox.showerror("FotoTool", data)
                elif kind == "done":
                    self._finished(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _eta(self, phase: str, done: int, total: int) -> str:
        """Restzeit-Schätzung je Phase."""
        import time
        now = time.monotonic()
        if getattr(self, "_eta_phase", None) != phase or done < getattr(self, "_eta_done0", 0):
            self._eta_phase, self._eta_t0, self._eta_done0 = phase, now, done
            return ""
        elapsed, step = now - self._eta_t0, done - self._eta_done0
        if elapsed < 5 or step <= 0:
            return ""
        rest = elapsed / step * (total - done)
        text = f"{rest / 60:.0f} min" if rest >= 90 else f"{rest:.0f} s"
        return f"  –  noch ca. {text}"

    def _finished(self, res: JobResult | None):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.prog.configure(mode="determinate", value=0)
        self.prog_label.configure(text="Fertig")
        self._hist_refresh()
        if res is None:
            return
        self.last_result = res
        if res.cancelled:
            self.log("Abgebrochen – Teilbericht:")
        self.log("\n".join(f"  {k}: {v}" for k, v in res.summary.items()))
        for r in res.reports:
            self.log(f"  Bericht: {r}")
        self.report_btn.configure(state="normal" if res.reports else "disabled")

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Beenden", "Ein Lauf ist aktiv. Abbrechen und beenden?"):
                return
            self.ctx.cancel()
        self._save_state()
        self.root.destroy()


def run_gui(cfg: Config) -> None:
    root = tk.Tk()
    App(root, cfg)
    root.mainloop()
