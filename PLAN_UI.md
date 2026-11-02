# Plan implementacji: interfejs LectureNoter (UI + czat z notatkami)

Cel: prosta, lokalna aplikacja, która pozwala (1) wybrać film do przetworzenia, (2) zobaczyć listę przerobionych filmów i bieżący etap, (3) regenerować wybrane etapy lub całość, (4) czytać notatki, (5) rozmawiać z lokalnym LLM o treści notatek z zachowaniem kontekstu rozmowy. Wszystko lokalnie (Ollama), bez zewnętrznych usług.

---

## 0. Decyzje i założenia

| # | Decyzja | Wartość |
|---|---------|---------|
| U1 | Forma interfejsu | **PWA** (aplikacja webowa instalowana z przeglądarki, działa w osobnym oknie jak aplikacja) serwowana lokalnie przez FastAPI |
| U2 | Użytkownicy | jeden użytkownik, brak logowania, serwer tylko na `127.0.0.1` |
| U3 | Wybór pliku | **wybór z katalogu `videos/`** (skanowany przez aplikację). Bez uploadu przez przeglądarkę |
| U4 | Zadania | **kolejka** (trwała, wiele filmów), ale **jedno zadanie GPU naraz** |
| U5 | Czat | **jeden film**, pytania do **notatek i transkryptu** (oraz opisów tablicy) |
| U6 | Notatki | czytanie, **edycja** w UI i **eksport** (Markdown, PDF, opcjonalnie Word) |
| U7 | Model czatu | domyślnie model z profilu VRAM, z możliwością wyboru spośród zainstalowanych w Ollamie |

---

## 1. Wybór technologii

### 1.1 Backend: FastAPI (uvicorn)
Długie zadania w tle, streaming (SSE) i prosty deployment. Front: statyczny HTML/JS (HTMX lub vanilla JS), biblioteki (marked, KaTeX, DOMPurify) wgrane **lokalnie**, bez CDN.

### 1.2 Powłoka „aplikacji”: porównanie

| Opcja | Rozmiar/narzut | Wymaga | Ocena |
|-------|----------------|--------|-------|
| Electron | ~150–300 MB, własny Chromium | Node.js | za ciężki (zgodnie z Twoją uwagą) |
| Tauri | ~5–15 MB, WebView2 | **Rust** + toolchain, osobny build na Windows | lekki, ale wraca Rust (świadomie go usunęliśmy), a aplikacja i tak musi uruchamiać serwer w WSL |
| pywebview | mały, WebView2 | Python po stronie Windows (osobne środowisko od WSL) | rozdzielone środowiska Windows/WSL, więcej kłopotów niż pożytku |
| **PWA (Edge/Chrome „Zainstaluj aplikację”)** | **0 MB dodatkowych**, korzysta z zainstalowanej przeglądarki | nic poza przeglądarką | **rekomendowane** |

**Decyzja: PWA.** Powody: zero dodatkowych zależności, jedno wspólne źródło (ten sam kod front-endu), okno bez paska przeglądarki, ikona w menu Start/na pasku zadań, a `localhost` jest traktowany jako bezpieczny kontekst, więc instalacja PWA działa bez HTTPS. Wymaga `manifest.webmanifest`, ikon i minimalnego service workera.

**Ograniczenie PWA, które trzeba obsłużyć:** PWA sama **nie uruchamia serwera**. Rozwiązanie (wszystko w repozytorium):
- `scripts/start.bat` (Windows): uruchamia `wsl.exe -d <dystrybucja> -- bash -lc "cd ~/LectureNoter && source .venv/bin/activate && python server.py"` i otwiera aplikację. Skrót do niego można przypiąć do paska zadań lub autostartu.
- Opcjonalnie: usługa `systemd --user` w WSL (wymaga włączonego systemd), tak aby serwer startował z WSL.
- Service worker cache'uje powłokę aplikacji, więc przy wyłączonym serwerze okno pokazuje czytelny ekran „Serwer nie działa: uruchom start.bat” zamiast błędu przeglądarki.
- Endpoint `/api/health` i automatyczne ponawianie połączenia SSE.

