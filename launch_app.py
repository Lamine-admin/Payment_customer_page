import subprocess
import webbrowser
import time
import os
import sys

def launch_app():
    # Chemin absolu vers le script principal
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_app.py")
    
    # Lancer Streamlit en arrière-plan
    process = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Attendre que le serveur démarre
    time.sleep(3)
    
    # Ouvrir le navigateur par défaut
    webbrowser.open("http://localhost:8501")
    
    print("🚀 L'application The Car Society est en cours de lancement...")
    print("🌐 Elle s'ouvrira automatiquement dans votre navigateur.")
    print("\nPour fermer l'application, fermez cette fenêtre.")
    
    try:
        # Garder la fenêtre ouverte
        process.wait()
    except KeyboardInterrupt:
        process.terminate()

if __name__ == "__main__":
    launch_app() 