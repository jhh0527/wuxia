@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo [2_3_1_mp4ToSrt] PyInstaller 빌드 시작...

set "PROOT=%~dp0.."
for %%I in ("%PROOT%") do set "PROOT=%%~fI"

set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
)
if not defined PYEXE (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where python') do (
      set "PYEXE=%%I"
      goto :have_python
    )
  )
)
:have_python
if not defined PYEXE (
  echo Python을 찾을 수 없습니다.
  exit /b 1
)

echo 사용 중인 Python: "!PYEXE!"
"!PYEXE!" -m pip install -q -r "%~dp0requirements-build.txt"
if errorlevel 1 exit /b 1

set "DISTEXE=!PROOT!\dist\2_3_1_mp4ToSrt_gui.exe"
taskkill /F /IM 2_3_1_mp4ToSrt_gui.exe >nul 2>&1
taskkill /F /IM 2_7_mp4ToSrt_gui.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul
if exist "!DISTEXE!" del /F /Q "!DISTEXE!" >nul 2>&1
if exist "!PROOT!\dist\2_7_mp4ToSrt_gui.exe" del /F /Q "!PROOT!\dist\2_7_mp4ToSrt_gui.exe" >nul 2>&1
if exist "%~dp0work" rmdir /s /q "%~dp0work"

echo PyInstaller 실행...
"!PYEXE!" -m PyInstaller --clean --noconfirm --distpath "!PROOT!\dist" --workpath "%~dp0work" "%~dp0mp4_to_srt_gui.spec"
if errorlevel 1 exit /b 1

if not exist "!DISTEXE!" (
  echo dist\2_3_1_mp4ToSrt_gui.exe 를 찾을 수 없습니다.
  exit /b 1
)
powershell -NoProfile -Command "Unblock-File -LiteralPath '%DISTEXE%' -ErrorAction SilentlyContinue" >nul 2>&1
echo.
echo 완료: "!DISTEXE!"
echo Whisper 모델은 첫 실행 시 다운로드됩니다. ffmpeg 가 PATH에 있으면 MP4 입력에 필요합니다.
exit /b 0
