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
- Python 3.11+, `ffmpeg`, [Ollama](https://ollama.com)
- opcjonalnie `pandoc` (eksport notatek do Worda)

## Instalacja (Linux lub WSL2 / Ubuntu)

```bash
git clone https://github.com/mateuszstoch/LectureNoter.git ~/LectureNoter
cd ~/LectureNoter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg pandoc

ollama pull qwen2.5vl:7b
ollama pull qwen2.5:7b-instruct    # karta 8 GB
ollama pull qwen2.5:14b-instruct   # karta 16 GB (opcjonalnie)
```

Projekt i nagrania trzymaj w systemie plików WSL (np. `~/LectureNoter`), nie w `/mnt/c/...`, bo tam odczyt jest dużo wolniejszy. Nagrania wrzucaj do `~/LectureNoter/videos/` (dowiązania symboliczne działają).

## Uruchomienie

### Windows 11 (WSL2)

Użyj skryptu `scripts\start.bat`: uruchamia serwer w WSL (jeśli jeszcze nie działa) i otwiera aplikację w oknie Edge/Chrome bez paska przeglądarki. Skopiuj go na Pulpit lub przypnij do paska zadań.

- Projekt w innym miejscu niż `~/LectureNoter`: ustaw zmienną środowiskową `LECTURENOTER_DIR`.
- Aplikację można też zainstalować z przeglądarki (Edge: menu → Aplikacje → „Zainstaluj tę witrynę jako aplikację”). Okno zainstalowanej aplikacji nie uruchamia serwera: najpierw musi działać `start.bat` (albo ręcznie jak niżej).

Ręcznie: w terminalu WSL

```bash
cd ~/LectureNoter && source .venv/bin/activate && python server.py
```

a w przeglądarce Windowsa otwórz `http://localhost:8000`.

### Linux

```bash
source .venv/bin/activate
python server.py        # http://127.0.0.1:8000 (tylko lokalnie)
```

### Wskazówki dla WSL2

- **Ollama zainstaluj w WSL** i nie uruchamiaj jej równolegle w Windowsie (dwa serwery mogą trzymać modele w VRAM jednocześnie). Bez systemd uruchom ją ręcznie: `ollama serve`.
- W `%UserProfile%\.wslconfig` możesz zwiększyć pamięć: `[wsl2]` i `memory=24GB` (domyślnie WSL dostaje ok. 50% RAM). Po zmianie: `wsl --shutdown`.
- Windows zajmuje ok. 0,5–1 GB VRAM. Na karcie 8 GB zamknij przeglądarkę i inne programy używające GPU na czas przetwarzania.
- Błąd `libcublas.so.12 is not found`: `pip install -r requirements.txt` ponownie w aktywnym `.venv`.

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

## Konfiguracja

Progi i modele są w `config.yaml`. Najczęściej trzeba dostroić sekcję `frames`, jeśli wybiera za dużo lub za mało klatek (np. `ink_level`, `erase_fraction`, `save_new_ink_px`, `max_hold_s`).

## Testy

```bash
pytest -q tests
```

Nie wymagają GPU ani Ollamy (używają atrap), działają także na macOS.

## Status

Sprawdzone na prawdziwym nagraniu: wykrywanie tablicy (Etap 2), kolejka, interfejs i czat (w tym anulowanie na prawdziwej Ollamie). **Niesprawdzone na docelowym sprzęcie:** ASR, VLM i LLM na GPU, `start.bat`, service worker/instalacja PWA, druk do PDF. Jakość odczytu pisma odręcznego i polskich notatek zależy od modeli. Plan i znane ryzyka: `PLAN_IMPLEMENTACJI.md`, `PLAN_UI.md`.
