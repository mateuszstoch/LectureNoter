#!/usr/bin/env python3
"""Uruchamia serwer LectureNoter na Linuksie/WSL z katalogu, w ktorym lezy projekt (niezaleznie od biezacego katalogu).

Przy pierwszym uruchomieniu tworzy srodowisko wirtualne i instaluje zaleznosci; potem tylko startuje serwer.
Plik uzywa wylacznie biblioteki standardowej i skladni zgodnej z Pythonem 3.8+, zeby dalo sie go uruchomic
systemowym python3 nawet na starszym Ubuntu (a sam serwer dziala na interpreterze 3.10+ ze srodowiska venv).

Zmienne srodowiskowe (opcjonalne):
  LECTURENOTER_PYTHON   interpreter do utworzenia venv (domyslnie najnowszy znaleziony python 3.10+)
  LECTURENOTER_VENV     katalog venv (domyslnie <projekt>/.venv; gdy projekt jest na dysku Windows pod /mnt/,
                        domyslnie ~/.local/share/lecturenoter/venv, bo NTFS jest wolny i slabo znosi dowiazania)
Argumenty skryptu (np. --port 8001, --auto-exit) sa przekazywane do server.py. Wyjatek: --background (--bg) uruchamia
serwer w tle, bez terminala (log: work/launch.log), z --auto-exit, czyli sam wylaczy sie po zamknieciu okna aplikacji.
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 10)
CANDIDATES = ["python3.13", "python3.12", "python3.11", "python3.10", "python3"]
ROOT = Path(__file__).resolve().parent.parent


def say(msg):
    print(f"[LectureNoter] {msg}", flush=True)


def die(msg, code=1):
    print(f"\n[LectureNoter] BLAD: {msg}\n", file=sys.stderr, flush=True)
    sys.exit(code)


def python_version(exe):
    """(major, minor) interpretera albo None, gdy sie nie uruchamia."""
    try:
        out = subprocess.run([str(exe), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                             capture_output=True, text=True, timeout=20).stdout.split()
        return int(out[0]), int(out[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def pick_python(env=None, which=shutil.which, version=python_version):
    """Pierwszy interpreter w wersji >= MIN_PY (wskazany zmienna lub z listy kandydatow)."""
    env = os.environ if env is None else env
    forced = env.get("LECTURENOTER_PYTHON")
    names = [forced] if forced else CANDIDATES
    for name in names:
        exe = which(name)
        if not exe:
            continue
        v = version(exe)
        if v and v >= MIN_PY:
            return exe
    return None


def default_venv(root=ROOT, env=None, home=None):
    env = os.environ if env is None else env
    if env.get("LECTURENOTER_VENV"):
        return Path(env["LECTURENOTER_VENV"]).expanduser()
    if str(root).startswith("/mnt/"):  # projekt na dysku Windows
        base = Path(env.get("XDG_DATA_HOME") or (home or Path.home()) / ".local" / "share")
        return base / "lecturenoter" / "venv"
    return root / ".venv"


def requirements_stamp(root=ROOT):
    return hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()


def needs_install(venv, root=ROOT):
    stamp = venv / ".lecturenoter-requirements"
    return not stamp.exists() or stamp.read_text().strip() != requirements_stamp(root)


def run(cmd, **kw):
    say("> " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


def ensure_venv(venv, root=ROOT):
    vpy = venv / "bin" / "python"
    if vpy.exists():
        v = python_version(vpy)
        if v and v >= MIN_PY:
            return vpy
        say(f"Istniejace srodowisko {venv} ma zbyt stary lub uszkodzony Python ({v}); tworze od nowa.")
        shutil.rmtree(venv, ignore_errors=True)
    exe = pick_python()
    if not exe:
        die("Nie znaleziono Pythona 3.10+ (zalecany 3.11). Na Ubuntu 20.04/22.04:\n"
            "  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update\n"
            "  sudo apt install python3.11 python3.11-venv python3.11-dev\n"
            "Mozesz tez wskazac interpreter zmienna LECTURENOTER_PYTHON.")
    say(f"Tworze srodowisko wirtualne w {venv} ({exe})")
    venv.parent.mkdir(parents=True, exist_ok=True)
    if run([exe, "-m", "venv", venv]).returncode != 0:
        shutil.rmtree(venv, ignore_errors=True)
        die(f"Nie udalo sie utworzyc venv. Na Ubuntu zainstaluj pakiet venv, np.: sudo apt install {Path(exe).name}-venv")
    return vpy


def spawn_background(vpy, args):
    """Uruchamia serwer odlaczony od terminala; log w work/launch.log."""
    log_dir = ROOT / "work"
    log_dir.mkdir(exist_ok=True)
    log = open(log_dir / "launch.log", "w", encoding="utf-8")
    proc = subprocess.Popen([str(vpy), "server.py"] + args, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log,
                            stderr=log, start_new_session=True)
    say(f"Serwer uruchomiony w tle (pid {proc.pid}), log: {log_dir / 'launch.log'}")
    say("Adres: http://localhost:8000 . Wylaczy sie sam po zamknieciu okna aplikacji (albo przycisk 'Zakoncz').")


def main(argv):
    background = any(a in ("--background", "--bg") for a in argv)
    argv = [a for a in argv if a not in ("--background", "--bg")]
    if background and "--auto-exit" not in argv:
        argv.append("--auto-exit")  # bez terminala nie bylo by czym go zatrzymac
    os.chdir(ROOT)
    say(f"Katalog projektu: {ROOT}")
    if str(ROOT).startswith("/mnt/"):
        say("Uwaga: projekt lezy na dysku Windows (/mnt/...). Dziala, ale odczyt plikow jest wolniejszy niz w "
            "systemie plikow WSL (np. ~/LectureNoter).")
    if not (ROOT / "server.py").exists():
        die(f"Nie znaleziono server.py w {ROOT}. Uruchom skrypt z projektu LectureNoter.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        say("OSTRZEZENIE: brak ffmpeg/ffprobe w PATH (sudo apt install ffmpeg). Serwer wystartuje, ale przetwarzanie zawiedzie.")
    venv = default_venv()
    vpy = ensure_venv(venv)
    if needs_install(venv):
        say("Instaluje zaleznosci (pierwsze uruchomienie moze potrwac kilka minut)...")
        if run([vpy, "-m", "pip", "install", "-r", ROOT / "requirements.txt"]).returncode != 0:
            die("Instalacja zaleznosci nie powiodla sie (patrz komunikaty powyzej).")
        (venv / ".lecturenoter-requirements").write_text(requirements_stamp())
    if background:
        spawn_background(vpy, list(argv))
        return
    say("Start serwera: http://localhost:8000 (Ctrl+C konczy)")
    os.execv(str(vpy), [str(vpy), "server.py"] + list(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
