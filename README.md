# FotoTool

Fotos und Videos zwischen Ordnern/Festplatten abgleichen, Duplikate finden, nach Metadaten sortieren.

## Einrichtung (einmalig)

```sh
sudo xcodebuild -license accept      # nur falls Homebrew danach fragt
brew install python-tk@3.14 exiftool
```

Zugriffsrechte nur für FotoTool (nicht fürs Terminal):
- Externe Festplatten: beim ersten Zugriff fragt macOS → „Erlauben“ (Wechseldatenträger)
- Apple-Fotos-Mediathek: Systemeinstellungen → Datenschutz & Sicherheit → **Festplattenvollzugriff** → `+` → `~/FotoTool/FotoTool.app`

## Start

Doppelklick auf **`FotoTool.app`** → GUI. (Alternativ `FotoTool.command`, dann braucht aber das Terminal die Rechte.)

App neu bauen (z.B. nach Änderung am Starter): `app/build-app.sh` – danach Freigabe ggf. neu setzen.
Änderungen am Python-Code brauchen keinen Neubau.

Prüfen, ob alles bereit ist: `python3 -m fototool pruefen`

## Funktionen

| Tab | Zweck |
|---|---|
| 1 Abgleich | Mehrere Quellordner mit einem Ziel vergleichen (Inhalt, nicht Name). Fehlende kopieren – Struktur beibehalten oder nach Muster sortieren. Doppelte in den Quellen werden nur 1× kopiert. Sidecars (XMP/AAE) schon gesicherter Dateien werden ergänzt bzw. ersetzt, wenn die Quelle neuer ist (alte Version bleibt für Rückgängig erhalten). |
| 2 Duplikate | Inhaltsgleiche Dateien in einem Ordner + Unterordnern. Behalten-Regel und Aktion (Bericht / in `_Duplikate` verschieben / Papierkorb) wählbar. Eine gemeinsame XMP (IMG.xmp neben IMG.CR3 + IMG.JPG) gehört immer zur RAW. |
| 3 Sortieren | Nach Aufnahmedatum, Kamera, Objektiv, Typ … in Ordnerstruktur kopieren oder verschieben. Optional Umbenennen. |
| Einstellungen | Alle Optionen (aus `defaults.toml` erzeugt, mit Erklärung). |
| Verlauf | Jeder echte Lauf ist protokolliert und kann rückgängig gemacht werden. |

- **Probelauf** ist standardmäßig an: zeigt nur, was passieren würde (Bericht als HTML/CSV).
- **Leere Dateien** (0 Byte) werden als „vermutlich defekt“ gemeldet und weder verglichen noch kopiert.
- **Profile**: Eingaben eines Tabs unter Namen speichern (z.B. „Pictures → Backup“) und später laden.
- Bibliotheken (`.photoslibrary`, `.lrlibrary`) werden nur gelesen (Originale), nie verändert.
- Vor dem Verschieben aus Ordnern, die ein Lightroom-Classic-Katalog nutzt, wird gewarnt.
- Abgebrochene Läufe einfach neu starten: Bereits Kopiertes wird als vorhanden erkannt. Kopien werden erst unter Temp-Namen geschrieben und per Prüfsumme kontrolliert.

## Muster

`/` trennt Ordner. Platzhalter: `{Jahr} {Monat} {MonatName} {Tag} {Quartal} {KW} {KWJahr} {Wochentag} {Stunde} {Minute} {Sekunde} {Kamera} {Hersteller} {Objektiv} {Typ} {Endung} {Name} {Ordner} {Quelle}`

Beispiele: `{Jahr}/{Jahr}-{Monat}-{Tag}`, `{Kamera}/{Jahr}`, Dateiname `{Jahr}{Monat}{Tag}_{Stunde}{Minute}{Sekunde}_{Name}`

**Namensprüfung:** Kommen `{Kamera}`, `{Hersteller}` oder `{Objektiv}` im Muster vor, zeigt FotoTool nach dem Lesen der Metadaten – vor jeder Änderung – alle Namen ohne Zuordnung (z.B. Drohne mit unterschiedlichen Modellnamen in Fotos und Videos). Namen anpassen oder übernehmen; die Zuordnung wird in Einstellungen → Metadaten (`kamera_namen`, `hersteller_namen`, `objektiv_namen`) gespeichert und nicht erneut abgefragt. Abschalten: `metadaten.namen_pruefen = false`.

## Kommandozeile (Automatisierung)

```sh
cd ~/FotoTool
python3 -m fototool abgleich --quelle ~/Pictures/2025 --quelle ~/Pictures/2026 --ziel /Volumes/Backup/Photos
python3 -m fototool abgleich --profil "Pictures → Backup" --ausfuehren
python3 -m fototool duplikate --ordner /Volumes/Backup/Photos --aktion verschieben --ausfuehren
python3 -m fototool sortieren --quelle ~/Import --ziel ~/Sortiert --muster "{Jahr}/{Kamera}" --verschieben --ausfuehren
python3 -m fototool rueckgaengig --ausfuehren
```

Ohne `--ausfuehren` immer Probelauf. `--ja` bestätigt Papierkorb/Überschreiben.

## Dateien

| Pfad | Inhalt |
|---|---|
| `fototool/defaults.toml` | Standardwerte mit Erklärungen – nicht ändern |
| `~/.fototool/config.toml` | Eigene Einstellungen (nur Abweichungen) |
| `~/.fototool/profile/` | Gespeicherte Profile |
| `~/.fototool/journal/` | Protokoll jeder Aktion (für Rückgängig) |
| `~/.fototool/berichte/` | Alle Berichte (echte Läufe zusätzlich als Kopie in `_FotoTool-Berichte` im Ziel) |
| `~/.fototool/cache.db` | Prüfsummen-Cache (macht Wiederholungen schnell; löschbar) |
| `~/.fototool/logs/` | Logdatei |

## Erweitern

- Neue Einstellung: in `defaults.toml` mit Kommentar eintragen → erscheint automatisch im Einstellungen-Tab.
- Inkompatible Config-Änderung: `CONFIG_VERSION` in `config.py` erhöhen und Funktion in `MIGRATIONS` eintragen.
- Neue Bibliothek-Typen: Einstellungen → Durchsuchen → bibliotheken (`endung: unterordner`).
- Tests: `python3 -m unittest discover -s tests` (schnell, vor jedem Commit)
- Praxis-Prüfung mit echten, selbst erzeugten Fotos/Videos, Disk-Images und Finder-Papierkorb:
  `python3 tests/praxis_pruefung.py` (3–5 Min., braucht exiftool + ffmpeg; nach größeren Änderungen)
