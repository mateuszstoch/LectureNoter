# Plan implementacji: LectureNoter (lokalny generator notatek z wykładów wideo)

Źródło: `instrukcja_implementacji_dla_agenta.md`. Plan zawiera **korekty** względem specyfikacji tam, gdzie spec jest technicznie wadliwy (oznaczone **[ZMIANA]**). Pełna lista uwag jest w sekcji 11.

**Decyzje właściciela (rewizja 2):** Linux, wszystkie wykłady po polsku, nagrania to głównie same slajdy lub ekran w stylu Painta (rysowanie/pisanie), **całość w czystym Pythonie, bez Rusta**.

---

## 0. Założenia

| # | Założenie | Wartość |
|---|-----------|---------|
| A1 | System docelowy | **Linux** z GPU NVIDIA (CUDA 12, cuDNN 9), np. Ubuntu 22.04/24.04. Rozwój na macOS możliwy tylko dla części CPU i testów z mockami |
| A2 | Język wykładów | **polski** (`language: pl` na sztywno w konfiguracji, bez autodetekcji) |
| A3 | Język notatek | polski |
| A4 | Typ wideo | **same slajdy lub ekran z rysowaniem (styl Painta)**, bez kamery z prelegentem |
| A7 | Stos | **czysty Python 3.11+** (OpenCV + NumPy do analizy klatek) |
| A5 | VRAM | autodetekcja (`nvidia-smi`); profile `8gb` / `16gb` |
| A6 | Jeden film = jeden katalog roboczy `work/<nazwa_filmu>/` | tak |

---

## 1. Architektura

```
video.mp4
   │
   ├─► [Etap 1] ASR  (GPU, faster-whisper)  ─► transcript.json
   ├─► [Etap 2] Slajdy (CPU, Python+OpenCV)  ─► frames/*.jpg + frames_metadata.json
   │                  (Etap 2 nie używa VRAM, więc może iść równolegle z Etapem 1)
   ├─► [Etap 3] VLM  (GPU, Ollama)           ─► vision_context.jsonl → vision_context.json
   └─► [Etap 4] LLM  (GPU, Ollama)           ─► lecture_notes.md
```

Zasady:
- W danym momencie w VRAM jest najwyżej jeden model. Etapy GPU (1, 3, 4) są ściśle sekwencyjne.
- **[ZMIANA]** Każdy etap GPU uruchamiany jest w **osobnym podprocesie** (`python -m lecturenoter.stages.asr ...`). Zwolnienie VRAM wynika wtedy z zakończenia procesu, a nie z `del model`. Gwarantuje to pełne zwolnienie (kontekst CUDA, cache alokatora).
- Komunikacja między etapami odbywa się wyłącznie przez pliki w katalogu roboczym (to jednocześnie daje mechanizm resume).
- Wszystkie zapisy plików są atomowe (zapis do `*.tmp` + `os.replace`).

### Struktura projektu (rozszerzona względem specyfikacji)

```
LectureNoter/
├── requirements.txt            # przypięte wersje (pip freeze po udanej instalacji)
├── config.yaml                 # progi, modele, profile VRAM
├── main.py                     # CLI + PipelineOrchestrator
├── asr_module.py               # faster-whisper
├── frames_module.py            # ekstrakcja slajdów (ffmpeg pipe + OpenCV)
├── vlm_module.py               # Ollama VLM
├── llm_synthesis.py            # fuzja + map-reduce + Ollama LLM
├── ollama_client.py            # wspólny klient REST (/api/generate, /api/ps, unload)
├── vram_guard.py               # sprawdzanie/oczekiwanie na pusty VRAM (nvidia-smi + /api/ps)
├── io_utils.py                 # atomowy zapis JSON, hash konfiguracji, znaczniki czasu
├── tests/
│   ├── test_frames.py          # syntetyczne wideo: ostre/rozmyte/duplikaty/dopisywanie
│   ├── test_fusion.py          # łączenie transkryptu i slajdów, chunkowanie
│   ├── test_resume.py          # wznawianie, nieważny cache
│   └── test_ollama_client.py   # mock serwera
└── README.md
```

