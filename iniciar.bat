@echo off
setlocal
cd /d "%~dp0"

set "PY="
for %%C in (python py) do (
  if not defined PY (
    %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=%%C"
  )
)
if not defined PY (
  echo Falta Python 3.10 o superior ^(o la version instalada es mas antigua^).
  echo Instalalo desde https://www.python.org/downloads/ ^(marca "Add python.exe to PATH"^) y vuelve a ejecutar iniciar.bat
  pause
  exit /b 1
)

echo %CD% | findstr /i "OneDrive" >nul && (
  echo AVISO: esta carpeta esta dentro de OneDrive. Muevela a una ruta corta y sin sincronizar, por ejemplo C:\proyectos\baitd, para evitar errores al instalar.
)

if not exist ".venv\Scripts\python.exe" (
  echo Creando el entorno ^(.venv^)...
  %PY% -m venv .venv || goto :fail
)
echo Instalando dependencias ^(la primera vez tarda unos minutos^)...
".venv\Scripts\python.exe" -m pip install --quiet -e . || goto :fail
".venv\Scripts\python.exe" -m baitd %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo Baitd se cerro con un error ^(codigo %RC%^). Revisa el mensaje de arriba.
  pause
)
exit /b %RC%

:fail
echo.
echo Algo fallo al preparar el entorno. Si la carpeta esta en una ruta muy larga, muevela a una corta como C:\proyectos\baitd.
pause
exit /b 1
