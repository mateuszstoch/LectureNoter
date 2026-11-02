import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import asr_module  # noqa: E402
import llm_synthesis as ls  # noqa: E402
import textguard as tg  # noqa: E402
import vlm_module  # noqa: E402

LOOP = "relacji " * 400 + "rel"  # jak w prawdziwym dokumencie: tysiace powtorzen i ucieta koncowka


def test_detects_and_collapses_real_world_loop():
    text = "Zaczęliśmy od definicji maszyny Turinga, konfiguracji oraz " + LOOP + "\n\n## Spis treści\n- Definicja maszyny"
    loops = tg.find_loops(text)
    assert len(loops) == 1 and loops[0]["repeats"] >= 400
    fixed = tg.collapse_loops(text)
    assert fixed.startswith("Zaczęliśmy od definicji maszyny Turinga, konfiguracji oraz relacji") and "relacji relacji" not in fixed
    assert fixed.endswith("## Spis treści\n- Definicja maszyny") and not tg.has_loop(fixed)
    assert "rel\n" not in fixed.replace("relacji", "")  # ucieta kopia 'rel' tez znika


def test_phrase_loops_detected():
    phrase = "Maszyna Turinga akceptuje język. "
    assert tg.has_loop("Wstęp. " + phrase * 6 + "Koniec.")
    assert tg.collapse_loops(phrase * 6).count("Maszyna Turinga") == 1


@pytest.mark.parametrize("legit", [
    "0 1 0 1 0 1 0 1 0 1 0 1",            # slowa binarne
    "1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1",     # zapis unarny
    "q q q q q q q q q q q q q q",         # stany maszyny
    "tak tak, nie nie, dobrze dobrze",
    "Dla każdego słowa w należącego do L maszyna akceptuje słowo w i kończy działanie.",
    "a b a b a b a b",
])
def test_no_false_positives_on_lecture_content(legit):
    assert not tg.has_loop(legit) and tg.collapse_loops(legit) == legit


def test_clean_heading_limits_length_and_loops():
    assert tg.clean_heading("Definicja " + "relacji " * 50) == "Definicja relacji"
    assert len(tg.clean_heading("x" * 300)) <= 90


# ---------- generowanie z kontrola ----------
class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def check(self): pass
    def assert_vram_free(self): pass
    def unload(self, m): pass

    def generate(self, model, prompt, **kw):
        self.calls.append((prompt, kw.get("options")))
        r = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return r(prompt) if callable(r) else r


GOOD = "## Definicja maszyny Turinga\n- osiem elementów: alfabet, taśma, stany, funkcja przejścia"
BAD = "## Definicja relacji\n" + LOOP


def test_retry_changes_sampling_after_loop():
    c = Scripted([BAD, GOOD])
    text, attempts, degraded = ls.generate_checked(c, "m", "p", "s", {"temperature": 0.3, "repeat_penalty": 1.1}, 2, "t")
    assert attempts == 2 and degraded is False and "relacji relacji" not in text
    first, second = c.calls[0][1], c.calls[1][1]
    assert second["temperature"] > first["temperature"] and second["repeat_penalty"] > first["repeat_penalty"]
    assert "seed" not in first and second["seed"] != 0


def test_all_attempts_loop_gives_repaired_degraded_result():
    c = Scripted([BAD])
    text, attempts, degraded = ls.generate_checked(c, "m", "p", "s", {"temperature": 0.3}, 2, "t")
    assert attempts == 3 and degraded is True and len(c.calls) == 3
    assert not tg.has_loop(text) and text.startswith("## Definicja relacji")


def timeline_workdir(tmp_path, n_chunks=3):
    # po jednym akapicie mowy na kazdy 10-minutowy fragment
    tr = [{"start": i * 700, "end": i * 700 + 5, "text": f"Omawiamy temat numer {i} z wykładu"} for i in range(n_chunks)]
    (tmp_path / "transcript.json").write_text(json.dumps(tr), encoding="utf-8")
    return tmp_path


