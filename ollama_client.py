"""Cienki klient REST Ollamy (generate, ps, unload) z retry."""
import http.client
import json
import os
import queue
import socket
import threading
import time
from urllib.parse import urlparse

import requests
from loguru import logger


_warned_no_vram = False


class OllamaError(RuntimeError):
    """Blad komunikacji z Ollama. fatal=True: ponawianie nie ma sensu (brak modelu, brak polaczenia z serwerem)."""

    def __init__(self, message, status=None, fatal=False):
        super().__init__(message)
        self.status, self.fatal = status, fatal


class OllamaClient:
    def __init__(self, host=None, timeout=600, retries=3):
        host = host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"  # standardowa zmienna Ollamy
        if "://" not in host:
            host = f"http://{host}"
        self.host, self.timeout, self.retries = host.rstrip("/"), timeout, retries

    @staticmethod
    def _detail(r):
        """Tresc bledu z odpowiedzi Ollamy ({'error': ...}); to w niej jest prawdziwa przyczyna 500."""
        try:
            return str(r.json().get("error", "")).strip() or r.text[:300]
        except ValueError:
            return r.text[:300].strip()

    def _post(self, path, payload):
        last, status = None, None
        for attempt in range(1, self.retries + 1):
            try:
                r = requests.post(f"{self.host}{path}", json=payload, timeout=self.timeout)
            except requests.RequestException as e:  # brak polaczenia / timeout: ponawiamy
                last, status = str(e), None
                logger.warning(f"Ollama {path} proba {attempt}/{self.retries}: {e}")
                time.sleep(2 * attempt)
                continue
            if r.status_code == 404:
                model = payload.get("model")
                raise OllamaError(f"model '{model}' niedostepny: uruchom `ollama pull {model}`", status=404, fatal=True)
            if r.status_code >= 500:  # np. 'model runner has unexpectedly stopped': ponawiamy, ale zachowujemy tresc bledu
                last, status = f"HTTP {r.status_code}: {self._detail(r)}", r.status_code
                logger.warning(f"Ollama {path} proba {attempt}/{self.retries}: {last}")
                time.sleep(2 * attempt)
                continue
            if r.status_code >= 400:
                raise OllamaError(f"Ollama HTTP {r.status_code}: {self._detail(r)}", status=r.status_code)
            return r.json()
        if status is None:
            raise OllamaError(f"Ollama niedostepna lub blad polaczenia: {last}", fatal=True)
        raise OllamaError(f"Ollama zwrocila blad {last}", status=status)

    def check(self):
        try:
            requests.get(f"{self.host}/api/tags", timeout=5).raise_for_status()
        except requests.RequestException as e:
            raise OllamaError(f"Serwer Ollama nie odpowiada pod {self.host}: {e}")

    def generate(self, model, prompt, system=None, images=None, options=None, keep_alive="10m", format=None):
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": keep_alive,
                   "options": options or {}}
        if format:
            payload["format"] = format  # np. "json": Ollama wymusza poprawny JSON
        if system:
            payload["system"] = system
        if images:
            payload["images"] = images
        data = self._post("/api/generate", payload)
        if data.get("done_reason") == "length":
            logger.warning("odpowiedz obcieta przez num_predict/kontekst")
        return data.get("response", "")

    def installed_models(self):
        r = requests.get(f"{self.host}/api/tags", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def chat_stream(self, model, messages, options=None, keep_alive="10m", cancel=None):
        """Generator tokenow z /api/chat (stream). Zadanie i odczyt ida w osobnym watku (http.client), a `cancel`
        (Event) zrywa gniazdo takze w trakcie oczekiwania na naglowki, czyli podczas ladowania modelu i przetwarzania
        promptu: Ollama wysyla pierwsze bajty dopiero po zakonczeniu tych etapow."""
        payload = json.dumps({"model": model, "messages": messages, "stream": True, "keep_alive": keep_alive,
                              "options": options or {}}).encode()
        u = urlparse(self.host)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        q = queue.Queue()

        def reader():
            try:
                conn.request("POST", "/api/chat", body=payload, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                q.put(("status", (resp.status, None)))
                if resp.status != 200:
                    q.put(("status", (resp.status, resp.read(2000).decode("utf-8", "replace"))))
                    return
                for line in resp:
                    q.put(("line", line))
                q.put(("end", None))
            except Exception as e:  # tez po zerwaniu polaczenia przez nas
                q.put(("err", e))

        threading.Thread(target=reader, daemon=True).start()
        try:
            while True:
                try:
                    kind, val = q.get(timeout=0.3)
                except queue.Empty:
                    if cancel is not None and cancel.is_set():
                        return
                    continue
                if kind == "end":
                    return
                if kind == "err":
                    if cancel is not None and cancel.is_set():
                        return
                    raise OllamaError(f"Ollama niedostepna lub blad polaczenia: {val}")
                if kind == "status":
                    status, body = val
                    if status == 404:
                        raise OllamaError(f"model '{model}' niedostepny: uruchom `ollama pull {model}`")
                    if status != 200 and body is not None:
                        raise OllamaError(f"Ollama HTTP {status}: {body[:300]}")
                    continue
                if not val.strip():
                    continue
                data = json.loads(val)
                if data.get("error"):
                    raise OllamaError(data["error"])
                piece = (data.get("message") or {}).get("content")
                if piece:
                    yield piece
                if data.get("done"):
                    if data.get("done_reason") == "length":
                        yield "\n\n*(odpowiedź ucięta limitem długości)*"
                    return
        finally:
            self._abort(conn)

    @staticmethod
    def _abort(conn):
        """Zrywa polaczenie, zeby Ollama przestala generowac (shutdown gniazda budzi tez zablokowany odczyt)."""
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

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
        global _warned_no_vram
        if not _warned_no_vram:  # ta funkcja jest wolana przy kazdym odswiezeniu stanu: ostrzegaj raz
            logger.warning("nie wykryto VRAM (brak NVML/GPU), wybieram profil 8gb")
            _warned_no_vram = True
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