---

## 2. Architektura

```
przeglądarka ──HTTP/SSE──► FastAPI (server.py)
                               ├── Library      (lista filmów, meta.json, stan etapów)
                               ├── JobManager   (kolejka, 1 zadanie GPU naraz, anulowanie)
                               │      └── podproces: python main.py ... (istniejący orkiestrator)
                               ├── NotesService (czytanie, spis treści, serwowanie klatek)
                               └── ChatService  (kontekst, retrieval, streaming z Ollamy)
                                      └── Ollama /api/chat
```

Zasady:
- UI **nie wywołuje** etapów bezpośrednio. Uruchamia `main.py` jako podproces (tak jak dziś), a stan czyta z plików w katalogu roboczym. Pipeline nadal działa z CLI bez UI.
- Źródłem prawdy o stanie jest dysk (`stage_state.json`, `progress.json`, `meta.json`), a nie pamięć serwera. Restart serwera nie gubi stanu.
- **Globalna blokada GPU** (`gpu.lock`): zadanie pipeline'u i czat nie działają jednocześnie (patrz ryzyko R1).

### Nowa struktura plików
```
LectureNoter/
├── server.py                  # FastAPI: routing, SSE
├── ui/
│   ├── library.py             # skanowanie work/, meta.json, statusy
│   ├── jobs.py                # JobManager: kolejka, podprocesy, anulowanie, heartbeat
│   ├── notes.py               # TOC, serwowanie notatek i klatek
│   ├── chat.py                # kontekst, retrieval, historia, streaming
│   ├── gpu_lock.py            # blokada GPU między pipeline'em a czatem
│   ├── retrieval.py           # BM25 po sekcjach notatek i oknach transkryptu
│   ├── export.py              # eksport MD/PDF/Word
│   └── static/
│       ├── index.html, app.js, app.css
│       ├── manifest.webmanifest, sw.js, icons/   # PWA
│       └── vendor/            # marked, KaTeX, DOMPurify (lokalnie)
├── scripts/start.bat          # Windows: start serwera w WSL + otwarcie aplikacji
├── videos/                    # katalog z nagraniami (skanowany przez UI)
└── tests/test_ui_*.py
```

---

## 2a. Zmiany w istniejącym kodzie (warunek konieczny)

Dziś orkiestrator tylko loguje, więc UI nie ma z czego pokazać postępu. Trzeba:

1. **`meta.json` w katalogu roboczym** (`{video_path, title, created, duration, has_audio, profile}`). Obecnie ścieżka do filmu nie jest nigdzie zapisana, a lista filmów w UI jej wymaga.
2. **`progress.json`** aktualizowany atomowo przez etapy: `{stage, done, total, message, updated_at}`.
   - ASR: czas przetworzonego materiału / długość (faster-whisper zwraca segmenty po kolei).
   - Klatki: numer próbki / liczba próbek.
   - VLM: liczba linii w `vision_context.jsonl` / liczba klatek.
   - LLM: liczba gotowych `chunks/*.md` / liczba fragmentów (+ krok „podsumowanie”).
3. **Stan „running”** w `stage_state.json` (`status: running`, `pid`, `heartbeat`). Po awarii (np. wyłączenie WSL, które już się zdarzyło) UI rozpoznaje stan „przerwany” po starym heartbeacie i pozwala wznowić.
4. **Anulowanie:** orkiestrator obsługuje `SIGTERM` (kończy podproces etapu, rozładowuje model Ollamy, zapisuje stan `cancelled`).
5. **Wyjście `status --json`** w CLI (`python main.py video --status`) zwracające aktualne stany etapów. Wspólne dla UI i debugowania.
6. **Regeneracja pojedynczego etapu** z kaskadą: unieważnienie etapu i następnych (już zaimplementowane w `invalidate` i hashach wejść). Trzeba tylko wystawić je jako operację z podglądem skutków („regeneracja VLM usunie też notatki”).
7. **Rozdzielenie `run` od logowania do pliku** tak, aby JobManager mógł przekazywać logi na żywo (tail `pipeline.log` przez SSE).

