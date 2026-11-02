"""Miedzyprocesowa blokada GPU (flock): pipeline i czat nie ladują modeli jednoczesnie."""
import fcntl
import json
import os
import threading
import time
from pathlib import Path

from io_utils import read_json


class GpuLock:
    def __init__(self, path):
        self.path = Path(path)
        self.info_path = Path(f"{path}.json")
        self._fd = None
        self._mu = threading.RLock()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)

    def acquire(self, owner, blocking=False):
        with self._mu:
            return self._acquire(owner, blocking)

    def _acquire(self, owner, blocking):
        if self._fd is not None:
            return True
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        self.info_path.write_text(json.dumps({"owner": owner, "pid": os.getpid(), "since": time.time()}))
        return True

    def release(self):
        with self._mu:
            self._release()

    def _release(self):
        if self._fd is None:
            return
        try:
            self.info_path.unlink()
        except OSError:
            pass
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def holder(self):
        """None, gdy GPU wolne; inaczej {'owner','pid','since'}."""
        with self._mu:
            return self._holder()

    def _holder(self):
        if self._fd is not None:
            return read_json(self.info_path, {"owner": "unknown"})
        fd = self._open()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return read_json(self.info_path, {"owner": "unknown"})
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        finally:
            os.close(fd)

    def __enter__(self):
        if not self.acquire("pipeline"):
            raise RuntimeError("GPU zajete")
        return self

    def __exit__(self, *exc):
        self.release()
