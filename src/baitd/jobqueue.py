from __future__ import annotations

import logging
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config
from .models import JobCancelled, Profile, Script, UserError
from .pipeline import run_job

log = logging.getLogger(__name__)

_STATUSES = ("pending", "running", "done", "error", "cancelled")
_FINISHED = ("done", "error", "cancelled")
_PUBLIC = ("id", "topic", "status", "step", "step_index", "percent", "error", "output",
           "profile_name", "created_at")
_UNEXPECTED = "Error inesperado: {} → Revisa el registro y vuelve a intentarlo"


class JobQueue:
    """Sequential batch queue persisted to a JSON file.

    Each item keeps a snapshot of the profile (and the reviewed script, if any) taken
    when it was added, so later profile edits never change queued jobs.
    """

    def __init__(self, path: Path, runner: Callable = run_job):
        self._path = Path(path)
        self._runner = runner
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._items: list[dict] = []
        self._cancels: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._load()

    # --- persistence (callers hold the lock, except __init__) ---

    def _load(self) -> None:
        raw = config._read_json(self._path, [], list)  # quarantines corrupt files
        for d in raw:
            item = self._normalize(d)
            if item is not None:
                self._items.append(item)
        running = [i for i in self._items if i["status"] == "running"]
        for i in running:
            self._remove_partial(i)
            self._reset_pending(i)
        if running:
            self._save()

    @staticmethod
    def _remove_partial(item: dict) -> None:
        """A job left `running` died with the app (e.g. its console was closed): its output
        folder still has the pipeline's .work/ folder, so it is removed before the job reruns."""
        out = item.get("output")
        if out and (Path(out) / ".work").is_dir():
            log.info("Borrando la salida a medias de %r: %s", item["topic"], out)
            shutil.rmtree(out, ignore_errors=True)

    @staticmethod
    def _normalize(d) -> dict | None:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not isinstance(d.get("topic"), str):
            return None
        if not isinstance(d.get("profile"), dict):
            return None
        status = d.get("status") if d.get("status") in _STATUSES else "pending"
        try:
            step_index = int(d.get("step_index") or 0)
            percent = float(d.get("percent") or 0.0)
        except (TypeError, ValueError):
            step_index, percent = 0, 0.0
        return {
            "id": d["id"], "topic": d["topic"], "status": status,
            "step": d.get("step") or "", "step_index": step_index,
            "percent": percent, "error": d.get("error") or "",
            "output": d.get("output") or "", "profile_name": d.get("profile_name") or "",
            "created_at": d.get("created_at") or "",
            "profile": d["profile"],
            "script": d["script"] if isinstance(d.get("script"), dict) else None,
        }

    def _save(self) -> None:
        try:
            config._write_json(self._path, self._items)
        except OSError:
            log.exception("No se pudo guardar la cola en %s", self._path)

    @staticmethod
    def _reset_pending(item: dict) -> None:
        item.update(status="pending", step="", step_index=0, percent=0.0, error="", output="")

    # --- public API ---

    def add(self, topics: list[str], profile: Profile, scripts: dict[str, Script] | None = None) -> list[str]:
        snapshot = profile.to_dict()
        now = datetime.now().isoformat(timespec="seconds")
        ids = []
        with self._lock:
            for topic in topics:
                topic = topic.strip()
                if not topic:
                    continue
                script = (scripts or {}).get(topic)
                item = {
                    "id": uuid.uuid4().hex[:12], "topic": topic, "status": "pending",
                    "step": "", "step_index": 0, "percent": 0.0, "error": "", "output": "",
                    "profile_name": profile.name, "created_at": now,
                    "profile": snapshot, "script": script.to_dict() if script else None,
                }
                self._items.append(item)
                ids.append(item["id"])
            self._save()
            self._wake.notify_all()
        return ids

    def list(self) -> list[dict]:
        with self._lock:
            return [{k: i[k] for k in _PUBLIC} for i in self._items]

    def cancel(self, id: str) -> None:
        with self._lock:
            item = self._find(id)
            if item is None:
                return
            if item["status"] == "pending":
                item["status"] = "cancelled"
                self._save()
            elif item["status"] == "running":
                self._cancels[id].set()

    def remove(self, id: str) -> None:
        """Delete one finished (done/error/cancelled) item; other statuses are left alone."""
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if not (i["id"] == id and i["status"] in _FINISHED)]
            if len(self._items) != before:
                self._save()

    def clear_finished(self) -> None:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["status"] not in _FINISHED]
            if len(self._items) != before:
                self._save()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._worker, name="baitd-queue", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop accepting work; a running job is cancelled and goes back to pending."""
        with self._lock:
            self._stopping = True
            for ev in self._cancels.values():
                ev.set()
            self._wake.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- worker ---

    def _find(self, id: str) -> dict | None:
        return next((i for i in self._items if i["id"] == id), None)

    def _worker(self) -> None:
        while True:
            with self._lock:
                item = None
                while not self._stopping:
                    item = next((i for i in self._items if i["status"] == "pending"), None)
                    if item is not None:
                        break
                    self._wake.wait()
                if self._stopping:
                    return
                item["status"] = "running"
                cancel = self._cancels[item["id"]] = threading.Event()
                self._save()
            self._run_one(item, cancel)

    def _run_one(self, item: dict, cancel: threading.Event) -> None:
        id = item["id"]
        try:
            profile = Profile.from_dict(item["profile"])
            script = Script.from_dict(item["script"]) if item["script"] else None
            settings = config.load_settings()
            path = self._runner(item["topic"], profile, settings, script, cancel,
                                lambda *a: self._progress(id, *a),
                                on_output=lambda folder: self._record_output(id, folder))
            outcome = {"status": "done", "output": str(path), "percent": 100.0}
        except JobCancelled:
            outcome = {"status": "cancelled", "output": ""}  # the pipeline removed the folder
        except UserError as e:
            outcome = {"status": "error", "error": str(e), "output": ""}
        except Exception as e:
            log.exception("Fallo inesperado en el trabajo %s (%r)", id, item["topic"])
            outcome = {"status": "error", "error": _UNEXPECTED.format(type(e).__name__), "output": ""}
        with self._lock:
            self._cancels.pop(id, None)
            if self._stopping and outcome["status"] == "cancelled":
                self._reset_pending(item)  # resumes on next launch
            else:
                item.update(outcome)
            self._save()

    def _record_output(self, id: str, folder: Path) -> None:
        """Persist the job's folder as soon as it exists, so a hard kill can be cleaned up."""
        with self._lock:
            item = self._find(id)
            if item is not None and item["status"] == "running":
                item["output"] = str(folder)
                self._save()

    def _progress(self, id: str, step: str, index: int, total: int, pct: float) -> None:
        overall = round(((index - 1) + max(0.0, min(100.0, pct)) / 100) / (total or 6) * 100, 1)
        with self._lock:
            item = self._find(id)
            if item is None or item["status"] != "running":
                return
            changed = item["step"] != step
            item.update(step=step, step_index=index, percent=overall)
            if changed:
                self._save()