---

## 3. Funkcje i ekrany

### 3.1 Biblioteka (ekran główny)
- Lista filmów z `work/*`: tytuł, data, czas trwania, **pasek 4 etapów** (ASR → Klatki → VLM → Notatki) ze statusem: `oczekuje | w kolejce | w toku (x/y) | gotowe | pominięte | błąd | przerwane | nieaktualne`.
- „Nieaktualne” = zmieniła się konfiguracja lub wejście (już liczone przez hashe w orkiestratorze).
- **„Dodaj film”:** lista plików wideo z katalogu `videos/` (ścieżkę katalogu można zmienić w ustawieniach), z oznaczeniem, które już przetworzono. Walidacja `ffprobe` (czas trwania, audio tak/nie). Wielokrotny wybór dodaje kilka filmów do kolejki.
- Profil (auto/8 GB/16 GB), język (domyślnie `pl`).
- **Kolejka:** trwała (`queue.json`, przeżywa restart serwera), kolejność, zmiana kolejności, anulowanie, wstrzymanie całej kolejki. Jedno zadanie GPU naraz, kolejne startują automatycznie po zakończeniu poprzedniego. Zadanie, które zakończyło się błędem, nie blokuje kolejki (kolejne filmy lecą dalej).

### 3.2 Szczegóły filmu
- Karty etapów z: statusem, czasem trwania, paskiem postępu, liczbą wyników (np. „42 klatki”), ostatnimi liniami logu (SSE na żywo), przyciskiem **„Regeneruj”**.
- **Regeneruj etap** (z potwierdzeniem i listą etapów, które zostaną unieważnione), **Uruchom całość od nowa** (`--force`), **Wznów** (po awarii/anulowaniu).
- Podgląd artefaktów: transkrypt (wyszukiwanie), galeria klatek z timestampami (miniatury + opis VLM obok), fragmenty `chunks/`.
- Ustawienia filmu (model, `num_ctx`, `chunk_minutes`, progi klatek) zapisywane per film i wchodzące do hashy konfiguracji.
- Opcjonalnie (faza 2): regeneracja pojedynczego fragmentu notatki lub pojedynczej klatki VLM.

### 3.3 Notatki
- Renderowanie Markdown z **LaTeX (KaTeX)** i podświetleniem kodu; spis treści z kotwicami; wyszukiwanie.
- Linki znaczników czasu `[HH:MM:SS]` otwierają klatkę/tablicę z tego momentu (jeśli istnieje) w panelu bocznym oraz odpowiadający fragment transkryptu.
- **Edycja:** edytor Markdown z podglądem obok. Edytowana wersja zapisywana jako `lecture_notes.edited.md`; wygenerowany plik `lecture_notes.md` pozostaje nietknięty. UI pokazuje, którą wersję czytasz, i pozwala wrócić do wygenerowanej. Ponowna regeneracja etapu LLM **nie nadpisuje** wersji edytowanej (ostrzeżenie przed regeneracją). Przechowywane ostatnie N wersji (historia, przywracanie).
- Czat zawsze widzi **aktualnie wybraną** wersję notatek.
- **Eksport:** Markdown (`.md`); **PDF** przez wydruk z dedykowanym CSS (druk z przeglądarki, bez dodatkowych zależności, wzory z KaTeX); **Word (`.docx`)** opcjonalnie przez `pandoc`, jeśli jest zainstalowany (wykrywany automatycznie, w przeciwnym razie przycisk nieaktywny z podpowiedzią instalacji).
- Zakładka „Czat” obok notatek (3.4).

### 3.4 Czat z notatkami i transkryptem (lokalny LLM)
**Źródła wiedzy dla jednego filmu**
1. notatki (`lecture_notes.md` lub wersja edytowana), podzielone na sekcje wg nagłówków `##/###`,
2. **transkrypt** (`transcript.json`) pocięty na okna ok. 60–90 s z znacznikami czasu,
3. opisy tablicy (`vision_context.json`), jako osobne źródło z timestampami.

