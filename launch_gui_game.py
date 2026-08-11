import subprocess
import time
import webbrowser
import sys

def main():
    print("Starting server...")
    server = subprocess.Popen([sys.executable, "server.py"])
    time.sleep(1)
    
    print("Starting Client 1 (GUI port 8081)...")
    c1 = subprocess.Popen([sys.executable, "gui_client.py", "--id", "player_1", "--deck", "decks/burn.txt", "--gui-port", "8081"])
    
    print("Starting Client 2 (GUI port 8082)...")
    c2 = subprocess.Popen([sys.executable, "gui_client.py", "--id", "player_2", "--deck", "decks/control.txt", "--gui-port", "8082"])
    
    time.sleep(2)
    webbrowser.open("http://127.0.0.1:8081")
    webbrowser.open("http://127.0.0.1:8082")
    
    print("Game running. Press Ctrl+C to stop.")
    
    try:
        server.wait()
    except KeyboardInterrupt:
        print("\nStopping...")
        server.terminate()
        c1.terminate()
        c2.terminate()

if __name__ == "__main__":
    main()
