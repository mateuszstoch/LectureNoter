"""LectureNoter: orkiestrator pipeline'u (ASR -> slajdy -> VLM -> LLM) z wznawianiem, statusem i anulowaniem."""
import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml
from loguru import logger

from io_utils import atomic_write_json, read_json
from ollama_client import OllamaClient, OllamaError, pick_profile, vram_used_gb
from progress import ProgressReporter, read_progress
from run_state import Heartbeat, is_live, read_run_state, write_run_state

ROOT = Path(__file__).resolve().parent
STAGES = ["asr", "frames", "vlm", "llm"]
OUTPUTS = {"asr": ["transcript.json"], "frames": ["frames_metadata.json"],
           "vlm": ["vision_context.json"], "llm": ["lecture_notes.md"]}
# artefakty posrednie do usuniecia, gdy etap jest nieaktualny
LEFTOVERS = {"asr": ["audio.wav"], "frames": ["frames"], "vlm": ["vision_context.jsonl"], "llm": ["chunks"]}
GPU_STAGES = {"asr", "vlm", "llm"}


class Cancelled(Exception):
    """Przerwanie na zadanie uzytkownika (SIGTERM/SIGINT)."""


def _raise_cancelled(signum, frame):
    raise Cancelled()


def cuda_lib_env():
    """Srodowisko z pip-owymi bibliotekami cuBLAS/cuDNN (wymagane przez faster-whisper) w LD_LIBRARY_PATH."""
    env = os.environ.copy()
    dirs = []
    for mod in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            pkg = __import__(mod, fromlist=["x"])
            dirs.append(str(Path(list(pkg.__path__)[0]) / "lib"))
        except Exception:
            pass
    if dirs:
        env["LD_LIBRARY_PATH"] = ":".join(dirs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    return env


def file_hash(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "-"


def video_sig(video):
    st = Path(video).stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def cfg_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


class PipelineOrchestrator:
    def __init__(self, video, workdir, cfg, language="pl", profile="auto", runners=None):
        self.video, self.workdir, self.cfg = Path(video), Path(workdir), cfg
        self.language = language
        self.profile = pick_profile(cfg, profile)
        self.prof = cfg["profiles"][self.profile]
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workdir / "stage_state.json"
        self.state = read_json(self.state_path, {})
        self.has_audio = None
        self.proc = None
        self.runners = runners or {
            "asr": self._run_worker, "frames": self._run_frames, "vlm": self._run_worker, "llm": self._run_worker}

    @classmethod
    def from_workdir(cls, workdir, cfg, profile="auto"):
        """Odtwarza orkiestrator z meta.json (UI zna tylko katalog roboczy)."""
        meta = read_json(Path(workdir) / "meta.json")
        if not meta:
            raise FileNotFoundError(f"brak meta.json w {workdir}")
        return cls(meta["video_path"], workdir, cfg, meta.get("language", cfg["language"]),
                   meta.get("profile", profile) if profile == "auto" else profile)

    # --- meta.json ---
    def ensure_meta(self):
        path = self.workdir / "meta.json"
        meta = read_json(path, {}) or {}
        new = dict(meta)
        new.update(video_path=str(self.video.resolve()), title=meta.get("title") or self.video.stem,
                   language=self.language, profile=self.profile)
        new.setdefault("created", time.time())
        sig = video_sig(self.video) if self.video.exists() else None
        changed = meta.get("video_sig") != sig or meta.get("video_path") != new["video_path"]
        new["video_sig"] = sig
        if new.get("duration") is None or changed:
            try:
                import frames_module
                p = frames_module.probe(self.video)
                new.update(duration=p["duration"], has_audio=p["has_audio"],
                           width=p["width"], height=p["height"])
            except Exception as e:
                logger.warning(f"ffprobe nie powiodl sie: {e}")
                new.setdefault("duration", None)
                new.setdefault("has_audio", None)
        if new != meta:
            atomic_write_json(str(path), new)
        if self.has_audio is None:
            self.has_audio = new.get("has_audio")
        return new

    # --- podpisy wejsc/konfiguracji decyduja o waznosci wyniku etapu ---
    def config_sig(self, stage):
        c = self.cfg
        part = {"asr": [c["asr"], self.language], "frames": [c["frames"]],
                "vlm": [c["vlm"]], "llm": [c["llm"], self.prof["llm_model"], self.prof["num_ctx"], self.language]}[stage]
        return cfg_hash(part)

    def input_sig(self, stage):
        w = self.workdir
        return {"asr": lambda: video_sig(self.video),
                "frames": lambda: video_sig(self.video),
                "vlm": lambda: file_hash(w / "frames_metadata.json"),
                "llm": lambda: file_hash(w / "transcript.json") + file_hash(w / "vision_context.json")}[stage]()

    def is_done(self, stage):
        s = self.state.get(stage)
        if not s or s.get("status") not in ("done", "skipped"):
            return False
        if s["config"] != self.config_sig(stage) or s["input"] != self.input_sig(stage):
            return False
        if s["status"] == "done" and not all((self.workdir / o).exists() for o in OUTPUTS[stage]):
            return False
        return True

    def save_state(self, stage, status, seconds):
        self.state[stage] = {"status": status, "config": self.config_sig(stage),
                             "input": self.input_sig(stage), "seconds": round(seconds, 1)}
        atomic_write_json(str(self.state_path), self.state)

    def invalidate(self, stage):
        for name in OUTPUTS[stage] + LEFTOVERS[stage]:
            p = self.workdir / name
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        self.state.pop(stage, None)
        ProgressReporter(self.workdir, stage).reset()

    # --- status (dla CLI i UI) ---
    def status(self):
        rs = read_run_state(self.workdir)
        live = is_live(rs)
        prog = read_progress(self.workdir)
        video_ok = self.video.exists()
        stages = {}
        for st in STAGES:
            saved = self.state.get(st, {})
            if live and rs.get("current_stage") == st:
                status = "running"
            elif rs.get("status") == "running" and not live and rs.get("current_stage") == st:
                status = "interrupted"
            elif rs.get("status") in ("error", "cancelled") and rs.get("current_stage") == st:
                status = rs["status"]
            elif video_ok and self.is_done(st):
                status = saved["status"]
            elif saved.get("status") in ("done", "skipped"):
                status = "stale"
            else:
                status = "pending"
            stages[st] = {"status": status, "seconds": saved.get("seconds"), "progress": prog.get(st)}
        if live:
            overall = "running"
        elif rs.get("status") == "running":
            overall = "interrupted"
        elif rs.get("status") in ("error", "cancelled"):
            overall = rs["status"]
        elif all(s["status"] in ("done", "skipped") for s in stages.values()):
            overall = "done"
        else:
            overall = "idle"
        return {"video": str(self.video), "video_exists": video_ok, "profile": self.profile,
                "overall": overall, "run": rs, "stages": stages}

    # --- uruchamianie etapow ---
    def _run_frames(self, stage):
        import frames_module
        rep = ProgressReporter(self.workdir, "frames")

        def cb(done, total, msg):
            rep.update(min(done, total), total, msg)
        frames_module.extract_frames(self.video, self.workdir, self.cfg["frames"], progress=cb)

    def _run_worker(self, stage):
        """Etapy GPU w osobnym podprocesie: VRAM zwalnia sie po zakonczeniu procesu."""
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", stage, str(self.video),
               "--workdir", str(self.workdir), "--language", self.language, "--profile", self.profile]
        self.proc = subprocess.Popen(cmd, env=cuda_lib_env())
        try:
            rc = self.proc.wait()
        except BaseException:  # tez Cancelled: zatrzymaj podproces (jego handler SIGTERM rozladuje model)
            self._stop_child()
            raise
        finally:
            self.proc = None
        if rc != 0:
            raise RuntimeError(f"etap {stage} zakonczyl sie kodem {rc}")

    def _stop_child(self, timeout=20):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def detect_audio(self):
        if self.has_audio is None:
            import frames_module
            self.has_audio = frames_module.probe(self.video)["has_audio"]
        return self.has_audio

    def run(self, from_stage=None, only_stage=None, force=False):
        stages = [only_stage] if only_stage else STAGES
        if from_stage:
            stages = [s for s in STAGES if STAGES.index(s) >= STAGES.index(from_stage)]
        rs = read_run_state(self.workdir)
        if is_live(rs) and rs.get("pid") != os.getpid():
            raise RuntimeError(f"dla tego filmu dziala juz inne uruchomienie (pid {rs.get('pid')})")
        self.ensure_meta()
        write_run_state(self.workdir, status="running", pid=os.getpid(), started=time.time(),
                        heartbeat=time.time(), current_stage=None, error=None, finished=None)
        hb = Heartbeat(self.workdir).start()
        total = time.time()
        stage = None
        try:
            for stage in stages:
                if not force and self.is_done(stage):
                    logger.info(f"[{stage}] pomijam (wynik aktualny)")
                    continue
                self.invalidate(stage)
                write_run_state(self.workdir, current_stage=stage)
                if stage == "asr" and not self.detect_audio():
                    logger.warning("[asr] brak sciezki audio: pomijam, notatki powstana tylko z tablicy")
                    self.save_state(stage, "skipped", 0)
                    continue
                logger.info(f"[{stage}] start")
                t0 = time.time()
                self.runners[stage](stage)
                dt = time.time() - t0
                self.save_state(stage, "done", dt)
                used = vram_used_gb() if stage in GPU_STAGES else None
                logger.info(f"[{stage}] gotowe w {dt:.0f}s" + (f", VRAM po etapie: {used:.1f} GB" if used else ""))
            write_run_state(self.workdir, status="done", current_stage=None, finished=time.time())
            logger.info(f"pipeline zakonczony w {time.time() - total:.0f}s")
        except Cancelled:
            hb.stop()
            self._release_models(stage)
            write_run_state(self.workdir, status="cancelled", finished=time.time())
            logger.warning(f"[{stage}] anulowano")
            raise
        except Exception as e:
            write_run_state(self.workdir, status="error", error=str(e), finished=time.time())
            raise
        finally:
            hb.stop()

    @staticmethod
    def _release_models(stage):
        if stage in ("vlm", "llm"):
            try:
                OllamaClient(retries=1, timeout=10).unload_all()
            except Exception as e:
                logger.warning(f"nie udalo sie rozladowac modeli po anulowaniu: {e}")


def worker(stage, args, cfg):
    signal.signal(signal.SIGTERM, _raise_cancelled)  # pozwala wykonac 'finally' (rozladowanie modelu)
    prof = cfg["profiles"][pick_profile(cfg, args.profile)]
    rep = ProgressReporter(args.workdir, stage)
    try:
        if stage == "asr":
            from asr_module import run_asr
            run_asr(args.video, args.workdir, cfg["asr"], args.language, progress=rep)
        elif stage == "vlm":
            from vlm_module import run_vlm
            run_vlm(args.workdir, cfg["vlm"], progress=rep)
        elif stage == "llm":
            from llm_synthesis import run_synthesis
            run_synthesis(args.workdir, cfg["llm"], prof["llm_model"], prof["num_ctx"], progress=rep)
    except Cancelled:
        sys.exit(143)


def main():
    ap = argparse.ArgumentParser(description="Lokalny generator notatek z wykladow wideo")
    ap.add_argument("video")
    ap.add_argument("--workdir", default=None, help="domyslnie work/<nazwa_filmu>")
    ap.add_argument("--language", default=None)
    ap.add_argument("--profile", default="auto", choices=["auto", "8gb", "16gb"])
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--from-stage", choices=STAGES)
    ap.add_argument("--only-stage", choices=STAGES)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true", help="wypisz stan etapow jako JSON i zakoncz")
    ap.add_argument("--worker", choices=STAGES, help=argparse.SUPPRESS)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    args.language = args.language or cfg["language"]
    args.workdir = args.workdir or str(ROOT / "work" / Path(args.video).stem)
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    if args.status:
        orch = PipelineOrchestrator(args.video, args.workdir, cfg, args.language, args.profile)
        print(json.dumps(orch.status(), ensure_ascii=False, indent=2))
        return
    logger.add(Path(args.workdir) / "pipeline.log", rotation="5 MB", encoding="utf-8")
    if args.worker:
        worker(args.worker, args, cfg)
        return
    if not Path(args.video).exists():
        sys.exit(f"brak pliku: {args.video}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"brak {tool} w PATH")
    signal.signal(signal.SIGTERM, _raise_cancelled)
    signal.signal(signal.SIGINT, _raise_cancelled)
    try:
        PipelineOrchestrator(args.video, args.workdir, cfg, args.language, args.profile).run(
            args.from_stage, args.only_stage, args.force)
    except Cancelled:
        sys.exit(130)
    except (OllamaError, RuntimeError) as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