**Retrieval (BM25, bez dodatkowego modelu w VRAM)**
- Dla każdego pytania ranking sekcji notatek, okien transkryptu i opisów tablicy. Do promptu trafia budżetowana mieszanka: spis treści + top-k sekcji notatek (główne źródło) + top-k okien transkryptu (szczegóły) + ewentualnie opis tablicy z tego samego czasu.
- Zapytanie = pytanie + ostatnia odpowiedź/pytanie (rozwiązuje „a co z tym drugim?”). Proste normalizowanie polskiej fleksji (obcinanie końcówek/prefiksy) poprawia trafność BM25. Embeddingi (`nomic-embed-text`) jako opcja późniejsza.
- Jeśli całe notatki mieszczą się w budżecie kontekstu, wkładane są w całości (tryb prosty), a transkrypt dociągany wyszukiwaniem.

**Zachowanie kontekstu rozmowy**
- Historia per film w `work/<film>/chat.json`: wiele rozmów, tytuły, „nowa rozmowa”, usuwanie.
- Okno historii liczone w tokenach; po przekroczeniu **streszczenie starszych tur** (rolling summary) zamiast ucinania.
- Streaming odpowiedzi przez `/api/chat` → SSE → UI; przycisk „Stop”.
- Odpowiedź zawiera **źródła**: klikalne sekcje notatek i znaczniki czasu transkryptu/klatki.

**Prompt systemowy:** odpowiadaj tylko na podstawie dostarczonych fragmentów, po polsku, cytuj sekcje i znaczniki czasu, rozróżniaj „z notatek” i „z transkryptu”, przyznaj się do braku informacji, nie zmyślaj, elementy interfejsu programu ignoruj. W UI stałe ostrzeżenie, że notatki i transkrypt są generowane automatycznie i mogą zawierać błędy.

**Model i GPU**
- Model czatu wybierany z listy zainstalowanych (`/api/tags`), domyślnie z profilu VRAM.
- Czat ładuje model z `keep_alive` (np. 10 min) i **rozładowuje go** przed startem zadania pipeline'u (blokada GPU, R1). Gdy zadanie trwa, czat jest wyłączony z komunikatem; po zakończeniu aktywny.

---

## 4. API (szkic)

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| GET | `/api/videos` | lista filmów + statusy etapów |
| GET | `/api/library/files` | pliki wideo z katalogu `videos/` (oznaczone: przetworzony / nie) |
| POST | `/api/videos` | dodaj film z katalogu (jeden lub kilka), utwórz `meta.json`, opcjonalnie dodaj do kolejki |
| GET/POST/DELETE | `/api/queue` | podgląd, zmiana kolejności, wstrzymanie, anulowanie zadania |
| GET | `/api/videos/{id}` | szczegóły, postęp, konfiguracja |
| POST | `/api/videos/{id}/run` | `{from_stage?, only_stage?, force?}` → trafia do kolejki |
| POST | `/api/jobs/{job}/cancel` | anuluj |
| GET | `/api/jobs/stream` | SSE: zmiany statusu, postęp, logi |
| GET/PUT | `/api/videos/{id}/notes` | notatki (md) + TOC; zapis wersji edytowanej |
| GET | `/api/videos/{id}/notes/export?format=md\|pdf\|docx` | eksport |
| GET | `/api/health` | żywotność serwera (PWA) |
| GET | `/api/videos/{id}/frames/{file}` | klatka (walidacja ścieżki) |
| GET | `/api/videos/{id}/artifacts/{name}` | transkrypt, opisy VLM, chunki |
| GET/POST | `/api/videos/{id}/chats` | lista / nowa rozmowa |
| POST | `/api/videos/{id}/chats/{cid}/messages` | pytanie → SSE ze strumieniem odpowiedzi |
| DELETE | `/api/videos/{id}/chats/{cid}` | usuń rozmowę |
| GET | `/api/system` | stan Ollamy, załadowane modele, VRAM, dostępne modele, czy GPU zajęte |

