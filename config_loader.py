"""Konfiguracja z pliku YAML przeladowywana po zmianie pliku (bez restartu serwera).

Serwer dziala dlugo, a zadania to osobne procesy, ktore czytaja config.yaml za kazdym razem. Gdy plik sie zmieni
(np. po `git pull`), serwer ze stara konfiguracja w pamieci liczylby inny podpis etapu niz zadanie i pokazywalby
swiezo wygenerowane wyniki jako 'nieaktualne'."""
import threading
from pathlib import Path

import yaml
from loguru import logger


class LiveConfig:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime = None
        self._cfg = None
        self()  # pierwsze wczytanie: blad tutaj ma przerwac start serwera

    def __call__(self):
        with self._lock:
            try:
                m = self.path.stat().st_mtime_ns
            except OSError:
                return self._cfg
            if m != self._mtime:
                try:
                    new = yaml.safe_load(self.path.read_text(encoding="utf-8"))
                    if self._cfg is not None:
                        logger.info(f"config: przeladowano {self.path.name} (zmiana pliku)")
                    self._cfg, self._mtime = new, m
                except (OSError, yaml.YAMLError) as e:
                    if self._cfg is None:
                        raise
                    logger.warning(f"config: nie udalo sie wczytac {self.path.name}, zostaje poprzednia wersja: {e}")
                    self._mtime = m  # nie ponawiaj bledu przy kazdym odczycie
            return self._cfg


def resolve(cfg):
    """cfg moze byc slownikiem albo funkcja zwracajaca aktualny slownik (LiveConfig)."""
    return cfg() if callable(cfg) else cfg
