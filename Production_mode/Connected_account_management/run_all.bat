@echo off
REM ============================================================
REM  Lanceur unique Malovelycar : Phase_7 + Phase_8
REM  Ouvre deux fenetres, chacune tournant en continu (polling 60s)
REM  et redemarrant automatiquement en cas d'arret.
REM    - Phase_7 : envoi des liens de paiement
REM    - Phase_8 : confirmation auto + envoi du recu apres paiement
REM  Double-cliquer ce fichier, ou le mettre au demarrage Windows.
REM ============================================================

cd /d "%~dp0"

start "Phase_7 - Liens de paiement" cmd /c run_phase7.bat
start "Phase_8 - Confirmation + recu" cmd /c run_phase8.bat

echo Phase_7 et Phase_8 lances dans deux fenetres separees.
echo Vous pouvez fermer cette fenetre.
timeout /t 5 /nobreak >nul
exit