---

## 5. Fazy implementacji i szacunki

| Faza | Zakres | Szac. |
|------|--------|-------|
| 1 | Zmiany w orkiestratorze: `meta.json`, `progress.json`, heartbeat, anulowanie, `--status` (sekcja 2a) + testy | 1–1,5 d |
| 2 | `JobManager` z trwałą kolejką, `gpu_lock`, `Library` (skan `videos/`), API listy/szczegółów/uruchamiania, SSE logów i postępu | 2 d |
| 3 | Front + PWA: biblioteka, wybór z katalogu, kolejka, szczegóły filmu, paski etapów, regeneracja, manifest/service worker, `start.bat` | 2 d |
| 4 | Notatki: render MD + KaTeX, TOC, galeria klatek, linki czasów, edycja z wersjami, eksport MD/PDF/(Word) | 1,5 d |
| 5 | Czat: sekcjonowanie, BM25 po notatkach i transkrypcie, prompt, historia, streaming, streszczanie historii, źródła, wybór modelu | 2,5 d |
| 6 | Dopracowanie: obsługa błędów, stany po awarii, README, testy ręczne w przeglądarce | 1 d |
| | **Razem** | **ok. 10–11 dni roboczych** |

Kolejność: 1 → 2 → 3 (MVP: wybór filmu, status, kolejka, regeneracja) → 4 (czytanie i edycja) → 5 (czat) → 6. Po fazie 4 aplikacja jest już użyteczna bez czatu.

---

## 6. Testy i kryteria akceptacji

- Testy jednostkowe: sekcjonowanie notatek, BM25, budżet kontekstu i streszczanie historii, kolejka zadań (atrapy podprocesów), przejścia statusów, ochrona przed path traversal, wykrywanie „przerwane” po starym heartbeacie.
- Testy API (FastAPI `TestClient`) z atrapą Ollamy.
- Test ręczny w przeglądarce na prawdziwym filmie (Windows 11 + WSL2).
- Akceptacja:
  1. Z UI da się dodać film i uruchomić całość bez terminala.
  2. Lista filmów pokazuje poprawny etap i postęp także po restarcie serwera i po awarii WSL.
  3. Regeneracja etapu unieważnia i powtarza tylko etapy zależne.
  4. Notatki renderują się czytelnie (nagłówki, listy, wzory) i otwierają klatki po kliknięciu znacznika czasu.
  5. Czat odpowiada na kolejne, powiązane pytania („a co z tym drugim?”) i podaje źródła w notatkach.
  6. Czat i pipeline nigdy nie ładują modeli jednocześnie.

---

## 7. Ryzyka, wady i niejasności

**R1. Konflikt GPU między czatem a pipeline'em (krytyczne).** Obecne `assert_vram_free()` rozładowuje *wszystkie* modele Ollamy, więc start pipeline'u zabije trwającą odpowiedź czatu, a czat uruchomiony w trakcie zadania może przepełnić VRAM (na 8 GB co najmniej jeden z modeli się nie zmieści) i, jak już zaobserwowano na WSL, doprowadzić do resetu sterownika. Rozwiązanie: wspólna blokada, czat wyłączony przy aktywnym zadaniu, jawny unload czatu przed startem zadania.

**R2. Pliki wideo (rozstrzygnięte: wybór z katalogu).** Bez uploadu. Pliki powinny leżeć w systemie plików WSL (np. `~/LectureNoter/videos/`), nie w `/mnt/c/...`, bo tam I/O jest dużo wolniejsze. Nagrania z Windowsa trzeba tam skopiować (np. `\\wsl$\<dystrybucja>\home\...` w Eksploratorze). Można ewentualnie dopuścić symlink do katalogu na `/mnt/c`, ale z ostrzeżeniem o wydajności.

