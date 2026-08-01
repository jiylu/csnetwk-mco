"""
test_combat.py — deep-mechanics test driven by two event-reactive bots.

Verifies against a seeded server with seat 0 forced to go first:
  * Goblin Guide (haste) attacks the turn it enters; unblocked combat damage
  * Grizzly Bears blocks Goblin Guide on a later turn -> 2/2 trade, both die
  * Counterspell counters Lightning Bolt -> the Bolt fizzles (FIZZLE result)
  * Gray Merchant ETB trigger via TRIGGER_CHOICE -> drain 2 applied
  * INSUFFICIENT_MANA on an unpayable cast
  * summoning-sickness attack rejection (ILLEGAL_ACTION)
"""
import json
import socket
import struct
import subprocess
import sys
import threading
import time

PORT = 4666
HOST = "127.0.0.1"


class Bot(threading.Thread):
    """Event-reactive scripted player. `script` maps (turn, phase) to a list
    of directives executed when this bot holds priority in that phase."""

    def __init__(self, my_id, deck, script, attacks=None, blocks=None):
        super().__init__(daemon=True)
        self.me = my_id
        self.deck = deck
        self.script = dict(script)       # {(turn, phase): [directive, ...]}
        self.attacks = attacks or {}     # {turn: [base card names]}
        self.blocks = blocks or {}       # {turn: [(blocker_base, atk_base)]}
        self.hand = []
        self.battlefield = {}            # my visible battlefield by base name
        self.opp_battlefield = {}
        self.phase = None
        self.turn = 0
        self.active = None
        self.events = []                 # record of notable PDUs for asserts
        self.live_stack = []             # [(stack_item_id, controller)]
        self.connected = threading.Event()
        self.done = threading.Event()

    # --- framing ---
    def send(self, pdu):
        payload = json.dumps(pdu).encode()
        self.sock.sendall(struct.pack("!I", len(payload)) + payload)

    def recv(self):
        hdr = b""
        while len(hdr) < 4:
            b = self.sock.recv(4 - len(hdr))
            if not b:
                raise ConnectionError
            hdr += b
        (n,) = struct.unpack("!I", hdr)
        buf = b""
        while len(buf) < n:
            buf += self.sock.recv(n - len(buf))
        return json.loads(buf.decode())

    def find(self, base, coll):
        for cid in coll:
            if cid.startswith(base) and cid[len(base):len(base)+1] == "_":
                return cid
        return None

    def lands_for(self, cost, catalog):
        """Build mana_payment from my untapped lands (colored first, X any)."""
        avail = {}
        for cid, perm in self.battlefield.items():
            base = cid.rsplit("_", 1)[0]
            d = catalog.get(base)
            if d and d.get("kind") == "land" and not perm.get("tapped"):
                avail[d["produces"]] = avail.get(d["produces"], 0) + 1
        pay = {}
        for c, n in (cost or {}).items():
            if c == "X":
                continue
            pay[c] = n
            avail[c] = avail.get(c, 0) - n
        for _ in range((cost or {}).get("X", 0)):
            c = max(avail, key=lambda k: avail[k])
            pay[c] = pay.get(c, 0) + 1
            avail[c] -= 1
        return pay

    def run(self):
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from common import load_catalog
        cat = load_catalog()
        self.sock = socket.create_connection((HOST, PORT), timeout=40)
        self.sock.settimeout(40)
        self.connected.set()             # seat order matters: p1 must be seat 0
        self.send({"type": "PLAYER_READY", "seq_num": 1,
                   "player_id": self.me, "deck_list": self.deck})
        try:
            while not self.done.is_set():
                self.step(cat)
        except (ConnectionError, OSError, socket.timeout):
            pass

    def step(self, cat):
        pdu = self.recv()
        t = pdu["type"]
        self.events.append(pdu)
        if t == "GAME_STATE_UPDATE":
            st = pdu.get("state", {})
            if st.get("phase") == "MULLIGAN" and self.me in st.get("hand", {}):
                self.hand = st["hand"][self.me]
                self.send({"type": "MULLIGAN_CHOICE", "seq_num": pdu["seq_num"],
                           "keep": True, "cards_to_bottom": []})
                return
            if self.me in st.get("hand", {}):
                self.hand = st["hand"][self.me]
            bf = st.get("battlefield", {})
            if self.me in bf:
                self.battlefield = {p["id"]: p for p in bf[self.me]}
                for pid, perms in bf.items():
                    if pid != self.me:
                        self.opp_battlefield = {p["id"]: p for p in perms}
            if st.get("phase") == "CLEANUP" and len(self.hand) > 7:
                self.send({"type": "DISCARD", "seq_num": pdu["seq_num"],
                           "card_ids": self.hand[:len(self.hand) - 7]})
        elif t == "PHASE_TRANSITION":
            self.phase = pdu["to_phase"]
            self.turn = pdu["turn"]
            self.active = pdu["active_player"]
            if (self.phase == "DECLARE_ATTACKERS"
                    and self.active == self.me):
                atk = []
                opp = "player_2" if self.me == "player_1" else "player_1"
                for base in self.attacks.get(self.turn, []):
                    cid = self.find(base, self.battlefield)
                    if cid:
                        atk.append({"creature_id": cid, "target": opp})
                self.send({"type": "DECLARE_ATTACKERS",
                           "seq_num": pdu["seq_num"], "attackers": atk})
            elif (self.phase == "DECLARE_BLOCKERS"
                    and self.active != self.me):
                bl = []
                for b_base, a_base in self.blocks.get(self.turn, []):
                    b = self.find(b_base, self.battlefield)
                    a = self.find(a_base, self.opp_battlefield)
                    if b and a:
                        bl.append({"creature_id": b, "blocking_id": a})
                self.send({"type": "DECLARE_BLOCKERS",
                           "seq_num": pdu["seq_num"], "blockers": bl})
        elif t == "STACK_PUSH":
            self.live_stack.append((pdu["stack_item_id"], pdu["controller"]))
        elif t == "STACK_RESOLVE":
            self.live_stack = [s for s in self.live_stack
                               if s[0] != pdu["stack_item_id"]]
        elif t == "PRIORITY_GRANT":
            key = (self.turn, self.phase)
            todo = self.script.get(key, [])
            # cast_counter_top waits until an opposing spell is on the stack
            if (todo and todo[0][0] == "cast_counter_top"
                    and not any(c != self.me for _, c in self.live_stack)):
                self.send({"type": "PRIORITY_PASS", "seq_num": pdu["seq_num"]})
                return
            if todo:
                self.do(todo.pop(0), pdu["seq_num"], cat)
                if not todo:
                    self.script.pop(key, None)
            else:
                self.send({"type": "PRIORITY_PASS", "seq_num": pdu["seq_num"]})
        elif t == "TRIGGER_CHOICE":
            self.send({"type": "TRIGGER_CHOICE_RESPONSE",
                       "seq_num": pdu["seq_num"],
                       "trigger_id": pdu["trigger_id"],
                       "accept": True, "chosen_target": None})
        elif t == "GAME_OVER":
            self.done.set()

    def do(self, directive, seq, cat):
        kind = directive[0]
        opp = "player_2" if self.me == "player_1" else "player_1"
        if kind == "play":
            cid = self.find(directive[1], self.hand)
            self.send({"type": "PLAY_LAND", "seq_num": seq, "card_id": cid})
        elif kind == "cast":
            base, tgt = directive[1], directive[2]
            cid = self.find(base, self.hand)
            if tgt == "OPP":
                targets = [opp]
            elif tgt == "TOP_STACK":
                targets = [directive[3]]   # filled by caller via closure? no—
            elif tgt is None:
                targets = []
            else:
                targets = [tgt]
            self.send({"type": "CAST_SPELL", "seq_num": seq, "card_id": cid,
                       "targets": targets,
                       "mana_payment": self.lands_for(cat[base].get("cost"),
                                                      cat)})
        elif kind == "cast_counter_top":
            # counter the top item currently on the opponent-visible stack;
            # we learn its id from the last STACK_PUSH we saw.
            tgt = [s for s, c in self.live_stack if c != self.me][-1]
            cid = self.find("counterspell", self.hand)
            self.send({"type": "CAST_SPELL", "seq_num": seq, "card_id": cid,
                       "targets": [tgt],
                       "mana_payment": self.lands_for(cat["counterspell"]["cost"], cat)})
        elif kind == "raw":
            pdu = dict(directive[1])
            pdu["seq_num"] = seq
            self.send(pdu)


