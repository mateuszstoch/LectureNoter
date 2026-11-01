# Specyfikacja Implementacyjna: Lokalny Generator Notatek Wideo (Pipeline Decoupling)

## 1. Kontekst Projektu i Ograniczenia Sprzętowe
Masz za zadanie zaimplementować lokalny system do automatycznej ekstrakcji notatek z wykładów wideo (ok. 1.5h długości). 
*   **Sprzęt docelowy:** NVIDIA GeForce RTX 4060 Ti (8 GB lub 16 GB VRAM) + 32 GB RAM.
*   **Problem:** Uruchomienie modelu ASR, VLM i LLM jednocześnie spowoduje błąd OOM (Out Of Memory).
*   **Wymagana Architektura:** *Pipeline Decoupling* – ścisłe przetwarzanie sekwencyjne. W danym momencie w pamięci VRAM może znajdować się **tylko jeden model**. Po zakończeniu etapu, pamięć VRAM musi zostać całkowicie zwolniona przed załadowaniem kolejnego modelu.

## 2. Stos Technologiczny (Hybryda Python + Rust)
*   **Język główny (Orkiestracja i AI):** Python 3.11+.
*   **Język pomocniczy (Wydajność CPU):** Rust (z wykorzystaniem `PyO3` / `maturin` do kompilacji modułu dla Pythona).
*   **Inference API:** Ollama (lokalnie) dla VLM i LLM.
*   **Transkrypcja:** `faster-whisper` (Python).
*   **Przetwarzanie wideo:** `OpenCV` lub `ffmpeg` (pod spodem Rusta).

---

## 3. Szczegółowe Etapy Implementacji (Zadania dla Agenta)

### Etap 0: Przygotowanie środowiska i Orkiestrator (Python)
1. Utwórz strukturę projektu wykorzystującą `maturin` (narzędzie do budowania rozszerzeń Rust dla Pythona).
2. Napisz klasę `PipelineOrchestrator` w Pythonie, która będzie zarządzać przepływem danych między etapami.
3. **Krytyczne:** Do komunikacji z Ollama używaj REST API z parametrem `"keep_alive": 0`. Zmusza to serwer Ollama do natychmiastowego usunięcia modelu z VRAM po zakończeniu generowania odpowiedzi.

### Etap 1: Podsystem ASR (Transkrypcja) - Python
**Zadanie:** Ekstrakcja audio z wideo i transkrypcja.
1. Użyj `ffmpeg-python`, aby wyciągnąć ścieżkę audio do pliku `.wav` (16kHz, mono).
2. Wykorzystaj bibliotekę `faster-whisper`.
   *   Model: `large-v3-turbo`
   *   Kwantyzacja: `compute_type="int8"`
3. Wygeneruj transkrypcję ze znacznikami czasu.
4. Zapisz wynik jako `transcript.json` (format: `[{"start": 0.0, "end": 5.0, "text": "..."}]`).
5. **Zarządzanie VRAM:** Po wykonaniu transkrypcji, wywołaj `del model` oraz `torch.cuda.empty_cache()`, uruchom `gc.collect()`, aby upewnić się, że model zniknął z VRAM.

### Etap 2: Podsystem Ekstrakcji Slajdów - Moduł RUST (Wydajność)
**Zadanie:** Przetworzenie wideo, detekcja klatek kluczowych, odrzucenie zamazanych (blur) i duplikatów.
1. Zaimplementuj kod w **Rust**, który zostanie skompilowany jako biblioteka Pythona (np. `import video_processor`).
2. **Logika Rusta:**
   *   Zdekoduj wideo (możesz użyć bindowań OpenCV w Rust lub strumieniować klatki z ffmpeg).
   *   Próbkuj klatkę co X sekund (np. co 5s).
   *   Oblicz **wariancję operatora Laplace'a** (Laplacian variance), aby ocenić ostrość. Odrzuć klatki poniżej zdefiniowanego progu.
   *   Porównaj obecną (ostrą) klatkę z poprzednią zapisaną klatką przy użyciu miary podobieństwa strukturalnego (SSIM) lub błędu średniokwadratowego (MSE). Jeśli klatki są niemal identyczne, odrzuć (brak zmiany slajdu na wykładzie).