**R3. Rozmiar notatek vs kontekst.** Notatki z 1,5 h to kilka–kilkanaście tysięcy tokenów. Na profilu 8 GB (`num_ctx` 16k) całość + historia + odpowiedź może się nie zmieścić, stąd retrieval. BM25 ma słabości przy fleksji polskiej (różne formy słów). Mitygacja: normalizacja prostym stemmingiem/prefiksami lub embeddingi jako opcja, ale to dodatkowy model.

**R4. Jakość odpowiedzi 7B po polsku.** Model może zmyślać lub błędnie streszczać. Czat jest tak dobry, jak notatki, a te mają znane błędy (odczyt pisma, powtórzenia, halucynacje w sekcjach). UI nie naprawi jakości notatek. Cytowanie źródeł i ostrzeżenie „odpowiedź oparta na automatycznych notatkach” ograniczają skutki.

**R5. Postęp niektórych etapów jest przybliżony.** ASR i LLM nie mają naturalnego „x z y”. Wymaga to dopisania raportowania w kodzie etapów (sekcja 2a), a pasek będzie szacunkowy.

**R6. Stany po awarii/ubiciu procesu.** WSL potrafi zgasnąć w trakcie (zdarzyło się w VLM). Bez heartbeatu UI pokazałby wieczny stan „w toku”. Plan: heartbeat + stan „przerwane” + przycisk „Wznów” (korzysta z istniejących checkpointów).

**R7. Regeneracja i utrata danych.** Regeneracja etapu kasuje jego artefakty i artefakty zależne (np. regeneracja klatek usuwa opisy VLM, godziny pracy). UI musi to jasno pokazywać przed potwierdzeniem. Do rozważenia: archiwizacja poprzedniej wersji notatek (`lecture_notes.md.bak`).

**R8. Bezpieczeństwo lokalne.** Serwer tylko na `127.0.0.1`, bez uwierzytelniania. Serwowanie plików wymaga ścisłej walidacji ścieżek (path traversal), a renderowanie Markdown wymaga sanityzacji HTML (DOMPurify), bo treść pochodzi z LLM. Dostęp z innych urządzeń w sieci (np. telefon) wymagałby osobnej decyzji (hasło, `--host 0.0.0.0`).

**R9. WSL2 i dostęp z Windowsa.** `localhost:8000` z Windowsa działa zwykle przez automatyczne przekierowanie portów WSL2; w trybie sieci „mirrored” również. Do sprawdzenia na docelowej maszynie.

**R10. PWA: serwer nie startuje sam.** Zamknięcie okna lub wyłączenie WSL zatrzymuje backend. Wymaga `start.bat`/autostartu (sekcja 1.2). Service worker pokazuje czytelny komunikat, gdy serwer jest wyłączony. Instalacja PWA wymaga Chrome/Edge (Firefox na desktopie nie instaluje PWA).

**R11. Edycja notatek vs regeneracja.** Wersja edytowana jest przechowywana osobno i nie jest nadpisywana przez ponowną generację (inaczej użytkownik straciłby pracę). Wymaga jasnego oznaczenia w UI, którą wersję czytasz i z której korzysta czat.

**R12. Eksport do Worda** wymaga `pandoc` (osobna instalacja, np. `apt install pandoc`). PDF z wydruku przeglądarki jest zawsze dostępny, ale jego wygląd zależy od przeglądarki i CSS druku.

**R13. Transkrypt w czacie** zwiększa ryzyko zbyt długiego kontekstu i szumu (błędy rozpoznawania mowy trafiają do odpowiedzi). Dlatego notatki są głównym źródłem, a transkrypt uzupełnieniem, z wyraźnym oznaczeniem źródła w odpowiedzi.

**R14. Brak możliwości zadania pytań do wielu wykładów** (poza zakresem; czat działa na jednym filmie). Ewentualne rozszerzenie na cały kurs wymagałoby wspólnego indeksu i osobnego zarządzania kontekstem.

**Pozostaje do ustalenia (drobne):**
- Domyślna lokalizacja katalogu `videos/` (w repozytorium czy w katalogu domowym WSL, np. `~/videos`).
- Czy wersje notatek mają być przechowywane w ograniczonej liczbie (np. 20), czy bez limitu.
