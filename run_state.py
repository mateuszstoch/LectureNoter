"""Stan biezacego/ostatniego uruchomienia (run_state.json) z heartbeatem do wykrywania awarii."""
import os
import threading
import time
from pathlib import Path

from io_utils import atomic_write_json, read_json

STALE_S = 30
_lock = threading.Lock()


def _path(workdir):
    return Path(workdir) / "run_state.json"


def read_run_state(workdir):
    return read_json(_path(workdir), {}) or {}


def write_run_state(workdir, **fields):
    with _lock:
        data = read_run_state(workdir)
        data.update(fields)
        atomic_write_json(str(_path(workdir)), data)
        return data


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_live(rs, stale_s=STALE_S):
    """Uruchomienie 'running' z swiezym heartbeatem i zyjacym procesem."""
    return (rs.get("status") == "running" and time.time() - rs.get("heartbeat", 0) < stale_s
            and pid_alive(rs.get("pid")))


class Heartbeat:
    def __init__(self, workdir, interval=5.0):
        self.workdir, self.interval = workdir, interval
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                write_run_state(self.workdir, heartbeat=time.time())
            except OSError:
                pass

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join(timeout=2)
