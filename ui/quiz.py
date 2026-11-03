"""Quizy z wykladu: generowanie pytan lokalnym LLM, zapis w work/<film>/quizzes.json, ocena odpowiedzi i historia podejsc."""
import contextlib
import difflib
import json
import math
import random
import re
import threading
import time
import uuid
from pathlib import Path

from loguru import logger

from config_loader import resolve
from io_utils import atomic_write_json, read_json
from llm_synthesis import est_tokens
from ollama_client import OllamaError
from ui import notes as notes_mod
from ui.chat import ChatBusy, ChatError
from ui.retrieval import build_context, split_notes

CONCRETE = {"open": "Opisowy", "single": "ABCD (jedna odpowiedź)", "multi": "ABCD (wiele odpowiedzi)", "term": "Definicja → nazwa"}
TYPES = {**CONCRETE, "auto": "Mieszany (automatyczny)"}
TYPE_HINTS = ('„term” – pojęcie z jedną nazwą i definicją; „single” – rozróżnianie podobnych pojęć, prawda/fałsz, wybór właściwej '
              'własności; „multi” – zagadnienie z kilkoma niezależnymi własnościami; „open” – przyczyny, mechanizmy, '
              'porównania i zależności wymagające wyjaśnienia')
MAX_COUNT = 30
BATCH = 5            # tyle pytan prosimy model naraz (dluzsze listy gorzej trzymaja format)
MAX_ANSWER = 4000

SYSTEM = (
    "Jesteś egzaminatorem, który układa pytania sprawdzające ZROZUMIENIE wykładu na podstawie dostarczonego materiału "
    "(notatki, transkrypt, odczyt tablicy). Zasady: 1) Pisz po polsku, poprawnym językiem; nie używaj znaków spoza alfabetu łacińskiego. "
    "2) Pytaj tylko o treści obecne w materiale; nie dodawaj faktów z zewnątrz. 3) Pytaj o sens, zależności, definicje, "
    "przyczyny i zastosowania, a nie o drobiazgi (daty, numery slajdów, znaczniki czasu, kolejność wypowiedzi). "
    "4) Każde pytanie musi być zrozumiałe samo w sobie, bez odwoływania się do „wykładowcy” czy „notatek”. "
    "5) Pytania mają się różnić tematycznie. 6) Wzory zapisuj w LaTeX ($...$). 7) Odpowiadaj WYŁĄCZNIE poprawnym JSON-em."
)
FORMATS = {
    "open": ('Pytania otwarte, na które odpowiada się kilkoma zdaniami. Format: {"questions": [{"question": "...", '
             '"model_answer": "wzorcowa odpowiedź (2-4 zdania)", "key_points": ["kluczowy element 1", "kluczowy element 2"], '
             '"section": "tytuł sekcji, z której pochodzi"}]}'),
    "single": ('Pytania jednokrotnego wyboru: zwykle 4 opcje, dokładnie JEDNA poprawna; pozostałe to wiarygodne, ale błędne '
               'dystraktory (typowe pomyłki). Dla twierdzenia, które jest prawdziwe albo fałszywe, użyj dokładnie 2 opcji: „Prawda” i „Fałsz”. Przy każdej opcji napisz w "why", dlaczego jest poprawna albo dlaczego jest błędna '
               '(1-2 zdania). Opcje bez liter A/B/C/D. Format: {"questions": [{"question": "...", "options": '
               '[{"text": "...", "correct": true, "why": "..."}, {"text": "...", "correct": false, "why": "..."}], '
               '"section": "tytuł sekcji"}]}'),
    "multi": ('Pytania wielokrotnego wyboru: dokładnie 4 opcje, poprawne są 2 albo 3 z nich (nie wszystkie i nie jedna). '
              'Pytanie ma jasno wynikać z treści, które opcje są poprawne. Przy każdej opcji napisz w "why", dlaczego jest poprawna '
              'albo błędna (1-2 zdania). Format: {"questions": [{"question": "...", "options": '
              '[{"text": "...", "correct": true, "why": "..."}, {"text": "...", "correct": false, "why": "..."}], '
              '"section": "tytuł sekcji"}]}'),
    "term": ('Zadania „podaj nazwę”: w "definition" opisz pojęcie (definicja, własności, zastosowanie) tak, by wskazywało jedno pojęcie, '
             'ale NIE używaj w opisie samej nazwy ani jej odmiany. "term" to krótka nazwa pojęcia (1-4 słowa), "aliases" to akceptowalne '
             'synonimy lub skróty, "explanation" 1-2 zdania uzupełnienia. Format: {"questions": [{"definition": "...", "term": "...", '
             '"aliases": ["..."], "explanation": "...", "section": "tytuł sekcji"}]}'),
}
GRADE_PROMPT = (
    "Oceń odpowiedź studenta na pytanie otwarte. Porównaj ją ze wzorcem i kluczowymi elementami; liczy się merytoryczna poprawność, "
    "nie styl. Skala: 1 = poprawna i wystarczająco pełna, 0.5 = częściowo poprawna (brakuje ważnych elementów albo są drobne błędy), "
    "0 = błędna lub niezwiązana z pytaniem. W \"feedback\" (po polsku, 1-3 zdania) napisz, co jest dobrze, czego brakuje lub co jest błędne.\n\n"
    "Pytanie: {question}\n\nWzorcowa odpowiedź: {model_answer}\n\nKluczowe elementy: {key_points}\n\nOdpowiedź studenta: {answer}\n\n"
    'Zwróć JSON: {{"score": 0 | 0.5 | 1, "feedback": "..."}}'
)


