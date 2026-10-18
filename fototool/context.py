"""Gemeinsamer Lauf-Kontext: Fortschritt, Log-Ausgabe, Abbrechen."""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("fototool")


class Cancelled(Exception):
    pass


class Declined(Cancelled):
    """Nutzer hat vor dem ersten Schritt abgelehnt (z.B. Namensprüfung) – kein Bericht nötig."""


class RunContext:
    def __init__(self,
                 on_message: Callable[[str], None] | None = None,
                 on_progress: Callable[[str, int, int], None] | None = None,
                 on_names: Callable[[dict], dict] | None = None):
        self._on_message = on_message
        self._on_progress = on_progress
        self._on_names = on_names
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def message(self, text: str, level: int = logging.INFO) -> None:
        log.log(level, text)
        if self._on_message:
            self._on_message(text)

    def progress(self, phase: str, done: int, total: int) -> None:
        if self._on_progress:
            self._on_progress(phase, done, total)

    def review_names(self, todo: dict) -> dict | None:
        """Neue Namen bestätigen lassen: {Platzhalter: {Name in Datei: Zielname}}.
        None = keine Oberfläche zum Fragen; Ablehnen wirft Declined."""
        return self._on_names(todo) if self._on_names else None
