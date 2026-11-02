"""Czat z notatkami i transkryptem (lokalny LLM przez Ollame): kontekst, historia, streszczanie, blokada GPU."""
import json
import threading
import time
import uuid
from pathlib import Path

from loguru import logger

from io_utils import atomic_write_json, read_json
from llm_synthesis import est_tokens
from ollama_client import OllamaError
from ui import materials, notes as notes_mod
from ui.retrieval import build_context

SYSTEM_CHAT = (
    "Jesteś asystentem, który odpowiada na pytania o jeden konkretny wykład. Dostajesz fragmenty materiału: "
    "notatki, transkrypt mowy ([Mowa ...]) i odczyt tablicy ([Tablica ...]). Zasady: "
    "1) Odpowiadaj po polsku, rzeczowo i tylko na podstawie dostarczonych fragmentów. "
    "2) Jeśli odpowiedzi nie ma w materiale, napisz to wprost; nie zgaduj i nie dodawaj faktów spoza wykładu. "
    "3) Notatki i odczyt tablicy mogą zawierać błędy automatycznego rozpoznawania (pismo odręczne, mowa); "
    "przy rozbieżnościach wspomnij o tym. 4) Wzory zapisuj w LaTeX ($...$ lub $$...$$). "
    "5) Przy odwołaniu do mowy lub tablicy podaj znacznik czasu w formacie [HH:MM:SS], a przy notatkach "
    "tytuł sekcji w nawiasie, np. (Notatki: Definicja maszyny Turinga). "
    "6) Bądź zwięzły, chyba że użytkownik prosi o szczegóły. Nie pisz znaków innych niż łacińskie."
)
SUMMARY_PROMPT = (
    "Zaktualizuj zwięzłe streszczenie rozmowy o wykładzie (max 150 słów, po polsku). Zachowaj ustalenia, "
    "o co użytkownik pytał, kluczowe odpowiedzi i otwarte wątki. Nie dodawaj nic od siebie.\n\n"
    "Dotychczasowe streszczenie:\n{summary}\n\nNowe wiadomości do uwzględnienia:\n{messages}\n\nNowe streszczenie:"
)
KEEP_RAW = 4          # tyle ostatnich wiadomosci zawsze zostaje w oryginale
MAX_QUESTION = 4000


class ChatError(Exception):
    pass


class ChatBusy(ChatError):
    """GPU zajete przez inne zadanie (np. przetwarzanie filmu)."""


class ChatStore:
    """Rozmowy jednego filmu w work/<film>/chat.json."""
    _lock = threading.RLock()

    def __init__(self, workdir):
        self.path = Path(workdir) / "chat.json"

    def _load(self):
        return read_json(self.path, {"chats": []}) or {"chats": []}

    def _save(self, data):
        atomic_write_json(str(self.path), data)

    def list(self):
        with self._lock:
            return sorted(({"id": c["id"], "title": c["title"], "created": c["created"], "updated": c["updated"],
                            "count": len(c["messages"]), "model": c.get("model")} for c in self._load()["chats"]),
                          key=lambda c: c["updated"], reverse=True)

    def create(self, title=None):
        with self._lock:
            data = self._load()
            now = time.time()
            chat = {"id": uuid.uuid4().hex[:8], "title": title or "Nowa rozmowa", "created": now, "updated": now,
                    "messages": [], "summary": "", "summarized_upto": 0, "model": None, "auto_title": not title}
            data["chats"].append(chat)
            self._save(data)
            return chat

    def get(self, cid):
        with self._lock:
            for c in self._load()["chats"]:
                if c["id"] == cid:
                    return c
        raise ChatError("nie ma takiej rozmowy")

    def update(self, cid, fn):
        """fn(chat) modyfikuje rozmowe w miejscu; zapis atomowy."""
        with self._lock:
            data = self._load()
            for c in data["chats"]:
                if c["id"] == cid:
                    fn(c)
                    c["updated"] = time.time()
                    self._save(data)
                    return c
        raise ChatError("nie ma takiej rozmowy")

    def delete(self, cid):
        with self._lock:
            data = self._load()
            n = len(data["chats"])
            data["chats"] = [c for c in data["chats"] if c["id"] != cid]
            if len(data["chats"]) == n:
                raise ChatError("nie ma takiej rozmowy")
            self._save(data)


