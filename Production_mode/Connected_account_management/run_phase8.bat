@echo off
REM ============================================================
REM  Lanceur permanent de Phase_8 (confirmation auto + recu)
REM  - Tourne en continu (boucle interne de Phase_8, polling 60s)
REM  - Redemarre automatiquement si le script s'arrete/plante
REM  Double-cliquer ce fichier, ou le mettre au demarrage Windows.
REM ============================================================

cd /d "%~dp0"
title Phase_8 - Confirmation paiements Malovelycar

:loop
echo [%date% %time%] Demarrage de Phase_8...
python Phase_8.py
echo [%date% %time%] Phase_8 s'est arrete (code %errorlevel%). Redemarrage dans 15s...
timeout /t 15 /nobreak >nul
goto loop
