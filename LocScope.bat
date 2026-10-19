@echo off
rem Starts LocScope. Double-clicking LocScope.pyw does the same. (locscope_gui.pyw is the older classic window.)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0LocScope.pyw" %*) || (where pyw >nul 2>nul && (start "" pyw "%~dp0LocScope.pyw" %*) || (echo Python is not installed. Get it from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^). & pause))
