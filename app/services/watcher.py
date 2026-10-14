"""Watchdog observer for the Inbox only (Phase 2).

- Non-recursive, Inbox only. Never watches data/, staging/, backups/.
- Debounces Windows bursts: coalesces events per path for debounce_sec.
- Ignores directories and temp files (filter decides); submits the rest
  to the Worker without blocking the event thread.
"""
from __future__ import annotations

import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..config import FormFlowConfig
from .filters import is_temp_file
from .jobs import Worker


class _Handler(FileSystemEventHandler):
    def __init__(self, cfg: FormFlowConfig, worker: Worker, debounce_sec: float):
        super().__init__()
        self.cfg = cfg
        self.worker = worker
        self.debounce_sec = debounce_sec
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, raw_path: str) -> None:
        p = Path(raw_path)
        # Ignore directories and anything outside Inbox (safety).
        try:
            if p.is_dir():
                return
        except OSError:
            pass
        try:
            rp = p.resolve()
            inbox = self.cfg.inbox_dir.resolve()
            rp.relative_to(inbox)
        except (OSError, ValueError):
            return
        if rp.parent != inbox and rp.parent not in (inbox,):
            # Non-recursive: only top-level Inbox files. Subdirs ignored.
            try:
                if rp.parent != inbox:
                    return
            except OSError:
                return
        if is_temp_file(rp.name):
            return

        key = str(rp)
        with self._lock:
            old = self._timers.pop(key, None)
            if old is not None:
                old.cancel()
            t = threading.Timer(self.debounce_sec, self._fire, args=(key,))
            t.daemon = True
            self._timers[key] = t
            t.start()

    def _fire(self, key: str) -> None:
        with self._lock:
            self._timers.pop(key, None)
        self.worker.submit(key)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", None)
        if dest and not event.is_directory:
            self._schedule(dest)


class InboxWatcher:
    def __init__(self, cfg: FormFlowConfig, worker: Worker):
        self.cfg = cfg
        self.worker = worker
        self._observer: Observer | None = None
        self.handler = _Handler(cfg, worker, cfg.debounce_sec)

    def start(self) -> "InboxWatcher":
        ok, msg = self.cfg.inbox_status()
        if not ok:
            raise RuntimeError(msg)
        try:
            inbox = str(self.cfg.inbox_dir.resolve())
        except OSError as e:
            raise RuntimeError(f"cannot watch Inbox: {e}") from e
        self._observer = Observer()
        # Non-recursive: Inbox top level only.
        self._observer.schedule(self.handler, inbox, recursive=False)
        try:
            self._observer.start()
        except OSError as e:
            raise RuntimeError(f"cannot watch Inbox {inbox}: {e}") from e
        return self

    def stop(self, timeout: float = 2.0) -> None:
        if self._observer is not None:
            self._observer.stop()
            try:
                self._observer.join(timeout=timeout)
            except RuntimeError:
                pass
            self._observer = None

    @property
    def running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def scan_existing(self) -> int:
        """One-shot scan of current Inbox files (used at startup)."""
        n = 0
        try:
            entries = list(self.cfg.inbox_dir.iterdir())
        except OSError:
            return 0
        for p in entries:
            try:
                if p.is_dir() or is_temp_file(p.name):
                    continue
            except OSError:
                continue
            self.worker.submit(p.resolve())
            n += 1
        return n
