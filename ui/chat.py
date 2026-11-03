"""Czat z notatkami i transkryptem (lokalny LLM przez Ollame): kontekst, historia, streszczanie, blokada GPU."""
import contextlib
import json
import threading
import time
import uuid
from pathlib import Path

from loguru import logger

from config_loader import resolve
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
GLOBAL = "_all"      # wirtualny identyfikator "wykladu" dla czatu po wszystkich wykladach
SYSTEM_GLOBAL_EXTRA = (" 7) Fragmenty pochodzą z RÓŻNYCH wykładów i są oznaczone tytułem wykładu i sekcją: przy każdym twierdzeniu podaj, "
                       "z którego wykładu pochodzi (np. (Wykład: tytuł, sekcja)); przy rozbieżnościach między wykładami wskaż je.")
KEEP_RAW = 4          # tyle ostatnich wiadomosci zawsze zostaje w oryginale
MAX_QUESTION = 4000


class ChatError(Exception):
    pass


class ChatBusy(ChatError):
    """GPU zajete przez inne zadanie (np. przetwarzanie filmu)."""


def export_markdown(chat, video_title=None):
    """Rozmowa jako czytelny Markdown: pytania, odpowiedzi (z wzorami LaTeX bez zmian) i uzyte fragmenty zrodlowe."""
    def when(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
    meta = [x for x in (f"Wykład: {video_title}" if video_title else None, f"model: {chat.get('model')}" if chat.get("model") else None,
                        when(chat.get("created"))) if x]
    lines = [f"# {chat.get('title') or 'Rozmowa'}", ""]
    if meta:
        lines += ["*" + " · ".join(meta) + "*", ""]
    if chat.get("summary"):
        lines += ["> Streszczenie wcześniejszej części rozmowy: " + chat["summary"].replace("\n", " "), ""]
    for m in chat.get("messages", []):
        who = "Ty" if m["role"] == "user" else "Asystent"
        stamp = when(m.get("ts")) if m.get("ts", 0) > 1e9 else ""
        lines += ["---", "", f"### {who}" + (f" · {stamp}" if stamp else ""), "", m["content"].strip(), ""]
        if m.get("stopped"):
            lines += ["*(odpowiedź przerwana)*", ""]
        srcs = m.get("sources") or []
        if srcs:
            labels = [(f"Notatki: {s['label']}" if s.get("kind") == "notes" else s["label"]) for s in srcs]
            lines += ["*Użyte fragmenty: " + "; ".join(labels) + "*", ""]
    return "\n".join(lines).rstrip() + "\n"


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
            return sorted(({"id": c.get("id"), "title": c.get("title") or "Rozmowa", "created": c.get("created", 0),
                            "updated": c.get("updated", c.get("created", 0)), "count": len(c.get("messages", [])), "model": c.get("model"),
                            "scope": c.get("scope")}
                           for c in self._load()["chats"] if c.get("id")),
                          key=lambda c: c["updated"], reverse=True)

    def create(self, title=None, scope=None):
        with self._lock:
            data = self._load()
            now = time.time()
            chat = {"id": uuid.uuid4().hex[:8], "title": title or "Nowa rozmowa", "created": now, "updated": now,
                    "messages": [], "summary": "", "summarized_upto": 0, "model": None, "auto_title": not title, "scope": scope}
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
        self.lib, self.gpu, self.client = library, gpu_lock, client
        self._cfg_src, self._cfg_override, self._ccfg_override = cfg, None, None
        self._lock = threading.RLock()
        self._generating = False
        self._gen_video = None
        self._held = False
        self._model = None
        self._timer = None
        self._idle_at = None
        self.global_dir = Path(self.lib.work_dir) / ".global"
        self.scope_ids = lambda scope: None   # (folder) -> zbior id wykladow albo None = wszystkie; ustawiane przez serwer

    def chat_dir(self, video_id):
        """Katalog rozmow: wyklad albo wspolny katalog czatu po wszystkich wykladach."""
        if video_id == GLOBAL:
            self.global_dir.mkdir(exist_ok=True)
            return self.global_dir
        return self.lib.workdir(video_id)

    @property
    def cfg(self):
        return self._cfg_override if self._cfg_override is not None else resolve(self._cfg_src)

    @cfg.setter
    def cfg(self, value):  # nadpisanie (testy)
        self._cfg_override = value

    @property
    def ccfg(self):
        return self._ccfg_override if self._ccfg_override is not None else self.cfg.get("chat", {})

    @ccfg.setter
    def ccfg(self, value):
        self._ccfg_override = value

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
            if self._generating:
                err = ChatBusy("Model jest teraz zajęty inną odpowiedzią (czat lub quiz). Spróbuj za chwilę.")
                err.transient = True  # chwilowe: inne zadania (quizy) moga poczekac w kolejce
                raise err
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

    @contextlib.contextmanager
    def session(self, video_id, model):
        """Wspolny dostep do modelu dla innych funkcji (quizy): ta sama blokada GPU, zwolnienie po czasie bezczynnosci."""
        self._acquire(model)
        self._gen_video = video_id
        try:
            yield
        finally:
            with self._lock:
                self._generating = False
                self._gen_video = None
            self._schedule_release()

    def is_generating_for(self, video_id):
        with self._lock:
            return self._generating and self._gen_video == video_id

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

    def _global_context(self, question, history, scope, budget):
        """Kontekst z wielu wykladow: sekcje notatek wszystkich (lub wybranych) wykladow, wybrane BM25. (tekst, zrodla)."""
        from ui.retrieval import BM25, split_notes, tokens, _fit
        allowed = self.scope_ids(scope)
        secs = []
        for vid, meta in self.lib.iter_meta():
            if allowed is not None and vid not in allowed:
                continue
            try:
                md = notes_mod.get_active(self.lib.workdir(vid))["md"]
            except notes_mod.NotesError:
                continue
            for s_ in split_notes(md):
                if s_["text"].strip() and s_["title"] not in ("Spis treści", "Podsumowanie"):
                    secs.append({"vid": vid, "lecture": meta.get("title") or vid, "title": s_["title"], "text": s_["text"]})
        if not secs:
            return "", []
        prev_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        q = tokens(question) * 2 + tokens(prev_q)
        sc = BM25([tokens(f"{x['lecture']} {x['title']} {x['text']}") for x in secs]).scores(q)
        ranked = [x for _, x in sorted(zip(sc, secs), key=lambda p: -p[0]) if _ > 0] or secs[:4]
        chosen = _fit(ranked, budget, lambda x: f"[Wykład: {x['lecture']} · {x['title']}]\n{x['text']}")
        ctx = "\n\n".join(f"[Wykład: {x['lecture']} · {x['title']}]\n{x['text']}" for x in chosen)
        return ctx, [{"kind": "lecture", "label": f"{x['lecture']} · {x['title']}", "video_id": x["vid"], "section": x["title"]} for x in chosen]

    def _lectures_with_notes(self, scope):
        allowed = self.scope_ids(scope)
        for vid, _ in self.lib.iter_meta():
            if allowed is not None and vid not in allowed:
                continue
            try:
                notes_mod.get_active(self.lib.workdir(vid))
                yield vid
            except notes_mod.NotesError:
                continue

    def build_messages(self, question, chat, wd, model, is_global=False):
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
        if is_global:
            ctx, sources = self._global_context(question, history, chat.get("scope"), max(500, total - fixed))
        else:
            notes_md, transcript, vision = self._materials(wd)
            ctx, sources = build_context(question, history, notes_md, transcript, vision, max(500, total - fixed))
        system = f"{SYSTEM_CHAT}{SYSTEM_GLOBAL_EXTRA if is_global else ''}{summary_txt}\n\n# Fragmenty wykładu\n{ctx or '(brak materiałów)'}"
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
        is_global = video_id == GLOBAL
        wd = self.chat_dir(video_id)
        store = ChatStore(wd)
        try:
            chat = store.get(chat_id)
            if is_global:
                if not any(True for _ in self._lectures_with_notes(chat.get("scope"))):
                    raise ChatError("Brak notatek w wybranym zakresie: najpierw przetwórz jakieś wykłady.")
            else:
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
        self._gen_video = video_id
        try:
            messages, sources = self.build_messages(content, chat, wd, model, is_global)
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
                self._gen_video = None
            self._schedule_release()
        if cancel is not None and cancel.is_set():
            return
        if failed:
            yield "error", failed
        else:
            yield "done", {"stopped": False}
