import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as m  # noqa: E402
from gpu_lock import GpuLock  # noqa: E402
from server import create_app  # noqa: E402
from ui.jobs import JobError, JobManager  # noqa: E402
from ui.library import Library, LibraryError  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config.yaml"))

FAKE = f"""
import sys, time, os
sys.path.insert(0, {str(ROOT)!r})
from run_state import write_run_state
wd, mode = sys.argv[1], sys.argv[2]
write_run_state(wd, status="running", pid=os.getpid(), heartbeat=time.time(), current_stage="asr")
if mode == "ok":
    write_run_state(wd, status="done", current_stage=None)
elif mode == "fail":
    write_run_state(wd, status="error", error="boom"); sys.exit(1)
elif mode == "sleep":
    time.sleep(30)
"""


def fake_cmd(job, wd, meta):
    mode = job["video_id"].split("-")[0]
    return [sys.executable, "-c", FAKE, str(wd), mode]


def make_video(path, secs=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5",
                    "-t", str(secs), "-pix_fmt", "yuv420p", str(path)], check=True)


@pytest.fixture
def env(tmp_path):
    videos, work = tmp_path / "videos", tmp_path / "work"
    for name in ("ok.mp4", "fail.mp4", "sleep.mp4", "other.mp4", "sub/nested.mp4", "a/b/c/deep.mp4"):
        make_video(videos / name)
    (videos / "notatki.txt").write_text("x")
    (videos / "garbage.mp4").write_bytes(b"to nie jest wideo")
    lib = Library(videos, work, CFG)
    gpu = GpuLock(work / ".gpu.lock")
    jm = JobManager(lib, gpu, cmd_builder=fake_cmd)
    return lib, gpu, jm, videos, work


def wait_for(jm, job_id, status, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        jm.tick()
        j = next(j for j in jm.snapshot()["jobs"] if j["id"] == job_id)
        if j["status"] == status:
            return j
        time.sleep(0.1)
    raise AssertionError(f"zadanie {job_id} nie osiagnelo {status}: {j}")


# ---------- Library ----------
def test_list_files_filters(env):
    lib, *_ = env
    files = {f["file"] for f in lib.list_files()}
    assert "ok.mp4" in files and "sub/nested.mp4" in files
    assert "notatki.txt" not in files and "a/b/c/deep.mp4" not in files


def test_add_video_and_idempotent(env):
    lib, *_ = env
    vid = lib.add_video("ok.mp4")
    assert vid == "ok" and lib.add_video("ok.mp4") == "ok"
    assert (lib.workdir("ok") / "meta.json").exists()
    assert {f["file"]: f["video_id"] for f in lib.list_files()}["ok.mp4"] == "ok"
    s = lib.summary("ok")
    assert s["overall"] == "idle" and s["title"] == "ok" and s["duration"] > 0


@pytest.mark.parametrize("bad", ["../outside.mp4", "/etc/passwd", "notatki.txt", "garbage.mp4", "brak.mp4"])
def test_add_video_rejects_bad(env, bad):
    lib, *_ = env
    with pytest.raises(LibraryError):
        lib.add_video(bad)


def test_symlink_inside_videos_is_accepted(env, tmp_path):
    lib, _, _, videos, _ = env
    outside = tmp_path / "poza" / "wyklad.mp4"
    make_video(outside)
    (videos / "link.mp4").symlink_to(outside)
    assert "link.mp4" in {f["file"] for f in lib.list_files()}
    vid = lib.add_video("link.mp4")
    assert lib.summary(vid)["video_path"] == str(outside.resolve())


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "A B", ".."])
def test_workdir_rejects_bad_ids(env, bad):
    with pytest.raises(LibraryError):
        env[0].workdir(bad)


# ---------- JobManager ----------
def test_job_done_error_cancel_and_duplicates(env):
    lib, gpu, jm, *_ = env
    for f in ("ok.mp4", "fail.mp4", "sleep.mp4"):
        lib.add_video(f)
    j_ok = jm.enqueue("ok")
    with pytest.raises(JobError):
        jm.enqueue("ok")  # duplikat aktywnego zadania
    assert wait_for(jm, j_ok["id"], "done")["finished"]
    j_fail = jm.enqueue("fail")
    j = wait_for(jm, j_fail["id"], "error")
    assert j["error"] == "boom"
    j_sleep = jm.enqueue("sleep")
    wait_for(jm, j_sleep["id"], "running")
    jm.cancel(j_sleep["id"])
    assert wait_for(jm, j_sleep["id"], "cancelled")


def test_only_one_running_and_order(env):
    lib, gpu, jm, *_ = env
    lib.add_video("sleep.mp4"); lib.add_video("ok.mp4"); lib.add_video("other.mp4")
    a, b, c = (jm.enqueue(v) for v in ("sleep", "ok", "other"))
    jm.tick()
    st = {j["id"]: j["status"] for j in jm.snapshot()["jobs"]}
    assert st == {a["id"]: "running", b["id"]: "queued", c["id"]: "queued"}
    jm.move(c["id"], 0)
    assert [j["id"] for j in jm.snapshot()["jobs"] if j["status"] == "queued"] == [c["id"], b["id"]]
    jm.cancel(a["id"])
    wait_for(jm, a["id"], "cancelled")
    jc = wait_for(jm, c["id"], "done")  # c wyprzedzilo b
    jb = wait_for(jm, b["id"], "done")
    assert jc["finished"] <= jb["started"]


def test_cancel_queued_job(env):
    lib, gpu, jm, *_ = env
    lib.add_video("ok.mp4")
    jm.set_paused(True)
    j = jm.enqueue("ok")
    assert jm.cancel(j["id"])["status"] == "cancelled"
    with pytest.raises(JobError):
        jm.cancel(j["id"])


def test_pause_blocks_start(env):
    lib, gpu, jm, *_ = env
    lib.add_video("ok.mp4")
    jm.set_paused(True)
    j = jm.enqueue("ok")
    for _ in range(3):
        jm.tick()
    assert jm.snapshot()["jobs"][0]["status"] == "queued"
    jm.set_paused(False)
    wait_for(jm, j["id"], "done")


def test_blocked_by_gpu_holder_and_preempt(env):
    lib, gpu, jm, *_ = env
    lib.add_video("ok.mp4")
    other = GpuLock(gpu.path)
    assert other.acquire("chat")
    j = jm.enqueue("ok")
    jm.tick()
    snap = jm.snapshot()
    assert snap["jobs"][0]["status"] == "queued" and snap["blocked_by"]["owner"] == "chat"
    jm.set_preempt(other.release)  # czat tego samego procesu zwalnia GPU
    wait_for(jm, j["id"], "done")