---

## 2. Etap 0 — Środowisko i szkielet (≈ 0,5 dnia)

1. Zainstalować na maszynie docelowej (Linux): Python 3.11+ z `venv`, `ffmpeg` (apt), sterownik NVIDIA, CUDA 12 + cuDNN 9 (wymagane przez CTranslate2/faster-whisper), Ollama (systemd). Sprawdzić `nvidia-smi` i `ollama --version`.
2. Utworzyć `venv`, bez Rusta i bez maturina.
3. `requirements.txt`: `faster-whisper`, `requests`, `loguru`, `pyyaml`, `pytest`, `numpy`, `opencv-python-headless`, `nvidia-ml-py` (zamiast parsowania `nvidia-smi`). **[ZMIANA]** Bez `ffmpeg-python` (porzucona biblioteka, `subprocess` jest prostszy) i bez `torch` (faster-whisper go nie potrzebuje).
4. `config.yaml`:
   ```yaml
   language: pl
   profiles:
     8gb:  { llm_model: "qwen2.5:7b-instruct",  num_ctx: 16384 }
     16gb: { llm_model: "qwen2.5:14b-instruct", num_ctx: 24576 }
   asr:    { model: large-v3-turbo, compute_type: int8_float16, vad_filter: true, beam_size: 5 }
   frames: { sample_every_s: 2, blur_threshold: 15.0, change_threshold: 0.02, stable_samples: 2, analysis_width: 1120, auto_crop_board: true, min_board_area: 0.05, max_hold_s: 120 }
   vlm:    { model: "qwen2.5vl:7b", num_predict: 800, keep_alive: "10m" }
   llm:    { chunk_minutes: 10, num_predict: 3000, temperature: 0.2 }
   ```
   Wartości progów to punkt startowy. Muszą zostać dostrojone na realnym nagraniu (zadanie 6.3).
5. `vram_guard.py`: funkcja `assert_vram_free(max_used_mb)` używająca NVML. Odpytuje `GET /api/ps` Ollamy i, jeśli są załadowane modele, wywołuje `POST /api/generate {"model":..., "keep_alive":0}` (to ściśle wymusza unload). Czeka do ok. 30 s, aż zużycie VRAM spadnie poniżej progu (uwzględnia ~0,5–1 GB zajęte przez system i przeglądarkę).
6. `PipelineOrchestrator` (w `main.py`):
   - CLI: `python main.py video.mp4 [--workdir work/] [--language pl] [--profile auto|8gb|16gb] [--from-stage N] [--force]`.
   - Dla każdego etapu: sprawdź, czy wyjście istnieje i czy `stage_state.json` zawiera zgodny hash (konfiguracja + sygnatura wideo: rozmiar i mtime). Jeśli tak, pomiń. Inaczej uruchom, zmierz czas (loguru) i zapisz stan.
   - Logowanie do konsoli i `pipeline.log`, czas każdego etapu, szczytowe zużycie VRAM (NVML).

**Kryterium ukończenia:** `pip install -r requirements.txt` przechodzi, `python main.py --help` działa, `vram_guard` poprawnie raportuje VRAM, `import cv2` działa.

---

## 3. Etap 1 — ASR (≈ 0,5 dnia)

`asr_module.py`:
1. Ekstrakcja audio: `ffmpeg -i video -vn -ac 1 -ar 16000 -c:a pcm_s16le audio.wav` przez `subprocess` (z `-nostdin`, kod wyjścia sprawdzany).
2. `WhisperModel("large-v3-turbo", device="cuda", compute_type="int8_float16")`.
   **[ZMIANA]** `int8_float16` na GPU zamiast czystego `int8`. Czysty `int8` na karcie CUDA jest zwykle wolniejszy i nie daje korzyści jakościowej.