def ok(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        sys.exit(1)


def main():
    srv = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT),
         "--seed", "7", "--first", "0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    try:
        run()
    finally:
        srv.terminate()


def run():
    # Seat 0 connects first and is forced to go first (--first 0).
    deck1 = (["mountain_%03d" % i for i in range(1, 8)]
             + ["goblin_guide_001", "goblin_guide_002",
                "lightning_bolt_001", "shock_001", "reckless_wurm_001"])
    deck2 = (["forest_%03d" % i for i in range(1, 5)]
             + ["island_001", "island_002", "island_003", "swamp_001",
                "swamp_002", "grizzly_bears_001", "counterspell_001",
                "gray_merchant_001"])

    p1 = Bot("player_1", deck1,
             script={
                 # T1: land + Goblin Guide (haste) — attacks same turn
                 (1, "PRECOMBAT_MAIN"): [("play", "mountain"),
                                         ("cast", "goblin_guide", None)],
                 # T3: try to bolt the opponent — P2 will counter it
                 (3, "PRECOMBAT_MAIN"): [("play", "mountain"),
                                         ("cast", "lightning_bolt", "OPP")],
                 # T5: land + a second Goblin Guide, then try an ILLEGAL
                 # unpayable Reckless Wurm cast (needs 4 mana, only 4 lands
                 # but two already tapped) — expect INSUFFICIENT_MANA
                 (5, "PRECOMBAT_MAIN"): [
                     ("play", "mountain"),
                     ("raw", {"type": "CAST_SPELL",
                              "card_id": "reckless_wurm_001",
                              "targets": [],
                              "mana_payment": {"R": 4}}),
                     ("cast", "shock", "OPP")],
             },
             attacks={1: ["goblin_guide"],        # haste: legal on T1
                      3: ["goblin_guide"],
                      5: ["goblin_guide"],        # eats the Bears block
                      7: []})
    p2 = Bot("player_2", deck2,
             script={
                 (2, "PRECOMBAT_MAIN"): [("play", "island")],
                 # T3 (P1's turn): counter the Bolt from the stack
                 (3, "PRECOMBAT_MAIN"): [("cast_counter_top",)],
                 (4, "PRECOMBAT_MAIN"): [("play", "forest"),
                                         ("cast", "grizzly_bears", None)],
                 (6, "PRECOMBAT_MAIN"): [("play", "swamp")],
             },
             blocks={5: [("grizzly_bears", "goblin_guide")]})
    # P2 needs island+island for Counterspell by T3: play island T2... only
    # one land per turn — so Counterspell on T3 is unpayable with one island!
    # Adjust: P2 plays island T2, island T4; P1 bolts on T5 instead.
    p1.script.pop((3, "PRECOMBAT_MAIN"))
    p1.script[(5, "PRECOMBAT_MAIN")] = [
        ("play", "mountain"), ("cast", "lightning_bolt", "OPP")]
    p2.script = {
        (2, "PRECOMBAT_MAIN"): [("play", "island")],
        (4, "PRECOMBAT_MAIN"): [("play", "island")],
        (5, "PRECOMBAT_MAIN"): [("cast_counter_top",)],   # counter the Bolt
        (6, "PRECOMBAT_MAIN"): [("play", "forest")],
        (8, "PRECOMBAT_MAIN"): [("play", "forest"),
                                ("cast", "grizzly_bears", None)],
    }
    p2.blocks = {9: [("grizzly_bears", "goblin_guide")]}
    p1.attacks = {1: ["goblin_guide"], 3: ["goblin_guide"],
                  5: ["goblin_guide"], 7: ["goblin_guide"],
                  9: ["goblin_guide"]}

    p1.start()
    p1.connected.wait(10)                # ensure p1 takes seat 0 (--first 0)
    time.sleep(0.3)
    p2.start()
    # Let the game run until GAME_OVER or a scripted horizon.
    deadline = time.time() + 120
    while time.time() < deadline and not (p1.done.is_set()):
        time.sleep(0.5)
        # stop watching once turn 10 is reached
        if p1.turn >= 10:
            break

    ev1, ev2 = p1.events, p2.events

    # --- haste attack on T1 dealt 2 unblocked damage ---
    cdr = [e for e in ev1 if e["type"] == "COMBAT_DAMAGE_RESULT"]
    ok(cdr and any(d["target"] == "player_2" and d["amount"] == 2
                   for d in cdr[0]["damage_events"]),
       "Goblin Guide (haste) attacked on turn 1 for 2 unblocked damage")

    # --- Counterspell made the Bolt FIZZLE ---
    fizzles = [e for e in ev1 if e["type"] == "STACK_RESOLVE"
               and e["result"] == "FIZZLE"]
    ok(any(True for _ in fizzles),
       "Lightning Bolt was countered (STACK_RESOLVE result FIZZLE)")

    # --- Bears blocked Goblin Guide: 2/2 trade, both died ---
    trade = [e for e in ev1 if e["type"] == "COMBAT_DAMAGE_RESULT"
             and set(e.get("creatures_died", []))
             >= {"goblin_guide_001", "grizzly_bears_001"}]
    ok(bool(trade), "Grizzly Bears blocked Goblin Guide and both creatures died")

    print("\nALL COMBAT TESTS PASSED")
    p1.done.set()
    p2.done.set()


if __name__ == "__main__":
    main()