def test_queue_persists_and_recovers_dead_running(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4"); lib.add_video("other.mp4")
    a = jm.enqueue("ok"); jm.enqueue("other")
    jm.tick()  # a: running
    jm.stop()
    data = json.loads((work / "queue.json").read_text())
    for j in data["jobs"]:
        if j["id"] == a["id"]:
            j.update(status="running", pid=99999999)
    (work / "queue.json").write_text(json.dumps(data))
    jm2 = JobManager(lib, gpu, cmd_builder=fake_cmd)
    st = {j["id"]: j["status"] for j in jm2.snapshot()["jobs"]}
    assert st[a["id"]] == "interrupted" and "queued" in st.values()


# ---------- GpuLock i orkiestrator ----------
def test_gpu_lock_between_objects(tmp_path):
    a, b = GpuLock(tmp_path / "l"), GpuLock(tmp_path / "l")
    assert a.holder() is None and a.acquire("pipeline") and not b.acquire("chat")
    assert b.holder()["owner"] == "pipeline"
    a.release()
    assert b.holder() is None and b.acquire("chat")
    b.release()


def test_orchestrator_refuses_when_gpu_busy(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    busy = GpuLock(tmp_path / ".gpu.lock")
    busy.acquire("chat")
    called = []
    o = m.PipelineOrchestrator(video, tmp_path / "w", CFG, profile="8gb",
                               runners={s: (lambda st: called.append(st)) for s in m.STAGES})
    with pytest.raises(RuntimeError, match="GPU zajete przez: chat"):
        o.run()
    assert called == [] and not (tmp_path / "w" / "run_state.json").exists()
    busy.release()
    o.has_audio = False
    o.run(only_stage="frames")  # etap CPU nie potrzebuje blokady
    assert called == ["frames"]


# ---------- API ----------
@pytest.fixture
def client(env):
    lib, gpu, jm, videos, work = env
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",))
    return TestClient(app), jm


def test_api_flow(client):
    c, jm = client
    assert c.get("/api/health").json() == {"ok": True}
    assert "ok.mp4" in {f["file"] for f in c.get("/api/library/files").json()}
    r = c.post("/api/videos", json={"files": ["ok.mp4"], "enqueue": False})
    assert r.json() == {"ids": ["ok"]}
    v = c.get("/api/videos").json()
    assert v[0]["id"] == "ok" and v[0]["overall"] == "idle"
    assert c.get("/api/videos/ok").json()["title"] == "ok"
    assert c.get("/api/videos/ok/impact", params={"stage": "vlm"}).json()["removes"] == []
    job = c.post("/api/videos/ok/run", json={"from_stage": "vlm", "force": True}).json()
    assert job["status"] == "queued" and job["from_stage"] == "vlm"
    assert c.post("/api/videos/ok/run", json={}).status_code == 400  # duplikat
    assert len(c.get("/api/queue").json()["jobs"]) == 1
    assert c.post("/api/queue/pause").json()["paused"] is True
    assert c.post(f"/api/queue/{job['id']}/cancel").json()["status"] == "cancelled"


def test_api_rejects_bad_input(client):
    c, _ = client
    assert c.post("/api/videos", json={"files": ["../x.mp4"]}).status_code == 400
    assert c.get("/api/videos/nie-ma").status_code == 400
    assert c.get("/api/videos/..%2Fetc").status_code in (400, 404)
    assert c.get("/api/videos/ok/impact", params={"stage": "zly"}).status_code in (400, 422)
    c.post("/api/videos", json={"files": ["ok.mp4"], "enqueue": False})
    assert c.post("/api/videos/ok/run", json={"from_stage": "zly"}).status_code == 400
    assert c.post("/api/videos/ok/run", json={"from_stage": "vlm", "only_stage": "llm"}).status_code == 400


def test_api_host_check(client):
    c, _ = client
    assert c.get("/api/health", headers={"host": "evil.example.com"}).status_code == 400


def test_api_stream_and_logs_once(client, env):
    c, _ = client
    c.post("/api/videos", json={"files": ["ok.mp4"], "enqueue": False})
    body = c.get("/api/stream", params={"once": "true"}).text
    assert body.startswith("event: state")
    state = json.loads(body.split("data: ", 1)[1].split("\n\n")[0])
    assert state["videos"][0]["id"] == "ok" and state["queue"]["jobs"] == []
    wd = env[0].workdir("ok")
    (wd / "pipeline.log").write_text("linia 1\nlinia 2\n", encoding="utf-8")
    logs = c.get("/api/videos/ok/logs", params={"once": "true"}).text
    assert "linia 1" in logs and "linia 2" in logs


def test_static_frontend_served_and_api_not_shadowed(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "LectureNoter" in r.text and r.headers["cache-control"] == "no-cache"
    assert c.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert c.get("/sw.js").status_code == 200 and c.get("/app.js").status_code == 200
    assert c.get("/icons/icon-512.png").headers["content-type"] == "image/png"
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/nie-ma-takiego-pliku").status_code == 404


# ---------- notatki i materialy ----------
@pytest.fixture
def notes_env(client, env):
    c, jm = client
    c.post("/api/videos", json={"files": ["ok.mp4"], "enqueue": False})
    return c, env[0].workdir("ok")


def test_notes_missing_then_generated_edit_history_revert(notes_env):
    c, wd = notes_env
    assert c.get("/api/videos/ok/notes").status_code == 404
    (wd / "lecture_notes.md").write_text("# Wygenerowane\n\n$x^2$", encoding="utf-8")
    r = c.get("/api/videos/ok/notes").json()
    assert r["source"] == "generated" and not r["has_edited"] and "Wygenerowane" in r["md"]
    r = c.put("/api/videos/ok/notes", json={"md": "# Moje v1"}).json()
    assert r["source"] == "edited" and r["has_generated"]
    assert c.get("/api/videos/ok/notes/versions").json() == []  # pierwszy zapis nic nie archiwizuje
    c.put("/api/videos/ok/notes", json={"md": "# Moje v2"})
    vs = c.get("/api/videos/ok/notes/versions").json()
    assert len(vs) == 1 and c.get(f"/api/videos/ok/notes/versions/{vs[0]['id']}").json()["md"] == "# Moje v1"
    r = c.delete("/api/videos/ok/notes/edited").json()
    assert r["source"] == "generated" and "Wygenerowane" in r["md"]
    assert len(c.get("/api/videos/ok/notes/versions").json()) == 2  # v2 zarchiwizowana przy powrocie
    assert (wd / "lecture_notes.md").read_text(encoding="utf-8").startswith("# Wygenerowane")  # nietknieta


def test_notes_validation(notes_env):
    c, wd = notes_env
    (wd / "lecture_notes.md").write_text("x", encoding="utf-8")
    assert c.put("/api/videos/ok/notes", json={"md": "   "}).status_code == 400
    assert c.put("/api/videos/ok/notes", json={"md": "a" * 2_100_000}).status_code == 400
    assert c.delete("/api/videos/ok/notes/edited").status_code == 400  # nie ma edytowanej
    for bad in ("abc", "20240101-000000%2F..%2Fx", "..%2F..%2Fmeta"):
        assert c.get(f"/api/videos/ok/notes/versions/{bad}").status_code in (400, 404)
    assert c.get("/api/videos/ok/notes/versions/20240101-000000").status_code == 400


def test_history_limited_to_max(notes_env, monkeypatch):
    c, wd = notes_env
    from ui import notes as nm
    monkeypatch.setattr(nm, "MAX_VERSIONS", 3)
    (wd / "lecture_notes.md").write_text("g", encoding="utf-8")
    for i in range(8):
        c.put("/api/videos/ok/notes", json={"md": f"v{i}"})
    assert len(c.get("/api/videos/ok/notes/versions").json()) == 3


def test_export_md_and_docx(notes_env, monkeypatch):
    import shutil
    c, wd = notes_env
    (wd / "lecture_notes.md").write_text("# Tytul\n\nWzor: $a^2+b^2=c^2$\n", encoding="utf-8")
    r = c.get("/api/videos/ok/notes/export?format=md")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "Tytul" in r.text
    if shutil.which("pandoc"):
        d = c.get("/api/videos/ok/notes/export?format=docx")
        assert d.status_code == 200 and d.content[:2] == b"PK"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert c.get("/api/videos/ok/notes/export?format=docx").status_code == 501
    assert c.get("/api/videos/ok/notes/export?format=pdf").status_code == 400


def test_materials_and_context(notes_env):
    c, wd = notes_env
    (wd / "frames").mkdir()
    (wd / "frames" / "frame_000010_t00020.jpg").write_bytes(b"\xff\xd8jpg")
    (wd / "frames" / "frame_000050_t00100.jpg").write_bytes(b"\xff\xd8jpg2")
    (wd / "frames_metadata.json").write_text(json.dumps([
        {"file": "frame_000010_t00020.jpg", "timestamp": 20.0, "index": 10},
        {"file": "frame_000050_t00100.jpg", "timestamp": 100.0, "index": 50}]))
    (wd / "vision_context.json").write_text(json.dumps([{"file": "frame_000010_t00020.jpg", "timestamp": 20.0, "text": "Plansza A"}]))
    (wd / "transcript.json").write_text(json.dumps([
        {"start": 0, "end": 5, "text": "wstep"}, {"start": 60, "end": 70, "text": "srodek"}, {"start": 300, "end": 310, "text": "koniec"}]))
    fr = c.get("/api/videos/ok/frames").json()
    assert [f["text"] for f in fr] == ["Plansza A", None]
    assert c.get("/api/videos/ok/frames/frame_000010_t00020.jpg").content == b"\xff\xd8jpg"
    for bad in ("../meta.json", "..%2Fmeta.json", "evil.jpg", "frame_1_t1.png"):
        assert c.get(f"/api/videos/ok/frames/{bad}").status_code == 404
    ctx = c.get("/api/videos/ok/context", params={"t": 50}).json()
    assert ctx["frame"]["file"] == "frame_000050_t00100.jpg"  # tablica widoczna o t=50 to stan zapisany o 100
    assert [s["text"] for s in ctx["transcript"]] == ["wstep", "srodek"]  # okno +/-60 s
    assert c.get("/api/videos/ok/context", params={"t": 5}).json()["frame"]["file"] == "frame_000010_t00020.jpg"
    assert c.get("/api/videos/ok/context", params={"t": 9999}).json()["frame"]["file"] == "frame_000050_t00100.jpg"
    assert len(c.get("/api/videos/ok/transcript").json()) == 3


def test_regeneration_keeps_edited_notes(notes_env, env):
    c, wd = notes_env
    (wd / "lecture_notes.md").write_text("g", encoding="utf-8")
    c.put("/api/videos/ok/notes", json={"md": "moje"})
    o = env[0].orchestrator("ok")
    o.invalidate("llm")  # regeneracja kasuje wygenerowana, ale nie edytowana
    assert not (wd / "lecture_notes.md").exists() and (wd / "lecture_notes.edited.md").read_text(encoding="utf-8") == "moje"
    assert c.get("/api/videos/ok/notes").json()["source"] == "edited"
    (wd / "stage_state.json").write_text(json.dumps({"llm": {"status": "done", "config": "x", "input": "x", "seconds": 1}}))
    (wd / "lecture_notes.md").write_text("g", encoding="utf-8")
    imp = c.get("/api/videos/ok/impact", params={"stage": "llm"}).json()
    assert imp["keeps_edited_notes"] is True


# ---------- czat ----------
import copy  # noqa: E402
import threading  # noqa: E402

from ollama_client import OllamaError  # noqa: E402
from ui import retrieval as rt  # noqa: E402
from ui.chat import ChatService, ChatStore  # noqa: E402

NOTES = """# Notatki
## Maszyna Turinga
Maszyna Turinga ma nieskończoną taśmę, głowicę i stany. Funkcja przejścia delta opisuje kroki.
## Problemy decyzyjne
Problem decyzyjny to pytanie tak lub nie o egzemplarz problemu, np. czy graf jest spójny.
## Organizacja
Zaliczenie od 6 punktów, dwa kolokwia, plusy za aktywność.
"""


class FakeOllama:
    def __init__(self, tokens=("Odpo", "wiedź ", "testowa."), installed=("qwen2.5:7b-instruct",)):
        self.tokens, self.installed = list(tokens), list(installed)
        self.calls, self.unloaded, self.generated, self.loaded = [], [], [], []
        self.fail_after = None
        self.on_token = None

    def installed_models(self): return self.installed
    def loaded_models(self): return list(self.loaded)
    def unload(self, m): self.unloaded.append(m)

    def generate(self, model, prompt, **kw):
        self.generated.append(prompt)
        return "Streszczenie rozmowy."

    def chat_stream(self, model, messages, options=None, keep_alive=None, cancel=None):
        self.calls.append({"model": model, "messages": messages, "options": options})
        for i, t in enumerate(self.tokens):
            if self.fail_after is not None and i >= self.fail_after:
                raise OllamaError("zerwane polaczenie")
            if self.on_token:
                self.on_token(i)
            yield t


@pytest.fixture
def chat_env(env, tmp_path):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    wd = lib.workdir("ok")
    (wd / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    (wd / "transcript.json").write_text(json.dumps([
        {"start": 10, "end": 20, "text": "Dzisiaj zaczniemy od maszyny Turinga."},
        {"start": 900, "end": 915, "text": "Problem faktoryzacji jest w NP."}]), encoding="utf-8")
    fake = FakeOllama()
    svc = ChatService(lib, gpu, CFG, fake)
    return svc, fake, ChatStore(wd), wd, gpu, jm


def run_stream(svc, cid, q, **kw):
    return list(svc.stream_answer("ok", cid, q, **kw))


# --- retrieval ---
def test_split_notes_and_polish_stemming():
    secs = rt.split_notes(NOTES)
    assert [s["title"] for s in secs] == ["Notatki", "Maszyna Turinga", "Problemy decyzyjne", "Organizacja"]
    bm = rt.BM25([rt.tokens(s["title"] + " " + s["text"]) for s in secs])
    sc = bm.scores(rt.tokens("Jak działa maszynie Turinga?"))  # inna forma fleksyjna niz w notatkach
    assert max(range(len(sc)), key=sc.__getitem__) == 1


def test_context_full_notes_when_small_and_chunks_when_large():
    ctx, src = rt.build_context("co to jest maszyna?", [], NOTES, [], [], 4000)
    assert "Maszyna Turinga ma nieskończoną" in ctx and src[0]["label"] == "Notatki (całość)"
    big = NOTES + "\n".join(f"## Sekcja {i}\n" + "słowo " * 300 for i in range(40)) + "\n## Zaliczenie punkty\nkolokwium 6 punktów"
    ctx, src = rt.build_context("ile punktów na kolokwium?", [], big, [], [], 1500)
    assert rt.est_tokens(ctx) <= 1500 + 150
    assert any(s["label"] == "Zaliczenie punkty" for s in src)
    assert "[Spis treści notatek]" in ctx


def test_context_timestamp_question_pulls_nearest_window():
    tr = [{"start": i * 100, "end": i * 100 + 10, "text": f"fragment numer {i}"} for i in range(20)]
    ctx, src = rt.build_context("co było w 15:00?", [], "", tr, [], 2000)
    assert any(s["kind"] == "transcript" and abs(s["t"] - 900) <= 100 for s in src)


# --- store ---
def test_chat_store_crud(tmp_path):
    st = ChatStore(tmp_path)
    c = st.create()
    assert st.list()[0]["id"] == c["id"] and st.get(c["id"])["title"] == "Nowa rozmowa"
    st.update(c["id"], lambda x: x["messages"].append({"role": "user", "content": "x"}))
    assert ChatStore(tmp_path).get(c["id"])["messages"][0]["content"] == "x"
    st.delete(c["id"])
    assert st.list() == []


# --- ChatService ---
def test_stream_events_persist_and_context(chat_env):
    svc, fake, store, wd, gpu, jm = chat_env
    c = store.create()
    ev = run_stream(svc, c["id"], "Jak działa maszyna Turinga?")
    assert [e for e, _ in ev][0] == "sources" and ev[-1][0] == "done"
    assert "".join(d for e, d in ev if e == "token") == "Odpowiedź testowa."
    msgs = store.get(c["id"])["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["content"] == "Odpowiedź testowa."
    assert store.get(c["id"])["title"].startswith("Jak działa maszyna")
    system = fake.calls[0]["messages"][0]["content"]
    assert "nieskończoną taśmę" in system and "[Mowa" in system and fake.calls[0]["messages"][-1]["content"] == "Jak działa maszyna Turinga?"
    assert svc.status()["held"] and gpu.holder()["owner"] == "chat" and not svc.status()["generating"]
    svc.release()


def test_second_turn_includes_history(chat_env):
    svc, fake, store, *_ = chat_env
    c = store.create()
    run_stream(svc, c["id"], "Pierwsze pytanie o maszynę")
    run_stream(svc, c["id"], "A co z drugim?")
    roles = [m["role"] for m in fake.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    svc.release()


def test_history_folded_into_summary(chat_env):
    svc, fake, store, wd, *_ = chat_env
    svc.cfg = copy.deepcopy(CFG)
    svc.cfg["profiles"]["8gb"]["num_ctx"] = 1500
    svc.ccfg = {"num_predict": 100, "history_ratio": 0.25}
    c = store.create()
    long = "Treść wiadomości. " * 60
    for i in range(3):
        store.update(c["id"], lambda x: x["messages"].extend([{"role": "user", "content": long}, {"role": "assistant", "content": long}]))
    run_stream(svc, c["id"], "Nowe pytanie")
    chat = store.get(c["id"])
    assert fake.generated and chat["summary"] == "Streszczenie rozmowy." and chat["summarized_upto"] >= 2
    assert "Streszczenie wcześniejszej rozmowy" in fake.calls[0]["messages"][0]["content"]
    raw = [m for m in fake.calls[0]["messages"][1:-1]]
    assert len(raw) <= 4
    svc.release()


def test_busy_gpu_blocks_and_does_not_persist(chat_env):
    svc, fake, store, wd, gpu, jm = chat_env
    other = GpuLock(gpu.path)
    assert other.acquire("pipeline")
    c = store.create()
    ev = run_stream(svc, c["id"], "pytanie")
    assert ev == [("error", "GPU jest zajęte przez: pipeline. Czat będzie dostępny po jego zakończeniu.")]
    assert store.get(c["id"])["messages"] == [] and fake.calls == []
    other.release()


def test_model_not_installed(chat_env):
    svc, fake, store, *_ = chat_env
    c = store.create()
    ev = run_stream(svc, c["id"], "q", model="qwen2.5:14b-instruct")
    assert ev[0][0] == "error" and "ollama pull qwen2.5:14b-instruct" in ev[0][1]
    assert not svc.status()["held"]


def test_empty_and_missing_materials(chat_env):
    svc, fake, store, wd, *_ = chat_env
    c = store.create()
    assert run_stream(svc, c["id"], "   ")[0][0] == "error"
    (wd / "lecture_notes.md").unlink()
    (wd / "transcript.json").unlink()
    assert "Brak notatek i transkryptu" in run_stream(svc, c["id"], "pytanie")[0][1]


def test_closed_generator_keeps_partial(chat_env):
    svc, fake, store, *_ = chat_env
    c = store.create()
    it = svc.stream_answer("ok", c["id"], "pytanie")
    assert next(it)[0] == "sources" and next(it) == ("token", "Odpo")
    it.close()
    msgs = store.get(c["id"])["messages"]
    assert msgs[-1]["content"] == "Odpo" and msgs[-1]["stopped"] is True
    assert not svc.status()["generating"]
    svc.release()


def test_cancel_event_stops_stream(chat_env):
    svc, fake, store, *_ = chat_env
    c = store.create()
    cancel = threading.Event()
    fake.on_token = lambda i: cancel.set() if i == 1 else None
    ev = run_stream(svc, c["id"], "pytanie", cancel=cancel)
    assert [e for e, _ in ev][-1] == "token" and store.get(c["id"])["messages"][-1]["stopped"] is True
    svc.release()


def test_stream_failure_modes(chat_env):
    svc, fake, store, *_ = chat_env
    c = store.create()
    fake.fail_after = 1
    ev = run_stream(svc, c["id"], "pytanie")
    assert ev[-1] == ("error", "zerwane polaczenie")
    msgs = store.get(c["id"])["messages"]
    assert msgs[-1]["content"] == "Odpo" and msgs[-1]["stopped"]
    c2 = store.create()
    fake.fail_after = 0
    run_stream(svc, c2["id"], "pytanie")
    assert store.get(c2["id"])["messages"] == []  # pytanie bez odpowiedzi nie zostaje w historii
    svc.release()


def test_preempt_releases_only_when_idle(chat_env):
    svc, fake, store, wd, gpu, jm = chat_env
    c = store.create()
    seen = {}

    def during(i):
        if i == 1:
            seen["released_while_generating"] = svc.release()
            seen["held"] = svc.status()["held"]
    fake.on_token = during
    run_stream(svc, c["id"], "pytanie")
    assert seen == {"released_while_generating": False, "held": True}
    svc.preempt()  # po odpowiedzi: zwalnia
    assert not svc.status()["held"] and gpu.holder() is None and "qwen2.5:7b-instruct" in fake.unloaded


def test_job_preempts_idle_chat(chat_env):
    svc, fake, store, wd, gpu, jm = chat_env
    jm.lib.add_video("other.mp4")
    c = store.create()
    run_stream(svc, c["id"], "pytanie")
    assert gpu.holder()["owner"] == "chat"
    jm.set_preempt(svc.preempt)
    j = jm.enqueue("other")
    wait_for(jm, j["id"], "done")
    assert not svc.status()["held"]


# --- API ---
def parse_sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        ev, data = block.split("\n", 1)
        out.append((ev[7:], json.loads(data[6:])))
    return out


@pytest.fixture
def chat_client(env):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    wd = lib.workdir("ok")
    (wd / "lecture_notes.md").write_text(NOTES, encoding="utf-8")
    fake = FakeOllama()
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",), ollama=fake)
    return TestClient(app), fake, app


def test_chat_api_flow(chat_client):
    c, fake, app = chat_client
    cid = c.post("/api/videos/ok/chats", json={}).json()["id"]
    r = c.post(f"/api/videos/ok/chats/{cid}/messages", json={"content": "Co to maszyna Turinga?"})
    ev = parse_sse(r.text)
    assert ev[0][0] == "sources" and ev[-1][0] == "done" and "".join(d for e, d in ev if e == "token") == "Odpowiedź testowa."
    chat = c.get(f"/api/videos/ok/chats/{cid}").json()
    assert len(chat["messages"]) == 2 and chat["messages"][1]["sources"]
    assert c.get("/api/videos/ok/chats").json()[0]["count"] == 2
    st = c.get("/api/chat/status").json()
    assert st["held"] and st["model"] == "qwen2.5:7b-instruct" and st["holder"]["owner"] == "chat"
    assert c.post("/api/chat/release").json()["held"] is False
    assert c.delete(f"/api/videos/ok/chats/{cid}").json() == {"ok": True}
    assert c.get(f"/api/videos/ok/chats/{cid}").status_code == 404


def test_chat_api_errors(chat_client):
    c, fake, app = chat_client
    assert c.post("/api/videos/ok/chats/brak/messages", json={"content": "x"}).status_code == 404
    assert c.post("/api/videos/nie-ma/chats", json={}).status_code == 400
    cid = c.post("/api/videos/ok/chats", json={}).json()["id"]
    ev = parse_sse(c.post(f"/api/videos/ok/chats/{cid}/messages", json={"content": "q", "model": "x:1"}).text)
    assert ev[0][0] == "error" and "ollama pull x:1" in ev[0][1]


def test_chat_stream_cancel_interrupts_wait_for_headers():
    """Prawdziwy OllamaClient: serwer nie wysyla nic (ladowanie modelu), a cancel konczy generator szybko."""
    import http.server
    import socketserver
    from ollama_client import OllamaClient

    release = threading.Event()

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            release.wait(10)  # nawet naglowki nie sa wyslane: jak Ollama podczas prefillu duzego promptu

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Slow)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cancel = threading.Event()
        client = OllamaClient(host=f"http://127.0.0.1:{srv.server_address[1]}", timeout=30)
        it = client.chat_stream("m", [{"role": "user", "content": "x"}], cancel=cancel)
        threading.Timer(0.5, cancel.set).start()
        t0 = time.time()
        assert list(it) == []
        assert time.time() - t0 < 3
    finally:
        release.set()
        srv.shutdown()


def test_chat_stream_parses_tokens_and_errors():
    import http.server
    import socketserver
    from ollama_client import OllamaClient

    class Tok(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            body = self.server.body
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Tok)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = OllamaClient(host=f"http://127.0.0.1:{srv.server_address[1]}", timeout=10)
    try:
        srv.body = b'{"message":{"content":"Ala "}}\n{"message":{"content":"ma kota"},"done":true,"done_reason":"stop"}\n'
        assert "".join(client.chat_stream("m", [])) == "Ala ma kota"
        srv.body = b'{"message":{"content":"x"}}\n{"error":"model zepsuty"}\n'
        with pytest.raises(OllamaError, match="model zepsuty"):
            list(client.chat_stream("m", []))
    finally:
        srv.shutdown()


# ---------- logi i diagnostyka ----------
def test_diagnostics_and_server_log_endpoints(client, env):
    c, jm = client
    c.post("/api/videos", json={"files": ["ok.mp4"], "enqueue": False})
    wd = env[0].workdir("ok")
    (wd / "run_state.json").write_text(json.dumps({"status": "error", "error_stage": "asr",
                                                   "error": "etap asr zakonczyl sie kodem 1: No module named 'faster_whisper'"}))
    (wd / "worker_asr.log").write_text("Traceback...\nImportError: boom\n")
    (wd / "pipeline.log").write_text("linia pipeline\n")
    (wd / "job.out").write_bytes(b"wyjscie zadania\n")
    d = c.get("/api/videos/ok/diagnostics").json()
    assert d["stage"] == "asr" and d["status"] == "error" and "pip install" in d["hint"]
    assert d["worker_log"][-1] == "ImportError: boom" and d["pipeline_log"] == ["linia pipeline"] and d["job_output"] == ["wyjscie zadania"]
    assert c.get("/api/videos/nie-ma/diagnostics").status_code == 400
    lg = c.get("/api/server-log", params={"lines": 5}).json()
    assert lg["lines"] == [] and lg["path"].endswith("server.log")


def test_unhandled_exception_returns_json_detail(env, monkeypatch):
    lib, gpu, jm, videos, work = env
    app = create_app(CFG, videos, work, jobs=jm, start_jobs=False, extra_hosts=("testserver",))

    def boom():
        raise ValueError("cos poszlo nie tak")
    monkeypatch.setattr(app.state.lib, "list_files", boom)
    r = TestClient(app, raise_server_exceptions=False).get("/api/library/files")
    assert r.status_code == 500 and "ValueError: cos poszlo nie tak" in r.json()["detail"] and "logu serwera" in r.json()["detail"]


def test_job_fails_with_clear_message_when_video_missing(env, tmp_path):
    lib, gpu, jm, videos, work = env
    lib.add_video("ok.mp4")
    (videos / "ok.mp4").unlink()  # film przeniesiony/usuniety po dodaniu
    j = jm.enqueue("ok")
    jm.tick()
    job = next(x for x in jm.snapshot()["jobs"] if x["id"] == j["id"])
    assert job["status"] == "error" and "Plik wideo nie istnieje" in job["error"] and "ok.mp4" in job["error"]
    assert not (lib.workdir("ok") / "job.out").exists()  # proces w ogole nie wystartowal
    # kolejka nie blokuje sie na bledzie: kolejne zadanie startuje
    lib.add_video("other.mp4")
    j2 = jm.enqueue("other")
    wait_for(jm, j2["id"], "done")


def test_cli_missing_video_message(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "main.py"), str(tmp_path / "nie-ma.mp4"), "--workdir", str(tmp_path / "w")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "brak pliku wideo" in r.stderr and "nie-ma.mp4" in r.stderr


# ---------- usuwanie wykladow ----------
def seed_lecture(lib, name="ok.mp4"):
    vid = lib.add_video(name)
    wd = lib.workdir(vid)
    (wd / "lecture_notes.md").write_text("notatki", encoding="utf-8")
    (wd / "lecture_notes.edited.md").write_text("moje", encoding="utf-8")
    (wd / "frames").mkdir()
    (wd / "frames" / "frame_000001_t00001.jpg").write_bytes(b"x" * 1000)
    (wd / "chat.json").write_text(json.dumps({"chats": [{"id": "a"}, {"id": "b"}]}))
    return vid, wd


def test_delete_preview_and_delete_keeps_video_file(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    vid, wd = seed_lecture(lib)
    p = c.get("/api/videos/ok/delete-preview").json()
    assert p["size_bytes"] >= 1000 and p["has_edited_notes"] and p["chat_count"] == 2
    assert p["video_entry"] == "ok.mp4" and p["video_is_link"] is False
    r = c.delete("/api/videos/ok").json()
    assert r == {"deleted": "ok", "video_deleted": False}
    assert not wd.exists() and (videos / "ok.mp4").exists()  # film zostaje
    assert c.get("/api/videos").json() == [] and c.get("/api/videos/ok").status_code == 400
    # film mozna dodac ponownie i dostaje czysty stan
    assert lib.add_video("ok.mp4") == "ok" and not (lib.workdir("ok") / "lecture_notes.md").exists()


def test_delete_with_video_removes_entry_but_only_the_link_for_symlinks(client, env, tmp_path):
    c, jm = client
    lib, _, _, videos, work = env
    seed_lecture(lib)
    assert c.delete("/api/videos/ok", params={"delete_video": "true"}).json() == {"deleted": "ok", "video_deleted": True}
    assert not (videos / "ok.mp4").exists()
    # dowiazanie symboliczne: kasujemy dowiazanie, cel zostaje
    outside = tmp_path / "poza" / "wyklad.mp4"
    make_video(outside)
    (videos / "link.mp4").symlink_to(outside)
    vid = lib.add_video("link.mp4")
    assert c.get(f"/api/videos/{vid}/delete-preview").json()["video_is_link"] is True
    assert c.delete(f"/api/videos/{vid}", params={"delete_video": "true"}).json()["video_deleted"] is True
    assert not (videos / "link.mp4").exists() and not (videos / "link.mp4").is_symlink() and outside.exists()


def test_delete_video_flag_ignored_when_file_not_in_library(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    seed_lecture(lib)
    (videos / "ok.mp4").rename(videos / "ok-przeniesiony.txt")  # wpis w videos/ juz nie wskazuje na film
    r = c.delete("/api/videos/ok", params={"delete_video": "true"}).json()
    assert r["video_deleted"] is False and (videos / "ok-przeniesiony.txt").exists()


def test_delete_blocked_for_active_job_live_run_and_chat(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    seed_lecture(lib)
    j = jm.enqueue("ok")
    for url in ("/api/videos/ok/delete-preview",):
        assert c.get(url).status_code == 409
    r = c.delete("/api/videos/ok")
    assert r.status_code == 409 and "kolejce" in r.json()["detail"] and lib.workdir("ok").exists()
    jm.cancel(j["id"])
    import run_state
    run_state.write_run_state(lib.workdir("ok"), status="running", pid=os.getppid(), heartbeat=time.time())
    assert c.delete("/api/videos/ok").status_code == 409  # zywe uruchomienie z CLI
    run_state.write_run_state(lib.workdir("ok"), status="done")
    c.app.state.chat._generating, c.app.state.chat._gen_video = True, "ok"
    assert c.delete("/api/videos/ok").status_code == 409
    c.app.state.chat._generating, c.app.state.chat._gen_video = False, None
    assert c.delete("/api/videos/ok").status_code == 200


def test_delete_forgets_finished_jobs_and_rejects_bad_ids(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    seed_lecture(lib)
    j = jm.enqueue("ok")
    wait_for(jm, j["id"], "done")
    assert any(x["video_id"] == "ok" for x in jm.snapshot()["jobs"])
    assert c.delete("/api/videos/ok").status_code == 200
    assert not any(x["video_id"] == "ok" for x in jm.snapshot()["jobs"])
    for bad in ("nie-ma", "..%2F..%2Fetc", "A B"):
        assert c.delete(f"/api/videos/{bad}").status_code in (400, 404, 405)
    assert work.exists() and (videos / "other.mp4").exists()  # nic poza wykladem nie ucierpialo


# ---------- wylaczanie serwera i licznik klientow ----------
def test_stream_tracks_connected_window(client):
    c, jm = client
    tr = c.app.state.tracker
    assert tr.count == 0 and tr.ever_connected is False
    c.get("/api/stream", params={"once": "true"})  # okno polaczylo sie i rozlaczylo
    assert tr.count == 0 and tr.ever_connected is True


def test_shutdown_requires_header_and_refuses_when_busy(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    called = []
    c.app.state.request_exit = lambda: called.append(1)
    assert c.post("/api/shutdown").status_code == 403           # bez naglowka (np. obca strona w przegladarce)
    assert c.post("/api/shutdown", headers={"x-lecturenoter": "0"}).status_code == 403
    lib.add_video("ok.mp4")
    jm.enqueue("ok")                                             # oczekujace zadanie = praca
    r = c.post("/api/shutdown", headers={"x-lecturenoter": "1"})
    assert r.status_code == 409 and "przetwarzanie" in r.json()["detail"] and not called
    jm.set_paused(True)                                          # wstrzymana kolejka nie blokuje wylaczenia
    assert c.post("/api/shutdown", headers={"x-lecturenoter": "1"}).json() == {"ok": True}
    time.sleep(0.8)
    assert called == [1]


def test_shutdown_force_and_idle(client):
    c, jm = client
    called = []
    c.app.state.request_exit = lambda: called.append(1)
    assert c.post("/api/shutdown", headers={"x-lecturenoter": "1"}).status_code == 200  # nic nie trwa
    time.sleep(0.8)
    assert called == [1]
    assert c.get("/api/system").json()["auto_exit"] is False


def test_busy_detection_includes_chat_generation(client):
    c, jm = client
    assert c.app.state.is_busy() is False
    c.app.state.chat._generating = True
    assert c.app.state.is_busy() is True
    c.app.state.chat._generating = False


# ---------- eksport rozmowy ----------
def test_chat_export_markdown(chat_client):
    c, fake, app = chat_client
    cid = c.post("/api/videos/ok/chats", json={}).json()["id"]
    c.post(f"/api/videos/ok/chats/{cid}/messages", json={"content": "Co to maszyna Turinga?"})
    r = c.get(f"/api/videos/ok/chats/{cid}/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert f'filename="ok-rozmowa-{cid}.md"' in r.headers["content-disposition"]
    md = r.text
    assert md.startswith("# Co to maszyna Turinga?") and "Wykład: ok" in md and "model: qwen2.5:7b-instruct" in md
    assert "### Ty" in md and "Co to maszyna Turinga?" in md
    assert "### Asystent" in md and "Odpowiedź testowa." in md
    assert "*Użyte fragmenty:" in md and "Notatki:" in md
    assert md.index("### Ty") < md.index("### Asystent")


def test_chat_export_keeps_latex_and_marks_stopped(tmp_path):
    from ui.chat import export_markdown
    chat = {"title": "T", "model": "m", "created": 1_700_000_000, "summary": "krótko o\nmaszynach", "messages": [
        {"role": "user", "content": "Wzór?", "ts": 1_700_000_001},
        {"role": "assistant", "content": "Wzór: $$\\sum_{i=1}^{4} x_i$$", "ts": 1_700_000_002, "stopped": True,
         "sources": [{"kind": "transcript", "label": "Mowa 00:01:35", "t": 95}]}]}
    md = export_markdown(chat, "Wykład 1")
    assert "$$\\sum_{i=1}^{4} x_i$$" in md and "*(odpowiedź przerwana)*" in md and "Mowa 00:01:35" in md
    assert "> Streszczenie wcześniejszej części rozmowy: krótko o maszynach" in md
    assert md.endswith("\n") and not md.endswith("\n\n")


def test_chat_export_errors_and_empty_chat(chat_client):
    c, fake, app = chat_client
    assert c.get("/api/videos/ok/chats/brak/export").status_code == 404
    cid = c.post("/api/videos/ok/chats", json={"title": "Pusta"}).json()["id"]
    assert c.get(f"/api/videos/ok/chats/{cid}/export", params={"format": "pdf"}).status_code == 400
    assert c.get(f"/api/videos/ok/chats/{cid}/export").text.startswith("# Pusta")  # pusta rozmowa: sam naglowek
    # usuwanie przez API i brak tej rozmowy w eksporcie
    assert c.delete(f"/api/videos/ok/chats/{cid}").json() == {"ok": True}
    assert c.get(f"/api/videos/ok/chats/{cid}/export").status_code == 404


# ---------- foldery, nazwy, wyszukiwanie ----------
from ui.folders import MAX_DEPTH, FolderError, FolderStore  # noqa: E402


def test_folder_store_tree_rules(tmp_path):
    fs = FolderStore(tmp_path)
    a = fs.create("Semestr 3")
    b = fs.create("Matematyka", a)
    c = fs.create("Analiza", b)
    paths = {f["id"]: f["path"] for f in fs.list()}
    assert paths[c] == ["Semestr 3", "Matematyka", "Analiza"] and [f["depth"] for f in fs.list()] == [0, 1, 2]
    with pytest.raises(FolderError, match="już folder"):
        fs.create("matematyka", a)                       # duplikat wsrod rodzenstwa (bez wzgledu na wielkosc liter)
    fs.create("Matematyka")                               # ta sama nazwa w innym miejscu jest ok
    for bad in ("", "   ", "x" * 61, "a\x00b"):
        with pytest.raises(FolderError):
            fs.create(bad)
    with pytest.raises(FolderError, match="nadrzędnego"):
        fs.create("X", "nie-ma")
    # cykle i glebokosc
    with pytest.raises(FolderError, match="do niego samego"):
        fs.update(a, parent=c)
    with pytest.raises(FolderError, match="do niego samego"):
        fs.update(b, parent=b)
    cur = c
    for i in range(MAX_DEPTH):
        try:
            cur = fs.create(f"L{i}", cur)
        except FolderError as e:
            assert "głębokie" in str(e)
            break
    else:
        raise AssertionError("limit zaglebienia nie zadzialal")
    fs.update(c, name="Analiza I")
    assert fs.exists(c) and [f["name"] for f in fs.list() if f["id"] == c] == ["Analiza I"]
    fs.update(c, parent=None)                              # przeniesienie do korzenia
    assert [f for f in fs.list() if f["id"] == c][0]["parent"] is None


def test_folder_delete_moves_children_up_and_resolves_name_clash(tmp_path):
    fs = FolderStore(tmp_path)
    top = fs.create("Top")
    mid = fs.create("Mid", top)
    inner = fs.create("Top", mid)          # taka sama nazwa jak 'top' po przesunieciu do korzenia
    parent = fs.delete(mid)
    assert parent == top
    by_id = {f["id"]: f for f in fs.list()}
    assert mid not in by_id and by_id[inner]["parent"] == top
    root_names = [f["name"] for f in fs.list() if f["parent"] is None]
    assert root_names == ["Top"]
    # usuniecie folderu glownego: dziecko trafia do korzenia, a duplikat nazwy dostaje numer
    fs.delete(top)
    assert [f["name"] for f in fs.list()] == ["Top"]


def test_rename_lecture_keeps_id_and_survives_pipeline(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    c.post("/api/videos", json={"files": ["ok.mp4"], "enqueue": False})
    r = c.patch("/api/videos/ok", json={"title": "  Analiza   matematyczna: wykład 4  "})
    assert r.status_code == 200 and r.json()["title"] == "Analiza matematyczna: wykład 4" and r.json()["id"] == "ok"
    assert lib.workdir("ok").name == "ok"                                      # identyfikator i katalog bez zmian
    o = lib.orchestrator("ok")
    o.ensure_meta()                                                             # proces zadania przepisuje meta.json
    assert c.get("/api/videos/ok").json()["title"] == "Analiza matematyczna: wykład 4"
    assert c.patch("/api/videos/ok", json={"title": "   "}).status_code == 400
    assert c.patch("/api/videos/ok", json={"title": "x" * 121}).status_code == 400
    assert c.patch("/api/videos/nie-ma", json={"title": "A"}).status_code == 400


def test_folders_api_assign_move_delete(client, env):
    c, jm = client
    lib, _, _, videos, work = env
    for f in ("ok.mp4", "other.mp4"):
        c.post("/api/videos", json={"files": [f], "enqueue": False})
    root = c.post("/api/folders", json={"name": "Studia"}).json()["id"]
    sub = c.post("/api/folders", json={"name": "Semestr 3", "parent": root}).json()["id"]
    assert c.post("/api/folders", json={"name": "studia"}).status_code == 400
    assert c.patch("/api/videos/ok", json={"folder": sub}).json()["folder"] == sub
    assert c.patch("/api/videos/other", json={"folder": "nie-ma"}).status_code == 400
    listing = {f["id"]: f for f in c.get("/api/folders").json()}
    assert listing[sub]["count"] == 1 and listing[root]["count"] == 0 and listing[sub]["path"] == ["Studia", "Semestr 3"]
    assert c.patch(f"/api/folders/{sub}", json={"name": "Sem. 3"}).status_code == 200
    assert c.patch(f"/api/folders/{root}", json={"parent": sub}).status_code == 400   # cykl
    # usuniecie folderu nie usuwa wykladow: przechodza do folderu nadrzednego
    r = c.delete(f"/api/folders/{sub}").json()
    assert r["moved_lectures"] == 1 and r["moved_to"] == root
    assert c.get("/api/videos/ok").json()["folder"] == root and (lib.workdir("ok") / "meta.json").exists()
    # przeniesienie do korzenia (folder: null)
    assert c.patch("/api/videos/ok", json={"folder": None}).json()["folder"] is None
    assert c.delete("/api/folders/nie-ma").status_code == 400
    # SSE niesie foldery
    state = json.loads(c.get("/api/stream", params={"once": "true"}).text.split("data: ", 1)[1].split("\n\n")[0])
    assert [f["name"] for f in state["folders"]] == ["Studia"]


@pytest.fixture
def search_env(client, env):
    c, jm = client
    lib, gpu, _, videos, work = env
    data = {
        "ok": ("Analiza matematyczna wykład 4", "## Granice ciągów\nGranica ciągu jest liczbą, do której zbliżają się wyrazy.\n## Szeregi\nSzereg zbieżny ma sumę.",
               [{"start": 10, "end": 20, "text": "Dzisiaj omawiamy granice funkcji i ich własności."}], "Wzór granicy: lim x do zera"),
        "other": ("Teoria grafów", "## Drzewa\nDrzewo to graf spójny i acykliczny.\n## Maszyny\nMaszyna Turinga nie jest grafem.",
                  [{"start": 5, "end": 9, "text": "Graf jest strukturą z wierzchołkami i krawędziami."}], ""),
        "sleep": ("Obliczalność", "## Maszyna Turinga\nMaszyna Turinga ma taśmę nieskończoną i głowicę.\n## Teza Churcha\nTeza o algorytmach.",
                  [{"start": 100, "end": 110, "text": "Maszyny Turinga rozstrzygają języki."}], ""),
    }
    for name, (title, notes, tr, board) in data.items():
        vid = lib.add_video(f"{name}.mp4")
        wd = lib.workdir(vid)
        lib.rename(vid, title)
        (wd / "lecture_notes.md").write_text(notes, encoding="utf-8")
        (wd / "transcript.json").write_text(json.dumps(tr), encoding="utf-8")
        if board:
            (wd / "vision_context.json").write_text(json.dumps([{"file": "f.jpg", "timestamp": 50.0, "text": board}]), encoding="utf-8")
    return c, lib


def search(c, q, **kw):
    return c.get("/api/search", params={"q": q, **kw}).json()


def test_search_finds_across_sources_with_polish_inflection(search_env):
    c, lib = search_env
    r = search(c, "maszynie Turinga")          # inna forma fleksyjna niz w tekstach
    ids = [x["video_id"] for x in r["results"]]
    assert set(ids[:2]) == {"sleep", "other"} and "ok" not in ids[:2]
    sleep = next(x for x in r["results"] if x["video_id"] == "sleep")
    kinds = {h["kind"] for h in sleep["hits"]}
    assert {"notes", "transcript"} <= kinds
    notes_hit = next(h for h in sleep["hits"] if h["kind"] == "notes")
    assert notes_hit["heading"] == "Maszyna Turinga" and "Turinga" in notes_hit["snippet"]
    tr_hit = next(h for h in sleep["hits"] if h["kind"] == "transcript")
    assert tr_hit["t"] == 100 and tr_hit["label"] == "Mowa 00:01:40"
    assert r["total_hits"] >= 3 and r["terms"]


def test_search_board_title_phrase_and_folder_filter(search_env):
    c, lib = search_env
    r = search(c, "lim")
    assert [x["video_id"] for x in r["results"]] == ["ok"] and r["results"][0]["hits"][0]["kind"] == "board"
    r = search(c, "teoria")                        # trafienie w tytule wykladu
    assert r["results"][0]["video_id"] == "other" and r["results"][0]["title_match"] is True
    phrase = search(c, '"maszyna turinga ma taśmę"')
    assert [x["video_id"] for x in phrase["results"]] == ["sleep"]    # fraza dokladna: nie pasuja inne wyklady z 'Turinga'
    fid = c.post("/api/folders", json={"name": "Teoria"}).json()["id"]
    c.patch("/api/videos/other", json={"folder": fid})
    in_folder = search(c, "maszyna", folder=fid)
    assert [x["video_id"] for x in in_folder["results"]] == ["other"]
    assert in_folder["results"][0]["folder_path"] == ["Teoria"]
    assert {x["video_id"] for x in search(c, "maszyna")["results"]} == {"other", "sleep"}


def test_search_title_includes_folder_name_and_handles_empty_queries(search_env):
    c, lib = search_env
    fid = c.post("/api/folders", json={"name": "Egzaminy"}).json()["id"]
    c.patch("/api/videos/ok", json={"folder": fid})
    assert search(c, "egzaminy")["results"][0]["video_id"] == "ok"
    for q in ("", " ", "a", "i w z"):             # za krotkie lub same slowa pomijane (stopwords)
        assert search(c, q)["results"] == []


def test_search_prefers_edited_notes_and_refreshes_after_changes(search_env):
    c, lib = search_env
    assert search(c, "kwaternion")["results"] == []
    wd = lib.workdir("ok")
    (wd / "lecture_notes.edited.md").write_text("## Moje uwagi\nKwaterniony opisują obroty.", encoding="utf-8")
    r = search(c, "kwaternion")
    assert [x["video_id"] for x in r["results"]] == ["ok"] and r["results"][0]["hits"][0]["heading"] == "Moje uwagi"
    # wersja edytowana ma pierwszenstwo: tekst z wygenerowanej juz nie jest szukany
    assert search(c, "szereg zbieżny")["results"] == []
    (wd / "lecture_notes.edited.md").unlink()
    assert search(c, "kwaternion")["results"] == [] and search(c, "szereg")["results"]
    c.patch("/api/videos/ok", json={"title": "Zupełnie nowa nazwa"})
    assert search(c, "nowa")["results"][0]["video_id"] == "ok"             # zmiana nazwy widoczna w wyszukiwarce od razu
    c.delete("/api/videos/ok")
    assert search(c, "nowa")["results"] == []                              # usuniety wyklad znika z wynikow


def test_search_snippet_window():
    from ui.search import make_snippet
    text = "Wstęp. " * 40 + "Tutaj pojawia się ważna definicja maszyny Turinga w środku tekstu. " + "Koniec. " * 40
    s = make_snippet(text, ["maszy"])
    assert s.startswith("… ") and s.endswith(" …") and "maszyny Turinga" in s and len(s) < 300
    assert make_snippet("## Tytuł\n**pogrubione** słowo", ["pogrub"]).startswith("Tytuł pogrubione")


def test_search_snippet_does_not_repeat_section_heading(search_env):
    c, lib = search_env
    r = search(c, "maszyna turinga")
    sleep = next(x for x in r["results"] if x["video_id"] == "sleep")
    hit = next(h for h in sleep["hits"] if h["kind"] == "notes" and h["heading"] == "Maszyna Turinga")
    assert hit["snippet"].lower().count("maszyna turinga") == 1          # tresc zaczyna sie od 'Maszyna Turinga ma taśmę…'
    assert hit["snippet"].startswith("Maszyna Turinga ma taśmę")