3. `model.transcribe(wav, language=cfg.language, vad_filter=True, beam_size=5, condition_on_previous_text=False, word_timestamps=False)`. `condition_on_previous_text=False` ogranicza halucynacje i pętle powtórzeń w długich nagraniach. VAD tnie ciszę.
4. Iteracja po generatorze segmentów, zapis do listy `[{"start","end","text"}]`. Pasek postępu w logu co ~60 s materiału.
5. Zapis `transcript.json` (UTF-8, `ensure_ascii=False`) atomowo.
6. Zwolnienie VRAM przez zakończenie podprocesu. W samym kodzie `del model; gc.collect()` jako dodatkowa asekuracja (bez `torch.cuda.empty_cache()`, bo torcha nie ma w stosie).
7. Filtr post-processingowy: usunąć segmenty puste i znane halucynacje Whispera („Dziękuję za uwagę”, „Napisy stworzone przez…”) pojawiające się w ciszy. Lista konfigurowalna.

**Test:** 2-minutowy klip z mową daje poprawny JSON, a `nvidia-smi` po zakończeniu pokazuje VRAM bez śladu modelu.

---

## 4. Etap 2 — Ekstrakcja slajdów, Python + OpenCV (≈ 1,5 dnia)

### 4.1 Decyzja architektoniczna
**[ZMIANA]** Zamiast Rusta: `frames_module.py`. Python uruchamia `ffmpeg` jako podproces i czyta surowe klatki z pipe'a, a analizę robi w NumPy/OpenCV. Wąskim gardłem jest dekodowanie (ffmpeg), więc Rust nie dałby zysku. Dekodowanie na CPU (bez NVDEC), więc VRAM nie jest ruszany.

### 4.2 Specyfika nagrań: slajdy i rysowanie w Paincie
Takie nagrania różnią się od klasycznych slajdów i spec ich nie uwzględnia:
- Treść **narasta stopniowo** (dopisywane linie, wzory). Klatka zrobiona w środku pisania daje niepełny obraz, a „zmiana slajdu” zdarza się dopiero po wymazaniu lub przejściu na nową planszę.
- Obraz jest rzadki (biała plansza, cienkie linie), więc **wariancja Laplasjanu jest niska** także dla ostrych klatek. Klasyczny próg 50–100 odrzuciłby wszystko. Próg jest tu dużo niższy i traktowany tylko jako filtr przeciw faktycznie rozmytym/zakodowanym klatkom.
- Małe zmiany (nowy wyraz) mają **niski SSIM-owy sygnał**, ale są istotne. Globalny SSIM ≥ 0,97 uznałby je za duplikat.
- Kursor myszy i pasek narzędzi powodują drobne, nieistotne zmiany.

### 4.3 Algorytm (strategia „zrzut stanu przed zmianą”)
1. `ffprobe` → szerokość, wysokość, czas trwania.
2. `ffmpeg -nostdin -i video -vf fps=1/X -f rawvideo -pix_fmt bgr24 -` z `X = sample_every_s` (domyślnie 2 s; przy rysowaniu gęstsze próbkowanie jest tanie). Odczyt klatek o stałym rozmiarze; timestamp = `index * X`.
3. Dla każdej klatki: skala szarości, zmniejszenie do `analysis_width`, lekki blur Gaussa (usuwa szum kompresji i kursor).
4. **Miara zmiany:** odsetek pikseli różniących się o więcej niż próg (np. |Δ| > 25) względem poprzedniej *próbki* oraz względem ostatniej *zapisanej* klatki. SSIM (`cv2` + własna implementacja lub `scikit-image`) jako opcjonalna miara dokładniejsza dla pełnych slajdów.
5. **Maszyna stanów:**
   - `STABLE`: obraz zmienia się mało względem poprzedniej próbki. Zapamiętaj go jako kandydata.
   - `CHANGING`: duża zmiana względem poprzedniej próbki (przejście slajdu, wymazanie planszy). **Wtedy zapisz ostatniego stabilnego kandydata** (pełny stan sprzed zmiany), jeśli różni się od ostatnio zapisanej klatki ponad `change_threshold`.
   - Na końcu filmu zapisz ostatniego kandydata.
   - Klatka zostaje kandydatem dopiero po `stable_samples` kolejnych stabilnych próbkach (omija środek animacji).
