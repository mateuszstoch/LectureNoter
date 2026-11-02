@echo off
setlocal EnableExtensions
rem LectureNoter: uruchamia serwer w WSL (jesli nie dziala) i otwiera aplikacje w osobnym oknie.
rem Folder projektu jest wyznaczany z polozenia tego pliku (..\ wzgledem folderu scripts) i zamieniany na sciezke WSL,
rem wiec projekt moze lezec na dysku Windows (np. C:\Users\...\LectureNoter -> /mnt/c/Users/.../LectureNoter).
rem Zmienne opcjonalne:
rem   LECTURENOTER_DIR     sciezka projektu W WSL (nadpisuje automatyczne wykrywanie), np. /home/jan/LectureNoter
rem   LECTURENOTER_DISTRO  nazwa dystrybucji WSL, np. Ubuntu (gdy domyslna to np. docker-desktop)
rem Plik jest celowo bez polskich znakow (kodowanie konsoli Windows).

set "URL=http://localhost:8000"
for %%I in ("%~dp0..") do set "PROJECT_WIN=%%~fI"
set "PROJECT_FWD=%PROJECT_WIN:\=/%"
set "DISTRO_ARG="
if defined LECTURENOTER_DISTRO set "DISTRO_ARG=-d %LECTURENOTER_DISTRO%"

where wsl.exe >nul 2>&1
if errorlevel 1 goto nowsl
where curl.exe >nul 2>&1
if errorlevel 1 goto nocurl

curl.exe -s -o nul "%URL%/api/health"
if not errorlevel 1 goto open

set "PROJECT_WSL="
if defined LECTURENOTER_DIR set "PROJECT_WSL=%LECTURENOTER_DIR%"
if defined PROJECT_WSL goto havepath
for /f "usebackq delims=" %%P in (`wsl.exe %DISTRO_ARG% bash -lc "wslpath -u -a '%PROJECT_FWD%'"`) do set "PROJECT_WSL=%%P"
if not defined PROJECT_WSL goto nopath
:havepath

echo Projekt (Windows): %PROJECT_WIN%
echo Projekt (WSL):     %PROJECT_WSL%
echo Uruchamiam serwer w WSL. Okno "LectureNoter server" pokaze postep i ewentualne bledy.
echo Pierwsze uruchomienie instaluje zaleznosci i moze potrwac kilka minut. Nie zamykaj tego okna.
start "LectureNoter server" wsl.exe %DISTRO_ARG% bash -lc "cd '%PROJECT_WSL%' && python3 scripts/launch.py; echo; echo Serwer zakonczyl prace. Nacisnij Enter, aby zamknac okno.; read"

set /a tries=0
:wait
set /a tries+=1
if %tries% gtr 900 goto fail
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

:nopath
echo Nie udalo sie zamienic sciezki projektu na sciezke WSL:
echo   %PROJECT_WIN%
echo Sprawdz, czy WSL dziala (wsl -l -v). Gdy domyslna dystrybucja to np. docker-desktop,
echo ustaw LECTURENOTER_DISTRO=Ubuntu. Mozesz tez podac sciezke recznie: LECTURENOTER_DIR=/mnt/c/...
pause
exit /b 1

:fail
echo.
echo Serwer nie odpowiada po 15 minutach. Sprawdz komunikat w oknie "LectureNoter server".
pause
exit /b 1
