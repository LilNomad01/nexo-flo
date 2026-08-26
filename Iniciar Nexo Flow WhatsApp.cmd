@echo off
setlocal
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js 20 ou mais recente nao foi encontrado.
  echo Instale em https://nodejs.org/ e execute este arquivo novamente.
  pause
  exit /b 1
)

if not exist ".env" copy /y ".env.example" ".env" >nul

if not exist "node_modules" (
  echo Instalando dependencias pela primeira vez...
  call npm install
  if errorlevel 1 (
    echo Nao foi possivel instalar as dependencias.
    pause
    exit /b 1
  )
)

start "" "http://127.0.0.1:3001"
echo Para encerrar a API, feche esta janela ou pressione Ctrl+C.
call npm start

if errorlevel 1 pause

