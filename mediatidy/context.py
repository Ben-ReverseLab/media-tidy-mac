"""Shared run context: progress, log output, cancelling."""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("mediatidy")


class Cancelled(Exception):
    pass


class Declined(Cancelled):
    """The user said no before the first step (e.g. name review) – no report needed."""


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
        """phase is a key such as "copy" (shown via t("phase.copy"))."""
        if self._on_progress:
            self._on_progress(phase, done, total)

    def review_names(self, todo: dict) -> dict | None:
        """Let the user confirm new names: {placeholder: {name in file: target name}}.
        None = no UI to ask; declining raises Declined."""
        return self._on_names(todo) if self._on_names else None