6. **Ostrość:** wariancja Laplasjanu liczona tylko dla kandydata; odrzucić, gdy < `blur_threshold` (domyślnie niski, ok. 15, do dostrojenia na realnych nagraniach).
7. Dodatkowo „pośrednie” zrzuty dla długo rosnącej planszy: jeśli stan stabilny trwa dłużej niż `max_hold_s` (np. 120 s) i różni się od ostatnio zapisanej, zapisz go (ogranicza utratę treści, gdy prowadzący pisze bez kasowania).
8. Zapis pełnej rozdzielczości jako `frames/frame_000123_t00615.jpg`; `frames_metadata.json`: `[{"file","timestamp","index"}]`, gdzie `timestamp` to czas zapisanej klatki.
9. Limit liczby klatek (np. 300) z ostrzeżeniem w logu.

### 4.4 Parametry do strojenia (faza 6)
`sample_every_s`, `blur_threshold`, `change_threshold`, pixel-diff threshold, `stable_samples`, `max_hold_s`, opcjonalny `--crop x,y,w,h` (wycięcie paska narzędzi Painta). Jedno narzędzie pomocnicze `tools/preview_frames.py` generuje kontaktową planszę wybranych klatek do szybkiej oceny progów.

### 4.5 Testy
- Syntetyczne wideo (ffmpeg `color`/`drawtext` + `boxblur`): 3 slajdy po 20 s, 1 rozmyty fragment, 1 duplikat, 1 plansza z dopisywaniem linii i następnym wymazaniem. Oczekiwane: po jednej klatce na końcowy stan każdej planszy (z pełną treścią), brak duplikatów.
- Test regresji progów na krótkim, realnym klipie.

### 4.6 Wnioski z próbki (`Screen Recording 2026-11-01 at 12.41.52.mov`)
Parametry: 3360×2100, 60 fps, 111 s, H.264, 35 MB, **brak ścieżki audio**. Zawartość: nagranie ekranu z odtwarzanego nagrania spotkania online. U góry pasek uczestników (miniatury, zmieniające się nazwiska i licznik „+91”), pośrodku biała tablica z odręcznym pismem na szarym tle, czasem nakładka odtwarzacza (pasek postępu, przyciski) i paski narzędzi. Tablica zmienia rozmiar (pełna plansza z agendą, potem mały biały prostokąt „Cel przedmiotu?” w lewym górnym rogu szarego obszaru), a treść dopisywana jest po jednym słowie.