class ChatService:
    def __init__(self, library, gpu_lock, cfg, client):
        self.lib, self.gpu, self.cfg, self.client = library, gpu_lock, cfg, client
        self.ccfg = cfg.get("chat", {})
        self._lock = threading.RLock()
        self._generating = False
        self._held = False
        self._model = None
        self._timer = None
        self._idle_at = None

    # ---------- GPU ----------
    def default_model(self):
        from ollama_client import pick_profile
        return self.cfg["profiles"][pick_profile(self.cfg)]["llm_model"]

    def num_ctx(self):
        from ollama_client import pick_profile
        return self.cfg["profiles"][pick_profile(self.cfg)]["num_ctx"]

    def status(self):
        with self._lock:
            return {"held": self._held, "generating": self._generating, "model": self._model,
                    "idle_release_at": self._idle_at, "holder": self.gpu.holder()}

    def _acquire(self, model):
        with self._lock:
            if not self._held:
                if not self.gpu.acquire("chat"):
                    h = self.gpu.holder() or {}
                    raise ChatBusy(f"GPU jest zajęte przez: {h.get('owner', 'inne zadanie')}. "
                                   "Czat będzie dostępny po jego zakończeniu.")
                self._held = True
                try:  # inne modele (np. po przerwanym przetwarzaniu) zwalniamy, zostaje tylko model czatu
                    for m in self.client.loaded_models():
                        if m != model:
                            self.client.unload(m)
                except Exception:
                    pass
            self._model = model
            self._generating = True  # od tej chwili preempt()/release() nie zabiora modelu
            if self._timer:
                self._timer.cancel()
                self._timer, self._idle_at = None, None

    def _schedule_release(self):
        idle = self.ccfg.get("idle_release_s", 600)
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._idle_at = time.time() + idle
            self._timer = threading.Timer(idle, self.release)
            self._timer.daemon = True
            self._timer.start()

    def release(self):
        """Rozladowuje model i zwalnia GPU. False, gdy trwa generowanie (wtedy nic nie robi)."""
        with self._lock:
            if self._generating:
                return False
            if self._timer:
                self._timer.cancel()
                self._timer, self._idle_at = None, None
            if self._held:
                try:
                    if self._model:
                        self.client.unload(self._model)
                except Exception:
                    pass
                self.gpu.release()
                self._held, self._model = False, None
            return True

    def preempt(self):
        """Wywolywane przez kolejke zadan, gdy zadanie czeka na GPU zajete przez czat."""
        self.release()

    # ---------- kontekst ----------
    def _materials(self, wd):
        try:
            notes_md = notes_mod.get_active(wd)["md"]
        except notes_mod.NotesError:
            notes_md = ""
        return notes_md, materials.transcript(wd), materials.frames(wd)

    def _history_messages(self, chat):
        msgs = chat["messages"]
        return [m for m in msgs[chat["summarized_upto"]:]]

    def _fold_history(self, cid, store, model, budget_tokens):
        """Gdy surowa historia przekracza budzet, starsze wiadomosci trafiaja do streszczenia."""
        chat = store.get(cid)
        msgs = chat["messages"]
        while True:
            raw = msgs[chat["summarized_upto"]:]
            cost = sum(est_tokens(m["content"]) for m in raw)
            if cost <= budget_tokens or len(raw) <= KEEP_RAW:
                return store.get(cid)
            n_fold = max(1, len(raw) - KEEP_RAW)
            fold = raw[:n_fold]
            text = "\n".join(f"{'Użytkownik' if m['role'] == 'user' else 'Asystent'}: {m['content'][:1500]}" for m in fold)
            summary = self.client.generate(model, SUMMARY_PROMPT.format(summary=chat["summary"] or "(brak)", messages=text),
                                           options={"temperature": 0.1, "num_predict": 400, "num_ctx": self.num_ctx()},
                                           keep_alive="10m").strip()
            upto = chat["summarized_upto"] + n_fold
            chat = store.update(cid, lambda c: c.update(summary=summary, summarized_upto=upto))

    def build_messages(self, question, chat, wd, model):
        """Zwraca (messages dla Ollamy, zrodla)."""
        num_ctx = self.num_ctx()
        predict = self.ccfg.get("num_predict", 1200)
        total = num_ctx - predict - 400
        hist_budget = int(total * self.ccfg.get("history_ratio", 0.25))
        store = ChatStore(wd)
        chat = self._fold_history(chat["id"], store, model, hist_budget)
        history = [{"role": m["role"], "content": m["content"]} for m in self._history_messages(chat)]
        summary_txt = f"\n\n# Streszczenie wcześniejszej rozmowy\n{chat['summary']}" if chat["summary"] else ""
        fixed = est_tokens(SYSTEM_CHAT) + est_tokens(summary_txt) + est_tokens(question) \
            + sum(est_tokens(m["content"]) for m in history) + 200
        notes_md, transcript, vision = self._materials(wd)
        ctx, sources = build_context(question, history, notes_md, transcript, vision, max(500, total - fixed))
        system = f"{SYSTEM_CHAT}{summary_txt}\n\n# Fragmenty wykładu\n{ctx or '(brak materiałów)'}"
        return [{"role": "system", "content": system}, *history, {"role": "user", "content": question}], sources

    # ---------- odpowiedz (generator zdarzen) ----------
    def stream_answer(self, video_id, chat_id, content, model=None, cancel=None):
        """Generator (event, data): sources, token, done | error. Zamkniecie generatora zachowuje czesciowa odpowiedz."""
        content = (content or "").strip()
        if not content:
            yield "error", "Pytanie nie może być puste."
            return
        if len(content) > MAX_QUESTION:
            yield "error", f"Pytanie jest zbyt długie (limit {MAX_QUESTION} znaków)."
            return
        wd = self.lib.workdir(video_id)
        store = ChatStore(wd)
        try:
            chat = store.get(chat_id)
            notes_md, transcript, _ = self._materials(wd)
            if not notes_md and not transcript:
                raise ChatError("Brak notatek i transkryptu: najpierw przetwórz film.")
            model = model or chat.get("model") or self.default_model()
            try:
                installed = self.client.installed_models()
            except Exception as e:
                raise ChatError(f"Ollama nie odpowiada: {e}")
            if model not in installed:
                raise ChatError(f"Model '{model}' nie jest zainstalowany: uruchom `ollama pull {model}`.")
            self._acquire(model)
        except ChatError as e:
            yield "error", str(e)
            return
        answer, sources, stopped, failed, user_added = [], [], True, None, False
        try:
            messages, sources = self.build_messages(content, chat, wd, model)
            now = time.time()

            def add_user(c):
                c["messages"].append({"role": "user", "content": content, "ts": now})
                c["model"] = model
                if c.get("auto_title") and len(c["messages"]) == 1:
                    c["title"] = content[:60] + ("…" if len(content) > 60 else "")
            store.update(chat_id, add_user)
            user_added = True
            yield "sources", sources
            opts = {"num_ctx": self.num_ctx(), "num_predict": self.ccfg.get("num_predict", 1200),
                    "temperature": self.ccfg.get("temperature", 0.2)}
            for piece in self.client.chat_stream(model, messages, options=opts, keep_alive="10m", cancel=cancel):
                if cancel is not None and cancel.is_set():
                    break
                answer.append(piece)
                yield "token", piece
            stopped = bool(cancel is not None and cancel.is_set())
        except (OllamaError, ChatError) as e:
            failed = str(e)
            logger.warning(f"czat ({video_id}/{chat_id}): {failed}")
        except GeneratorExit:
            raise
        finally:
            text = "".join(answer)
            try:
                if text:
                    msg = {"role": "assistant", "content": text, "ts": time.time(), "sources": sources, "model": model}
                    if stopped:
                        msg["stopped"] = True
                    store.update(chat_id, lambda c: c["messages"].append(msg))
                elif user_added:  # brak odpowiedzi (blad/przerwanie): nie zostawiaj pytania bez odpowiedzi
                    store.update(chat_id, lambda c: c["messages"].pop() if c["messages"] and c["messages"][-1]["role"] == "user" else None)
            except ChatError:
                pass
            with self._lock:
                self._generating = False
            self._schedule_release()
        if cancel is not None and cancel.is_set():
            return
        if failed:
            yield "error", failed
        else:
            yield "done", {"stopped": False}
