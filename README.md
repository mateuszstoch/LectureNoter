# LectureNoter

Lokalny generator notatek z nagrań wykładów (mowa + tablica/slajdy → notatki w Markdown) z aplikacją webową: kolejka przetwarzania, czytnik i edytor notatek oraz czat z lokalnym LLM o treści wykładu. Wszystko działa lokalnie, modele ładują się po kolei (jeden naraz w VRAM).

## Jak to działa

1. **ASR** – `faster-whisper` (`large-v3-turbo`) → `transcript.json`
2. **Tablica/slajdy** – ffmpeg + OpenCV wykrywa obszar tablicy i zapisuje jej stan tuż przed wymazaniem → `frames/`
3. **VLM** – Ollama (`qwen2.5vl:7b`) odczytuje tekst z klatek → `vision_context.json`
4. **LLM** – Ollama (`qwen2.5:7b-instruct` lub `14b-instruct`) składa notatki fragmentami → `lecture_notes.md`

Wyniki trafiają do `work/<nazwa_filmu>/`. Przerwane przetwarzanie wznawia się od ostatniego checkpointu, a zmiana konfiguracji powtarza tylko etapy, których dotyczy.

## Wymagania

**Backend działa tylko na Linuksie.** Na Windowsie uruchamiasz go w **WSL2** (nie natywnie: `python server.py` w PowerShellu się nie uruchomi). Potrzebne:

