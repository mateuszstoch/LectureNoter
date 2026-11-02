"""Cykl zycia serwera: samoczynne wylaczanie po zamknieciu aplikacji i blokada uspienia komputera na czas pracy."""
import os
import platform
import shutil
import subprocess
import threading
import time

from loguru import logger


class ClientTracker:
    """Liczy polaczone okna aplikacji (strumien SSE /api/stream) i zapamietuje, kiedy ostatnio cos sie dzialo."""

    def __init__(self, clock=time.time):
        self._clock, self._lock = clock, threading.Lock()
        self.count = 0
        self.ever_connected = False
        self.last_activity = clock()

    def enter(self):
        with self._lock:
            self.count += 1
            self.ever_connected = True
            self.last_activity = self._clock()

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

    def __init__(self, tracker, is_busy, request_exit, grace=30.0, startup_grace=180.0, clock=time.time):
        super().__init__()
        self.tracker, self.is_busy, self.request_exit = tracker, is_busy, request_exit
        self.grace, self.startup_grace, self.clock = grace, startup_grace, clock
        self.started = clock()
        self.fired = False

    def tick(self):
        if self.fired:
            return False
        if self.is_busy():
            self.tracker.touch()  # licz bezczynnosc od konca pracy
            return False
        if self.tracker.count > 0:
            return False
        if self.tracker.ever_connected:
            idle, limit, why = self.clock() - self.tracker.last_activity, self.grace, "zamknieto okno aplikacji"
        else:
            idle, limit, why = self.clock() - self.started, self.startup_grace, "nikt nie polaczyl sie po starcie"
        if idle < limit:
            return False
        self.fired = True
        logger.info(f"auto-exit: {why}, brak pracy od {idle:.0f} s: wylaczam serwer")
        self.request_exit()
        return True


def is_wsl():
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


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
