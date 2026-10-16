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
    "duplikate.behalten": ["aeltestes", "neuestes", "kuerzester_pfad", "laengster_pfad", "erster"],
    "duplikate.aktion": ["bericht", "verschieben", "papierkorb"],
}
LABELS = {
    "aeltestes": "ältestes", "neuestes": "neuestes", "kuerzester_pfad": "kürzester Pfad",
    "laengster_pfad": "längster Pfad", "erster": "erster (alphabetisch)",
    "bericht": "nur Bericht", "verschieben": "in Duplikat-Ordner verschieben", "papierkorb": "in Papierkorb",
    "struktur": "Ordnerstruktur beibehalten", "muster": "nach Muster sortieren",
    "copy": "kopieren", "move": "verschieben",
}
SECTION_TITLES = {
    "dateien": "Dateitypen", "scan": "Durchsuchen", "vergleich": "Vergleich", "metadaten": "Metadaten",
    "muster": "Muster", "aktionen": "Aktionen", "abgleich": "Abgleich", "duplikate": "Duplikate",
    "berichte": "Berichte", "pfade": "Pfade", "oberflaeche": "Oberfläche",
}


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

        root.title(f"FotoTool {__version__}")
        root.geometry(cfg.get("oberflaeche.fenster", "1000x720"))
        size = int(cfg.get("oberflaeche.schriftgroesse", 13))
        for f in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tk.font.nametofont(f).configure(size=size)
            except (tk.TclError, AttributeError):
                pass

        self._build_profile_bar()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self._build_sync_tab()
        self._build_dup_tab()
        self._build_sort_tab()
        self._build_settings_tab()
        self._build_history_tab()
        self._build_bottom()
        self._load_state()
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        hints = check(cfg)
        if hints:
            self.log("Hinweise:\n" + "\n".join("• " + h for h in hints))

    # --- Bausteine -------------------------------------------------------------
    def _folder_row(self, parent, row, label, var, multi=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="nw", pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="Wählen…", command=lambda: self._choose(var)).grid(row=row, column=2, padx=4)

    def _choose(self, var: tk.StringVar):
        p = filedialog.askdirectory(initialdir=var.get() or str(Path.home()), mustexist=False)
        if p:
            var.set(p)

    def _pattern_row(self, parent, row, label, var, values):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        cb = ttk.Combobox(parent, textvariable=var, values=values)
        cb.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="?", width=2, command=self._show_placeholders).grid(row=row, column=2, padx=4)
        return cb

    def _show_placeholders(self):
        messagebox.showinfo("Platzhalter", "Platzhalter für Muster (/ trennt Ordner):\n\n" +
                            "\n".join(f"{{{k}}}  –  {v}" for k, v in PLACEHOLDERS.items()))

    def _tab(self, title):
        f = ttk.Frame(self.nb, padding=12)
        f.columnconfigure(1, weight=1)
        self.nb.add(f, text=title)
        return f

    # --- Profile ---------------------------------------------------------------
    def _build_profile_bar(self):
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="Profil:").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(bar, textvariable=self.profile_var, width=36, values=self.profiles.names())
        self.profile_cb.pack(side="left", padx=6)
        ttk.Button(bar, text="Laden", command=self._profile_load).pack(side="left")
        ttk.Button(bar, text="Speichern", command=self._profile_save).pack(side="left", padx=4)
        ttk.Button(bar, text="Löschen", command=self._profile_delete).pack(side="left")
        ttk.Label(bar, text="Speichert die Eingaben des aktuellen Tabs unter einem Namen",
                  foreground="gray").pack(side="left", padx=10)

    def _current_job(self) -> str | None:
        idx = self.nb.index(self.nb.select())
        return ["abgleich", "duplikate", "sortieren"][idx] if idx < 3 else None

    def _profile_save(self):
        job = self._current_job()
        name = self.profile_var.get().strip()
        if not job:
            messagebox.showinfo("Profil", "Bitte zuerst den Tab Abgleich, Duplikate oder Sortieren wählen.")
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
        self.nb.select(["abgleich", "duplikate", "sortieren"].index(job))
        self.log(f"Profil geladen: {name}")

    def _profile_delete(self):
        name = self.profile_var.get().strip()
        if name and messagebox.askyesno("Profil löschen", f"Profil „{name}“ löschen?"):
            self.profiles.delete(name)
            self.profile_cb["values"] = self.profiles.names()
            self.profile_var.set("")

    # --- Tab 1: Abgleich ---------------------------------------------------------
    def _build_sync_tab(self):
        f = self._tab("1  Abgleich")
        ttk.Label(f, text="Prüft, welche Dateien der Quellordner schon im Ziel liegen (per Inhalt, "
                          "unabhängig vom Namen), und kopiert fehlende.", wraplength=800
                  ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(f, text="Quellordner:").grid(row=1, column=0, sticky="nw", pady=4)
        lf = ttk.Frame(f)
        lf.grid(row=1, column=1, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        self.sync_sources = tk.Listbox(lf, height=6, selectmode="extended")
        self.sync_sources.grid(row=0, column=0, sticky="nsew")
        bf = ttk.Frame(f)
        bf.grid(row=1, column=2, sticky="n", padx=4)
        ttk.Button(bf, text="Hinzufügen…", command=self._add_source).pack(fill="x")
        ttk.Button(bf, text="Entfernen", command=lambda: [self.sync_sources.delete(i) for i in
                                                          reversed(self.sync_sources.curselection())]
                   ).pack(fill="x", pady=4)
        self.sync_target = tk.StringVar()
        self._folder_row(f, 2, "Zielordner:", self.sync_target)
        self.sync_copy = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Fehlende Dateien ins Ziel kopieren (sonst nur Bericht)",
                        variable=self.sync_copy).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Ablage im Ziel:").grid(row=4, column=0, sticky="w")
        self.sync_ablage = ChoiceVar(f, CHOICES["abgleich.fehlende_ablage"], self.cfg.get("abgleich.fehlende_ablage"))
        self.sync_ablage.widget.grid(row=4, column=1, sticky="w", pady=4)
        self.sync_pattern = tk.StringVar(value=self.cfg.get("muster.standard"))
        self._pattern_row(f, 5, "Muster (bei Sortierung):", self.sync_pattern, self.cfg.get("muster.vorlagen"))
        self.sync_fname = tk.StringVar(value=self.cfg.get("muster.dateiname"))
        ttk.Label(f, text="Dateiname-Muster:").grid(row=6, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.sync_fname).grid(row=6, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="leer = Originalname", foreground="gray").grid(row=6, column=2, sticky="w")

    def _add_source(self):
        p = filedialog.askdirectory(initialdir=str(Path.home() / "Pictures"))
        if p and p not in self.sync_sources.get(0, "end"):
            self.sync_sources.insert("end", p)

    # --- Tab 2: Duplikate --------------------------------------------------------
    def _build_dup_tab(self):
        f = self._tab("2  Duplikate")
        ttk.Label(f, text="Findet inhaltsgleiche Dateien in einem Ordner und allen Unterordnern.",
                  wraplength=800).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.dup_folder = tk.StringVar()
        self._folder_row(f, 1, "Ordner:", self.dup_folder)
        ttk.Label(f, text="Behalten wird:").grid(row=2, column=0, sticky="w")
        self.dup_keep = ChoiceVar(f, CHOICES["duplikate.behalten"], self.cfg.get("duplikate.behalten"))
        self.dup_keep.widget.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Mit Duplikaten:").grid(row=3, column=0, sticky="w")
        self.dup_action = ChoiceVar(f, CHOICES["duplikate.aktion"], self.cfg.get("duplikate.aktion"))
        self.dup_action.widget.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Duplikat-Ordner:").grid(row=4, column=0, sticky="w")
        self.dup_dir = tk.StringVar(value=self.cfg.get("duplikate.ordner"))
        ttk.Entry(f, textvariable=self.dup_dir, width=30).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Label(f, text="Dateien in Foto-Bibliotheken (Apple Fotos, Lightroom) werden nie verschoben "
                          "oder gelöscht – dort wird bevorzugt das Original behalten.",
                  foreground="gray", wraplength=800).grid(row=5, column=0, columnspan=3, sticky="w", pady=8)

    # --- Tab 3: Sortieren --------------------------------------------------------
    def _build_sort_tab(self):
        f = self._tab("3  Sortieren")
        ttk.Label(f, text="Legt Dateien anhand ihrer Metadaten (Aufnahmedatum, Kamera, …) in eine neue "
                          "Ordnerstruktur.", wraplength=800).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.sort_src = tk.StringVar()
        self._folder_row(f, 1, "Quellordner:", self.sort_src)
        self.sort_dst = tk.StringVar()
        self._folder_row(f, 2, "Zielordner:", self.sort_dst)
        self.sort_pattern = tk.StringVar(value=self.cfg.get("muster.standard"))
        self._pattern_row(f, 3, "Ordner-Muster:", self.sort_pattern, self.cfg.get("muster.vorlagen"))
        self.sort_fname = tk.StringVar(value=self.cfg.get("muster.dateiname"))
        ttk.Label(f, text="Dateiname-Muster:").grid(row=4, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.sort_fname).grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="leer = Originalname", foreground="gray").grid(row=4, column=2, sticky="w")
        ttk.Label(f, text="Aktion:").grid(row=5, column=0, sticky="w")
        self.sort_mode = ChoiceVar(f, ["copy", "move"], "copy")
        self.sort_mode.widget.grid(row=5, column=1, sticky="w", pady=4)
        self.sort_example = ttk.Label(f, foreground="gray")
        self.sort_example.grid(row=6, column=1, sticky="w", pady=6)
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
            self.sort_example.configure(text=f"Beispiel: …/{path}", foreground="gray")
        except (PatternError, KeyError, ValueError, IndexError) as err:
            self.sort_example.configure(text=str(err).splitlines()[0], foreground="red")

    # --- Tab Einstellungen (automatisch aus defaults.toml erzeugt) ---------------
    def _build_settings_tab(self):
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text="Einstellungen")
        top = ttk.Frame(outer, padding=(12, 8))
        top.pack(fill="x")
        ttk.Button(top, text="Speichern", command=self._settings_save).pack(side="left")
        ttk.Button(top, text="Auf Standard zurücksetzen", command=self._settings_reset).pack(side="left", padx=6)
        ttk.Button(top, text="Config-Datei öffnen", command=lambda: (self.cfg.save(), open_path(self.cfg.user_file))
                   ).pack(side="left")
        ttk.Label(top, text=str(self.cfg.user_file), foreground="gray").pack(side="left", padx=10)

        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.settings_frame = ttk.Frame(canvas, padding=12)
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
            ttk.Label(f, text=SECTION_TITLES.get(section, section), font=("TkDefaultFont", 15, "bold")
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(14, 4))
            row += 1
            for key, default in values.items():
                path = f"{section}.{key}"
                current = self.cfg.get(path, default)
                ttk.Label(f, text=key.replace("_", " ")).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=3)
                getter, widget = self._setting_widget(f, path, default, current)
                widget.grid(row=row, column=1, sticky="ew", pady=3)
                self.setting_getters[path] = getter
                row += 1
                if help_text := self.helps.get(path):
                    ttk.Label(f, text=help_text, foreground="gray", wraplength=700
                              ).grid(row=row, column=1, sticky="w")
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
        box = tk.Text(parent, height=min(8, max(2, text.count("\n") + 2)), wrap="none", font=("Menlo", 12))
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

    # --- Tab Verlauf / Rückgängig ----------------------------------------------
    def _build_history_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="Verlauf")
        ttk.Label(f, text="Jeder echte Lauf wird protokolliert und kann rückgängig gemacht werden "
                          "(Kopien entfernen, Verschobenes zurücklegen).", wraplength=800).pack(anchor="w")
        self.hist = tk.Listbox(f, height=14)
        self.hist.pack(fill="both", expand=True, pady=8)
        bf = ttk.Frame(f)
        bf.pack(fill="x")
        ttk.Button(bf, text="Aktualisieren", command=self._hist_refresh).pack(side="left")
        ttk.Button(bf, text="Rückgängig machen", command=self._hist_undo).pack(side="left", padx=6)
        ttk.Button(bf, text="Berichte-Ordner", command=lambda: open_path(self.cfg.data_dir / "berichte")
                   ).pack(side="left")
        ttk.Button(bf, text="Logdatei", command=lambda: open_path(self.cfg.data_dir / "logs" / "fototool.log")
                   ).pack(side="left", padx=6)
        self._hist_refresh()

    def _hist_refresh(self):
        self.hist_files = list_journals(self.cfg.data_dir)
        self.hist.delete(0, "end")
        for p in self.hist_files:
            n = sum(1 for e in read_entries(p) if e.get("aktion") != "lauf_rueckgaengig")
            self.hist.insert("end", f"{p.stem}   ({n} Aktionen)")

    def _hist_undo(self):
        sel = self.hist.curselection()
        if not sel:
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

    # --- Unten: Start, Fortschritt, Log -------------------------------------------
    def _build_bottom(self):
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="both")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.dry_var = tk.BooleanVar(value=self.cfg.get("aktionen.probelauf", True))
        ttk.Checkbutton(row, text="Probelauf (nichts verändern, nur Vorschau)", variable=self.dry_var).pack(side="left")
        self.start_btn = ttk.Button(row, text="Start", command=self._run)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(row, text="Abbrechen", state="disabled",
                                     command=lambda: self.ctx and self.ctx.cancel())
        self.cancel_btn.pack(side="right", padx=6)
        self.report_btn = ttk.Button(row, text="Bericht öffnen", state="disabled", command=self._open_report)
        self.report_btn.pack(side="right")
        self.prog = ttk.Progressbar(f, mode="determinate")
        self.prog.pack(fill="x", pady=(8, 2))
        self.prog_label = ttk.Label(f, text="Bereit")
        self.prog_label.pack(anchor="w")
        self.logbox = tk.Text(f, height=10, wrap="word", font=("Menlo", 12), state="disabled")
        self.logbox.pack(fill="both", expand=True, pady=(4, 0))

    def log(self, text: str):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
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
                     **{j: params_to_dict(self._params(j)) for j in ("abgleich", "duplikate", "sortieren")}}
            self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, tk.TclError):
            pass

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for j in ("abgleich", "duplikate", "sortieren"):
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
            messagebox.showinfo("Start", "Bitte Tab Abgleich, Duplikate oder Sortieren wählen.")
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
                        self.prog_label.configure(text=f"{phase}: {done} / {total}")
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

    def _finished(self, res: JobResult | None):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.prog.configure(mode="determinate", value=0)
        self.prog_label.configure(text="Fertig")
        self._hist_refresh()
        if res is None:
            return
        self.last_result = res
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
