"""Simple background worker for one Windows PC (Phase 2).

A single queue + N daemon threads (default 1). Flask requests never block:
the watcher submits paths; the worker ingests asynchronously.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from ..config import FormFlowConfig
from .ingest import ingest_file


class Worker:
    # A file still growing when first checked must be re-polled, otherwise it
    # would sit in 'waiting' with no further filesystem events to wake it.
    WAIT_RETRY_DELAY_SEC = 2.0
    WAIT_MAX_RETRIES = 30

    def __init__(self, cfg: FormFlowConfig):
        self.cfg = cfg
        self.q: queue.Queue[str] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._wait_retries: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self) -> "Worker":
        n = max(1, self.cfg.worker_threads)
        for i in range(n):
            t = threading.Thread(target=self._loop, name=f"ff-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)

    def submit(self, path: Path | str) -> bool:
        """Coalesce duplicate submissions. Returns True if queued."""
        key = str(path)
        with self._lock:
            if key in self._seen and not self.q.empty():
                # Still pending; skip duplicate Windows events.
                return False
            self._seen.add(key)
        self.q.put(key)
        return True

    def process_now(self, path: Path | str) -> dict | None:
        """Synchronous ingest for tests and manual retry (no thread)."""
        return ingest_file(Path(path), self.cfg)

    @property
    def pending(self) -> int:
        return self.q.qsize()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                key = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = ingest_file(Path(key), self.cfg)
                # Phase 3: prepare a copy for the upload workflow. Failures
                # are recorded on the document row; the loop never crashes.
                if isinstance(result, dict) and result.get("state") == "ready":
                    staged = result.get("staged_path")
                    if staged:
                        try:
                            from .extract import run_extraction
                            from .prepare import prepare_document

                            doc = prepare_document(Path(staged), self.cfg,
                                                   job_id=result.get("id"))
                            run_extraction(doc["id"], self.cfg)
                        except Exception:
                            pass
            except Exception:
                # ingest_file records failures in DB; never crash the loop.
                # No PII in logs (Phase 8 hardens logging centrally).
                result = None
            finally:
                with self._lock:
                    self._seen.discard(key)
                self.q.task_done()
            if isinstance(result, dict) and result.get("state") == "waiting":
                self._retry_waiting(key)
            else:
                with self._lock:
                    self._wait_retries.pop(key, None)

    def _retry_waiting(self, key: str) -> None:
        with self._lock:
            n = self._wait_retries.get(key, 0) + 1
            if n > self.WAIT_MAX_RETRIES:
                self._wait_retries.pop(key, None)
                return
            self._wait_retries[key] = n
        if self._stop.is_set():
            return
        t = threading.Timer(self.WAIT_RETRY_DELAY_SEC, self._refire_if_still_waiting, args=(key,))
        t.daemon = True
        t.start()

    def _refire_if_still_waiting(self, key: str) -> None:
        if self._stop.is_set():
            return
        try:
            if not Path(key).exists():
                with self._lock:
                    self._wait_retries.pop(key, None)
                return
        except OSError:
            return
        # Clear retry count once the file leaves 'waiting'.
        self.submit(key)
