@echo off
setlocal
title HoraireManager - envoi de la mise a jour sur GitHub
cd /d "%~dp0"
echo.
echo  Envoi de app_horaire.py et CLAUDE.md sur GitHub (Tseli485/horaire-namur).
echo  La prod (PythonAnywhere) ne change PAS tant que vous n'avez pas fait git pull la-bas.
echo.
if exist ".git\index.lock" del /f /q ".git\index.lock"
git add app_horaire.py CLAUDE.md
git commit -F "_message_commit.txt"
if errorlevel 1 goto :echec
git push origin master
if errorlevel 1 goto :echec
del /f /q "_message_commit.txt"
echo.
echo  OK : mise a jour envoyee sur GitHub.
echo  Etape suivante sur PythonAnywhere (console Bash) : voir le message de Claude.
echo.
pause
exit /b 0
:echec
echo.
echo  ECHEC : rien n'a ete publie. Copiez ce message a Claude.
pause
exit /b 1
