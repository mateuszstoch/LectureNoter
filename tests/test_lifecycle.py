import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ui.lifecycle import AutoExit, ClientTracker, KeepAwake, awake_command  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(busy=lambda: False, grace=30, startup=180):
    clk = Clock()
    tr = ClientTracker(clk)
    fired = []
    ae = AutoExit(tr, busy, lambda: fired.append(clk.t), grace, startup, clk)
    return clk, tr, ae, fired


def test_exits_after_window_closed_and_grace():
    clk, tr, ae, fired = make()
    tr.enter()
    clk.advance(500)
    assert ae.tick() is False and not fired        # okno otwarte: serwer dziala dowolnie dlugo
    tr.leave()                                     # zamknieto okno
    clk.advance(29)
    assert ae.tick() is False and not fired        # jeszcze w czasie karencji (np. odswiezenie strony)
    clk.advance(2)
    assert ae.tick() is True and len(fired) == 1
    assert ae.tick() is False and len(fired) == 1  # tylko raz


def test_page_reload_does_not_shut_down():
    clk, tr, ae, fired = make()
    tr.enter()
    tr.leave()
    clk.advance(5)
    tr.enter()                                     # po odswiezeniu strony okno wraca
    clk.advance(120)
    assert ae.tick() is False and not fired


def test_busy_server_survives_without_window_then_exits_after_work_ends():
    state = {"busy": True}
    clk, tr, ae, fired = make(busy=lambda: state["busy"])
    tr.enter()
    tr.leave()
    clk.advance(10_000)                            # godziny przetwarzania bez okna
    assert ae.tick() is False and not fired
    state["busy"] = False                          # koniec pracy: karencja liczona od teraz
    clk.advance(20)
    assert ae.tick() is False
    clk.advance(15)
    assert ae.tick() is True and fired


def test_exits_when_nobody_ever_connects():
    clk, tr, ae, fired = make()
    clk.advance(179)
    assert ae.tick() is False
    clk.advance(2)
    assert ae.tick() is True and fired


def test_busy_blocks_startup_exit_too():
    clk, tr, ae, fired = make(busy=lambda: True)
    clk.advance(10_000)
    assert ae.tick() is False and not fired


def test_tracker_never_negative():
    tr = ClientTracker()
    tr.leave()
    assert tr.count == 0


# ---------- keep-awake ----------
def test_awake_command_per_platform():
    ps = awake_command(wsl=True, system="Linux")
    assert ps[0] == "powershell.exe" and "SetThreadExecutionState" in ps[-1] and "0x80000001" in ps[-1]
    assert awake_command(wsl=False, system="Darwin")[0] == "caffeinate"
    assert awake_command(wsl=False, system="Linux", which=lambda n: "/usr/bin/systemd-inhibit")[0] == "systemd-inhibit"
    assert awake_command(wsl=False, system="Linux", which=lambda n: None) is None
    assert awake_command(wsl=False, system="Windows") is None


def test_keepawake_renews_only_while_busy():
    clk = Clock()
    state = {"busy": False}
    spawned = []
    ka = KeepAwake(lambda: state["busy"], spawn=lambda cmd, **kw: spawned.append(cmd), command=lambda: ["x"], period=60, clock=clk)
    assert ka.tick() is False and spawned == []
    state["busy"] = True
    assert ka.tick() is True and len(spawned) == 1
    clk.advance(30)
    assert ka.tick() is False and len(spawned) == 1   # blokada jeszcze wazna
    clk.advance(31)
    assert ka.tick() is True and len(spawned) == 2     # odnowiona
    state["busy"] = False
    clk.advance(100)
    assert ka.tick() is False and len(spawned) == 2   # po pracy nic nie odnawiamy: blokada wygasa sama
    state["busy"] = True
    assert ka.tick() is True and len(spawned) == 3     # nowa praca: od razu nowa blokada


def test_keepawake_survives_missing_tool_and_spawn_errors():
    clk = Clock()
    ka = KeepAwake(lambda: True, command=lambda: None, clock=clk)
    assert ka.tick() is False  # brak polecenia: tylko ostrzezenie

    def boom(cmd, **kw):
        raise OSError("nie ma powershell.exe")
    ka2 = KeepAwake(lambda: True, spawn=boom, command=lambda: ["powershell.exe"], clock=clk)
    assert ka2.tick() is False  # nie wyrzuca wyjatku
