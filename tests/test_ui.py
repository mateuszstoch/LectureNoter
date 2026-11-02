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
