"""
client.py — MTGNP v1.0 Player Client (RFC 0001, CSNETWK).

Usage:
    python3 client.py --id player_1 --deck decks/burn.txt [--host H] [--port 4444] [--verbose]

The client is intentionally thin (RFC 4.3): it renders the Visible State the
server sends and translates keyboard commands into PDUs. It never computes
game outcomes locally; every GAME_STATE_UPDATE overwrites its local view.

Three threads:
  * reader    — receives PDUs, updates the local view, tracks the current
                priority token / request tokens, prints prompts
  * heartbeat — PING every 30 s; disconnects if no PONG within 10 s (RFC 4.3)
  * main      — reads commands from stdin and sends PDUs

Commands (type 'help' in-game):
  ready                 send PLAYER_READY with your deck
  keep [c1 c2 ...]      keep hand (list cards to bottom after mulligans)
  mull                  take a mulligan
  play <card>           play a land
  cast <card> [target]  cast a spell (mana payment auto-computed)
  pass                  pass priority
  attack [c1 c2 ...]    declare attackers (none = no attack)
  block c:a [c:a ...]   declare blockers, e.g. wall_of_stone_004:goblin_guide_001
  order <atk> b1 b2 ..  assign damage order for a multi-blocked attacker
  discard c1 [c2 ...]   discard down to 7 at cleanup
  yes / no              accept or decline an optional trigger
  torder t1 t2 ...      order your simultaneous triggers (last resolves first)
  hand / state          re-print your hand / the full visible state
  concede               concede the game
"""

import argparse
import socket
import sys
import threading
import time

from common import (DEFAULT_PORT, FramingError, base_name, card_def,
                    load_catalog, recv_pdu, send_pdu, set_verbose)


