import argparse
import sys
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
from client import Client, load_deck

logs = []
logs_lock = threading.Lock()

class LogInterceptor:
    def __init__(self, orig):
        self.orig = orig
        self.buf = ""
    def write(self, s):
        self.orig.write(s)
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            line = line.strip()
            if line:
                with logs_lock:
                    logs.append(line)
                    if len(logs) > 100:
                        logs.pop(0)
    def flush(self):
        self.orig.flush()

class GUIClient(Client):
    def __init__(self, host, port, player_id, deck, gui_port):
        super().__init__(host, port, player_id, deck)
        self.gui_port = gui_port
        
    def start_gui(self):
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        
        handler = make_handler(self)
        server = HTTPServer(('127.0.0.1', self.gui_port), handler)
        print(f"[gui] {self.player_id} GUI running on http://127.0.0.1:{self.gui_port}")
        server.serve_forever()

def make_handler(client_instance):
    class GUIHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                with open('gui/index.html', 'rb') as f:
                    self.wfile.write(f.read())
            elif self.path == '/api/state':
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                
                with client_instance.lock:
                    state_copy = dict(client_instance.state)
                    ptok = client_instance.priority_token
                    rtok = client_instance.request_token
                    trig = client_instance.trigger
                    torder = client_instance.trigger_order
                
                with logs_lock:
                    logs_copy = list(logs)
                
                resp = {
                    "player_id": client_instance.player_id,
                    "state": state_copy,
                    "priority_token": ptok,
                    "request_token": rtok,
                    "trigger": trig,
                    "trigger_order": torder,
                    "logs": logs_copy
                }
                self.wfile.write(json.dumps(resp).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
                
        def do_POST(self):
            if self.path == '/api/command':
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length > 0:
                    post_data = self.rfile.read(content_length)
                    try:
                        data = json.loads(post_data)
                        cmd = data.get("cmd", "")
                        if cmd:
                            client_instance.command(cmd)
                        self.send_response(200)
                        self.send_header('Content-type', 'application/json')
                        self.end_headers()
                        self.wfile.write(b'{"status": "ok"}')
                    except Exception as e:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(str(e).encode('utf-8'))
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
                
        def log_message(self, format, *args):
            # Disable default HTTP logging
            pass
            
    return GUIHandler

def main():
    ap = argparse.ArgumentParser(description="MTGNP v1.0 GUI Player Client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--id", required=True, help="your player_id")
    ap.add_argument("--deck", required=True, help="deck file")
    ap.add_argument("--gui-port", type=int, default=8080, help="port for the web GUI")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    
    import common
    common.set_verbose(args.verbose)
    
    sys.stdout = LogInterceptor(sys.stdout)
    
    gc = GUIClient(args.host, args.port, args.id, load_deck(args.deck), args.gui_port)
    gc.start_gui()

if __name__ == "__main__":
    main()
