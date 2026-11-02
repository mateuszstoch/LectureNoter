"""Postep etapow zapisywany do progress.json (czytany przez status/UI)."""
import time
from pathlib import Path

from io_utils import atomic_write_json, read_json


def read_progress(workdir):
    return read_json(Path(workdir) / "progress.json", {}) or {}


class ProgressReporter:
    """update(done, total, message) zapisuje postep etapu; zapisy ograniczone czasowo."""

    def __init__(self, workdir, stage, min_interval=0.5):
        self.path = Path(workdir) / "progress.json"
        self.stage, self.min_interval, self._last = stage, min_interval, 0.0

    def update(self, done, total, message=""):
        now = time.time()
        if done < total and now - self._last < self.min_interval:
            return
        self._last = now
        data = read_json(self.path, {}) or {}
        data[self.stage] = {"done": round(float(done), 1), "total": round(float(total), 1),
                            "message": message, "updated_at": now}
        atomic_write_json(str(self.path), data)

    def reset(self):
        data = read_json(self.path, {}) or {}
        if self.stage in data:
            del data[self.stage]
            atomic_write_json(str(self.path), data)