class QuizError(Exception):
    pass


class _Deleted(Exception):
    """Quiz usuniety przez uzytkownika w trakcie generowania."""


def _norm(text):
    t = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return " ".join(t.split())


def _strip_label(text):
    return re.sub(r"^\s*(?:[A-Da-d][\)\.:]|\([A-Da-d]\))\s+", "", str(text or "")).strip()


def extract_json(text):
    """Wyciaga obiekt/liste JSON z odpowiedzi modelu (takze gdy opakowal go w tekst lub blok kodu)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = text.find(a), text.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


def _salvage_objects(text):
    """Odzyskuje kompletne obiekty z uciętego JSON-a (model skonczyl na limicie tokenow w srodku listy)."""
    dec, out, i = json.JSONDecoder(), [], text.find("[")
    while 0 <= i < len(text):
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            break
        out.append(obj)
        i = end
    return [o for o in out if isinstance(o, dict)]


QUESTION_KEYS = ("questions", "pytania", "items", "quiz", "data")


def questions_of(raw):
    """Lista pytan z odpowiedzi modelu: toleruje inne nazwy klucza, pojedyncze pytanie zamiast listy i ucieta odpowiedz."""
    data = extract_json(raw)
    if isinstance(data, dict):
        for k in QUESTION_KEYS:
            if isinstance(data.get(k), list):
                return [q for q in data[k] if isinstance(q, dict)]
        if any(k in data for k in ("question", "pytanie", "definition", "definicja")):
            return [data]  # model zwrocil jedno pytanie bez listy
        return []
    if isinstance(data, list):
        return [q for q in data if isinstance(q, dict)]
    return _salvage_objects(raw or "")


def _text(v, limit=1200):
    return v.strip()[:limit] if isinstance(v, str) else ""


def _pick(raw, *keys):
    for k in keys:
        if raw.get(k) not in (None, "", []):
            return raw[k]
    return None


def _truthy(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "tak", "1", "yes", "poprawna", "prawda")


def _options(raw):
    """Opcje w roznych ksztaltach: lista obiektow {text, correct, why}, lista napisow + osobno wskazanie poprawnych."""
    items = _pick(raw, "options", "opcje", "answers", "odpowiedzi", "choices")
    if isinstance(items, dict):  # {"A": "...", "B": "..."}
        items = [{"text": v} for v in items.values()]
    if not isinstance(items, list):
        raise ValueError("brak listy opcji")
    marked = _pick(raw, "correct_answers", "correct_answer", "correct", "poprawne", "poprawna", "answer", "odpowiedz", "poprawna_odpowiedz")
    marked = marked if isinstance(marked, list) else ([marked] if marked is not None else [])
    opts = []
    for i, o in enumerate(items):
        if isinstance(o, str):
            o = {"text": o}
        if not isinstance(o, dict):
            raise ValueError("opcja nie jest tekstem")
        t = _strip_label(_text(_pick(o, "text", "tekst", "option", "opcja", "answer", "odpowiedz", "content"), 400))
        if not t:
            raise ValueError("pusta opcja")
        own = _pick(o, "correct", "is_correct", "poprawna", "poprawny")
        ok = _truthy(own) if own is not None else None
        if ok is None:  # poprawnosc podana osobno: litera, numer (od 1) albo tresc opcji
            ok = any(str(m).strip().rstrip(").:").upper() == "ABCDEF"[i] or str(m).strip() == str(i + 1) or _norm(str(m)) == _norm(t)
                     for m in marked if str(m).strip())
        opts.append({"text": t, "correct": bool(ok), "why": _text(_pick(o, "why", "dlaczego", "explanation", "wyjasnienie", "uzasadnienie"), 500)})
    return opts


def _normalize(raw, qtype, rng=random):
    """Zwraca pytanie w naszym formacie; rzuca ValueError(powod), gdy nie spelnia zasad typu."""
    section = _text(_pick(raw, "section", "sekcja"), 160)
    if qtype in ("single", "multi"):
        q = _text(_pick(raw, "question", "pytanie", "q"))
        if not q:
            raise ValueError("brak tresci pytania")
        opts = _options(raw)
        if len({_norm(o["text"]) for o in opts}) != len(opts):
            raise ValueError("powtorzone opcje")
        n_ok = sum(o["correct"] for o in opts)
        if not 2 <= len(opts) <= 6:  # 2 opcje = prawda/falsz
            raise ValueError(f"{len(opts)} opcji zamiast 2-6")
        if qtype == "single" and n_ok != 1:
            raise ValueError(f"{n_ok} poprawnych opcji w pytaniu jednokrotnego wyboru")
        if qtype == "multi" and not 1 <= n_ok < len(opts):
            raise ValueError("nieprawidlowa liczba poprawnych opcji w pytaniu wielokrotnego wyboru")
        rng.shuffle(opts)  # modele lubia stawiac poprawna odpowiedz pierwsza
        return {"type": qtype, "question": q, "options": opts, "section": section}
    if qtype == "term":
        d = _text(_pick(raw, "definition", "definicja", "question", "pytanie", "opis"))
        term = _text(_pick(raw, "term", "termin", "pojecie", "pojęcie", "nazwa", "answer", "odpowiedz"), 120)
        if not d or not term:
            raise ValueError("brak definicji lub nazwy")
        raw_al = _pick(raw, "aliases", "synonimy") or []
        aliases = [a.strip() for a in (raw_al if isinstance(raw_al, list) else [raw_al]) if isinstance(a, str) and a.strip()][:6]
        if _norm(term) and _norm(term) in _norm(d):  # opis zdradza odpowiedz
            raise ValueError("opis zawiera nazwe pojecia")
        return {"type": "term", "question": d, "term": term, "aliases": aliases,
                "explanation": _text(_pick(raw, "explanation", "wyjasnienie", "uzasadnienie"), 500), "section": section}
    if qtype == "open":
        q = _text(_pick(raw, "question", "pytanie", "q"))
        ans = _pick(raw, "model_answer", "wzorcowa_odpowiedz", "answer", "odpowiedz", "odpowiedz_wzorcowa")
        ans = _text(ans if isinstance(ans, str) else "", 2000)
        raw_pts = _pick(raw, "key_points", "kluczowe_elementy", "punkty") or []
        pts = [p.strip() for p in (raw_pts if isinstance(raw_pts, list) else [raw_pts]) if isinstance(p, str) and p.strip()][:6]
        if not q or not ans:
            raise ValueError("brak pytania lub wzorcowej odpowiedzi")
        return {"type": "open", "question": q, "model_answer": ans, "key_points": pts, "section": section}
    raise ValueError("nieznany typ")


def normalize_question(raw, qtype, rng=random):
    try:
        return _normalize(raw, qtype, rng)
    except ValueError:
        return None


def too_similar(question, existing):
    n = _norm(question)
    return any(difflib.SequenceMatcher(None, n, _norm(e)).ratio() > 0.8 for e in existing)


# ---------- ocena ----------
def grade_term(question, answer):
    """Dopasowanie nazwy: po normalizacji (wielkosc liter, interpunkcja) lub z drobna literowka."""
    a = _norm(answer)
    if not a:
        return False
    for cand in [question["term"], *question.get("aliases", [])]:
        c = _norm(cand)
        if a == c or difflib.SequenceMatcher(None, a, c).ratio() >= 0.88:
            return True
    return False


def grade_choice(question, selected):
    """selected: lista indeksow opcji (w kolejnosci zapisanej w pytaniu). Zwraca score 0..1 (wszystko albo nic)."""
    right = {i for i, o in enumerate(question["options"]) if o["correct"]}
    return 1.0 if set(selected) == right else 0.0


class QuizStore:
    """Quizy jednego filmu w work/<film>/quizzes.json."""
    _lock = threading.RLock()

    def __init__(self, workdir):
        self.path = Path(workdir) / "quizzes.json"

    def _load(self):
        d = read_json(self.path, {"quizzes": []})
        return d if isinstance(d, dict) and isinstance(d.get("quizzes"), list) else {"quizzes": []}

    def _save(self, data):
        atomic_write_json(str(self.path), data)

    def all(self):
        with self._lock:
            return [q for q in self._load()["quizzes"] if isinstance(q, dict) and q.get("id")]

    def get(self, qid):
        with self._lock:
            for q in self.all():
                if q["id"] == qid:
                    return q
        raise QuizError("nie ma takiego quizu")

    def exists(self, qid):
        try:
            self.get(qid)
            return True
        except QuizError:
            return False

    def add(self, quiz):
        with self._lock:
            data = self._load()
            data["quizzes"].append(quiz)
            self._save(data)
            return quiz

    def update(self, qid, fn):
        with self._lock:
            data = self._load()
            for q in data["quizzes"]:
                if isinstance(q, dict) and q.get("id") == qid:
                    fn(q)
                    self._save(data)
                    return q
        raise QuizError("nie ma takiego quizu")

    def delete(self, qid):
        with self._lock:
            data = self._load()
            n = len(data["quizzes"])
            data["quizzes"] = [q for q in data["quizzes"] if not (isinstance(q, dict) and q.get("id") == qid)]
            if len(data["quizzes"]) == n:
                raise QuizError("nie ma takiego quizu")
            self._save(data)


def summarize(quiz, pass_ratio):
    done = [a for a in quiz.get("attempts", []) if a.get("finished")]
    best = max((a["percent"] for a in done), default=None)
    return {"id": quiz["id"], "title": quiz.get("title"), "type": quiz.get("type"), "status": quiz.get("status"),
            "error": quiz.get("error"), "warning": quiz.get("warning"), "created": quiz.get("created", 0),
            "count": len(quiz.get("questions", [])), "requested": quiz.get("requested"), "progress": quiz.get("progress"),
            "scope": quiz.get("scope"), "attempts": len(done), "best": best, "last": done[-1]["percent"] if done else None,
            "passed": best is not None and best >= pass_ratio * 100, "model": quiz.get("model")}


class QuizService:
    def __init__(self, library, chat, cfg, client):
        self.lib, self.chat, self.client = library, chat, client
        self._cfg_src, self._cfg_override = cfg, None
        self._active = {}  # (video_id, quiz_id) -> watek generujacy

    @property
    def cfg(self):
        return self._cfg_override if self._cfg_override is not None else resolve(self._cfg_src)

    @cfg.setter
    def cfg(self, value):
        self._cfg_override = value

    @property
    def qcfg(self):
        return self.cfg.get("quiz", {})

    @property
    def pass_ratio(self):
        return float(self.qcfg.get("pass_ratio", 0.8))

    def store(self, video_id):
        return QuizStore(self.lib.workdir(video_id))

    def list(self, video_id):
        st = self.store(video_id)
        out = []
        for q in st.all():
            if q.get("status") == "generating" and (video_id, q["id"]) not in self._active:  # serwer padl w trakcie
                q = st.update(q["id"], lambda x: x.update(status="error", error="Generowanie zostało przerwane (restart serwera)."))
            out.append(summarize(q, self.pass_ratio))
        return sorted(out, key=lambda q: q["created"], reverse=True)

    def get(self, video_id, qid):
        q = self.store(video_id).get(qid)
        if q.get("status") == "generating" and (video_id, qid) not in self._active:
            q = self.store(video_id).update(qid, lambda x: x.update(status="error", error="Generowanie zostało przerwane (restart serwera)."))
        return dict(q, summary=summarize(q, self.pass_ratio), pass_ratio=self.pass_ratio)

    def scopes(self, video_id):
        """Sekcje notatek, z ktorych mozna wybrac obszar quizu."""
        try:
            md = notes_mod.get_active(self.lib.workdir(video_id))["md"]
        except notes_mod.NotesError:
            return []
        return [s["title"] for s in split_notes(md) if s["text"].strip()]

    # ---------- tworzenie ----------
    def create(self, video_id, qtype, count, sections=None, topic="", model=None, title=None, types=None, adaptive=False):
        if qtype not in TYPES:
            raise QuizError("Nieznany typ quizu.")
        allowed = [t for t in (types if types is not None else list(CONCRETE)) if t in CONCRETE]
        if qtype == "auto" and not allowed:
            raise QuizError("Wybierz co najmniej jeden dozwolony typ pytań.")
        if adaptive and not self.weak_topics(video_id):
            raise QuizError("Brak historii słabych stron: najpierw rozwiąż jakiś quiz tego wykładu (z błędami).")
        count = int(count)
        if not 1 <= count <= int(self.qcfg.get("max_count", MAX_COUNT)):
            raise QuizError(f"Liczba pytań musi być z zakresu 1-{int(self.qcfg.get('max_count', MAX_COUNT))}.")
        wd = self.lib.workdir(video_id)
        notes_md, transcript, vision = self.chat._materials(wd)
        if not notes_md and not transcript:
            raise QuizError("Brak notatek i transkryptu: najpierw przetwórz film.")
        model = model or self.chat.default_model()
        try:
            if model not in self.client.installed_models():
                raise QuizError(f"Model '{model}' nie jest zainstalowany: uruchom `ollama pull {model}`.")
        except QuizError:
            raise
        except Exception as e:
            raise QuizError(f"Ollama nie odpowiada: {e}")
        holder = self.chat.gpu.holder()
        if holder and holder.get("owner") not in ("chat", None) and not self.chat.status()["held"]:
            raise ChatBusy(f"GPU jest zajęte przez: {holder.get('owner')}. Quiz można wygenerować po zakończeniu zadania.")
        sections = [s for s in (sections or []) if isinstance(s, str)]
        topic = (topic or "").strip()[:300]
        scope = {"sections": sections, "topic": topic, "adaptive": bool(adaptive), "types": allowed if qtype == "auto" else None}
        label = TYPES[qtype] + (" · słabe strony" if adaptive else "")
        auto = f"{label}: " + (topic or (", ".join(sections[:2]) + ("…" if len(sections) > 2 else "") if sections else "cały wykład"))
        quiz = {"id": uuid.uuid4().hex[:8], "title": (title or "").strip()[:120] or auto[:120], "type": qtype, "created": time.time(),
                "status": "generating", "model": model, "requested": count, "scope": scope, "questions": [], "attempts": [],
                "progress": {"done": 0, "total": count}}
        st = self.store(video_id)
        st.add(quiz)
        t = threading.Thread(target=self._generate, args=(video_id, quiz["id"]), daemon=True, name=f"quiz-{quiz['id']}")
        self._active[(video_id, quiz["id"])] = t
        t.start()
        return summarize(quiz, self.pass_ratio)

    def delete(self, video_id, qid):
        self.store(video_id).delete(qid)  # trwajace generowanie samo zauwazy brak quizu i przerwie

    def is_generating(self, video_id):
        return any(v == video_id for v, _ in self._active)

    # ---------- slabe strony ----------
    def weak_topics(self, video_id):
        """Zagadnienia, z ktorych ostatnie odpowiedzi byly bledne lub czesciowe (wg najnowszego podejscia do kazdego pytania),
        od najslabszych. Zwraca [{topic, section, type, score, stems}]."""
        per = {}
        for quiz in self.store(video_id).all():
            by_id = {q["id"]: q for q in quiz.get("questions", []) if isinstance(q, dict) and q.get("id")}
            latest = {}
            for att in quiz.get("attempts", []):
                if att.get("finished"):
                    for qid, res in (att.get("answers") or {}).items():
                        latest[qid] = (att["finished"], res.get("score", 0))
            for qid, (_, score) in latest.items():
                q = by_id.get(qid)
                if not q:
                    continue
                topic = q.get("topic") or q.get("section") or q["question"][:80]
                e = per.setdefault(_norm(topic), {"topic": topic, "section": q.get("section", ""), "type": q.get("type"), "scores": [], "stems": []})
                e["scores"].append(score)
                e["stems"].append(q["question"])
        out = [{"topic": e["topic"], "section": e["section"], "type": e["type"], "score": sum(e["scores"]) / len(e["scores"]), "stems": e["stems"]}
               for e in per.values() if sum(e["scores"]) / len(e["scores"]) < 1]
        return sorted(out, key=lambda e: e["score"])

    # ---------- material ----------
    def _groups(self, wd, scope, budget):
        """Dzieli material na grupy mieszczace sie w budzecie tokenow; zwraca liste (tekst, wagi)."""
        notes_md, transcript, vision = self.chat._materials(wd)
        secs = split_notes(notes_md) if notes_md else []
        chosen = set(scope.get("sections") or [])
        if chosen:
            secs = [s for s in secs if s["title"] in chosen]
        elif scope.get("topic"):
            text, _ = build_context(scope["topic"], [], notes_md, transcript, vision, budget)
            return [text] if text.strip() else []
        if not secs:
            if chosen:
                return []
            from llm_synthesis import merge_speech
            speech = merge_speech(transcript, window_s=75) if transcript else []
            secs = [{"title": "Transkrypt", "text": s["text"]} for s in speech]
        groups, cur, used = [], [], 0
        for s in secs:
            block = f"## {s['title']}\n{s['text']}"
            cost = est_tokens(block)
            if cost > budget:
                block, cost = block[: budget * 3], budget
            if cur and used + cost > budget:
                groups.append("\n\n".join(cur))
                cur, used = [], 0
            cur.append(block)
            used += cost
        if cur:
            groups.append("\n\n".join(cur))
        return groups

    @staticmethod
    def _split_counts(count, groups):
        """Rozdziela liczbe pytan proporcjonalnie do dlugosci grup; przy malej liczbie wybiera rownomiernie rozstawione grupy."""
        if len(groups) > count:
            idx = sorted({round(i * (len(groups) - 1) / max(1, count - 1)) for i in range(count)}) if count > 1 else [len(groups) // 2]
            groups = [groups[i] for i in idx]
            sizes = [1] * len(groups)
            extra = count - len(groups)
            for i in range(extra):
                sizes[i % len(sizes)] += 1
            return list(zip(groups, sizes))
        weights = [max(1, len(g)) for g in groups]
        total = sum(weights)
        sizes = [max(1, round(count * w / total)) for w in weights]
        while sum(sizes) > count:
            sizes[sizes.index(max(sizes))] -= 1
        while sum(sizes) < count:
            sizes[sizes.index(min(sizes))] += 1
        return list(zip(groups, sizes))

    @contextlib.contextmanager
    def _wait_session(self, video_id, model, alive, wait_s=900):
        """Sesja modelu; gdy model zajmuje inna odpowiedz (czat, inny quiz), czeka w kolejce zamiast konczyc bledem.
        Zajete GPU przez zadanie przetwarzania nadal konczy sie bledem od razu."""
        deadline = time.time() + wait_s
        while True:
            cm = self.chat.session(video_id, model)
            try:
                cm.__enter__()
                break
            except ChatBusy as e:
                if not getattr(e, "transient", False) or time.time() > deadline:
                    raise
                if not alive():
                    raise _Deleted()
                time.sleep(0.5)
        try:
            yield
        finally:
            cm.__exit__(None, None, None)

    # ---------- generowanie ----------
    def _generate(self, video_id, qid):
        st = self.store(video_id)
        try:
            quiz = st.get(qid)
            self._run(video_id, st, quiz)
        except _Deleted:
            logger.info(f"quiz {video_id}/{qid}: usuniety w trakcie generowania")
        except (OllamaError, ChatError, QuizError) as e:
            logger.warning(f"quiz {video_id}/{qid}: {e}")
            self._fail(st, qid, str(e))
        except Exception as e:  # nieprzewidziany blad nie moze zostawic quizu w stanie "generating"
            logger.exception(f"quiz {video_id}/{qid}: nieoczekiwany blad")
            self._fail(st, qid, f"Nieoczekiwany błąd: {e}")
        finally:
            self._active.pop((video_id, qid), None)

    @staticmethod
    def _fail(st, qid, msg):
        try:
            st.update(qid, lambda q: q.update(status="error", error=msg))
        except QuizError:
            pass

    def _run(self, video_id, st, quiz):
        qid, qtype, count, model = quiz["id"], quiz["type"], quiz["requested"], quiz["model"]
        wd = self.lib.workdir(video_id)
        predict = int(self.qcfg.get("num_predict", 1500))
        num_ctx = self.chat.num_ctx()
        budget = max(1000, num_ctx - predict - 1200)
        groups = self._groups(wd, quiz["scope"], budget)
        if not groups:
            raise QuizError("Wybrany obszar nie zawiera materiału, z którego można ułożyć pytania.")
        plan = [{"material": m, "want": w, "got": 0, "topics": [], "used": [], "dry": False} for m, w in self._split_counts(count, groups)]
        limit = max(count + 1, int(count * float(self.qcfg.get("attempts_factor", 2))))
        ctx = {"questions": [], "stems": [], "rejected": {}, "attempts": 0, "limit": limit, "count": count, "qid": qid, "st": st,
               "video": video_id, "type": qtype, "model": model, "type_counts": {},
               "allowed": list((quiz["scope"].get("types") or list(CONCRETE))) if qtype == "auto" else [qtype],
               "opts": {"num_ctx": num_ctx, "num_predict": predict, "temperature": float(self.qcfg.get("temperature", 0.5))}}

        if quiz["scope"].get("adaptive"):
            self._seed_weak(video_id, ctx, plan, quiz["scope"])

        def need():
            return len(ctx["questions"]) < count and ctx["attempts"] < limit

        with self._wait_session(video_id, model, lambda: st.exists(qid)):
            for g in plan:  # faza A: kazda grupa materialu dostarcza swoja czesc pytan
                while need() and g["got"] < g["want"] and not g["dry"]:
                    self._step(ctx, g)
            while need() and not all(g["dry"] for g in plan):  # faza B: brakujace pytania z dowolnych grup, az do limitu prob
                for g in plan:
                    if need() and not g["dry"]:
                        self._step(ctx, g)
        questions, rejected = ctx["questions"], ctx["rejected"]
        why = "; ".join(f"{r} (×{n})" for r, n in sorted(rejected.items(), key=lambda x: -x[1])[:3])
        if not questions:
            raise QuizError("Model nie zwrócił poprawnych pytań" + (f" (odrzucone: {why})" if why else "")
                            + ". Spróbuj ponownie lub wybierz inny, większy model.")
        warn = (f"Udało się ułożyć {len(questions)} z {count} pytań mimo {ctx['attempts']} prób"
                + (f" (odrzucone: {why})" if why else "") + ".") if len(questions) < count else None
        st.update(qid, lambda x: x.update(status="ready", questions=questions, warning=warn, error=None,
                                          progress={"done": len(questions), "total": count}))
        logger.info(f"quiz {video_id}/{qid}: {len(questions)}/{count} pytan ({qtype}), prob: {ctx['attempts']}")

    def _seed_weak(self, video_id, ctx, plan, scope):
        """Tryb adaptacyjny: do kolejek zagadnien wstawia najpierw slabe strony (do ~70% pytan danej grupy), a stare pytania
        trafiaja na liste 'juz zadanych', zeby powstaly nowe pytania, nie powtorki."""
        weak = self.weak_topics(video_id)
        for g in plan:
            titles = re.findall(r"(?m)^## (.+)$", g["material"])
            mine = [w for w in weak if not titles or not w["section"] or any(_norm(w["section"]) in _norm(t) or _norm(t) in _norm(w["section"]) for t in titles)]
            if scope.get("sections") or scope.get("topic"):
                mine = [w for w in mine if titles and w["section"]]  # w ograniczonym obszarze tylko zagadnienia z jego sekcji
            cap = max(1, math.ceil(g["want"] * 0.7))
            for w in mine[:cap]:
                g["topics"].append({"topic": w["topic"], "section": w["section"], "type": w["type"]})
                ctx["stems"].extend(w["stems"])
            weak = [w for w in weak if w not in mine[:cap]]
        ctx["weak_seeded"] = sum(len(g["topics"]) for g in plan)

    def _choose_type(self, ctx, suggested):
        """Typ pytania w trybie mieszanym: podpowiedz modelu, ale z limitami (zaden typ >50% quizu, opisowe <=20%) i zroznicowaniem."""
        allowed, counts, total = ctx["allowed"], ctx["type_counts"], ctx["count"]
        if len(allowed) == 1:
            return allowed[0]

        def cap(t):
            return max(1, math.ceil(total * (0.2 if t == "open" else 0.5)))

        def room(t):
            return counts.get(t, 0) < cap(t)
        if suggested in allowed and room(suggested):
            return suggested
        free = [t for t in allowed if room(t)] or allowed
        return min(free, key=lambda t: counts.get(t, 0))

    def _reject(self, ctx, reason, detail=None):
        ctx["rejected"][reason] = ctx["rejected"].get(reason, 0) + 1
        if detail is not None:
            logger.info(f"quiz {ctx['video']}/{ctx['qid']}: odrzucono ({reason}): {detail}")

    def _plan_topics(self, ctx, g):
        """Etap 1: lista roznych zagadnien do zapytania (z zapasem), z pominieciem juz uzytych."""
        k = max(2, math.ceil((g["want"] - g["got"]) * 1.5))
        avoid = ("\n\nZagadnienia już wykorzystane (nie powtarzaj ich):\n" + "\n".join(f"- {t}" for t in g["used"][-30:])) if g["used"] else ""
        prompt = (f"Zaplanuj quiz sprawdzający zrozumienie wykładu. Wypisz {k} RÓŻNYCH zagadnień (pojęcie, twierdzenie, mechanizm, "
                  "zależność lub zastosowanie) z poniższego materiału, z których da się ułożyć dobre pytanie. Zagadnienia mają dotyczyć "
                  "różnych części materiału. Każde opisz krótko (kilka słów) i podaj tytuł sekcji, z której pochodzi. "
                  f'Format: {{"topics": [{{"topic": "...", "section": "tytuł sekcji"}}]}}{avoid}\n\n# Materiał\n{g["material"]}')
        if ctx["type"] == "auto":
            prompt = prompt.replace('Format: {"topics": [{"topic": "...", "section": "tytuł sekcji"}]}',
                                    "Dla każdego zagadnienia wybierz też NAJLEPSZY typ pytania sprawdzającego je: " + TYPE_HINTS
                                    + f'. Dozwolone typy: {", ".join(ctx["allowed"])}. Format: {{"topics": [{{"topic": "...", "section": "tytuł sekcji", "type": "typ"}}]}}', 1)
        try:
            raw = self.client.generate(ctx["model"], prompt, system=SYSTEM, options=ctx["opts"], keep_alive="10m", format="json")
        except OllamaError:
            raise
        data = extract_json(raw)
        items = data.get("topics") if isinstance(data, dict) else data if isinstance(data, list) else None
        if items is None:
            items = _salvage_objects(raw or "")
        out = []
        for it in items or []:
            if isinstance(it, str):
                it = {"topic": it}
            if not isinstance(it, dict):
                continue
            t = _text(_pick(it, "topic", "zagadnienie", "temat"), 200)
            if t and not too_similar(t, g["used"] + [x["topic"] for x in out]):
                out.append({"topic": t, "section": _text(_pick(it, "section", "sekcja"), 160), "type": _text(_pick(it, "type", "typ"), 20).lower()})
        return out

    @staticmethod
    def _focus(material, section):
        """Fragment materialu odpowiadajacy sekcji zagadnienia (mniejszy prompt, pytanie z wlasciwego miejsca)."""
        blocks = re.split(r"(?m)^(?=## )", material)
        if len(blocks) < 2 or not section:
            return material
        ns = _norm(section)
        for i, b in enumerate(blocks):
            title = _norm(b.split("\n", 1)[0].lstrip("# "))
            if title and (title == ns or title in ns or ns in title):
                return b + (blocks[i + 1] if len(b) < 1200 and i + 1 < len(blocks) else "")
        return material

    def _step(self, ctx, g):
        """Jedna proba: bierze kolejne zagadnienie (w razie potrzeby planuje nowe) i prosi o jedno pytanie."""
        st, qid = ctx["st"], ctx["qid"]
        if not st.exists(qid):
            raise _Deleted()
        if not g["topics"]:
            g["topics"] = self._plan_topics(ctx, g)
            if not g["topics"]:
                if g.get("planless"):
                    g["dry"] = True  # model nie potrafi juz wskazac nowych zagadnien z tej grupy
                    return
                g["planless"] = True
                g["topics"] = [{"topic": None, "section": ""}]  # awaryjnie: pytanie bez narzuconego zagadnienia
        item = g["topics"].pop(0)
        ctx["attempts"] += 1
        topic = item["topic"]
        qtype = self._choose_type(ctx, item.get("type")) if ctx["type"] == "auto" else ctx["type"]
        if topic:
            g["used"].append(topic)
        material = self._focus(g["material"], item["section"]) if topic else g["material"]
        prompt = self._prompt(qtype, material, ctx["stems"], topic)
        raw = self.client.generate(ctx["model"], prompt, system=SYSTEM, options=ctx["opts"], keep_alive="10m", format="json")
        found = questions_of(raw)
        if not found:
            self._reject(ctx, "nieczytelna odpowiedz modelu")
            logger.warning(f"quiz {ctx['video']}/{qid}: nie rozpoznano pytan w odpowiedzi modelu: {(raw or '')[:400]!r}")
            return
        for r in found[:1]:
            try:
                q = _normalize(r, qtype)
            except ValueError as e:
                self._reject(ctx, str(e), json.dumps(r, ensure_ascii=False)[:300])
                return
            if too_similar(q["question"], ctx["stems"]):
                self._reject(ctx, "powtorzone pytanie")
                return
            q["id"] = uuid.uuid4().hex[:6]
            if topic:
                q["topic"] = topic
            ctx["questions"].append(q)
            ctx["type_counts"][qtype] = ctx["type_counts"].get(qtype, 0) + 1
            ctx["stems"].append(q["question"])
            g["got"] += 1
            done = len(ctx["questions"])
            st.update(qid, lambda x, d=done, qs=list(ctx["questions"]): x.update(progress={"done": d, "total": ctx["count"]}, questions=qs))

    @staticmethod
    def _prompt(qtype, material, stems, topic=None):
        avoid = ("\n\nNie powtarzaj tych pytań:\n" + "\n".join(f"- {s[:140]}" for s in stems[-20:])) if stems else ""
        focus = f"\nPytanie ma dotyczyć zagadnienia: «{topic}»." if topic else ""
        # material na poczatku: Ollama zachowuje wspolny prefiks promptu w pamieci podrecznej, wiec kolejne pytania z tego samego materialu sa tansze
        return (f"# Materiał\n{material}\n\n# Zadanie\nUłóż 1 pytanie na podstawie powyższego materiału z wykładu.{focus}\n{FORMATS[qtype]}{avoid}")

    # ---------- podejscia ----------
    def start_attempt(self, video_id, qid):
        st = self.store(video_id)
        quiz = st.get(qid)
        if quiz.get("status") != "ready":
            raise QuizError("Quiz nie jest jeszcze gotowy.")
        att = {"id": uuid.uuid4().hex[:6], "started": time.time(), "answers": {}, "finished": None, "percent": None}

        def add(q):
            q["attempts"] = [a for a in q.get("attempts", []) if a.get("finished")] + [att]  # porzucone podejscia znikaja
        st.update(qid, add)
        return att

    def answer(self, video_id, qid, aid, question_id, answer, self_score=None):
        st = self.store(video_id)
        quiz = st.get(qid)
        att = next((a for a in quiz.get("attempts", []) if a["id"] == aid), None)
        if att is None:
            raise QuizError("nie ma takiego podejscia")
        q = next((x for x in quiz["questions"] if x["id"] == question_id), None)
        if q is None:
            raise QuizError("nie ma takiego pytania")
        result = self._grade(video_id, quiz, q, answer, self_score)
        result["answer"] = answer

        def apply(qz):
            a = next(x for x in qz["attempts"] if x["id"] == aid)
            a["answers"][question_id] = result
            if all(x["id"] in a["answers"] for x in qz["questions"]) and not a["finished"]:
                total = sum(v["score"] for v in a["answers"].values())
                a["percent"] = round(100 * total / len(qz["questions"]), 1)
                a["finished"] = time.time()
            elif a["finished"]:  # poprawka oceny po zakonczeniu (np. samoocena) przelicza wynik
                a["percent"] = round(100 * sum(v["score"] for v in a["answers"].values()) / len(qz["questions"]), 1)
        quiz = st.update(qid, apply)
        att = next(x for x in quiz["attempts"] if x["id"] == aid)
        return {"result": result, "attempt": {"id": aid, "finished": att["finished"], "percent": att["percent"],
                                              "answered": len(att["answers"])},
                "summary": summarize(quiz, self.pass_ratio)}

    def _grade(self, video_id, quiz, q, answer, self_score):
        if self_score is not None:  # reczna ocena uzytkownika (np. gdy model nie mogl ocenic albo nazwa jest synonimem)
            s = float(self_score)
            if s not in (0.0, 0.5, 1.0):
                raise QuizError("Niepoprawna ocena.")
            return {"score": s, "correct": s == 1.0, "graded_by": "self"}
        t = q["type"]
        if t in ("single", "multi"):
            sel = answer if isinstance(answer, list) else []
            if not all(isinstance(i, int) and 0 <= i < len(q["options"]) for i in sel) or not sel:
                raise QuizError("Zaznacz co najmniej jedną odpowiedź.")
            s = grade_choice(q, sel)
            return {"score": s, "correct": s == 1.0, "graded_by": "auto"}
        text = (answer if isinstance(answer, str) else "").strip()
        if not text:
            raise QuizError("Odpowiedź nie może być pusta.")
        if len(text) > MAX_ANSWER:
            raise QuizError(f"Odpowiedź jest zbyt długa (limit {MAX_ANSWER} znaków).")
        if t == "term":
            ok = grade_term(q, text)
            return {"score": 1.0 if ok else 0.0, "correct": ok, "graded_by": "auto"}
        return self._grade_open(video_id, quiz, q, text)

    def _grade_open(self, video_id, quiz, q, text):
        """Ocena odpowiedzi opisowej modelem. Gdy model niedostepny, zwraca graded=False i UI prosi o samoocene."""
        prompt = GRADE_PROMPT.format(question=q["question"], model_answer=q["model_answer"],
                                     key_points="; ".join(q.get("key_points") or []) or "(brak)", answer=text)
        try:
            with self.chat.session(video_id, quiz.get("model") or self.chat.default_model()):
                raw = self.client.generate(quiz.get("model") or self.chat.default_model(), prompt, system=None,
                                           options={"num_ctx": self.chat.num_ctx(), "num_predict": 400, "temperature": 0.1},
                                           keep_alive="10m", format="json")
        except (OllamaError, ChatError) as e:
            return {"score": 0.0, "correct": False, "graded": False, "graded_by": "none", "error": str(e)}
        data = extract_json(raw)
        try:
            score = float(data["score"])
        except (TypeError, KeyError, ValueError):
            return {"score": 0.0, "correct": False, "graded": False, "graded_by": "none", "error": "Model zwrócił niepoprawną ocenę."}
        score = 1.0 if score >= 0.75 else 0.5 if score >= 0.25 else 0.0
        return {"score": score, "correct": score == 1.0, "graded": True, "graded_by": "llm", "feedback": _text(data.get("feedback"), 800)}