def test_looped_heading_does_not_poison_following_chunks(tmp_path):
    """Regresja z prawdziwego dokumentu: zapetlony naglowek z 1. fragmentu trafial do listy 'tematy juz opisane'."""
    wd = timeline_workdir(tmp_path)
    c = Scripted([BAD, GOOD, GOOD, "Streszczenie wykładu o maszynach Turinga i ich definicjach formalnych."])
    cfg = {"chunk_minutes": 10, "num_predict": 300, "temperature": 0.3, "loop_retries": 0}
    ls.run_synthesis(wd, cfg, "m", 4096, client=c)
    second_prompt = c.calls[1][0]
    assert "Tematy już opisane" in second_prompt and "relacji relacji" not in second_prompt
    assert second_prompt.count("relacji") <= 2
    meta = json.loads((wd / "notes_meta.json").read_text())
    assert meta["degraded_chunks"] == [1] and meta["chunks"] == 3
    notes = (wd / "lecture_notes.md").read_text(encoding="utf-8")
    assert not tg.has_loop(notes) and len(notes) < 5000


def test_looped_summary_is_omitted(tmp_path):
    wd = timeline_workdir(tmp_path, 1)
    c = Scripted([GOOD, "Wykład dotyczy " + "relacji " * 100])
    cfg = {"chunk_minutes": 10, "num_predict": 300, "temperature": 0.3}
    ls.run_synthesis(wd, cfg, "m", 4096, client=c)
    notes = (wd / "lecture_notes.md").read_text(encoding="utf-8")
    assert "## Podsumowanie" not in notes and "relacji relacji" not in notes and "## Spis treści" in notes
    assert json.loads((wd / "notes_meta.json").read_text())["summary_omitted"] is True


def test_old_looped_cache_is_repaired_on_reassembly(tmp_path):
    wd = timeline_workdir(tmp_path, 1)
    (wd / "chunks").mkdir()
    (wd / "chunks" / "chunk_01.md").write_text(BAD, encoding="utf-8")
    c = Scripted(["Streszczenie wykładu o maszynach Turinga i ich definicjach formalnych."])
    ls.run_synthesis(wd, {"chunk_minutes": 10, "num_predict": 300, "temperature": 0.3}, "m", 4096, client=c)
    assert not tg.has_loop((wd / "lecture_notes.md").read_text(encoding="utf-8"))
    assert json.loads((wd / "notes_meta.json").read_text())["degraded_chunks"] == [1]


# ---------- scalanie powtorzonych sekcji ----------
def test_merge_duplicate_sections_across_chunks():
    d1 = "## Definicja maszyny Turinga\nMaszyna Turinga M to osiem elementów: alfabet wejściowy, alfabet roboczy, stany.\n## Teza Churcha\nPojęcie algorytmu."
    d2 = "## Definicja Maszyny Turinga\nMaszyna Turinga M to osiem elementów: alfabet wejściowy, alfabet roboczy oraz stany.\n## Maszyny k-taśmowe\nMają k taśm."
    other = "## Definicja maszyny Turinga\nZupełnie inna treść: opis działania maszyny na przykładzie słowa 0110 krok po kroku."
    merged, dropped = ls.merge_duplicate_sections([d1, d2, other])
    assert dropped == 1
    assert "Definicja Maszyny Turinga" not in merged[1] and "Maszyny k-taśmowe" in merged[1]
    assert "Zupełnie inna treść" in merged[2]  # ten sam tytul, ale inna tresc: zostaje
    assert "Teza Churcha" in merged[0]


# ---------- ASR i VLM ----------
def test_asr_drops_repeated_segments_and_inner_loops():
    segs = [{"start": 0, "end": 1, "text": "Dzień dobry"}] + \
           [{"start": i, "end": i + 1, "text": "Dziękuję bardzo."} for i in range(1, 30)] + \
           [{"start": 40, "end": 41, "text": "to jest " + "tak " * 30 + "koniec"}]
    out = asr_module.drop_repeated_segments(segs)
    texts = [s["text"] for s in out]
    assert texts.count("Dziękuję bardzo.") == 2 and texts[0] == "Dzień dobry"
    assert not tg.has_loop(texts[-1]) and texts[-1].startswith("to jest tak")


def test_vlm_text_cleanup_collapses_word_loops():
    out = vlm_module.clean_repetitions("Definicja " + "relacji " * 200 + "\n- stan q0\n- stan q1")
    assert "relacji relacji" not in out and "stan q1" in out


def test_notes_endpoint_exposes_quality(tmp_path):
    from ui import notes as nm
    (tmp_path / "lecture_notes.md").write_text("# n", encoding="utf-8")
    assert nm.get_active(tmp_path)["quality"] is None
    (tmp_path / "notes_meta.json").write_text(json.dumps({"chunks": 4, "degraded_chunks": [2]}))
    assert nm.get_active(tmp_path)["quality"]["degraded_chunks"] == [2]
