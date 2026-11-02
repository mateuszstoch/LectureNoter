"""JobManager: trwala kolejka zadan pipeline'u (jedno naraz), uruchamianie main.py jako podprocesu."""
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from io_utils import atomic_write_json, read_json
from main import STAGES
from run_state import is_live, pid_alive, read_run_state

ROOT = Path(__file__).resolve().parent.parent
ACTIVE = ("queued", "running")
CANCEL_CODES = (130, 143, -2, -15)
MAX_HISTORY = 50


class JobError(ValueError):
    pass


def default_cmd(job, wd, meta):
    cmd = [sys.executable, str(ROOT / "main.py"), meta["video_path"], "--workdir", str(wd),
           "--profile", meta.get("profile", "auto"), "--language", meta.get("language", "pl")]
    if job.get("from_stage"):
        cmd += ["--from-stage", job["from_stage"]]
    if job.get("only_stage"):
        cmd += ["--only-stage", job["only_stage"]]
    if job.get("force"):
        cmd.append("--force")
    return cmd


class JobManager:
    def __init__(self, library, gpu_lock, cmd_builder=default_cmd, poll_s=0.5):
        self.lib, self.gpu_lock, self.cmd_builder, self.poll_s = library, gpu_lock, cmd_builder, poll_s
        self.path = library.work_dir / "queue.json"
        self._lock = threading.RLock()
        self._proc = None
        self._stop = threading.Event()
        self._thread = None
        self._preempt = None
        data = read_json(self.path, {}) or {}
        self.paused = bool(data.get("paused", False))
        self.jobs = data.get("jobs", [])
        self.blocked_by = None
        self._recover()

    # --- trwalosc ---
    def _save(self):
        atomic_write_json(str(self.path), {"paused": self.paused, "jobs": self.jobs})

    def _recover(self):
        """Po restarcie serwera: zadanie 'running' bywa nadal zywe (adopcja) albo przerwane."""
        for j in self.jobs:
            if j["status"] != "running":
                continue
            try:
                rs = read_run_state(self.lib.workdir(j["video_id"]))
            except ValueError:
                rs = {}
            if not is_live(rs) or rs.get("pid") != j.get("pid"):
                self._finalize(j, None)
        self._save()

    def set_preempt(self, callback):
        """callback() zwalnia GPU zajete przez czat tego procesu (rozladowuje model)."""
        self._preempt = callback

    # --- operacje ---
    def enqueue(self, video_id, from_stage=None, only_stage=None, force=False):
        self.lib.workdir(video_id)
        for s in (from_stage, only_stage):
            if s is not None and s not in STAGES:
                raise JobError(f"nieznany etap: {s}")
        if from_stage and only_stage:
            raise JobError("podaj tylko jedno z: from_stage, only_stage")
        with self._lock:
            if any(j["video_id"] == video_id and j["status"] in ACTIVE for j in self.jobs):
                raise JobError("ten film jest juz w kolejce lub w trakcie przetwarzania")
            job = {"id": uuid.uuid4().hex[:8], "video_id": video_id, "from_stage": from_stage,
                   "only_stage": only_stage, "force": bool(force), "status": "queued",
                   "added": time.time(), "started": None, "finished": None, "error": None, "pid": None}
            self.jobs.append(job)
            self._trim()
            self._save()
            return dict(job)

    def _trim(self):
        done = [j for j in self.jobs if j["status"] not in ACTIVE]
        for j in done[:-MAX_HISTORY]:
            self.jobs.remove(j)

    def _get(self, job_id):
        for j in self.jobs:
            if j["id"] == job_id:
                return j
        raise JobError("nie ma takiego zadania")

    def cancel(self, job_id):
        with self._lock:
            j = self._get(job_id)
            if j["status"] == "queued":
                j.update(status="cancelled", finished=time.time())
            elif j["status"] == "running" and j.get("pid"):
                try:
                    os.kill(j["pid"], signal.SIGTERM)  # orkiestrator sprzata i zapisuje 'cancelled'
                except ProcessLookupError:
                    pass
            else:
                raise JobError("zadania nie mozna anulowac")
            self._save()
            return dict(j)

    def move(self, job_id, position):
        with self._lock:
            j = self._get(job_id)
            if j["status"] != "queued":
                raise JobError("przesuwac mozna tylko zadania oczekujace")
            queued = [x for x in self.jobs if x["status"] == "queued"]
            queued.remove(j)
            queued.insert(max(0, min(position, len(queued))), j)
            self.jobs = [x for x in self.jobs if x["status"] != "queued"] + queued
            self._save()

    def set_paused(self, paused):
        with self._lock:
            self.paused = bool(paused)
            self._save()

    def snapshot(self):
        with self._lock:
            return {"paused": self.paused, "blocked_by": self.blocked_by,
                    "jobs": [dict(j) for j in self.jobs]}

    # --- harmonogram ---
    def _finalize(self, job, rc):
        try:
            rs = read_run_state(self.lib.workdir(job["video_id"]))
        except ValueError:
            rs = {}
        if rc == 0 or (rc is None and rs.get("status") == "done"):
            status = "done"
        elif rc in CANCEL_CODES or (rc is None and rs.get("status") == "cancelled"):
            status = "cancelled"
        elif rc is None and rs.get("status") in (None, "running"):
            status = "interrupted"
        else:
            status = "error"
        err = rs.get("error") if status == "error" else None
        if status == "error" and not err:
            err = f"proces zakonczyl sie kodem {rc}" if rc is not None else "blad"
        job.update(status=status, finished=time.time(), error=err)

    def tick(self):
        """Jeden krok harmonogramu: obsluz zakonczenie biezacego zadania, ewentualnie uruchom nastepne."""
        with self._lock:
            running = next((j for j in self.jobs if j["status"] == "running"), None)
            if running:
                if self._proc is not None:
                    rc = self._proc.poll()
                    if rc is None:
                        return
                    self._proc = None
                    self._finalize(running, rc)
                else:  # zadanie zaadoptowane po restarcie serwera
                    rs = read_run_state(self.lib.workdir(running["video_id"]))
                    if pid_alive(running.get("pid")) and rs.get("status") == "running":
                        return
                    self._finalize(running, None)
                self._save()
            if self.paused:
                return
            nxt = next((j for j in self.jobs if j["status"] == "queued"), None)
            if not nxt:
                self.blocked_by = None
                return
            holder = self.gpu_lock.holder()
            if holder and holder.get("pid") == os.getpid() and holder.get("owner") == "chat" and self._preempt:
                self._preempt()
                holder = self.gpu_lock.holder()
            if holder:
                self.blocked_by = holder
                return
            self.blocked_by = None
            self._start(nxt)

    def _start(self, job):
        try:
            wd = self.lib.workdir(job["video_id"])
            meta = read_json(wd / "meta.json", {})
            out = open(wd / "job.out", "ab")
            self._proc = subprocess.Popen(self.cmd_builder(job, wd, meta), stdout=out, stderr=out,
                                          stdin=subprocess.DEVNULL, start_new_session=True)
            out.close()
            job.update(status="running", started=time.time(), pid=self._proc.pid)
        except Exception as e:
            job.update(status="error", finished=time.time(), error=f"nie udalo sie uruchomic: {e}")
        self._save()

    def start(self):
        def loop():
            while not self._stop.wait(self.poll_s):
                try:
                    self.tick()
                except Exception as e:  # petla nie moze zginac
                    print(f"JobManager: {e}", file=sys.stderr)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Zatrzymuje petle harmonogramu; uruchomione zadanie dziala dalej (zostanie zaadoptowane)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