Konsekwencje i zmiany w planie:
1. **Automatyczne wykrywanie obszaru tablicy (obowiązkowe).** Pasek uczestników zmienia się cały czas (mówiący, nazwiska, licznik), więc globalna miara zmiany wykrywałaby fałszywe zmiany co kilka sekund. Plan: w każdej próbce znaleźć największy prostokąt „prawie białych” pikseli (próg jasności, morfologia, `cv2.boundingRect` największego konturu) i liczyć wszystko (zmianę, ostrość, zapis) **tylko w jego obrębie**. Dodatkowo odrzucać próbki, w których prostokąt jest mniejszy niż np. 5% kadru (brak tablicy, np. ekran udostępniania nie jest aktywny). Ręczny `--crop` zostaje jako nadpisanie.
2. **Przycięta klatka idzie do VLM** (nie pełny ekran), co maksymalnie zwiększa rozdzielczość pisma. Przy 3360 px szerokości tablica ma kilkaset do ponad tysiąca pikseli szerokości.
3. **Nakładka odtwarzacza** (pasek postępu, przyciski) może wejść w obszar tablicy (widoczna na pierwszej klatce, przysłania dolną część). Próbki, w których dół prostokąta jest przyciemniony/przysłonięty (gradient + kontrolki), traktować jako niestabilne i czekać na następną; reguła `stable_samples` to załatwia, o ile nakładka znika.
4. **Zmiana rozmiaru tablicy ≠ zmiana treści.** Porównywać po normalizacji: przyciąć do bbox, przeskalować do stałej szerokości i porównywać z ostatnim zapisanym stanem tylko wtedy, gdy bbox jest podobny; duża zmiana bbox wymusza zapis ostatniego stabilnego stanu (nowa plansza).
5. **Wydajność przy 3360×2100 @ 60 fps.** Dwuprzebiegowo: (a) ffmpeg z `-vf fps=1/X,scale=1120:-1` do pipe'a (analiza na małych klatkach, mały narzut), (b) dla wybranych timestampów wyciągnąć pełną rozdzielczość `ffmpeg -ss T -i video -frames:v 1` i przyciąć do przeskalowanego bbox. Unika to przesyłania ~21 MB/klatkę przez pipe.
6. **Brak audio w tej próbce, ale prawdziwe nagrania mają audio (potwierdzone przez właściciela).** Próbka to nagranie ekranu bez dźwięku systemowego, więc nie sprawdzi Etapu 1. Mowa jest głównym źródłem treści, a tablica uzupełnieniem (struktura, wzory, nazwy własne). Zabezpieczenie zostaje: orkiestrator wykrywa brak strumienia audio (`ffprobe`), pomija Etap 1 z ostrzeżeniem, a Etap 4 przechodzi w tryb „tylko tablica”. Do sprawdzenia przy realnych plikach: czy dźwięk jest w tym samym pliku, w jakim kodeku/kanałach i czy nie jest rozsynchronizowany względem obrazu (przy nagraniach ekranu bywa przesunięcie).
7. **Postępujące dopisywanie** (jedno słowo co kilkanaście sekund) oznacza, że dla długich fragmentów powstanie wiele podobnych klatek. Rozwiązanie: dla VLM wysyłać tylko klatki zapisane po regule „zrzut przed zmianą/wymazaniem” oraz co `max_hold_s`; opis późniejszej klatki zawiera wcześniejszą treść, więc w fuzji zachować **tylko najpełniejszy stan** każdej planszy, a nie wszystkie pośrednie.
8. **Pismo odręczne po polsku**, w tym skróty i symbole (np. „TOiZO”, „UPeL”). Model VLM 7B może je czytać z błędami; część odczytów będzie wymagała weryfikacji. Wynik testu na tej próbce jest bezpośrednim sprawdzianem jakości (zadanie 6.3, patrz niżej).
9. **Próbka jest za krótka i bez dźwięku** do oceny ASR, map-reduce i czasów; służy do strojenia Etapu 2 i oceny odczytu pisma w Etapie 3. Do pełnej weryfikacji potrzebny jest jeden realny, pełnowymiarowy wykład z dźwiękiem (najlepiej 10–20 min fragment, a potem całość).
10. **Fuzja w Etapie 4** przy realnych nagraniach: wykładowca mówi o treści, którą dopiero dopisze (lub już skreślił), więc znaczniki tablicy przesuwać z oknem tolerancji ±15–30 s względem mowy, a nie dopasowywać co do sekundy.

---

## 5. Etap 3 — VLM przez Ollama (≈ 1 dzień)

`ollama_client.py` + `vlm_module.py`:
1. `assert_vram_free()` (sekcja 2, pkt 5).
2. **[ZMIANA]** Model: **`qwen2.5vl:7b`** (Qwen2.5-VL), a nie `qwen2-vl:7b`. W oficjalnej bibliotece Ollamy dostępny jest Qwen2.5-VL; tag `qwen2-vl:7b` należy zweryfikować (`ollama list` / `ollama pull`), a gdy go nie ma, użyć Qwen2.5-VL. Model jest konfigurowalny.
3. **[ZMIANA]** `keep_alive`: **nie `0` w każdym żądaniu.** `keep_alive: 0` przy każdej klatce oznaczałoby przeładowanie 6 GB modelu setki razy. Plan: `keep_alive: "10m"` w trakcie etapu, a po ostatniej klatce **jawne rozładowanie** (`POST /api/generate {"model":..., "keep_alive":0}`) i weryfikacja przez `/api/ps` oraz NVML. Rozładowanie wykonywane również w `finally`.
4. Dla każdej klatki: `POST /api/generate` z `images:[base64]`, `stream:false`, `options:{temperature:0, num_predict:800}`. Obraz przeskalowany do maks. ~1344 px dłuższego boku (oszczędność tokenów wizyjnych).
5. Prompt (po angielsku, jak w spec) rozszerzony o: *„Respond in the language of the slide. Do not invent content. If the slide is blank or only a title, say so briefly.”*
6. Timeout i retry (3 próby, backoff). Błąd po wyczerpaniu prób zapisuje wpis `{"error":...}` i nie zatrzymuje całego etapu.
7. **Checkpoint:** każdy wynik dopisywany od razu do `vision_context.jsonl` (resume w połowie etapu). Po zakończeniu scalenie do `vision_context.json`: `[{"timestamp","file","text"}]`.
8. Post-processing: wykrywanie zapętlonych/zbyt długich odpowiedzi (powtórzenia), przycięcie.