- karta NVIDIA ze sterownikiem (na Windowsie sterownik instalujesz tylko w Windowsie, w WSL `nvidia-smi` ma pokazać kartę)
- Python 3.10+ (zalecany 3.11), `ffmpeg`, [Ollama](https://ollama.com) uruchomiona w WSL
- opcjonalnie `pandoc` (eksport notatek do Worda)

## Instalacja i uruchomienie

Projekt możesz mieć **na dysku Windows** (np. `C:\Users\...\LectureNoter`) albo w systemie plików WSL/Linuksa. Środowisko Pythona i zależności instalują się same przy pierwszym uruchomieniu (skrypt `scripts/launch.py`), więc ręczne `venv`/`pip` nie jest potrzebne. Jednorazowo potrzebujesz tylko pakietów systemowych i modeli (w terminalu WSL/Linux):

```bash
sudo apt install ffmpeg pandoc python3-venv
ollama pull qwen2.5vl:7b
ollama pull qwen2.5:7b-instruct    # karta 8 GB
ollama pull qwen2.5:14b-instruct   # karta 16 GB (opcjonalnie)
```

Wymagany jest Python **3.10+** (zalecany 3.11). Na starszym Ubuntu: `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt update && sudo apt install python3.11 python3.11-venv python3.11-dev`. Skrypt sam wybierze najnowszy znaleziony interpreter (lub użyj `LECTURENOTER_PYTHON`).

### Windows 11 (WSL2)

Uruchom `scripts\start.bat` (dwuklik albo skrót na Pulpicie/pasku zadań). Skrypt:

1. wyznacza folder projektu z położenia samego pliku i zamienia go na ścieżkę WSL (`C:\Users\Jan\LectureNoter` → `/mnt/c/Users/Jan/LectureNoter`),
2. uruchamia serwer w WSL (okno „LectureNoter server” pokazuje postęp i błędy; pierwsze uruchomienie instaluje zależności i trwa kilka minut),
3. otwiera aplikację w oknie Edge/Chrome bez paska przeglądarki.

- Gdy projekt jest na dysku Windows (`/mnt/...`), środowisko Pythona jest tworzone w `~/.local/share/lecturenoter/venv` (NTFS jest wolny i słabo znosi dowiązania). Odczyt plików z `/mnt/c` jest wolniejszy niż z systemu plików WSL, ale działa.
- Domyślna dystrybucja WSL inna niż Ubuntu (np. `docker-desktop`): ustaw zmienną `LECTURENOTER_DISTRO=Ubuntu` (nazwy: `wsl -l`).
- Własna ścieżka projektu w WSL: `LECTURENOTER_DIR=/home/jan/LectureNoter`.
- Jeśli nic się nie dzieje, uruchom `scripts\start.bat` z `cmd` (nie dwuklikiem), żeby zobaczyć komunikaty.
- Aplikację można też zainstalować z przeglądarki (Edge: menu → Aplikacje → „Zainstaluj tę witrynę jako aplikację”). Okno zainstalowanej aplikacji nie uruchamia serwera: najpierw musi działać `start.bat`.

### Linux / terminal WSL

```bash
scripts/start.sh        # z dowolnego katalogu; http://localhost:8000
```

Opcje: `LECTURENOTER_VENV` (katalog środowiska), `LECTURENOTER_PYTHON` (interpreter), argumenty trafiają do serwera (np. `scripts/start.sh --port 8001`).

### Wskazówki dla WSL2

- **Ollama zainstaluj w WSL** i nie uruchamiaj jej równolegle w Windowsie (dwa serwery mogą trzymać modele w VRAM jednocześnie). Bez systemd uruchom ją ręcznie: `ollama serve`.
- W `%UserProfile%\.wslconfig` możesz zwiększyć pamięć: `[wsl2]` i `memory=24GB` (domyślnie WSL dostaje ok. 50% RAM). Po zmianie: `wsl --shutdown`.
- Windows zajmuje ok. 0,5–1 GB VRAM. Na karcie 8 GB zamknij przeglądarkę i inne programy używające GPU na czas przetwarzania.
- Błąd `libcublas.so.12 is not found`: uruchom ponownie `start.bat`/`start.sh` (doinstaluje zależności) albo usuń środowisko venv, żeby powstało od nowa.

## Aplikacja

- **Biblioteka:** wybór filmu z katalogu `videos/`, lista z postępem czterech etapów, trwała kolejka (jedno zadanie naraz; pauza, zmiana kolejności, anulowanie).
- **Szczegóły filmu:** regeneracja pojedynczego etapu (z podglądem, co zostanie usunięte), uruchomienie całości od nowa, wznowienie po awarii, logi na żywo.
- **Notatki:** Markdown z wzorami (KaTeX, działa offline), spis treści, wyszukiwanie, klikalne znaczniki czasu (pokazują tablicę i mowę z danego momentu). Edycja z historią wersji: wersja edytowana zapisuje się osobno (`lecture_notes.edited.md`) i nie jest nadpisywana przy ponownej generacji. Eksport: Markdown, PDF (przycisk „PDF” otwiera okno drukowania, wybierz „Zapisz jako PDF”), Word (wymaga `pandoc`).
- **Materiały:** klatki z tablicy i przeszukiwalny transkrypt.
- **Czat:** pytania do notatek, transkryptu i odczytu tablicy jednego filmu. Odpowiedzi są strumieniowane, rozmowy zapisują się per film, starsze tury są streszczane. Model wybierasz spośród zainstalowanych w Ollamie. Czat i przetwarzanie nie używają GPU jednocześnie: w czasie zadania czat jest wyłączony, a bezczynny model czatu jest zwalniany przed startem zadania.

Inna lokalizacja Ollamy: zmienna `OLLAMA_HOST` (np. `OLLAMA_HOST=127.0.0.1:11435`).

## Bez interfejsu (CLI)

```bash
python main.py wyklad.mp4
```

| Opcja | Znaczenie |
|-------|-----------|
| `--workdir DIR` | katalog wyników (domyślnie `work/<nazwa_filmu>`) |
| `--profile auto\|8gb\|16gb` | wybór modelu LLM i `num_ctx` (domyślnie wykrywany z VRAM) |
| `--from-stage asr\|frames\|vlm\|llm` | zacznij od wskazanego etapu |
| `--only-stage ...` | uruchom tylko jeden etap |
| `--force` | wymuś ponowne wykonanie etapów |
| `--status` | wypisz stan etapów i postęp jako JSON (nic nie uruchamia) |

Ctrl+C przerywa pracę bezpiecznie (model zostaje rozładowany); ponowne uruchomienie wznawia. Brak ścieżki audio w pliku: ASR jest pomijany, a notatki powstają tylko z tablicy.

## Logi i diagnostyka błędów

- **W aplikacji:** przy błędzie przetwarzania film pokazuje przyczynę (np. brakujący model, brak bibliotek CUDA) wraz z wskazówką naprawy oraz przycisk „Szczegóły błędu i logi” (stderr procesu etapu, `pipeline.log`, wyjście zadania). Link „Logi” w nagłówku pokazuje log serwera.
- **Pliki** (w `work/`): `server.log` (serwer i kolejka), a w `work/<film>/`: `pipeline.log` (przebieg etapów), `worker_<etap>.log` (stderr procesu etapu, w tym awarie bibliotek natywnych bez tracebacku Pythona), `job.out`, `worker_error.json`.
- Wznawianie: etapy VLM i LLM po awarii zachowują checkpointy (opisy klatek, gotowe fragmenty notatek), więc ponowne uruchomienie nie zaczyna od zera, o ile konfiguracja i wejście się nie zmieniły.

## Konfiguracja

Progi i modele są w `config.yaml`. Najczęściej trzeba dostroić sekcję `frames`, jeśli wybiera za dużo lub za mało klatek (np. `ink_level`, `erase_fraction`, `save_new_ink_px`, `max_hold_s`).

## Testy

```bash
pytest -q tests
```

Nie wymagają GPU ani Ollamy (używają atrap), działają także na macOS.

## Status

Sprawdzone na prawdziwym nagraniu: wykrywanie tablicy (Etap 2), kolejka, interfejs i czat (w tym anulowanie na prawdziwej Ollamie). **Niesprawdzone na docelowym sprzęcie:** ASR, VLM i LLM na GPU, `start.bat`, service worker/instalacja PWA, druk do PDF. Jakość odczytu pisma odręcznego i polskich notatek zależy od modeli. Plan i znane ryzyka: `PLAN_IMPLEMENTACJI.md`, `PLAN_UI.md`.
