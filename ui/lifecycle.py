"""Cykl zycia serwera: samoczynne wylaczanie po zamknieciu aplikacji i blokada uspienia komputera na czas pracy."""
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from loguru import logger


class ClientTracker:
    """Liczy polaczone okna aplikacji (strumien SSE /api/stream) i zapamietuje, kiedy ostatnio cos sie dzialo."""

    def __init__(self, clock=time.time):
        self._clock, self._lock = clock, threading.Lock()
        self.count = 0
        self.ever_connected = False
        self.last_activity = clock()
        self.bye_at = None  # okno zglosilo zamkniecie (pagehide): wtedy karencja jest krotka

    def enter(self):
        with self._lock:
            self.count += 1
            self.ever_connected = True
            self.bye_at = None
            self.last_activity = self._clock()

    def bye(self):
        with self._lock:
            self.bye_at = self._clock()

    def leave(self):
        with self._lock:
            self.count = max(0, self.count - 1)
            self.last_activity = self._clock()

    def touch(self):
        with self._lock:
            self.last_activity = self._clock()


class _Loop:
    """Watek wywolujacy tick() co `interval` sekund."""
    interval = 2.0

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        def run():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:
                    logger.exception(f"{type(self).__name__}: blad petli")
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class AutoExit(_Loop):
    """Konczy serwer, gdy nikt nie jest polaczony (zamknieto okno aplikacji) i nic nie jest przetwarzane.

    - po odlaczeniu ostatniego klienta czeka `grace` s (odswiezenie strony nie wylacza serwera),
    - jesli zaden klient nie polaczy sie w ciagu `startup_grace` s od startu, tez konczy (zeby nie zostawal w tle),
    - dopoki trwa praca (zadanie, odpowiedz czatu), serwer dziala nawet bez okna i konczy po jej zakonczeniu."""

    def __init__(self, tracker, is_busy, request_exit, grace=120.0, startup_grace=180.0, clock=time.time, bye_grace=15.0):
        super().__init__()
        self.tracker, self.is_busy, self.request_exit = tracker, is_busy, request_exit
        self.grace, self.startup_grace, self.bye_grace, self.clock = grace, startup_grace, bye_grace, clock
        self.started = clock()
        self.fired = False
        self.reason = None

    def tick(self):
        if self.fired:
            return False
        if self.is_busy():
            self.tracker.touch()  # licz bezczynnosc od konca pracy
            return False
        if self.tracker.count > 0:
            return False
        if self.tracker.ever_connected:
            # jawne zamkniecie okna (pagehide) = krotka karencja; samo zerwane polaczenie (uspienie karty, przerwa w sieci,
            # zasniecie komputera) = dluga, zeby serwer nie znikal spod otwartego okna
            idle, why = self.clock() - self.tracker.last_activity, "zamknieto okno aplikacji"
            limit = self.bye_grace if self.tracker.bye_at is not None else self.grace
            if self.tracker.bye_at is None:
                why = "polaczenie okna zerwane i nie wrocilo"
        else:
            idle, limit, why = self.clock() - self.started, self.startup_grace, "nikt nie polaczyl sie po starcie"
        if idle < limit:
            return False
        self.fired = True
        self.reason = f"auto-exit: {why} (brak okna i pracy od {idle:.0f} s)"
        logger.info(self.reason)
        self.request_exit()
        return True


def is_wsl():
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


def read_mem():
    """Pamiec procesu serwera i dostepna pamiec systemu (MB); None poza Linuksem. Zapisywane w heartbeat, zeby po nagłym
    zabiciu (np. przez OOM killer) bylo widac, czy system byl na granicy pamieci."""
    out = {"rss_mb": None, "mem_available_mb": None}
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                out["rss_mb"] = int(line.split()[1]) // 1024
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                out["mem_available_mb"] = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