**Test:** 5 klatek testowych, poprawne JSON, po zakończeniu `/api/ps` pusty.

---

## 6. Etap 4 — Synteza LLM (≈ 2 dni)

`llm_synthesis.py`:

### 6.1 Fuzja
1. Wczytać `transcript.json` i `vision_context.json`.
2. **Deduplikacja slajdów:** jeśli opis kolejnej klatki jest niemal identyczny z poprzednim (podobieństwo tekstu), połączyć w jeden „slajd” z zakresem czasu.
3. Zbudować chronologiczną oś: `[HH:MM:SS] [Mowa]: ...` oraz `[HH:MM:SS] [Slajd]: ...`. Segmenty mowy łączyć w akapity (~30–60 s), żeby nie marnować tokenów na znaczniki.
4. Znacznik slajdu cofnąć o `sample_every_s`, bo klatka mogła pojawić się do X sekund przed pierwszą próbką.

### 6.2 Chunking (map-reduce) **[ZMIANA]**
Transkrypt 1,5 h to w polskim ok. 13–18 tys. słów. W tokenizerze Qwena daje to realnie **ok. 30–50 tys. tokenów**, co przekracza 32k nawet bez opisów slajdów i bez miejsca na odpowiedź. Jedno żądanie nie zadziała.
1. Liczenie tokenów szacunkowo (znaki/3 dla polskiego) lub przez `/api/tokenize`, jeśli dostępne.
2. Podział osi na okna ≈ `chunk_minutes` (10 min), granice dopasowane do zmiany slajdu, z 30 s zakładki.
3. **Map:** dla każdego okna wygenerować notatkę Markdown (prompt z spec, plus: „to fragment 5/9 wykładu, nie pisz wstępu ani podsumowania”). Wynik zapisywany do `chunks/chunk_XX.md` (resume).
4. **Reduce:** (a) wygenerować spójny spis treści/plan z nagłówków chunków; (b) połączyć notatki, ujednolicić poziomy nagłówków, usunąć powtórzenia. Preferowane jest składanie deterministyczne w kodzie plus jedno krótkie wywołanie LLM na wstęp i streszczenie, żeby uniknąć ponownego przepuszczania całego tekstu przez ograniczony kontekst.
5. Zapis `lecture_notes.md`.

### 6.3 Parametry Ollamy
- `num_ctx`: **8 GB → 16384, 16 GB → 24576–32768** (patrz VRAM w sekcji 11 pkt 7). Ustawić `OLLAMA_FLASH_ATTENTION=1` i `OLLAMA_KV_CACHE_TYPE=q8_0` w środowisku serwera, jeśli brakuje VRAM.
- `num_predict` jawnie (domyślnie Ollama tnie odpowiedź), `temperature: 0.2`.
- Po etapie jawny unload (`keep_alive: 0`).
- Walidacja wyniku: LaTeX w blokach `$$`, brak urwanych bloków kodu, kontrola długości.

### 6.4 System prompt
Tekst ze specyfikacji, rozszerzony o: język wyjścia, zakaz dodawania faktów spoza materiału, preferencja dla treści ze slajdów przy wzorach (ASR błędnie rozpoznaje nazwy i symbole), format znaczników czasu `[HH:MM:SS]`.

