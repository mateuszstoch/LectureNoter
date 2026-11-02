import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launch", ROOT / "scripts" / "launch.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


def test_pick_python_prefers_first_new_enough():
    versions = {"/u/python3.12": (3, 12), "/u/python3.9": (3, 9), "/u/python3": (3, 8)}
    which = lambda n: {"python3.12": "/u/python3.12", "python3.11": None, "python3.10": None, "python3": "/u/python3"}.get(n)
    assert launch.pick_python({}, which, versions.get) == "/u/python3.12"
    only_old = lambda n: {"python3": "/u/python3"}.get(n)
    assert launch.pick_python({}, only_old, versions.get) is None  # 3.8 jest za stary
    assert launch.pick_python({"LECTURENOTER_PYTHON": "python3.9"}, lambda n: "/u/python3.9", versions.get) is None


def test_default_venv_location(tmp_path):
    assert launch.default_venv(Path("/home/u/LectureNoter"), {}) == Path("/home/u/LectureNoter/.venv")
    # projekt na dysku Windows: venv w katalogu domowym WSL (szybki, z dowiazaniami)
    v = launch.default_venv(Path("/mnt/c/Users/Jan Kowalski/LectureNoter"), {}, home=tmp_path)
    assert v == tmp_path / ".local" / "share" / "lecturenoter" / "venv"
    assert launch.default_venv(Path("/mnt/c/x"), {"LECTURENOTER_VENV": "~/moj"}) == Path("~/moj").expanduser()


def test_needs_install_tracks_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("a\n")
    venv = tmp_path / "venv"
    venv.mkdir()
    assert launch.needs_install(venv, tmp_path)
    (venv / ".lecturenoter-requirements").write_text(launch.requirements_stamp(tmp_path))
    assert not launch.needs_install(venv, tmp_path)
    (tmp_path / "requirements.txt").write_text("a\nb\n")
    assert launch.needs_install(venv, tmp_path)


def test_script_works_from_any_directory_and_passes_args(tmp_path):
    """Izolowany projekt: skrypt tworzy venv, 'instaluje' puste zaleznosci i uruchamia server.py z katalogu projektu."""
    proj = tmp_path / "projekt ze spacjami"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "launch.py", proj / "scripts" / "launch.py")
    (proj / "requirements.txt").write_text("# brak zaleznosci\n")
    (proj / "server.py").write_text("import os, sys\nprint('SERVER cwd=' + os.getcwd() + ' args=' + ' '.join(sys.argv[1:]))\n")
    elsewhere = tmp_path / "inny katalog"
    elsewhere.mkdir()
    env = dict(os.environ, LECTURENOTER_VENV=str(tmp_path / "venv"), LECTURENOTER_PYTHON=sys.executable)
    r = subprocess.run([sys.executable, str(proj / "scripts" / "launch.py"), "--port", "8123"], cwd=elsewhere,
                       env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert f"SERVER cwd={proj.resolve()} args=--port 8123" in r.stdout
    assert (tmp_path / "venv" / "bin" / "python").exists() and (tmp_path / "venv" / ".lecturenoter-requirements").exists()
    # drugie uruchomienie nie instaluje ponownie
    r2 = subprocess.run([sys.executable, str(proj / "scripts" / "launch.py")], cwd=elsewhere, env=env,
                        capture_output=True, text=True, timeout=60)
    assert "Instaluje zaleznosci" not in r2.stdout and "SERVER cwd=" in r2.stdout


def test_missing_python_gives_instructions(tmp_path):
    proj = tmp_path / "p"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "launch.py", proj / "scripts" / "launch.py")
    (proj / "requirements.txt").write_text("\n")
    (proj / "server.py").write_text("print('nie powinno sie uruchomic')\n")
    env = dict(os.environ, LECTURENOTER_VENV=str(tmp_path / "v"), LECTURENOTER_PYTHON="python-ktorego-nie-ma")
    r = subprocess.run([sys.executable, str(proj / "scripts" / "launch.py")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "deadsnakes" in r.stderr and "nie powinno" not in r.stdout


def test_background_mode_detaches_and_adds_auto_exit(tmp_path):
    """--background: skrypt wraca od razu, serwer dziala w tle z --auto-exit, a jego wyjscie trafia do work/launch.log."""
    proj = tmp_path / "projekt"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "launch.py", proj / "scripts" / "launch.py")
    (proj / "requirements.txt").write_text("\n")
    (proj / "server.py").write_text(
        "import sys, time, pathlib\n"
        "time.sleep(1.0)\n"
        "pathlib.Path('marker.txt').write_text(' '.join(sys.argv[1:]))\n"
        "print('SERWER W TLE', ' '.join(sys.argv[1:]), flush=True)\n")
    env = dict(os.environ, LECTURENOTER_VENV=str(tmp_path / "venv"), LECTURENOTER_PYTHON=sys.executable)
    import time
    t0 = time.time()
    r = subprocess.run([sys.executable, str(proj / "scripts" / "launch.py"), "--background", "--port", "8123"],
                       env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "uruchomiony w tle" in r.stdout and "SERWER W TLE" not in r.stdout  # nic z serwera nie leci na terminal
    marker = proj / "marker.txt"
    for _ in range(100):  # serwer konczy prace po ~1 s, niezaleznie od launch.py
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.read_text() == "--port 8123 --auto-exit"
    log = (proj / "work" / "launch.log").read_text()
    assert "SERWER W TLE --port 8123 --auto-exit" in log
