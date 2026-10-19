@echo off
rem Builds LocScope.exe - a single file that runs on any Windows PC without Python.
rem Needs Python (python.org) on THIS PC only, to do the build. Takes a minute or two.
cd /d "%~dp0"
if not exist "locscope_app.py" if exist "locscope\locscope_app.py" cd /d "%~dp0locscope"
if not exist "locscope_app.py" (
  echo.
  echo Can't find locscope_app.py next to this file. This folder is:
  echo   %CD%
  echo.
  echo Most likely build_exe.bat was opened from INSIDE the zip. Windows then copies
  echo only this one file to a temp folder. Fix: right-click locscope.zip, choose
  echo "Extract All...", open the extracted folder and run build_exe.bat from there.
  echo All of these files must sit together: locscope.py, locscope_extras.py,
  echo locscope_more.py, locscope_plus.py, locscope_app.py, locscope_app.html, report_template.html, mapdata\
  echo.
  pause & exit /b 1
)
for %%F in (locscope_app.html locscope.py locscope_extras.py locscope_more.py locscope_plus.py report_template.html mapdata\world-countries-50m.json) do if not exist "%%F" (echo Missing %%F in %CD% - extract the whole zip again. & pause & exit /b 1)
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ & pause & exit /b 1)
python -m pip install --upgrade pyinstaller tzdata pillow
if errorlevel 1 (echo pip failed - see above. & pause & exit /b 1)
rem Optional: pywebview gives LocScope its own native window. If it can't install on this Python
rem (it needs pythonnet, which can lag behind the newest Python), LocScope opens in an Edge app window instead.
python -m pip install --upgrade pywebview
set WV=
python -c "import webview" >nul 2>nul && set WV=--collect-all webview --hidden-import clr
if not defined WV echo Note: pywebview isn't available on this Python, so the exe will open in an Edge app window. That's fine.
python -m PyInstaller --noconfirm --clean --onefile --windowed --name LocScope ^
  --add-data "report_template.html;." --add-data "locscope_app.html;." --add-data "mapdata;mapdata" ^
  --hidden-import locscope --hidden-import locscope_extras --hidden-import locscope_more --hidden-import locscope_plus --collect-data tzdata %WV% ^
  locscope_app.py
if errorlevel 1 (echo Build failed - see above. & pause & exit /b 1)
echo.
echo Done: %CD%\dist\LocScope.exe
echo Copy that one file to any PC. Settings and places are kept in %%APPDATA%%\LocScope on each PC.
pause
