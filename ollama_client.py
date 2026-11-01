"""Cienki klient REST Ollamy (generate, ps, unload) z retry."""
import time

import requests
from loguru import logger


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host="http://localhost:11434", timeout=600, retries=3):
        self.host, self.timeout, self.retries = host.rstrip("/"), timeout, retries

    def _post(self, path, payload):
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                r = requests.post(f"{self.host}{path}", json=payload, timeout=self.timeout)
                if r.status_code == 404:
                    raise OllamaError(f"model '{payload.get('model')}' niedostepny: uruchom `ollama pull {payload.get('model')}`")
                r.raise_for_status()
                return r.json()
            except OllamaError:
                raise
            except requests.RequestException as e:
                last = e
                logger.warning(f"Ollama {path} proba {attempt}/{self.retries}: {e}")
                time.sleep(2 * attempt)
        raise OllamaError(f"Ollama niedostepna lub blad zadania: {last}")

    def check(self):
        try:
            requests.get(f"{self.host}/api/tags", timeout=5).raise_for_status()
        except requests.RequestException as e:
            raise OllamaError(f"Serwer Ollama nie odpowiada pod {self.host}: {e}")

    def generate(self, model, prompt, system=None, images=None, options=None, keep_alive="10m"):
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive,
                   "options": options or {}}
        if system:
            payload["system"] = system
        if images:
            payload["images"] = images
        data = self._post("/api/generate", payload)
        if data.get("done_reason") == "length":
            logger.warning("odpowiedz obcieta przez num_predict/kontekst")
        return data.get("response", "")

    def loaded_models(self):
        r = requests.get(f"{self.host}/api/ps", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def unload(self, model):
        self._post("/api/generate", {"model": model, "keep_alive": 0})

    def unload_all(self):
        for m in self.loaded_models():
            logger.info(f"rozladowuje model {m}")
            self.unload(m)

    def assert_vram_free(self, wait_s=30):
        """Rozlada wszystkie modele Ollamy i potwierdza przez /api/ps, ze nic nie jest zaladowane."""
        self.unload_all()
        deadline = time.time() + wait_s
        while self.loaded_models():
            if time.time() > deadline:
                raise OllamaError("modele nadal zaladowane po rozladowaniu")
            time.sleep(1)


def total_vram_gb():
    """Calkowity VRAM GPU 0 w GB lub None (brak NVML)."""
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return pynvml.nvmlDeviceGetMemoryInfo(h).total / 1024 ** 3
    except Exception:
        return None


def pick_profile(cfg, requested="auto"):
    if requested in cfg["profiles"]:
        return requested
    gb = total_vram_gb()
    if gb is None:
        logger.warning("nie wykryto VRAM, wybieram profil 8gb")
        return "8gb"
    return "16gb" if gb >= 14 else "8gb"


def vram_used_gb():
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return pynvml.nvmlDeviceGetMemoryInfo(h).used / 1024 ** 3
    except Exception:
        return None
