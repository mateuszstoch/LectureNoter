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
- sterownik NVIDIA, CUDA 12 i cuDNN 9 (dla faster-whisper)
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

Brak ścieżki audio w pliku: ASR jest pomijany, a notatki powstają tylko z tablicy.

## Konfiguracja

Wszystkie progi i modele są w `config.yaml`. Najczęściej trzeba dostroić sekcję `frames`, jeśli wybiera za dużo lub za mało klatek (np. `ink_level`, `erase_fraction`, `save_new_ink_px`, `max_hold_s`).

## Testy

```bash
pytest -q tests
```

Testy nie wymagają GPU ani Ollamy (korzystają z atrap), działają także na macOS.

## Status

Etap 2 sprawdzony na prawdziwym nagraniu. ASR, VLM i LLM zweryfikowane tylko testami z atrapami, nie na prawdziwych modelach. Szczegóły i znane ryzyka: `PLAN_IMPLEMENTACJI.md`.
