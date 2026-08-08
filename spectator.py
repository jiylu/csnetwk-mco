"""
spectator.py — MTGNP v1.0 Spectator Client (Bonus Feature)

Connects to the server, receives GAME_STATE_UPDATEs and PHASE_TRANSITIONs,
and renders the game state without interacting.
"""
import argparse
import socket
import threading
import time

from common import DEFAULT_PORT, FramingError, recv_pdu, send_pdu, set_verbose

class Spectator:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        self.running = True
        self.lock = threading.Lock()
        
        self.ping_seq = 0
        self.pong_deadline = None

    def render(self, s):
        """Render the visible state."""
        print(f"\n================ turn {s.get('turn')} | "
              f"{s.get('phase')} | active: {s.get('active_player')}")
        print(f" life: {s.get('life_totals')}")
        for pid, perms in (s.get("battlefield") or {}).items():
            row = []
            for p in perms:
                tag = "T" if p.get("tapped") else "u"
                if "power" in p:
                    tag += f" {p['power']}/{p['toughness']}"
                    if p.get("damage"):
                        tag += f" dmg{p['damage']}"
                    if p.get("summoning_sick"):
                        tag += " sick"
                if p.get("auras"):
                    tag += f" auras={p['auras']}"
                row.append(f"{p['id']}({tag})")
            print(f" battlefield[{pid}]: {row}")
        if s.get("stack"):
            print(f" stack (top last): "
                  f"{[i['source'] for i in s['stack']]}")
        print(f" hands: {s.get('hand_counts')} | "
              f"libraries: {s.get('library_counts')}")

    def reader(self):
        """Read PDUs from the server."""
        while self.running:
            try:
                pdu = recv_pdu(self.sock, who="server")
            except FramingError as e:
                print(f"[spectator] framing error: {e}")
                continue
            except (ConnectionError, OSError):
                print("\n[spectator] disconnected from server.")
                self.running = False
                return

            t = pdu.get("type")
            if t == "PONG":
                with self.lock:
                    self.pong_deadline = None
            elif t == "GAME_STATE_UPDATE":
                self.render(pdu.get("visible_state", {}))
            elif t == "PHASE_TRANSITION":
                fr, to = pdu.get("from_phase"), pdu.get("to_phase")
                ap = pdu.get("active_player")
                print(f"\n--- Phase Transition: {fr} -> {to} (Active: {ap}) ---")
            elif t == "STACK_PUSH":
                print(f"--- Stack Push: {pdu.get('item_type')} from {pdu.get('controller')} ---")
            elif t == "STACK_RESOLVE":
                print(f"--- Stack Resolve: {pdu.get('result')} ---")
            elif t == "GAME_OVER":
                print(f"\n*** GAME OVER: {pdu.get('winner_id')} wins! Reason: {pdu.get('reason')} ***")
                self.running = False

    def heartbeat(self):
        while self.running:
            time.sleep(30)
            if not self.running:
                return
            with self.lock:
                self.ping_seq += 1
                self.pong_deadline = time.monotonic() + 10
                seq = self.ping_seq
            try:
                send_pdu(self.sock, {"type": "PING", "seq_num": seq,
                                     "timestamp": int(time.time() * 1000)},
                         who="server", lock=self.lock)
            except OSError:
                self.running = False
                return
            time.sleep(10)
            with self.lock:
                dead = (self.pong_deadline is not None
                        and time.monotonic() > self.pong_deadline)
            if dead:
                print("\n[spectator] no PONG within 10s — disconnecting.")
                self.running = False
                try:
                    self.sock.close()
                except OSError:
                    pass
                return

    def run(self):
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        print(f"Connected to {self.host}:{self.port} as Spectator.")
        print("Press Ctrl+C to exit.")
        while self.running:
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                break
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

def main():
    ap = argparse.ArgumentParser(description="MTGNP v1.0 Spectator Client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print every PDU sent and received")
    args = ap.parse_args()
    set_verbose(args.verbose)
    Spectator(args.host, args.port).run()

if __name__ == "__main__":
    main()
