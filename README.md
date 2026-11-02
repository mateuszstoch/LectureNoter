# LectureNoter

Lokalny generator notatek z nagrań wykładów (mowa + tablica/slajdy → `lecture_notes.md`).
Wszystko działa lokalnie. Modele ładowane są po kolei (jeden naraz w VRAM).

## Jak to działa

1. **ASR** – `faster-whisper` (`large-v3-turbo`) → `transcript.json`
2. **Tablica/slajdy** – ffmpeg + OpenCV wykrywa obszar tablicy i zapisuje jej stan tuż przed wymazaniem → `frames/`, `frames_metadata.json`
3. **VLM** – Ollama (`qwen2.5vl:7b`) odczytuje tekst z klatek → `vision_context.json`
4. **LLM** – Ollama (`qwen2.5:7b-instruct` lub `14b-instruct`) składa notatki fragmentami → `lecture_notes.md`

Wyniki trafiają do `work/<nazwa_filmu>/`. Przerwany pipeline można uruchomić ponownie: ukończone etapy są pomijane, a zmiana konfiguracji powtarza tylko etapy, których dotyczy.

## Wymagania (Linux + GPU NVIDIA)

- Python 3.11+
- `ffmpeg` i `ffprobe` w `PATH`
- sterownik NVIDIA (cuBLAS 12 i cuDNN 9 dla faster-whisper instalują się z `requirements.txt` jako pakiety pip; orkiestrator sam ustawia dla nich `LD_LIBRARY_PATH`)
- [Ollama](https://ollama.com) uruchomiona lokalnie

## Instalacja

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

ollama pull qwen2.5vl:7b
ollama pull qwen2.5:7b-instruct    # karta 8 GB
ollama pull qwen2.5:14b-instruct   # karta 16 GB (opcjonalnie)
```

## Uruchomienie

```bash
python main.py wyklad.mp4
```

Przydatne opcje:

| Opcja | Znaczenie |
|-------|-----------|
| `--workdir DIR` | katalog wyników (domyślnie `work/<nazwa_filmu>`) |
| `--profile auto\|8gb\|16gb` | wybór modelu LLM i `num_ctx` (domyślnie wykrywany z VRAM) |
| `--from-stage asr\|frames\|vlm\|llm` | zacznij od wskazanego etapu |
| `--only-stage ...` | uruchom tylko jeden etap |
| `--force` | wymuś ponowne wykonanie etapów |
| `--status` | wypisz stan etapów i postęp jako JSON (nic nie uruchamia) |

Ctrl+C lub `kill <pid>` przerywa pracę bezpiecznie (model zostaje rozładowany, stan „cancelled”); ponowne uruchomienie wznawia od ostatniego checkpointu. W katalogu roboczym powstają `meta.json`, `progress.json` i `run_state.json` (stan na potrzeby interfejsu).

Brak ścieżki audio w pliku: ASR jest pomijany, a notatki powstają tylko z tablicy.

## Serwer API (baza pod interfejs)

```bash
python server.py            # http://127.0.0.1:8000 (tylko lokalnie)
```

Filmy wrzucaj do katalogu `videos/` (dowiązania symboliczne działają). API: lista plików i filmów, trwała kolejka zadań (jedno naraz), regeneracja etapów, anulowanie, strumień stanu (`/api/stream`) i logów. 
Interfejs (PWA) jest pod `http://localhost:8000`: lista filmów z postępem etapów, dodawanie z `videos/`, kolejka, regeneracja etapów i logi. Zakładka **Notatki** renderuje Markdown z wzorami (KaTeX, działa offline), ze spisem treści, wyszukiwaniem i klikalnymi znacznikami czasu (pokazują tablicę i mowę z danego momentu). Notatki można edytować: wersja edytowana zapisuje się osobno (`lecture_notes.edited.md`), ma historię i nie jest nadpisywana przy ponownej generacji. Eksport: Markdown, PDF (przycisk otwiera okno drukowania, wybierz „Zapisz jako PDF”) i Word (wymaga `pandoc`: `sudo apt install pandoc`). Zakładka **Materiały** pokazuje klatki z tablicy i przeszukiwalny transkrypt. Czat: w przygotowaniu, patrz `PLAN_UI.md`.

Jako aplikację zainstalujesz w Edge/Chrome (menu → „Zainstaluj aplikację”). Na Windows 11 + WSL2 skrót `scripts\start.bat` uruchamia serwer w WSL i otwiera okno aplikacji (domyślnie projekt w `~/LectureNoter`, zmień zmienną `LECTURENOTER_DIR`).

## Windows 11 (WSL2)

Działa przez WSL2 z GPU (niesprawdzone przez autora, testowane tylko na macOS):

- Sterownik NVIDIA instaluj **tylko w Windowsie**. W WSL `nvidia-smi` ma pokazać kartę.
- cuDNN 9 i cuBLAS 12 instalują się z `requirements.txt`. Błąd `libcublas.so.12 is not found`: zrób `pip install -r requirements.txt` ponownie w aktywnym `.venv`.
- Najprościej zainstalować **Ollamę w WSL** (jak w instalacji powyżej). Bez systemd uruchom ją ręcznie: `ollama serve`. Nie uruchamiaj jednocześnie Ollamy z Windowsa, bo oba serwery mogą trzymać modele w VRAM.
- W `%UserProfile%\.wslconfig` zwiększ pamięć, np. `[wsl2]` i `memory=24GB` (domyślnie WSL dostaje ok. 50% RAM).
- Projekt i filmy trzymaj w systemie plików WSL (np. `~/`), nie w `/mnt/c/...`, bo tam I/O jest dużo wolniejsze.
- Windows zajmuje ok. 0,5–1 GB VRAM. Na karcie 8 GB zamknij przeglądarkę i inne programy używające GPU na czas uruchomienia.

## Konfiguracja

Wszystkie progi i modele są w `config.yaml`. Najczęściej trzeba dostroić sekcję `frames`, jeśli wybiera za dużo lub za mało klatek (np. `ink_level`, `erase_fraction`, `save_new_ink_px`, `max_hold_s`).

## Testy

```bash
pytest -q tests
```

Testy nie wymagają GPU ani Ollamy (korzystają z atrap), działają także na macOS.

## Status

Etap 2 sprawdzony na prawdziwym nagraniu. ASR, VLM i LLM zweryfikowane tylko testami z atrapami, nie na prawdziwych modelach. Szczegóły i znane ryzyka: `PLAN_IMPLEMENTACJI.md`.
