# LectureNoter

Lokalny generator notatek z nagrań wykładów. Z nagrania (mowa + tablica lub slajdy) tworzy notatki w Markdown, a aplikacja webowa pozwala je czytać, edytować, przeszukiwać, omawiać z lokalnym modelem i sprawdzać wiedzę quizami. Wszystko działa na Twoim komputerze; modele ładują się po kolei (jeden naraz w VRAM).

## Spis treści

- [Jak to działa](#jak-to-działa)
- [Wymagania](#wymagania)
- [Instalacja i uruchomienie](#instalacja-i-uruchomienie)
- [Funkcje aplikacji](#funkcje-aplikacji)
- [Eksport i import wykładów (.lec)](#eksport-i-import-wykładów-lec)
- [Linia poleceń (CLI)](#linia-poleceń-cli)
- [Konfiguracja](#konfiguracja)
- [Diagnostyka](#diagnostyka)
- [Testy i status](#testy-i-status)

## Jak to działa

| Etap | Narzędzie | Wynik |
|------|-----------|-------|
| 1. Transkrypcja | `faster-whisper` (`large-v3-turbo`) | `transcript.json` |
| 2. Klatki z tablicy | ffmpeg + OpenCV (stan tablicy tuż przed wymazaniem) | `frames/` |
| 3. Odczyt tablicy | Ollama, `qwen2.5vl:7b` | `vision_context.json` |
| 4. Notatki | Ollama, `qwen2.5:7b-instruct` lub `14b-instruct` | `lecture_notes.md` |

Wyniki trafiają do `work/<nazwa_filmu>/`. Przerwane przetwarzanie wznawia się od ostatniego punktu kontrolnego, a zmiana konfiguracji powtarza tylko dotknięte nią etapy. Brak ścieżki audio jest dozwolony: notatki powstają wtedy tylko z tablicy.

## Wymagania

Backend działa na **Linuksie**. Na Windowsie uruchamiasz go w **WSL2** (nie natywnie).

- karta NVIDIA ze sterownikiem (w WSL `nvidia-smi` ma pokazywać kartę; sterownik instalujesz tylko w Windowsie); bez karty działa tryb CPU (patrz niżej)
- Python 3.10+ (zalecany 3.11), `ffmpeg`, [Ollama](https://ollama.com) uruchomiona w WSL/Linuksie
- opcjonalnie `pandoc` (eksport notatek do Worda)

## Instalacja i uruchomienie

Środowisko Pythona i zależności instalują się same przy pierwszym uruchomieniu (`scripts/launch.py`). Ręcznie potrzebne są tylko pakiety systemowe i modele:

```bash
sudo apt install ffmpeg pandoc python3-venv
ollama pull qwen2.5vl:7b
ollama pull qwen2.5:7b-instruct    # karta 8 GB
ollama pull qwen2.5:14b-instruct   # karta 16 GB (opcjonalnie)
```

Na starszym Ubuntu nowszy Python: `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11 python3.11-venv python3.11-dev`. Skrypt wybiera najnowszy znaleziony interpreter (lub użyj `LECTURENOTER_PYTHON`).

### Windows 11 (WSL2)

Uruchom `scripts\start.bat` (dwuklik lub skrót na Pulpicie). Skrypt zamienia ścieżkę projektu na ścieżkę WSL, startuje serwer w tle bez okna terminala i otwiera aplikację w oknie Edge/Chrome. Pierwsze uruchomienie instaluje zależności i trwa kilka minut.

- **Wyłączanie:** serwer kończy pracę ok. 15 s po zamknięciu okna aplikacji (2 min, gdy połączenie tylko się zerwało, np. po uśpieniu). Podczas przetwarzania lub odpowiedzi czatu działa dalej i wyłącza się po ich zakończeniu. Można też użyć linku „Zakończ” w nagłówku.
- **Uśpienie komputera:** na czas przetwarzania serwer prosi system o niewyłączanie komputera. Jeśli mimo to znika po wygaszeniu ekranu, ustaw w Windowsie Zasilanie → Uśpienie → „Nigdy”. Przerwane zadanie wznowisz przyciskiem „Wznów”.
- **Pliki projektu na dysku Windows** (`/mnt/c/...`) działają, ale środowisko Pythona trafia wtedy do `~/.local/share/lecturenoter/venv` (NTFS jest wolny).
- **Zmienne:** `LECTURENOTER_DISTRO` (dystrybucja WSL inna niż domyślna, nazwy: `wsl -l`), `LECTURENOTER_DIR` (własna ścieżka projektu w WSL).
- **Diagnostyka startu:** log w `work\launch.log`; przy braku reakcji uruchom `start.bat` z `cmd`, by zobaczyć komunikaty.
- **PWA:** aplikację można zainstalować z przeglądarki, ale jej okno nie uruchamia serwera, więc najpierw musi działać `start.bat`.

### Linux / terminal WSL

```bash
scripts/start.sh          # na pierwszym planie, http://localhost:8000 (Ctrl+C kończy)
scripts/start.sh --bg     # w tle; sam się wyłączy po zamknięciu okna aplikacji
```

Zmienne: `LECTURENOTER_VENV` (katalog środowiska), `LECTURENOTER_PYTHON`. Pozostałe argumenty trafiają do serwera (np. `--port 8001`). Bezpośrednie `python server.py` nie wyłącza się samo; dodaj `--auto-exit`.

### Wskazówki dla WSL2

- Ollamę instaluj w WSL i nie uruchamiaj jej równolegle w Windowsie (dwa serwery mogą zajmować VRAM jednocześnie). Bez systemd: `ollama serve`. Inny adres: `OLLAMA_HOST=127.0.0.1:11435`.
- Pamięć WSL zwiększysz w `%UserProfile%\.wslconfig` (`[wsl2]`, `memory=24GB`), potem `wsl --shutdown`.
- Windows zajmuje ok. 0,5–1 GB VRAM; na karcie 8 GB zamknij inne programy używające GPU.
- Błąd `libcublas.so.12 is not found`: uruchom ponownie skrypt startowy (doinstaluje zależności) albo usuń katalog venv.

### Tryb CPU (bez GPU)

Gdy nie wykryto karty NVIDIA, aplikacja sama wybiera profil `cpu`: transkrypcja `faster-whisper` na CPU (model `small`, `int8`), lżejsze modele Ollamy (`qwen2.5:3b-instruct`, `qwen2.5vl:3b`) i mniejszy kontekst. W pasku u góry pojawia się znacznik „tryb CPU”. Potrzebne modele: `ollama pull qwen2.5:3b-instruct` i `ollama pull qwen2.5vl:3b`. Profil można też wybrać ręcznie (przy dodawaniu filmu albo `--profile cpu`), a wykrywanie wymusić zmienną `LECTURENOTER_DEVICE=cpu|cuda`.

Spodziewaj się czasu wielokrotnie dłuższego niż na GPU: transkrypcja zbliżona do czasu nagrania lub dłuższa, odczyt tablicy (VLM) to nawet godziny przy dłuższym wykładzie, a quiz na 20 pytań to dziesiątki minut. Jakość małych modeli jest niższa (zwłaszcza odczyt pisma odręcznego). Importowane wykłady (`.lec`), czytanie notatek, wyszukiwanie i quizy z gotowych notatek działają bez ograniczeń poza szybkością modelu. Parametry profilu są w `config.yaml` (`profiles.cpu`).

## Funkcje aplikacji

**Biblioteka**
- Wybór filmów z katalogu `videos/`, postęp czterech etapów, trwała kolejka (jedno zadanie naraz; pauza, zmiana kolejności, anulowanie).
- Zmiana nazwy (tylko wyświetlanej), zagnieżdżone foldery (do 4 poziomów, przenoszenie przyciskiem lub przeciąganiem), sortowanie.
- **Wyszukiwarka** po wszystkich wykładach: tytuły, foldery, notatki, transkrypty i odczyt tablicy. Rozumie polskie odmiany, obsługuje frazy w cudzysłowie, pokazuje fragmenty i przenosi do właściwego miejsca. Skrót `/` aktywuje pole, Esc czyści.
- **Usuwanie** wykładu z listą tego, co zostanie utracone. Film źródłowy zostaje, chyba że zaznaczysz usunięcie także jego.

**Szczegóły wykładu**
- *Przetwarzanie:* regeneracja pojedynczego etapu (z podglądem skutków), uruchomienie od nowa, wznowienie po awarii, logi na żywo.
- *Notatki:* Markdown z wzorami (KaTeX, offline), spis treści, wyszukiwanie, klikalne znaczniki czasu, edycja z historią wersji (wersja edytowana nie jest nadpisywana przy regeneracji). Eksport do Markdown, PDF (drukowanie przeglądarki) i Worda (wymaga `pandoc`).
- *Materiały:* klatki z tablicy i przeszukiwalny transkrypt.
- *Czat:* pytania do notatek, transkryptu i odczytu tablicy; odpowiedzi strumieniowane, rozmowy zapisywane i eksportowalne (Markdown, PDF), model do wyboru spośród zainstalowanych w Ollamie.
- *Podpowiedzi z wykładu:* przy generowaniu notatek model zapisuje słowa kluczowe i pytania pomocnicze (`suggestions.json`). Czat pokazuje je jako klikalne pojęcia i pytania (wstawiają się do pola, które możesz poprawić), a formularz quizu podpowiada pojęcia przy wyborze obszaru. Dla starszych wykładów podpowiedzi powstają z nagłówków notatek, a link „Wygeneruj lepsze modelem” tworzy je na żądanie (także po dużej edycji notatek).
- *Quiz:* patrz niżej.

Czat, quizy i przetwarzanie nie używają GPU jednocześnie: w czasie zadania są niedostępne, a bezczynny model jest zwalniany przed jego startem.

### Quizy

Generowane lokalnym modelem z notatek i transkryptu.

| Typ | Opis |
|-----|------|
| ABCD, jedna odpowiedź | cztery opcje, jedna poprawna |
| ABCD, wiele odpowiedzi | trzeba zaznaczyć wszystkie poprawne |
| Definicja → nazwa | dostajesz opis, podajesz nazwę pojęcia (tolerowane literówki i synonimy; własny synonim można uznać ręcznie) |
| Opisowy | odpowiedź ocenia model względem wzorca; gdy jest niedostępny, oceniasz sam |
| Mieszany (automatyczny) | model sam wybiera zagadnienia i najlepszy typ pytania do każdego z nich (definicje → „podaj nazwę”, rozróżnianie pojęć → ABCD, mechanizmy → opisowe) |

Wybierasz liczbę pytań (1–30) i obszar: cały wykład, wybrane sekcje notatek lub dowolny temat. Pytania sprawdzają zrozumienie, nie szczegóły typu daty. Po każdej odpowiedzi widzisz poprawną odpowiedź i uzasadnienie, także dlaczego pozostałe opcje są błędne. Quiz jest zaliczony od 80% (`quiz.pass_ratio`). Quizy zapisują się z historią podejść (`work/<film>/quizzes.json`), można je powtarzać i usuwać. Kilka quizów czeka w kolejce na model.

Generowanie ma dwa etapy: najpierw model planuje listę **różnych zagadnień** z materiału (to eliminuje powtarzające się pytania), potem układa pytanie do każdego z nich. Gdy pytanie jest niepoprawne albo się powtarza, bierze kolejne zagadnienie (lub planuje nowe) i próbuje dalej, aż liczba pytań dojdzie do wybranej. Poddaje się dopiero po 2× tylu próbach (`quiz.attempts_factor`) i wtedy pokazuje, ile udało się ułożyć i dlaczego odrzucano pytania. W pytaniach ABCD dozwolone są też 2 opcje (prawda/fałsz).

**Tryb mieszany** pozwala wyłączyć niechciane typy (np. opisowe, które są wolne na CPU). Podpowiedź modelu jest korygowana regułami w kodzie: żaden typ nie przekracza 50% quizu, pytań opisowych jest najwyżej 20%, a quiz ma co najmniej kilka różnych typów, nawet gdy mały model uparcie proponuje jeden.

**Słabe strony:** opcja „Skup się na moich słabych stronach” układa nowy quiz głównie (do ~70%) z zagadnień, w których ostatnie odpowiedzi były błędne lub częściowe, na podstawie historii wszystkich quizów tego wykładu. Pytania są nowe, nie powtórką starych, a zagadnienie przestaje być „słabe”, gdy poprawnie odpowiesz na nie przy kolejnym podejściu. Opcja jest dostępna po rozwiązaniu quizu z błędami.

### Zrozumiałość notatek

Notatki powstają fragmentami, ale każdy fragment dostaje kontekst całości, żeby nie były urywane ani wyrwane z kontekstu:
- **Szkielet wykładu** (`outline.md`): osobny pierwszy przebieg tworzy temat, przebieg i słownik kluczowych pojęć z definicjami; dostaje go każdy fragment.
- **Ciągłość:** fragment widzi koniec poprzednich notatek i koniec poprzedniego odcinka nagrania (jako kontekst, bez przepisywania), więc kończy zaczęte wątki.
- **Styl:** pełne zdania zamiast samych haseł, wyjaśnienie pojęć przy pierwszym użyciu, zachowane powiązania i przykłady. Temat opisany wcześniej jest przypominany jednym zdaniem zamiast całkowicie pomijany.

Koszt to jedno dodatkowe wywołanie modelu i dłuższe prompty (etap notatek ok. 20–40% dłuższy). Szkielet można wyłączyć przez `llm.outline: false`. Po aktualizacji notatki istniejących wykładów trzeba wygenerować ponownie (etap „Notatki”); wersje edytowane ręcznie nie są nadpisywane.

### Jakość notatek

Mały model potrafi zapętlić się w powtórzeniach. Generator to wykrywa: ponawia generowanie z inną temperaturą i karą za powtórzenia (do 2 razy), a w ostateczności wycina powtórzenia. Pętle w opisach tablicy i transkrypcji są usuwane przed syntezą, a zdublowane sekcje scalane. Gdy fragmenty trzeba było naprawić, zakładka Notatki pokazuje ostrzeżenie z propozycją ponownej generacji. Parametry: `llm.repeat_penalty`, `llm.loop_retries`.

## Eksport i import wykładów (.lec)

Plik `.lec` służy do przenoszenia wykładów między komputerami i instalacjami aplikacji; jest przeznaczony do wczytania w LectureNoter, a nie do ręcznej edycji. Zawiera `manifest.json` (tytuł, folder, `uid`) oraz w `data/`: transkrypt, klatki, odczyt tablicy, notatki (wszystkie wersje), czaty, quizy i stan etapów. Logi i pliki tymczasowe są pomijane. Film jest opcjonalny (domyślnie niedołączany).

- **Eksport:** „Eksportuj” przy wykładzie → rozmiar i opcja „Dołącz plik wideo” → pasek postępu → pobranie. Zablokowany w trakcie przetwarzania.
- **Import:** „Importuj” na liście lub przeciągnięcie pliku na stronę. Po sprawdzeniu archiwum wybierasz: zapis dołączonego filmu do `videos/` (bez nadpisywania), miejsce w bibliotece oraz sposób obsługi duplikatu (po `uid`): kopia, zastąpienie lub pominięcie.
- **Wykład bez filmu** działa normalnie (notatki, czat, quizy, wyszukiwarka, regeneracja notatek). Film wskażesz później w szczegółach („Wskaż plik wideo”); etapy czytające film wymagają go.
- **Bezpieczeństwo:** archiwum jest traktowane jak niezaufane. Odrzucane są ścieżki spoza katalogu wykładu, dowiązania symboliczne, wpisy zaszyfrowane, bomby kompresji, uszkodzone sumy CRC i niepoprawny JSON; rozpakowywane są tylko znane pliki. Limit rozmiaru: `import.max_gb` (domyślnie 50).

## Linia poleceń (CLI)

```bash
python main.py wyklad.mp4
```

| Opcja | Znaczenie |
|-------|-----------|
| `--workdir DIR` | katalog wyników (domyślnie `work/<nazwa_filmu>`) |
| `--profile auto\|8gb\|16gb` | model LLM i `num_ctx` (domyślnie wykrywany z VRAM) |
| `--from-stage asr\|frames\|vlm\|llm` | zacznij od wskazanego etapu |
| `--only-stage ...` | uruchom tylko jeden etap |
| `--force` | wymuś ponowne wykonanie etapów |
| `--status` | wypisz stan etapów jako JSON |

Ctrl+C przerywa bezpiecznie (model zostaje rozładowany); ponowne uruchomienie wznawia pracę.

## Konfiguracja

Modele, progi i parametry są w `config.yaml`; serwer przeładowuje go sam po zmianie pliku, a wyniki wygenerowane wcześniej nie są przez to oznaczane jako nieaktualne. Po aktualizacji kodu zrestartuj serwer. Najczęściej dostraja się:

- `frames` – gdy wybiera za dużo lub za mało klatek (`ink_level`, `erase_fraction`, `save_new_ink_px`, `max_hold_s`),
- `vlm` – odporność odczytu tablicy (`max_side`, `num_ctx`, `reload_every`, `frame_retries`, `max_failed_frames`),
- `llm`, `chat`, `quiz`, `import`.

## Diagnostyka

**Błędy przetwarzania.** Aplikacja pokazuje przyczynę z podpowiedzią naprawy oraz „Szczegóły błędu i logi”. Logi w `work/`: `server.log` (serwer i kolejka); w `work/<film>/`: `pipeline.log`, `worker_<etap>.log` (stderr etapu, także awarie bibliotek natywnych), `job.out`, `worker_error.json`. Etapy VLM i LLM zachowują punkty kontrolne, więc wznowienie nie zaczyna od zera.

**HTTP 500 z Ollamy przy odczycie tablicy** zwykle oznacza upadek procesu modelu (brak VRAM/RAM, duży obraz). Etap VLM jest na to odporny: ponawia klatkę po odtworzeniu modelu i z mniejszym obrazem (100%, 75%, 55% `vlm.max_side`), odświeża model co `vlm.reload_every` klatek, a nieopisywalne klatki pomija (do `vlm.max_failed_frames` i 20% klatek). Po 5 kolejnych błędach etap zatrzymuje się z komunikatem. Gdy problem wraca, zmniejsz `max_side` (np. 1024) i sprawdź log Ollamy (`journalctl -u ollama`).

**Serwer przestał działać.** Każde zamknięcie zostawia ślad w `work/server.log` (sygnał, auto-exit, „Zakończ”, wyjątek), a co 10 s zapisywany jest stan w `work/server.state.json`. Po nagłym zakończeniu (np. zabicie procesu przy braku pamięci, uśpienie lub zamknięcie WSL) aplikacja po starcie pokazuje komunikat z czasem ostatniej aktywności. Awarie natywne zapisują traceback do `work/server.crash.log`. Zadania przetwarzania działają w osobnych procesach, więc przeżywają restart serwera.

## Testy i status

```bash
pytest -q tests
```

Testy nie wymagają GPU ani Ollamy (używają atrap) i działają także na macOS.

Sprawdzone na prawdziwym nagraniu: wykrywanie tablicy, kolejka, interfejs, czat. **Niesprawdzone na docelowym sprzęcie:** ASR, VLM i LLM na GPU, `start.bat`, instalacja PWA, druk do PDF, jakość pytań quizów. Jakość odczytu pisma odręcznego i polskich notatek zależy od modeli. Plan i znane ryzyka: `PLAN_IMPLEMENTACJI.md`, `PLAN_UI.md`.

Interfejs ma styl zeszytu akademickiego (jasny i ciemny motyw według systemu); czcionki Source Serif 4, IBM Plex Sans i IBM Plex Mono (licencja SIL OFL) są wgrane lokalnie, więc aplikacja działa offline. Ikony generuje `python tools/make_icons.py`.
