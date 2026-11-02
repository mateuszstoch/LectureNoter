#!/usr/bin/env bash
# Uruchamia LectureNoter na Linuksie/WSL (tworzy venv i instaluje zaleznosci przy pierwszym uruchomieniu).
# Dziala z dowolnego katalogu: sciezke projektu bierze z lokalizacji tego pliku.
set -e
exec python3 "$(dirname "$0")/launch.py" "$@"
