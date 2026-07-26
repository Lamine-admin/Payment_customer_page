@echo off
REM ============================================================
REM  Lanceur permanent de Phase_7 (envoi des liens de paiement)
REM  - Tourne en continu (boucle interne de Phase_7, polling 60s)
REM  - Redemarre automatiquement si le script s'arrete/plante
REM ============================================================

cd /d "%~dp0"
title Phase_7 - Liens de paiement Malovelycar

:loop
echo [%date% %time%] Demarrage de Phase_7...
python Phase_7.py
echo [%date% %time%] Phase_7 s'est arrete (code %errorlevel%). Redemarrage dans 15s...
timeout /t 15 /nobreak >nul
goto loop
