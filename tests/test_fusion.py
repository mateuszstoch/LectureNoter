import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm_synthesis as ls  # noqa: E402
import vlm_module  # noqa: E402

CFG_LLM = {"chunk_minutes": 10, "num_predict": 500, "temperature": 0.2}


class FakeClient:
    def __init__(self):
        self.calls, self.unloaded = [], []

    def check(self): pass
    def assert_vram_free(self): pass

    def generate(self, model, prompt, **kw):
        self.calls.append((model, prompt, kw))
        if "system" in kw and kw["system"] == ls.SUMMARY_SYSTEM:
            return "Podsumowanie."
        return "# Temat\n## Sekcja\n- punkt"

    def unload(self, model): self.unloaded.append(model)


def test_merge_speech_groups_paragraphs():
    segs = [{"start": i * 10, "end": i * 10 + 9, "text": f"zdanie {i}"} for i in range(10)]
    items = ls.merge_speech(segs, window_s=45)
    assert len(items) == 2 and "zdanie 0 zdanie 1" in items[0]["text"]


def test_dedupe_board_keeps_fullest():
    f = [{"timestamp": 10, "text": "Cel\n- a"}, {"timestamp": 20, "text": "Cel\n- a\n- b"}, {"timestamp": 30, "text": "Zupelnie inna plansza o czyms innym"}]
    out = ls.dedupe_board(f, ratio=0.6)
    assert len(out) == 2 and out[0]["text"].endswith("- b")


def test_chunking_respects_budget_and_time():
    items = [{"t": i * 60, "kind": "Mowa", "text": "x" * 300} for i in range(30)]
    chunks = ls.chunk_timeline(items, token_budget=10 ** 6, max_minutes=10)
    assert len(chunks) == 3 and sum(map(len, chunks)) == 30
    chunks = ls.chunk_timeline(items, token_budget=400, max_minutes=1000)
    assert all(sum(ls.est_tokens(ls.render(i)) for i in c) <= 400 + 110 for c in chunks)


def test_timeline_order_and_render():
    tl = ls.build_timeline([{"start": 5, "end": 6, "text": "mowa"}], [{"timestamp": 5, "text": "tab"}])
    assert [i["kind"] for i in tl] == ["Mowa", "Tablica"]
    assert ls.render(tl[0]).startswith("[00:00:05] [Mowa]")


def test_repetition_cleanup():
    assert vlm_module.clean_repetitions("a\n" * 10).count("a") == 3


def test_synthesis_resume_and_unload(tmp_path):
    (tmp_path / "transcript.json").write_text(json.dumps(
        [{"start": i * 120, "end": i * 120 + 5, "text": "tresc " * 50} for i in range(12)]), encoding="utf-8")
    c = FakeClient()
    out = ls.run_synthesis(tmp_path, CFG_LLM, "m", 4096, client=c)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Notatki z wykładu") and "\n# Temat" not in text
    n_first = len(c.calls)
    assert c.unloaded == ["m"]
    c2 = FakeClient()
    ls.run_synthesis(tmp_path, CFG_LLM, "m", 4096, client=c2)
    assert len(c2.calls) == 2 and n_first > 3  # tylko podsumowanie i podpowiedzi; chunki z cache


def test_vlm_resume_and_unload(tmp_path):
    import cv2, numpy as np
    (tmp_path / "frames").mkdir()
    meta = []
    for i in range(3):
        cv2.imwrite(str(tmp_path / "frames" / f"f{i}.jpg"), np.full((100, 200, 3), 255, np.uint8))
        meta.append({"file": f"f{i}.jpg", "timestamp": i * 10.0, "index": i})
    (tmp_path / "frames_metadata.json").write_text(json.dumps(meta))
    c = FakeClient()
    cfg = {"model": "v", "num_predict": 50, "keep_alive": "10m"}
    res = vlm_module.run_vlm(tmp_path, cfg, client=c)
    assert len(res) == 3 and c.unloaded == ["v"] and len(c.calls) == 3
    c2 = FakeClient()
    vlm_module.run_vlm(tmp_path, cfg, client=c2)
    assert c2.calls == [] and c2.unloaded == []


def test_strip_fences_wrapper_and_stray():
    md = "```markdown\n## A\n- x\n```\n\n### B\n- y\n```\n\n```markdown\n## C\n```"
    out = ls.strip_fences(md)
    assert "```" not in out and "## A" in out and "## C" in out


def test_strip_fences_keeps_labeled_code():
    md = "```markdown\n## A\n```python\nprint(1)\n```\n```"
    out = ls.strip_fences(md)
    assert out.count("```") == 2 and "```python" in out


def test_clean_chunk_removes_meta_and_normalizes_latex():
    md = ("## A\ntekst \\( x^2 \\) i \\[ y \\]\n\n## Fragment 8 z 10 wykładu\n**[01:01:50]** - [01:11:28]\n"
          "Ten fragment notatek zawiera definicje.\n**Uwagi:**\n- coś\n- coś2\n\n## B\n- ok")
    out = ls.clean_chunk_md(md)
    assert "$x^2$" in out and "$$ y $$" in out
    assert "Fragment 8" not in out and "Uwagi" not in out and "coś" not in out and "## B" in out


def test_cjk_retry_and_drop(tmp_path):
    (tmp_path / "transcript.json").write_text(json.dumps([{"start": 0, "end": 5, "text": "tresc"}]), encoding="utf-8")

    class C(FakeClient):
        def generate(self, model, prompt, **kw):
            self.calls.append(prompt)
            if kw.get("system") == ls.SUMMARY_SYSTEM:
                return "Ok."
            return "## T\n- po polsku\n- 这是中文"

    c = C()
    out = ls.run_synthesis(tmp_path, CFG_LLM, "m", 4096, client=c).read_text(encoding="utf-8")
    assert "中文" not in out and "po polsku" in out


def test_second_chunk_gets_covered_topics(tmp_path):
    (tmp_path / "transcript.json").write_text(json.dumps(
        [{"start": i * 700, "end": i * 700 + 5, "text": "tresc"} for i in range(3)]), encoding="utf-8")
    c = FakeClient()
    ls.run_synthesis(tmp_path, CFG_LLM, "m", 4096, client=c)
    prompts = [call[1] for call in c.calls]
    assert "Tematy już opisane" not in prompts[0] and "Sekcja" in prompts[1]
