@echo off
setlocal EnableExtensions
rem LectureNoter: uruchamia serwer w WSL (jesli nie dziala) i otwiera aplikacje w osobnym oknie.
rem Zmienne opcjonalne (ustaw przed uruchomieniem lub w systemie):
rem   LECTURENOTER_DIR     katalog projektu w WSL, domyslnie ~/LectureNoter (bez spacji)
rem   LECTURENOTER_DISTRO  nazwa dystrybucji WSL, np. Ubuntu (gdy domyslna to np. docker-desktop)
rem Plik jest celowo bez polskich znakow (kodowanie konsoli Windows).

set "URL=http://localhost:8000"
if not defined LECTURENOTER_DIR set "LECTURENOTER_DIR=~/LectureNoter"
set "DISTRO_ARG="
if defined LECTURENOTER_DISTRO set "DISTRO_ARG=-d %LECTURENOTER_DISTRO%"

where wsl.exe >nul 2>&1
if errorlevel 1 goto nowsl
where curl.exe >nul 2>&1
if errorlevel 1 goto nocurl

curl.exe -s -o nul "%URL%/api/health"
if not errorlevel 1 goto open

echo Uruchamiam serwer w WSL (katalog: %LECTURENOTER_DIR%)...
echo Okno "LectureNoter server" pokaze ewentualne bledy. Nie zamykaj go, dopoki uzywasz aplikacji.
start "LectureNoter server" wsl.exe %DISTRO_ARG% bash -lc "cd %LECTURENOTER_DIR% && source .venv/bin/activate && python server.py; echo; echo Serwer zakonczyl prace. Nacisnij Enter, aby zamknac okno.; read"

set /a tries=0
:wait
set /a tries+=1
if %tries% gtr 90 goto fail
timeout /t 1 /nobreak >nul
curl.exe -s -o nul "%URL%/api/health"
if errorlevel 1 goto wait

:open
rem Edge/Chrome nie sa w PATH, wiec szukamy ich standardowych lokalizacji (bez blokow w nawiasach: w sciezkach sa nawiasy)
set "BROWSER="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "BROWSER=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER goto defaultbrowser
start "" "%BROWSER%" --app=%URL%
exit /b 0

:defaultbrowser
start "" "%URL%"
exit /b 0

:nowsl
echo Nie znaleziono wsl.exe. Zainstaluj WSL2 (PowerShell jako administrator: wsl --install).
pause
exit /b 1

:nocurl
echo Nie znaleziono curl.exe (Windows 10 1803+ i Windows 11 go maja). Zaktualizuj Windows.
pause
exit /b 1

:fail
echo.
echo Serwer nie odpowiada po 90 s. Sprawdz komunikat w oknie "LectureNoter server".
echo Najczestsze przyczyny: zly katalog projektu (ustaw LECTURENOTER_DIR), brak .venv,
echo inna domyslna dystrybucja WSL (ustaw LECTURENOTER_DISTRO), blad Pythona.
pause
exit /b 1