class Client:
    def __init__(self, host, port, player_id, deck):
        self.player_id = player_id
        self.deck = deck
        self.catalog = load_catalog()
        self.sock = socket.create_connection((host, port))
        self.send_lock = threading.Lock()
        self.lock = threading.Lock()          # guards the fields below
        self.state = {}                       # last visible state
        self.priority_token = None            # seq of last PRIORITY_GRANT
        self.request_token = None             # seq for mulligan/discard/etc.
        self.trigger = None                   # pending TRIGGER_CHOICE
        self.trigger_order = None             # pending TRIGGER_ORDER
        self.last_server_seq = 0              # for CONCEDE (RFC 5.4)
        self.ping_seq = 0
        self.ready_seq = 0
        self.pong_deadline = None
        self.running = True

    # ---------------- sending ----------------
    def send(self, pdu):
        send_pdu(self.sock, pdu, who="server", lock=self.send_lock)

    # ---------------- reader thread ----------------
    def reader(self):
        while self.running:
            try:
                pdu = recv_pdu(self.sock, who="server")
            except (ConnectionError, OSError, FramingError) as e:
                print(f"\n[client] connection lost: {e}")
                self.running = False
                return
            self.handle(pdu)

    def handle(self, pdu):
        t = pdu.get("type")
        with self.lock:
            if t != "PONG":
                self.last_server_seq = pdu.get("seq_num", self.last_server_seq)
        if t == "GAME_STATE_UPDATE":
            with self.lock:
                self.state = pdu.get("state", {})
                # A state update doubles as the request PDU for
                # MULLIGAN_CHOICE and DISCARD (RFC 5.4).
                if self.state.get("phase") in ("MULLIGAN", "CLEANUP"):
                    self.request_token = pdu["seq_num"]
            self.render()
        elif t == "PRIORITY_GRANT":
            with self.lock:
                self.priority_token = pdu["seq_num"]
            print(f"\n>>> You have priority (seq {pdu['seq_num']}, "
                  f"{pdu.get('time_limit_ms', 0)//1000}s). "
                  f"Commands: cast / play / pass / attack ...")
        elif t == "PHASE_TRANSITION":
            with self.lock:
                # DECLARE_ATTACKERS / DECLARE_BLOCKERS / ASSIGN_DAMAGE_ORDER
                # PDUs echo the PHASE_TRANSITION's seq_num (RFC 5.4).
                self.request_token = pdu["seq_num"]
                if "state" not in self.state:
                    pass
                self.state["phase"] = pdu.get("to_phase")
            print(f"\n--- {pdu.get('from_phase')} -> {pdu.get('to_phase')} "
                  f"(turn {pdu.get('turn')}, active: "
                  f"{pdu.get('active_player')}) ---")
        elif t == "TRIGGER_ORDER":
            with self.lock:
                self.trigger_order = pdu
            print(f"\n??? You have simultaneous triggers: "
                  f"{pdu.get('trigger_ids')}\n"
                  f"    Order them with: torder <id> <id> ... "
                  f"(last listed resolves first)")
        elif t == "TRIGGER_CHOICE":
            with self.lock:
                self.trigger = pdu
            print(f"\n??? Optional trigger from {pdu.get('source_id')}: "
                  f"{pdu.get('effect_summary')}  -> answer 'yes' or 'no'")
        elif t == "STACK_PUSH":
            print(f"\n[stack+] {pdu.get('controller')} put "
                  f"{pdu.get('source')} on the stack "
                  f"(targets: {pdu.get('targets')})")
        elif t == "STACK_RESOLVE":
            print(f"\n[stack-] {pdu.get('stack_item_id')} "
                  f"{pdu.get('result')}: {pdu.get('state_changes')}")
        elif t == "COMBAT_DAMAGE_RESULT":
            print(f"\n[combat] damage: {pdu.get('damage_events')} | "
                  f"life: {pdu.get('life_totals')} | "
                  f"died: {pdu.get('creatures_died')}")
        elif t == "GAME_OVER":
            print(f"\n===== GAME OVER: {pdu.get('winner_id')} wins "
                  f"({pdu.get('reason')}) =====\n"
                  f"Type 'ready' to play again on this connection.")
        elif t == "ERROR":
            print(f"\n[ERROR {pdu.get('code')}] {pdu.get('message')}")
        elif t == "PONG":
            with self.lock:
                self.pong_deadline = None

    # ---------------- rendering (RFC 4.3) ----------------
    def render(self):
        s = self.state
        if s.get("phase") == "LOBBY":
            print(f"\n[lobby] ready: {s.get('players_ready')} "
                  f"waiting for: {s.get('waiting_for')}")
            return
        if s.get("phase") == "GAME_SETUP":
            print("\n[setup] both players ready — server is dealing.")
            return
        me = self.player_id
        hand = s.get("hand", {}).get(me, [])
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
        print(f" your hand: {hand}")
        print(f" hands: {s.get('hand_counts')} | "
              f"libraries: {s.get('library_counts')}")

    # ---------------- heartbeat thread (RFC 4.3) ----------------
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
                self.send({"type": "PING", "seq_num": seq,
                           "timestamp": int(time.time() * 1000)})
            except OSError:
                self.running = False
                return
            time.sleep(10)
            with self.lock:
                dead = (self.pong_deadline is not None
                        and time.monotonic() > self.pong_deadline)
            if dead:
                print("\n[client] no PONG within 10s — disconnecting.")
                self.running = False
                self.sock.close()
                return

    # ---------------- mana auto-payment helper ----------------
    def auto_mana(self, card_id):
        """Build a mana_payment dict for a card from the untapped lands the
        server last showed us. Colored requirements first, then generic (X)
        from whatever colors remain."""
        d = card_def(self.catalog, card_id)
        cost = dict(d.get("cost") or {})
        avail = {}
        for p in (self.state.get("battlefield", {})
                  .get(self.player_id, [])):
            pd = card_def(self.catalog, p["id"])
            if pd and pd.get("kind") == "land" and not p.get("tapped"):
                avail[pd["produces"]] = avail.get(pd["produces"], 0) + 1
        pay = {}
        for color, n in cost.items():
            if color == "X":
                continue
            pay[color] = pay.get(color, 0) + n
            avail[color] = avail.get(color, 0) - n
        for _ in range(cost.get("X", 0)):
            c = max(avail, key=lambda k: avail[k], default=None)
            if c is None or avail[c] <= 0:
                break                      # let the server reject it
            pay[c] = pay.get(c, 0) + 1
            avail[c] -= 1
        return pay

    # ---------------- command loop ----------------
    def run(self):
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        print("Connected. Type 'ready' to join the game, 'help' for commands.")
        while self.running:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                continue
            try:
                self.command(line.strip())
            except Exception as e:            # never crash on bad input
                print(f"[client] command failed: {e}")
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def command(self, line):
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        with self.lock:
            ptok, rtok = self.priority_token, self.request_token
            last = self.last_server_seq
            trig = self.trigger

        if cmd == "help":
            print(__doc__)
        elif cmd == "ready":
            self.ready_seq += 1
            self.send({"type": "PLAYER_READY", "seq_num": self.ready_seq,
                       "player_id": self.player_id, "deck_list": self.deck})
        elif cmd == "keep":
            self.send({"type": "MULLIGAN_CHOICE", "seq_num": rtok,
                       "keep": True, "cards_to_bottom": args})
        elif cmd == "mull":
            self.send({"type": "MULLIGAN_CHOICE", "seq_num": rtok,
                       "keep": False, "cards_to_bottom": []})
        elif cmd == "play":
            self.send({"type": "PLAY_LAND", "seq_num": ptok,
                       "card_id": args[0]})
        elif cmd == "cast":
            card = args[0]
            targets = args[1:2]
            self.send({"type": "CAST_SPELL", "seq_num": ptok,
                       "card_id": card, "targets": targets,
                       "mana_payment": self.auto_mana(card)})
        elif cmd == "activate":
            src = args[0]
            ab_idx = int(args[1]) if len(args) > 1 else 0
            targets = args[2:3] if len(args) > 2 else []
            self.send({"type": "ACTIVATE_ABILITY", "seq_num": ptok,
                       "source_id": src, "ability_index": ab_idx, "targets": targets,
                       "mana_payment": self.auto_mana(src)}) # Just guess mana from source card if any
        elif cmd == "pass":
            self.send({"type": "PRIORITY_PASS", "seq_num": ptok})
        elif cmd == "attack":
            opp = [p for p in (self.state.get("life_totals") or {})
                   if p != self.player_id]
            tgt = opp[0] if opp else "?"
            self.send({"type": "DECLARE_ATTACKERS", "seq_num": rtok,
                       "attackers": [{"creature_id": c, "target": tgt}
                                     for c in args]})
        elif cmd == "block":
            blockers = []
            for pair in args:
                c, a = pair.split(":")
                blockers.append({"creature_id": c, "blocking_id": a})
            self.send({"type": "DECLARE_BLOCKERS", "seq_num": rtok,
                       "blockers": blockers})
        elif cmd == "order":
            self.send({"type": "ASSIGN_DAMAGE_ORDER", "seq_num": rtok,
                       "attacker_id": args[0], "blocker_order": args[1:]})
        elif cmd == "discard":
            self.send({"type": "DISCARD", "seq_num": rtok,
                       "card_ids": args})
        elif cmd == "torder":
            with self.lock:
                to = self.trigger_order
            if not to:
                print("[client] no pending trigger ordering")
                return
            self.send({"type": "TRIGGER_ORDER_RESPONSE",
                       "seq_num": to["seq_num"],
                       "ordered_trigger_ids": args})
            with self.lock:
                self.trigger_order = None
        elif cmd in ("yes", "no"):
            if not trig:
                print("[client] no pending trigger choice")
                return
            self.send({"type": "TRIGGER_CHOICE_RESPONSE",
                       "seq_num": trig["seq_num"],
                       "trigger_id": trig["trigger_id"],
                       "accept": cmd == "yes", "chosen_target": None})
            with self.lock:
                self.trigger = None
        elif cmd == "concede":
            self.send({"type": "CONCEDE", "seq_num": last,
                       "player_id": self.player_id})
        elif cmd == "hand":
            print(f"hand: {self.state.get('hand', {}).get(self.player_id)}")
        elif cmd == "state":
            self.render()
        elif cmd == "verbose":
            import common
            common.set_verbose(not common.VERBOSE)
            print(f"[client] verbose mode {'ON' if common.VERBOSE else 'OFF'}")
        elif cmd == "quit":
            self.running = False
        else:
            print(f"[client] unknown command '{cmd}' — try 'help'")


def load_deck(path):
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip()
                and not ln.startswith("#")]


def main():
    ap = argparse.ArgumentParser(description="MTGNP v1.0 Player Client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--id", required=True, help="your player_id")
    ap.add_argument("--deck", required=True,
                    help="deck file: one card instance ID per line")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print every PDU sent and received")
    args = ap.parse_args()
    set_verbose(args.verbose)
    Client(args.host, args.port, args.id, load_deck(args.deck)).run()


if __name__ == "__main__":
    main()
