@echo off
REM TalkWithMe launcher - starts uvicorn (which auto-starts TTS)
REM Double-click this .bat file. Console window stays open.
REM Press Ctrl+C in THIS window, then N to stop both servers.

setlocal

set TALKWITHME_DIR=C:\ai\TalkWithMe\TalkWithMe
set PYTHON=C:\Users\walde\AppData\Local\Programs\Python\Python313\python.exe
set PORT=8000

cd /d "%TALKWITHME_DIR%"

REM Fix PowerShell execution policy so TTS auto-start works
powershell -Command "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force" 2>nul

echo.
echo ============================================
echo   TalkWithMe Server Launcher
echo ============================================
echo.

REM Kill any stale processes on ports 8000 and 9000
echo Cleaning up any stale server processes...
for %%P in (8000 9000) do (
  for /f "tokens=5" %%T in ('netstat -a -n -o ^| findstr ":%%P .*LISTENING"') do (
    if not "%%T"=="0" (
      echo Killing stale process PID %%T on port %%P
      taskkill /PID %%T /F >nul 2>&1
    )
  )
)
REM Small delay to let ports free up
ping 127.0.0.1 -n 3 -w 1000 >nul 2>&1

echo.
echo Starting TalkWithMe server on port %PORT%...
echo TTS server will auto-start automatically.
echo.
echo To STOP: Press Ctrl+C in this window, then N
echo ============================================
echo.

"%PYTHON%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%

REM When uvicorn exits (Ctrl+C), lifespan cleans up TTS subprocess
echo.
echo TalkWithMe server stopped.
echo.
echo Press any key to close this window...
pause >nul
