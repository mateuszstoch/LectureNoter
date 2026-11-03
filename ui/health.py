"""Diagnostyka srodowiska (Ollama, modele, ffmpeg, GPU, miejsce na dysku) i pobieranie brakujacych modeli jednym kliknieciem."""
import json
import re
import shutil
import sys
import threading
import time

import requests
from loguru import logger

from ollama_client import apply_profile, has_cuda, pick_profile, total_vram_gb

MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*(?:/[\w.\-]+)*(?::[\w.\-]+)?$")


class PullManager:
    """Pobieranie modeli przez Ollame (/api/pull) w tle, z postepem do odpytywania z interfejsu."""

    def __init__(self, host):
        self.host = host
        self._lock = threading.Lock()
        self.state = {}     # model -> {status, percent, error, done, started}

    def start(self, model):
        if not MODEL_RE.match(model or "") or len(model) > 120:
            raise ValueError("Niepoprawna nazwa modelu.")
        with self._lock:
            cur = self.state.get(model)
            if cur and not cur["done"]:
                return cur
            self.state[model] = {"status": "start", "percent": 0.0, "error": None, "done": False, "started": time.time()}
        threading.Thread(target=self._run, args=(model,), daemon=True, name=f"pull-{model}").start()
        return self.state[model]

    def _set(self, model, **kw):
        with self._lock:
            self.state[model].update(kw)

    def _run(self, model):
        try:
            with requests.post(f"{self.host}/api/pull", json={"model": model, "stream": True}, stream=True, timeout=(10, 120)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    pct = (100.0 * d["completed"] / d["total"]) if d.get("total") and d.get("completed") is not None else None
                    self._set(model, status=d.get("status", ""), **({"percent": round(pct, 1)} if pct is not None else {}))
            self._set(model, status="gotowe", percent=100.0, done=True)
            logger.info(f"pobrano model {model}")
        except Exception as e:
            logger.warning(f"pobieranie modelu {model} nie powiodlo sie: {e}")
            self._set(model, error=str(e), done=True)

    def snapshot(self):
        with self._lock:
            return {m: dict(s) for m, s in self.state.items()}


def _cuda_runtime():
    """Czy biblioteka transkrypcji widzi CUDA (gdy jest karta, a mimo to nie: brak libcublas/cudnn)."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return None


def run_checks(cfg, installed, ollama_ok, work_dir):
    """Lista kontroli: {id, label, status: ok|warn|fail, detail, model?}. `installed`: nazwy modeli zainstalowanych w Ollamie."""
    profile = pick_profile(cfg)
    eff = apply_profile(cfg, profile)
    out = []

    def add(id_, label, status, detail, model=None):
        out.append({"id": id_, "label": label, "status": status, "detail": detail, **({"model": model} if model else {})})

    v = sys.version_info
    add("python", "Python", "ok" if v >= (3, 10) else "fail", f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 10) else " (wymagany 3.10+)"))
    for tool, need in (("ffmpeg", True), ("ffprobe", True), ("pandoc", False)):
        path = shutil.which(tool)
        add(tool, tool, "ok" if path else ("fail" if need else "warn"),
            path or ("brak: sudo apt install ffmpeg" if need else "opcjonalny (eksport notatek do Worda): sudo apt install pandoc"))
    cuda = has_cuda()
    vram = total_vram_gb()
    add("gpu", "GPU NVIDIA", "ok" if cuda else "warn",
        (f"wykryto, VRAM {vram:.0f} GB, profil {profile}" if vram else f"wykryto, profil {profile}") if cuda
        else "nie wykryto: tryb CPU (lżejsze modele, znacznie wolniej)")
    if cuda:
        rt = _cuda_runtime()
        if rt is False:
            add("cuda_rt", "CUDA dla transkrypcji", "fail", "karta jest, ale biblioteka transkrypcji jej nie widzi (brak libcublas/cudnn): uruchom ponownie skrypt startowy")
        elif rt:
            add("cuda_rt", "CUDA dla transkrypcji", "ok", "działa")
    add("ollama", "Ollama", "ok" if ollama_ok else "fail", "odpowiada" if ollama_ok else "nie odpowiada: uruchom `ollama serve` (WSL/Linux)")
    needs = [("Model notatek, czatu i quizów", eff["profiles"][profile]["llm_model"]), ("Model odczytu tablicy (VLM)", eff["vlm"]["model"])]
    for label, model in needs:
        have = ollama_ok and model in installed
        add(f"model:{model}", label, "ok" if have else ("fail" if ollama_ok else "warn"),
            f"{model}: zainstalowany" if have else f"{model}: brak (można pobrać poniżej)" if ollama_ok else f"{model}: nie można sprawdzić (Ollama nie odpowiada)", model)
    add("asr", "Model transkrypcji", "ok", f"{eff['asr']['model']} ({eff['asr'].get('device', 'cuda')}): pobiera się automatycznie przy pierwszej transkrypcji")
    try:
        free = shutil.disk_usage(work_dir).free / 1024 ** 3
        add("disk", "Wolne miejsce w katalogu danych", "ok" if free >= 10 else ("warn" if free >= 3 else "fail"), f"{free:.0f} GB")
    except OSError:
        pass
    return out
