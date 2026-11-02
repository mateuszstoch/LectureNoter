import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ui import archive as ar  # noqa: E402
from ui.archive import ArchiveError  # noqa: E402

META = {"title": "Teoria obliczeń: wykład 7", "created": 1.0, "uid": "abc123", "language": "pl"}


def make_workdir(wd, with_logs=True):
    wd.mkdir(parents=True)
    (wd / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (wd / "transcript.json").write_text(json.dumps([{"start": 0, "end": 1, "text": "Dzień dobry"}]), encoding="utf-8")
    (wd / "lecture_notes.md").write_text("# Notatki\n$x^2$ ąęł", encoding="utf-8")
    (wd / "chat.json").write_text(json.dumps({"chats": [{"id": "a"}]}), encoding="utf-8")
    (wd / "stage_state.json").write_text(json.dumps({"llm": {"status": "done", "config": "x", "input": "y"}}), encoding="utf-8")
    (wd / "frames").mkdir()
    (wd / "frames" / "frame_000001_t00001.jpg").write_bytes(b"\xff\xd8jpg" * 100)
    (wd / "frames_metadata.json").write_text(json.dumps([{"file": "frame_000001_t00001.jpg", "timestamp": 1.0}]), encoding="utf-8")
    (wd / "notes_history").mkdir()
    (wd / "notes_history" / "20260101-120000.md").write_text("stara wersja", encoding="utf-8")
    if with_logs:
        for n in ("pipeline.log", "job.out", "run_state.json", "progress.json", "checkpoints.json", "worker_asr.log", "audio.wav", "x.tmp"):
            (wd / n).write_text("smiec")


def zip_with(path, entries, manifest=None):
    """Zlosliwe/niestandardowe archiwum: entries = {nazwa: bajty | ZipInfo-tuple}."""
    manifest = manifest if manifest is not None else {"format": ar.FORMAT, "version": ar.VERSION, "title": "x"}
    with zipfile.ZipFile(path, "w") as zf:
        if manifest is not False:
            zf.writestr("manifest.json", json.dumps(manifest))
        for name, data in entries.items():
            if isinstance(data, zipfile.ZipInfo):
                zf.writestr(data, b"x")
            else:
                zf.writestr(name, data)
    return path


GOOD = {"data/meta.json": json.dumps(META)}


def test_build_inspect_extract_round_trip(tmp_path):
    wd = tmp_path / "wd"
    make_workdir(wd)
    dest = tmp_path / "wyklad.lec"
    seen = []
    manifest = ar.build_archive(wd, dest, {"uid": "abc123", "title": META["title"], "folder_path": ["Semestr 3"]},
                                progress=lambda d, t: seen.append((d, t)))
    assert zipfile.is_zipfile(dest)                           # .lec to zwykly ZIP
    names = zipfile.ZipFile(dest).namelist()
    assert names[0] == "manifest.json"
    assert "data/lecture_notes.md" in names and "data/frames/frame_000001_t00001.jpg" in names and "data/notes_history/20260101-120000.md" in names
    assert not any(n.endswith((".log", ".tmp", "audio.wav", "job.out", "run_state.json", "progress.json", "checkpoints.json")) for n in names)
    assert manifest["files"] == 8 and manifest["has_video"] is False and manifest["folder_path"] == ["Semestr 3"]
    assert seen[-1][0] == seen[-1][1] > 0                      # postep dochodzi do 100%
    info = ar.inspect_archive(dest)
    assert info["meta"]["title"] == META["title"] and info["manifest"]["version"] == ar.VERSION
    out = tmp_path / "out"
    assert ar.extract_archive(dest, out, None, info=info) is None
    assert (out / "lecture_notes.md").read_text(encoding="utf-8") == "# Notatki\n$x^2$ ąęł"
    assert json.loads((out / "meta.json").read_text(encoding="utf-8")) == META
    assert (out / "frames" / "frame_000001_t00001.jpg").read_bytes() == b"\xff\xd8jpg" * 100
    assert not (out / "pipeline.log").exists()


def test_video_is_optional_and_extracted_only_on_request(tmp_path):
    wd = tmp_path / "wd"
    make_workdir(wd, with_logs=False)
    video = tmp_path / "nagranie 1.mp4"
    video.write_bytes(b"VIDEO" * 1000)
    with_v = ar.build_archive(wd, tmp_path / "a.lec", {"uid": "u"}, video_path=video)
    assert with_v["has_video"] is True and with_v["video_name"] == "nagranie 1.mp4" and with_v["video_bytes"] == 5000
    out, vdir = tmp_path / "out", tmp_path / "videos"
    v = ar.extract_archive(tmp_path / "a.lec", out, vdir)
    assert v == vdir / "nagranie 1.mp4" and v.read_bytes() == b"VIDEO" * 1000
    assert ar.extract_archive(tmp_path / "a.lec", tmp_path / "out2", None) is None
    assert not (tmp_path / "out2" / "nagranie 1.mp4").exists()
    no_v = ar.build_archive(wd, tmp_path / "b.lec", {"uid": "u"})
    assert no_v["has_video"] is False and not any(n.startswith("video/") for n in zipfile.ZipFile(tmp_path / "b.lec").namelist())


@pytest.mark.parametrize("evil", [
    "data/../../evil.txt", "../evil.txt", "/etc/passwd", "data\\..\\evil", "C:/Windows/evil", "data/frames/../../../x.jpg",
])
def test_path_traversal_entries_are_rejected(tmp_path, evil):
    p = zip_with(tmp_path / "e.lec", {**GOOD, evil: b"x"})
    with pytest.raises(ArchiveError, match="Niebezpieczna sciezka"):
        ar.inspect_archive(p)


def test_symlink_and_encrypted_entries_are_rejected(tmp_path):
    zi = zipfile.ZipInfo("data/transcript.json")
    zi.external_attr = (0o120777 << 16)                       # dowiazanie symboliczne
    with pytest.raises(ArchiveError, match="dowiazanie"):
        ar.inspect_archive(zip_with(tmp_path / "l.lec", {**GOOD, "data/transcript.json": zi}))
    zi2 = zipfile.ZipInfo("data/transcript.json")
    zi2.flag_bits |= 0x1                                       # zipfile zeruje te flage przy zapisie, wiec testujemy klasyfikator
    with pytest.raises(ArchiveError, match="zaszyfrowane"):
        ar._classify(zi2)


def test_unknown_entries_are_ignored_and_never_extracted(tmp_path):
    p = zip_with(tmp_path / "u.lec", {**GOOD, "data/evil.sh": b"rm -rf /", "data/frames/x.exe": b"MZ", "data/frames/ok.jpg": b"\xff\xd8",
                                      "data/frames/zly nazwa;rm.jpg": b"x", "readme.txt": b"hej", "video/film.exe": b"x"})
    info = ar.inspect_archive(p)
    assert info["ignored"] == 5 and sorted(r for k, r, _ in info["entries"] if k == "data") == ["frames/ok.jpg", "meta.json"]
    out = tmp_path / "out"
    ar.extract_archive(p, out, tmp_path / "v", info=info)
    assert sorted(x.relative_to(out).as_posix() for x in out.rglob("*") if x.is_file()) == ["frames/ok.jpg", "meta.json"]
    assert not (tmp_path / "v").exists()


@pytest.mark.parametrize("build, message", [
    (lambda p: p.write_bytes(b"to nie jest zip"), "nie jest poprawne archiwum"),
    (lambda p: zip_with(p, GOOD, manifest=False), "brak manifest.json"),
    (lambda p: zip_with(p, GOOD, manifest={"format": "inny", "version": 1}), "nie jest w formacie"),
    (lambda p: zip_with(p, GOOD, manifest={"format": ar.FORMAT, "version": 99}), "nowszej wersji"),
    (lambda p: zip_with(p, {"data/transcript.json": "[]"}), "brak data/meta.json"),
    (lambda p: zip_with(p, {"data/meta.json": "{niepoprawny"}), "niepoprawny JSON"),
    (lambda p: zip_with(p, {"data/meta.json": json.dumps({"bez": "tytulu"})}), "brak tytulu"),
    (lambda p: zip_with(p, {**GOOD, "data/transcript.json": json.dumps({"to": "nie lista"})}), "niepoprawna strukture"),
    (lambda p: zip_with(p, {**GOOD, "video/a.mp4": b"1", "video/b.mp4": b"2"}), "wiecej niz jeden plik wideo"),
    (lambda p: zip_with(p, {**GOOD, "data/chat.json": json.dumps({"chats": [{"title": "bez id"}]})}), "Rozmowa w chat.json"),
    (lambda p: zip_with(p, {**GOOD, "data/chat.json": json.dumps({"chats": [{"id": "a", "messages": [{"role": "hacker", "content": "x"}]}]})}), "Rozmowa w chat.json"),
])
def test_invalid_archives_give_clear_errors(tmp_path, build, message):
    p = tmp_path / "x.lec"
    build(p)
    with pytest.raises(ArchiveError, match=message):
        ar.inspect_archive(p)


def test_size_limit_zip_bomb_and_corruption(tmp_path):
    p = zip_with(tmp_path / "big.lec", {**GOOD, "data/transcript.json": json.dumps(["x" * 1000])})
    with pytest.raises(ArchiveError, match="wiecej niz dozwolone"):
        ar.inspect_archive(p, max_bytes=500)
    bomb = tmp_path / "bomb.lec"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format": ar.FORMAT, "version": 1}))
        zf.writestr("data/meta.json", json.dumps(META))
        zf.writestr("data/vision_context.jsonl", b"\0" * (30 * 1024 * 1024))   # 30 MB zer: kompresja >1000:1
    with pytest.raises(ArchiveError, match="bombe kompresji"):
        ar.inspect_archive(bomb)
    wd = tmp_path / "wd"
    make_workdir(wd, with_logs=False)
    good = tmp_path / "good.lec"
    ar.build_archive(wd, good, {"uid": "u"})
    raw = bytearray(good.read_bytes())
    i = raw.index(b"\xff\xd8jpg")                              # zepsuj bajty obrazu (STORED): CRC sie nie zgodzi
    raw[i + 7] ^= 0xFF
    good.write_bytes(bytes(raw))
    with pytest.raises(ArchiveError, match="uszkodzone"):
        ar.inspect_archive(good)