3. Zapisz unikalne, ostre klatki do folderu `frames/` jako `.jpg`.
4. Wygeneruj plik `frames_metadata.json` zawierający nazwy plików i odpowiadające im znaczniki czasu (timestamp) z wideo.

### Etap 3: Podsystem Wizyjny (VLM) - Python + Ollama API
**Zadanie:** Analiza wyodrębnionych klatek z użyciem modelu Vision.
1. Sprawdź, czy w VRAM nie ma innych modeli.
2. Przygotuj skrypt ładujący po kolei obrazy z folderu `frames/`.
3. Dla każdego obrazu wyślij żądanie do lokalnego serwera Ollama.
   *   Model: `qwen2-vl:7b`
   *   Prompt: *"Extract all text, mathematical formulas, and code from this slide. Describe any charts or diagrams concisely."*
   *   Dodaj obraz zakodowany w Base64.
4. Zagreguj wyniki (timestamp klatki + opis tekstowy z VLM) i zapisz do `vision_context.json`.
5. **Zarządzanie VRAM:** Upewnij się, że użyto `"keep_alive": 0` lub manualnie zresetuj serwer Ollama, aby zwolnić 6 GB VRAM zajmowane przez `qwen2-vl`.

### Etap 4: Podsystem Syntezy Tekstu (LLM) - Python + Ollama API
**Zadanie:** Fuzja danych i generowanie finalnego dokumentu Markdown.
1. Zbierz tekst z `transcript.json` i połącz go chronologicznie z opisami z `vision_context.json`.
   *   *Przykład fuzji w kodzie:* "[00:15:20] [Mowa]: Zatem to równanie opisuje nasz problem... [00:15:22] [Slajd na ekranie]: Wzór: E = mc^2."
2. Skonfiguruj żądanie do Ollama:
   *   Model: `qwen2.5:7b-instruct` (dla karty 8GB) lub `qwen2.5:14b-instruct` (dla 16GB).
   *   Opcje: `"num_ctx": 32768` (Kluczowe! Wymagane obsłużenie długiego kontekstu 32k tokenów).
3. **System Prompt dla LLM:** 
   *"Jesteś asystentem akademickim. Otrzymujesz połączoną transkrypcję audio i opisy slajdów (OCR) z wykładu. Twoim zadaniem jest stworzenie kompleksowej, zorganizowanej notatki w formacie Markdown. Używaj nagłówków H1/H2/H3, list punktowych. Wszelkie wzory matematyczne formatuj w bloku LaTeX (np. `$$ E = mc^2 $$`). Zignoruj błędy wymowy z transkrypcji, wyciągnij sens. Gdzie to użyteczne, dodawaj znaczniki czasu."*
4. Zapisz odpowiedź do pliku `lecture_notes.md`.

## 4. Oczekiwana struktura projektu do wygenerowania przez Agenta
```text
project_root/
├── Cargo.toml               # Konfiguracja Rust (PyO3)
├── src/
│   └── lib.rs               # Moduł analizy wideo w Rust (wariancja Laplace'a, SSIM)
├── requirements.txt         # Zależności Python (faster-whisper, requests, itp.)
├── main.py                  # Główny orkiestrator Pipeline
├── asr_module.py            # Klasa integracji z faster-whisper
├── vlm_module.py            # Klasa komunikacji z Ollama (Qwen2-VL)
└── llm_synthesis.py         # Klasa komunikacji z Ollama (Qwen2.5) i fuzji danych
```

## 5. Wytyczne dot. obsługi błędów
*   Zaimplementuj mechanizm "resume" (wznawiania). Jeśli proces wywali się na Etapie 3, skrypt powinien móc pominąć Etapy 1 i 2 na podstawie istnienia plików `.json`.
*   Dodaj dokładne logowanie (np. biblioteką `loguru` w Pythonie), monitorujące czas wykonania każdego etapu.