**Test:** na przykładowym transkrypcie 30 min wynik ma poprawną hierarchię nagłówków i działające bloki LaTeX.

---

## 7. Obsługa błędów, resume, logowanie (≈ 0,5 dnia, przeplata się z etapami)

- `stage_state.json`: dla każdego etapu `{status, started, finished, config_hash, input_signature, outputs}`. Zmiana parametrów etapu unieważnia ten etap i następne.
- Pliki wyjściowe zapisywane atomowo. Samo „istnieje plik `.json`” (spec) nie wystarcza, bo plik może być częściowy.
- Checkpointy wewnątrz etapów: `vision_context.jsonl`, `chunks/*.md`.
- `loguru`: konsola + plik z rotacją; log czasu, liczby klatek, tokenów, szczytu VRAM.
- Flagi `--force`, `--from-stage`, `--only-stage`.
- Czytelne błędy: brak ffmpeg, brak Ollamy (`/api/tags` niedostępne), brak modelu (podpowiedź `ollama pull`), brak CUDA.

---

## 8. Harmonogram

| Faza | Zakres | Szac. |
|------|--------|-------|
| 0 | Środowisko, szkielet, orkiestrator, vram_guard | 0,5–1 d |
| 1 | ASR | 0,5 d |
| 2 | Python/OpenCV: ekstrakcja klatek + testy | 1,5 d |
| 3 | VLM | 1 d |
| 4 | Fuzja + map-reduce + LLM | 2 d |
| 5 | Resume, logi, CLI, README | 0,5 d |
| 6 | Strojenie progów i jakości na 2–3 realnych wykładach | 1 d |
| | **Razem** | **ok. 6–7 dni roboczych** |

Kolejność zalecana: 0 → 2 (największe ryzyko) → 1 → 3 → 4 → 5 → 6. Etapy 1 i 2 są niezależne.

---

## 9. Szacunkowy czas działania (RTX 4060 Ti, wykład 1,5 h)

| Etap | Czas |
|------|------|
| ASR (large-v3-turbo) | ok. 3–8 min |
| Ekstrakcja klatek (Python + ffmpeg) | ok. 2–5 min |
| VLM (~60–150 klatek × 5–15 s) | ok. 10–30 min |
| LLM map-reduce (9–10 chunków) | ok. 10–30 min |
| **Razem** | **ok. 25–70 min** |

Są to szacunki, nie pomiary. Zweryfikować na sprzęcie w fazie 6.

---

## 10. Kryteria akceptacji

1. `python main.py wyklad.mp4` produkuje `lecture_notes.md` bez OOM na profilu 8 GB.
2. W żadnym momencie NVML nie pokazuje dwóch załadowanych modeli naraz (logowane).
3. Po zabiciu procesu w trakcie Etapu 3 ponowne uruchomienie pomija Etapy 1–2 i kontynuuje od ostatniej przetworzonej klatki.
4. Test syntetyczny Etapu 2 zwraca dokładnie oczekiwane klatki.
5. Notatki zawierają wzory w `$$…$$`, hierarchię nagłówków i znaczniki czasu, a ich treść jest zgodna ze slajdami w ręcznym przeglądzie 3 wykładów.

---

## 11. Wady, niejasności i ryzyka specyfikacji

**Krytyczne (zmieniają działanie systemu)**
1. **`keep_alive: 0` w każdym żądaniu VLM** powoduje przeładowanie modelu przy każdej klatce (dziesiątki sekund narzutu × setki klatek). Właściwe: trzymać model podczas etapu i rozładować na końcu.
2. **Kontekst 32k nie wystarczy** na 1,5 h polskiej transkrypcji plus opisy slajdów, a do tego potrzebne jest miejsce na odpowiedź. Konieczny map-reduce. Pojedyncza odpowiedź LLM nie wygeneruje też „kompleksowej” notatki z 90 minut.
3. **Nazwa modelu `qwen2-vl:7b`** prawdopodobnie nie istnieje w oficjalnej bibliotece Ollamy. Dostępny jest `qwen2.5vl:7b`. Trzeba zweryfikować.
4. **Platforma (rozstrzygnięte: Linux).** Środowisko pracy to macOS, a cel to Linux + RTX, więc etapów GPU nie da się przetestować na Macu. Spec nie podawał systemu docelowego.
5. **`torch.cuda.empty_cache()`** nie ma sensu: faster-whisper opiera się na CTranslate2, nie na PyTorch, a torch nie jest w wymaganiach. Zwolnienie VRAM najpewniej zapewnia osobny proces.

