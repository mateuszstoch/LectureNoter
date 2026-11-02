@echo off
rem Uruchamia serwer LectureNoter w WSL (jesli jeszcze nie dziala) i otwiera aplikacje w oknie bez paska przegladarki.
rem Katalog projektu w WSL mozna zmienic zmienna LECTURENOTER_DIR (domyslnie ~/LectureNoter).
setlocal enabledelayedexpansion
set URL=http://localhost:8000
if "%LECTURENOTER_DIR%"=="" set LECTURENOTER_DIR=~/LectureNoter

curl.exe -s -o nul %URL%/api/health && goto open

echo Uruchamiam serwer w WSL...
start "LectureNoter server" /min wsl.exe bash -lc "cd %LECTURENOTER_DIR% && source .venv/bin/activate && python server.py"

set /a tries=0
:wait
set /a tries+=1
if !tries! gtr 60 (
  echo Serwer nie odpowiada po 60 s. Sprawdz okno "LectureNoter server".
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
curl.exe -s -o nul %URL%/api/health || goto wait

:open
where msedge >nul 2>&1 && (start "" msedge --app=%URL% & exit /b 0)
where chrome >nul 2>&1 && (start "" chrome --app=%URL% & exit /b 0)
start "" %URL%