class Liveness(_Loop):
    """Zapisuje stan serwera (work/server.state.json) co 10 s i znacznik czystego zamkniecia. Przy kolejnym starcie
    brak znacznika przy martwym procesie oznacza NAGLE zakonczenie (zabicie procesu, OOM, zasniecie/zamkniecie WSL),
    ktore inaczej nie zostawiloby zadnego sladu."""
    interval = 10.0

    def __init__(self, path, clock=time.time, pid=None, alive=_pid_alive):
        super().__init__()
        from io_utils import read_json
        self.path, self.clock, self.pid = Path(path), clock, pid or os.getpid()
        old = read_json(self.path, None)
        self.previous = self._classify(old, alive)
        self.started = clock()

    def _classify(self, old, alive):
        if not old:
            return None
        if old.get("clean_exit"):
            return {"kind": "clean", "reason": old.get("reason"), "ended": old.get("ended")}
        if old.get("pid") and old["pid"] != self.pid and alive(old["pid"]):
            return {"kind": "running", "pid": old["pid"]}  # inna instancja nadal dziala
        return {"kind": "unexpected", "started": old.get("started"), "last_seen": old.get("heartbeat"),
                "rss_mb": old.get("rss_mb"), "mem_available_mb": old.get("mem_available_mb")}

    def _write(self, **extra):
        from io_utils import atomic_write_json
        data = {"pid": self.pid, "started": self.started, "heartbeat": self.clock(), "clean_exit": False, **read_mem(), **extra}
        try:
            atomic_write_json(str(self.path), data)
        except OSError as e:
            logger.warning(f"liveness: nie udalo sie zapisac {self.path.name}: {e}")

    def tick(self):
        self._write()

    def begin(self):
        if self.previous and self.previous["kind"] == "unexpected":
            logger.warning(f"POPRZEDNIE URUCHOMIENIE ZAKONCZYLO SIE NIEOCZEKIWANIE (brak znacznika czystego zamkniecia): "
                           f"ostatnia aktywnosc {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.previous.get('last_seen') or 0))}, "
                           f"pamiec serwera {self.previous.get('rss_mb')} MB, wolna pamiec systemu {self.previous.get('mem_available_mb')} MB")
        self._write()
        return self.start()

    def close(self, reason):
        self.stop()
        self._write(clean_exit=True, reason=reason, ended=self.clock())


AWAKE_SECONDS = 90
_PS = ("$s='[DllImport(\"kernel32.dll\")] public static extern uint SetThreadExecutionState(uint f);';"
       "$t=Add-Type -MemberDefinition $s -Name W -Namespace LN -PassThru;"
       "[void]$t::SetThreadExecutionState(0x80000001);"  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED (ekran moze gasnac)
       f"Start-Sleep -Seconds {AWAKE_SECONDS}")


def awake_command(wsl=None, system=None, which=shutil.which):
    """Polecenie, ktore przez ~AWAKE_SECONDS s blokuje uspienie komputera (a po zakonczeniu samo przestaje)."""
    wsl = is_wsl() if wsl is None else wsl
    system = platform.system() if system is None else system
    if wsl:
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS]
    if system == "Darwin":
        return ["caffeinate", "-i", "-t", str(AWAKE_SECONDS)]
    if system == "Linux" and which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=sleep:idle", "--who=LectureNoter", "--why=Przetwarzanie wykladu",
                "sleep", str(AWAKE_SECONDS)]
    return None


class KeepAwake(_Loop):
    """Na czas pracy (zadanie, odpowiedz czatu) co `period` s odnawia krotka blokade uspienia. Gdy serwer padnie,
    blokada wygasa sama w ciagu ~90 s, wiec komputer nie zostaje nigdy 'zablokowany' na stale."""
    interval = 5.0

    def __init__(self, is_busy, spawn=subprocess.Popen, command=awake_command, period=60.0, clock=time.time):
        super().__init__()
        self.is_busy, self.spawn, self.command, self.period, self.clock = is_busy, spawn, command, period, clock
        self._last = None
        self._warned = False

    def tick(self):
        if not self.is_busy():
            self._last = None
            return False
        now = self.clock()
        if self._last is not None and now - self._last < self.period:
            return False
        cmd = self.command()
        if not cmd:
            if not self._warned:
                logger.warning("keep-awake: brak sposobu na zablokowanie uspienia na tym systemie")
                self._warned = True
            return False
        try:
            self.spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            self._last = now
            if not self._warned:
                logger.info("keep-awake: komputer nie bedzie usypiany na czas przetwarzania (ekran moze gasnac)")
                self._warned = True
            return True
        except OSError as e:
            if not self._warned:
                logger.warning(f"keep-awake: nie udalo sie uruchomic {cmd[0]}: {e}")
                self._warned = True
            self._last = now
            return False