**Istotne**
6. **Rust (rozstrzygnięte: usunięty).** Był nadmiarowy, bo wąskim gardłem jest dekodowanie wideo. Etap 2 jest w Pythonie z OpenCV.
7. **Niejednoznaczne „8 lub 16 GB”:** parametry (model 7B/14B, num_ctx) muszą zależeć od profilu. Samo KV cache dla 14B przy 32k to ok. 6 GB przy wagach ok. 9 GB. Na 16 GB jest to bardzo ciasne, więc plan obniża `num_ctx`. Karta 8 GB z modelem VLM 7B (~6 GB) ma też mały zapas, bo system i przeglądarka zajmują ok. 0,5–1 GB.
8. **(Dla rysowania w Paincie jeszcze ważniejsze, patrz 4.2.)** **Wariancja Laplasjanu jako filtr rozmycia** zawodzi na slajdach z małą ilością treści (biała ściana z tytułem ma niską wariancję i zostanie odrzucona jako „rozmyta”). Próg zależy od rozdzielczości i kompresji, więc trzeba go stroić per nagranie, a spec go nie podaje.
9. **SSIM względem ostatniej klatki** (zastąpione maszyną stanów „zrzut przed zmianą”, patrz 4.3) zawodzi, gdy w kadrze jest prelegent, kursor, pasek zadań albo animacje. Wtedy każda klatka „zmienia się” i powstaje ich za dużo. Spec nie zakłada niczego o układzie nagrania. Zdarza się też odwrotnie: drobna zmiana na slajdzie (nowy punkt listy, podświetlenie) przekracza próg SSIM słabo i ginie.
10. **Próbkowanie co 5 s** może pominąć slajd, który był widoczny krócej, i trafić w środek przejścia.
11. **Whisper large-v3-turbo** daje gorsze wyniki dla polskiego niż pełny `large-v3`. Język jest już ustalony (`pl`), ale warto przetestować `large-v3` na 8 GB (ok. 3 GB w int8_float16 się mieści, kosztem czasu). Brak VAD/ochrony przed halucynacjami w ciszy.
12. **Wymagane biblioteki CUDA** (cuDNN 9 / cuBLAS 12) dla faster-whisper nie są wspomniane, a bywają źródłem problemów z instalacją.
13. **Resume oparty tylko na istnieniu `.json`:** plik może być częściowy po awarii, a zmiana parametrów lub wideo nie unieważnia cache. Brak checkpointów wewnątrz długich etapów (VLM, LLM).
14. **Jakość modeli 7B po polsku** jest przeciętna: możliwe halucynacje, błędy w terminologii, uproszczenia. Spec nie definiuje, jak mierzyć „jakość notatki”.

**Drobne / braki**
15. Brak katalogu testów, README, pliku konfiguracji.
16. Brak definicji interfejsu uruchomienia (CLI), obsługi wielu filmów, katalogu wyjściowego.
17. `ffmpeg-python` jest praktycznie nieutrzymywany.
18. Brak kryteriów akceptacji i danych testowych (przykładowe wideo).
19. Spec nakazuje sprawdzać „czy w VRAM nie ma innych modeli”, ale nie mówi jak; plan używa `/api/ps` + NVML.
20. Prompt systemowy jest po polsku, a opisy slajdów generowane po angielsku; nie wiadomo, w jakim języku mają być notatki.
21. Etap 2 nie wymaga GPU i może iść równolegle z Etapem 1; spec wymusza pełną sekwencyjność, co niepotrzebnie wydłuża czas.
22. Szacunki czasu i zajętości VRAM podane w specyfikacji (np. „6 GB”) to przybliżenia; trzeba zmierzyć.
