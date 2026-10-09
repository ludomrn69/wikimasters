@echo off
rem Lanceur du bot WikiMasters (Windows). Au premier lancement, il installe ce quil faut.
rem   wm login        ajoute un compte
rem   wm tout         simulation ; wm tout --execute pour de vrai
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python introuvable : installez-le depuis https://www.python.org/downloads/ en cochant "Add python.exe to PATH", puis relancez.
    exit /b 1
)
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" || (
    echo Python trop ancien : il faut 3.10 ou plus, https://www.python.org/downloads/
    exit /b 1
)
echo Premiere utilisation : installation (une minute)...
%PY% -m venv .venv || exit /b 1
:install
rem .venv\.installe est une copie de requirements.txt : si le fichier a change, on reinstalle.
fc /b requirements.txt ".venv\.installe" >nul 2>nul && goto run
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || exit /b 1
copy /y requirements.txt ".venv\.installe" >nul
:run
set "WM_CMD=wm"
".venv\Scripts\python.exe" wikimasters.py %*
exit /b %ERRORLEVEL%
