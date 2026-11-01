"""LectureNoter: orkiestrator pipeline'u (ASR -> slajdy -> VLM -> LLM) z wznawianiem."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from loguru import logger

from io_utils import atomic_write_json
from ollama_client import OllamaClient, OllamaError, pick_profile, vram_used_gb

ROOT = Path(__file__).resolve().parent
STAGES = ["asr", "frames", "vlm", "llm"]
OUTPUTS = {"asr": ["transcript.json"], "frames": ["frames_metadata.json"],
           "vlm": ["vision_context.json"], "llm": ["lecture_notes.md"]}
# co nalezy usunac, gdy etap jest nieaktualny (artefakty posrednie)
LEFTOVERS = {"asr": ["audio.wav"], "frames": ["frames"], "vlm": ["vision_context.jsonl"], "llm": ["chunks"]}
GPU_STAGES = {"asr", "vlm", "llm"}


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
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.has_audio = None
        self.runners = runners or {
            "asr": self._run_worker, "frames": self._run_frames, "vlm": self._run_worker, "llm": self._run_worker}

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

    # --- uruchamianie etapow ---
    def _run_frames(self, stage):
        import frames_module
        frames_module.extract_frames(self.video, self.workdir, self.cfg["frames"])

    def _run_worker(self, stage):
        """Etapy GPU w osobnym podprocesie: VRAM zwalnia sie po zakonczeniu procesu."""
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", stage, str(self.video),
               "--workdir", str(self.workdir), "--language", self.language, "--profile", self.profile]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"etap {stage} zakonczyl sie kodem {r.returncode}")

    def detect_audio(self):
        if self.has_audio is None:
            import frames_module
            self.has_audio = frames_module.probe(self.video)["has_audio"]
        return self.has_audio

    def run(self, from_stage=None, only_stage=None, force=False):
        stages = [only_stage] if only_stage else STAGES
        if from_stage:
            stages = [s for s in STAGES if STAGES.index(s) >= STAGES.index(from_stage)]
        total = time.time()
        for stage in stages:
            if not force and self.is_done(stage):
                logger.info(f"[{stage}] pomijam (wynik aktualny)")
                continue
            self.invalidate(stage)
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
        logger.info(f"pipeline zakonczony w {time.time() - total:.0f}s")


def worker(stage, args, cfg):
    prof = cfg["profiles"][pick_profile(cfg, args.profile)]
    if stage == "asr":
        from asr_module import run_asr
        run_asr(args.video, args.workdir, cfg["asr"], args.language)
    elif stage == "vlm":
        from vlm_module import run_vlm
        run_vlm(args.workdir, cfg["vlm"])
    elif stage == "llm":
        from llm_synthesis import run_synthesis
        run_synthesis(args.workdir, cfg["llm"], prof["llm_model"], prof["num_ctx"])


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
    ap.add_argument("--worker", choices=STAGES, help=argparse.SUPPRESS)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    args.language = args.language or cfg["language"]
    args.workdir = args.workdir or str(ROOT / "work" / Path(args.video).stem)
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    logger.add(Path(args.workdir) / "pipeline.log", rotation="5 MB", encoding="utf-8")
    if args.worker:
        worker(args.worker, args, cfg)
        return
    if not Path(args.video).exists():
        sys.exit(f"brak pliku: {args.video}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"brak {tool} w PATH")
    try:
        PipelineOrchestrator(args.video, args.workdir, cfg, args.language, args.profile).run(
            args.from_stage, args.only_stage, args.force)
    except (OllamaError, RuntimeError) as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
