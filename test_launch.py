import subprocess
import time
import urllib.request
import urllib.error
import json
import sys

def test():
    print("Starting launch_gui_game.py...")
    proc = subprocess.Popen([sys.executable, "launch_gui_game.py"])
    time.sleep(3)
    
    req = urllib.request.Request('http://127.0.0.1:8081/api/command', data=json.dumps({'cmd': 'ready'}).encode(), headers={'Content-Type': 'application/json'})
    try:
        print("Sending ready to P1...")
        resp = urllib.request.urlopen(req).read().decode()
        print("P1 Response:", resp)
    except urllib.error.HTTPError as e:
        print("P1 HTTP ERROR:", e.read().decode())
    except Exception as e:
        print("P1 ERROR:", e)

    req2 = urllib.request.Request('http://127.0.0.1:8082/api/command', data=json.dumps({'cmd': 'ready'}).encode(), headers={'Content-Type': 'application/json'})
    try:
        print("Sending ready to P2...")
        resp2 = urllib.request.urlopen(req2).read().decode()
        print("P2 Response:", resp2)
    except urllib.error.HTTPError as e:
        print("P2 HTTP ERROR:", e.read().decode())
    except Exception as e:
        print("P2 ERROR:", e)

    time.sleep(2)
    print("Terminating...")
    proc.terminate()
    proc.wait()

if __name__ == "__main__":
    test()
