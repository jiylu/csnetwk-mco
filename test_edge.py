"""
test_edge.py — edge-case validation for MTGNP v1.0. Three seeded games:

GAME A (scripted 10-turn game, seat 0 first):
  E1  PLAY_LAND during Upkeep        -> ERROR WRONG_PHASE
  E2  unpayable CAST_SPELL           -> ERROR INSUFFICIENT_MANA
  E3  after E2 the server re-issues PRIORITY_GRANT with the SAME seq_num
  E4  attacking with a summoning-sick creature -> ERROR ILLEGAL_ACTION
  E5  FIRST_STRIKE_DAMAGE step runs when a first-striker fights
  E6  ASSIGN_DAMAGE_ORDER step runs when an attacker is double-blocked
  E7  damage-order math: Reckless Wurm (4/4) blocked by Bears (2/2) + Wall
      (0/8): Bears dies, Wall survives with exactly 2 damage, Wurm survives;
      Youthful Knight (2/1 first strike) kills its Bears blocker before
      taking damage and survives
  E8  Gray Merchant ETB trigger accepted -> drain 2 applied to life totals

GAME B: both decks 8 cards -> the 4th draw hits an empty library
  E9  GAME_OVER reason DECK_EMPTY with the correct winner

GAME C: --time-limit 1500 and an unresponsive priority holder
  E10 GAME_OVER reason DISCONNECT with the correct winner

The scripted turns need specific cards in hand on specific turns. The
server's shuffle is seeded, so we REPLICATE the shuffle locally (same
`random` module, same call order as server.run_setup) and search for a seed
whose deal satisfies every prerequisite, then launch the server with it.
"""
import json
import random
import socket
import struct
import subprocess
import sys
import threading
import time

from test_combat import Bot   # reuse the event-reactive bot

HOST = "127.0.0.1"

DECK1 = (["mountain_%03d" % i for i in range(1, 9)]
         + ["plains_001", "plains_002", "youthful_knight_001",
            "reckless_wurm_001", "lightning_bolt_001", "shock_001"])
DECK2 = (["forest_%03d" % i for i in range(1, 5)]
         + ["mountain_001", "mountain_002", "swamp_001", "swamp_002",
            "grizzly_bears_001", "grizzly_bears_002", "wall_of_stone_001",
            "gray_merchant_001", "island_001", "island_002"])


# ---------------------------------------------------------------------------
# Seed search: replicate server.run_setup's RNG usage (shuffle seat 0's
# library, then seat 1's; --first skips the coin-flip randint).
# ---------------------------------------------------------------------------
def deal(seed):
    random.seed(seed)
    a, b = DECK1[:], DECK2[:]
    random.shuffle(a)
    random.shuffle(b)
    return a, b


def cnt(lst, k, base):
    return sum(1 for c in lst[:k] if c.startswith(base + "_"))


def prereq(a, b):
    # P1 sees 7 cards on T1, +1 each of its turns (draws T3,5,7,9).
    p1 = (cnt(a, 7, "mountain") >= 1
          and cnt(a, 8, "plains") >= 1 and "youthful_knight_001" in a[:8]
          and "reckless_wurm_001" in a[:9] and cnt(a, 9, "mountain") >= 2
          and cnt(a, 10, "mountain") >= 3)
    # P2 sees 7+1 on T2, then +1 on T4,6,8,10.
    p2 = (cnt(b, 8, "forest") >= 1
          and cnt(b, 9, "forest") >= 2 and cnt(b, 9, "grizzly_bears") >= 1
          and cnt(b, 10, "mountain") >= 1 and "wall_of_stone_001" in b[:10]
          and cnt(b, 11, "swamp") >= 1 and cnt(b, 11, "grizzly_bears") >= 2
          and cnt(b, 12, "swamp") >= 2 and "gray_merchant_001" in b[:12])
    return p1 and p2


def find_seed():
    for s in range(5000):
        if prereq(*deal(s)):
            return s
    raise RuntimeError("no suitable seed found")


# ---------------------------------------------------------------------------
# EdgeBot: adds damage-order handling, unique blocker instances, and
# re-declaring attackers after an ILLEGAL_ACTION rejection.
# ---------------------------------------------------------------------------
class EdgeBot(Bot):
    def __init__(self, *args, orders=None, **kw):
        super().__init__(*args, **kw)
        self.orders = orders or {}       # {turn: (attacker_base, [blk bases])}
        self.req_tok = None

    def find_unused(self, base, coll, used):
        for cid in coll:
            if cid.startswith(base + "_") and cid not in used:
                return cid
        return None

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
            for pid, perms in bf.items():
                if pid == self.me:
                    self.battlefield = {p["id"]: p for p in perms}
                else:
                    self.opp_battlefield = {p["id"]: p for p in perms}
            if st.get("phase") == "CLEANUP" and len(self.hand) > 7:
                self.send({"type": "DISCARD", "seq_num": pdu["seq_num"],
                           "card_ids": self.hand[:len(self.hand) - 7]})
        elif t == "PHASE_TRANSITION":
            self.phase = pdu["to_phase"]
            self.turn = pdu["turn"]
            self.active = pdu["active_player"]
            self.req_tok = pdu["seq_num"]
            opp = "player_2" if self.me == "player_1" else "player_1"
            if self.phase == "DECLARE_ATTACKERS" and self.active == self.me:
                atk = [{"creature_id": self.find(b, self.battlefield),
                        "target": opp}
                       for b in self.attacks.get(self.turn, [])]
                atk = [a for a in atk if a["creature_id"]]
                self.send({"type": "DECLARE_ATTACKERS",
                           "seq_num": pdu["seq_num"], "attackers": atk})
            elif self.phase == "DECLARE_BLOCKERS" and self.active != self.me:
                used, bl = set(), []
                for b_base, a_base in self.blocks.get(self.turn, []):
                    b = self.find_unused(b_base, self.battlefield, used)
                    a = self.find(a_base, self.opp_battlefield)
                    if b and a:
                        used.add(b)
                        bl.append({"creature_id": b, "blocking_id": a})
                self.send({"type": "DECLARE_BLOCKERS",
                           "seq_num": pdu["seq_num"], "blockers": bl})
            elif (self.phase == "ASSIGN_DAMAGE_ORDER"
                    and self.active == self.me):
                atk_base, blk_bases = self.orders[self.turn]
                atk = self.find(atk_base, self.battlefield)
                used, order = set(), []
                for b in blk_bases:
                    cid = self.find_unused(b, self.opp_battlefield, used)
                    used.add(cid)
                    order.append(cid)
                self.send({"type": "ASSIGN_DAMAGE_ORDER",
                           "seq_num": pdu["seq_num"],
                           "attacker_id": atk, "blocker_order": order})
        elif t == "STACK_PUSH":
            self.live_stack.append((pdu["stack_item_id"], pdu["controller"]))
        elif t == "STACK_RESOLVE":
            self.live_stack = [s for s in self.live_stack
                               if s[0] != pdu["stack_item_id"]]
        elif t == "PRIORITY_GRANT":
            key = (self.turn, self.phase)
            todo = self.script.get(key, [])
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
        elif t == "ERROR":
            # summoning-sick attack rejected: re-declare with no attackers
            if (pdu.get("code") == "ILLEGAL_ACTION"
                    and self.phase == "DECLARE_ATTACKERS"
                    and self.active == self.me):
                self.send({"type": "DECLARE_ATTACKERS",
                           "seq_num": self.req_tok, "attackers": []})
        elif t == "GAME_OVER":
            self.done.set()


def ok(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        sys.exit(1)


def start_server(port, *extra):
    srv = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), *extra],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return srv


# ---------------------------------------------------------------------------
# GAME A — scripted mechanics game
# ---------------------------------------------------------------------------
def game_a():
    global_port = 4777
    seed = find_seed()
    print(f"[game A] using seed {seed}")
    import test_combat
    test_combat.PORT = global_port     # Bot reads module-level PORT

    srv = start_server(global_port, "--seed", str(seed), "--first", "0")
    try:
        p1 = EdgeBot("player_1", DECK1,
                     script={
                         (1, "UPKEEP"): [("raw", {"type": "PLAY_LAND",
                                                  "card_id": "mountain_001"})],
                         (1, "PRECOMBAT_MAIN"): [("play", "mountain")],
                         (3, "PRECOMBAT_MAIN"): [("play", "plains"),
                                                 ("cast", "youthful_knight",
                                                  None)],
                         (5, "PRECOMBAT_MAIN"): [
                             ("play", "mountain"),
                             ("raw", {"type": "CAST_SPELL",
                                      "card_id": "reckless_wurm_001",
                                      "targets": [],
                                      "mana_payment": {"R": 4}})],
                         (7, "PRECOMBAT_MAIN"): [("play", "mountain"),
                                                 ("cast", "reckless_wurm",
                                                  None)],
                     },
                     attacks={7: ["reckless_wurm"],          # sick -> E4
                              9: ["reckless_wurm", "youthful_knight"]},
                     orders={9: ("reckless_wurm",
                                 ["grizzly_bears", "wall_of_stone"])})
        p2 = EdgeBot("player_2", DECK2,
                     script={
                         (2, "PRECOMBAT_MAIN"): [("play", "forest")],
                         (4, "PRECOMBAT_MAIN"): [("play", "forest"),
                                                 ("cast", "grizzly_bears",
                                                  None)],
                         (6, "PRECOMBAT_MAIN"): [("play", "mountain"),
                                                 ("cast", "wall_of_stone",
                                                  None)],
                         (8, "PRECOMBAT_MAIN"): [("play", "swamp"),
                                                 ("cast", "grizzly_bears",
                                                  None)],
                         (10, "PRECOMBAT_MAIN"): [("play", "swamp"),
                                                  ("cast", "gray_merchant",
                                                   None)],
                     },
                     blocks={9: [("grizzly_bears", "reckless_wurm"),
                                 ("wall_of_stone", "reckless_wurm"),
                                 ("grizzly_bears", "youthful_knight")]})
        p1.start()
        p1.connected.wait(10)
        time.sleep(0.3)
        p2.start()

        deadline = time.time() + 120
        while time.time() < deadline and p1.turn < 11 and not p1.done.is_set():
            time.sleep(0.5)

        ev = p1.events
        errs = [e for e in ev if e["type"] == "ERROR"]
        ok(any(e["code"] == "WRONG_PHASE" for e in errs),
           "E1 land during Upkeep rejected with WRONG_PHASE")
        ok(any(e["code"] == "INSUFFICIENT_MANA" for e in errs),
           "E2 unpayable cast rejected with INSUFFICIENT_MANA")

        # E3: the grant following the INSUFFICIENT_MANA error re-uses the
        # same seq_num as the grant that preceded the rejected cast.
        last_grant, regrant_same = None, False
        for e in ev:
            if e["type"] == "PRIORITY_GRANT":
                if regrant_same is None:            # first grant after error
                    regrant_same = (e["seq_num"] == last_grant)
                    break
                last_grant = e["seq_num"]
            elif e["type"] == "ERROR" and e["code"] == "INSUFFICIENT_MANA":
                regrant_same = None
        ok(regrant_same is True,
           "E3 PRIORITY_GRANT re-issued with the SAME seq_num after rejection")

        ok(any(e["code"] == "ILLEGAL_ACTION" and "attack" in e["message"]
               for e in errs),
           "E4 summoning-sick attacker rejected with ILLEGAL_ACTION")

        phases = [e["to_phase"] for e in ev if e["type"] == "PHASE_TRANSITION"]
        ok("ASSIGN_DAMAGE_ORDER" in phases,
           "E6 ASSIGN_DAMAGE_ORDER step ran for the double-blocked attacker")
        ok("FIRST_STRIKE_DAMAGE" in phases,
           "E5 FIRST_STRIKE_DAMAGE step ran for the first-striker")

        died = set()
        for e in ev:
            if e["type"] == "COMBAT_DAMAGE_RESULT":
                died |= set(e.get("creatures_died", []))
        ok({"grizzly_bears_001", "grizzly_bears_002"} <= died,
           "E7a both Grizzly Bears died in combat")
        wall_dmg2 = wurm_alive = knight_alive = False
        for e in ev:
            if e["type"] != "GAME_STATE_UPDATE":
                continue
            bf = e.get("state", {}).get("battlefield", {})
            for perms in bf.values():
                for p in perms:
                    if p["id"] == "wall_of_stone_001" and p.get("damage") == 2:
                        wall_dmg2 = True
            if e.get("state", {}).get("turn") == 10:
                flat = [p["id"] for perms in bf.values() for p in perms]
                wurm_alive = "reckless_wurm_001" in flat
                knight_alive = "youthful_knight_001" in flat
                wall_alive = "wall_of_stone_001" in flat
        ok(wall_dmg2, "E7b Wall of Stone took exactly 2 overflow damage")
        ok(wurm_alive and knight_alive and wall_alive,
           "E7c Wurm, Knight and Wall all survived combat")

        drains = [e for e in ev if e["type"] == "GAME_STATE_UPDATE"
                  and e["state"].get("life_totals")
                  == {"player_1": 18, "player_2": 22}]
        ok(bool(drains),
           "E8 Gray Merchant drain applied: life 18 / 22 after trigger")
        p1.done.set()
        p2.done.set()
    finally:
        srv.terminate()


# ---------------------------------------------------------------------------
# GAME B — deck-out loss
# ---------------------------------------------------------------------------
def game_b():
    port = 4778
    import test_combat
    test_combat.PORT = port
    srv = start_server(port, "--first", "0")
    try:
        deck = ["mountain_%03d" % i for i in range(1, 9)]     # 8 cards
        p1 = EdgeBot("player_1", deck[:], script={})
        p2 = EdgeBot("player_2", deck[:], script={})
        p1.start()
        p1.connected.wait(10)
        time.sleep(0.3)
        p2.start()
        ok(p1.done.wait(60), "GAME B finished")
        over = [e for e in p1.events if e["type"] == "GAME_OVER"][0]
        ok(over["reason"] == "DECK_EMPTY" and over["winner_id"] == "player_1",
           "E9 drawing from an empty library loses: DECK_EMPTY, "
           "player_1 (seat that skipped the first draw) wins")
    finally:
        srv.terminate()


# ---------------------------------------------------------------------------
# GAME C — priority timeout treated as disconnect
# ---------------------------------------------------------------------------
class SilentBot(EdgeBot):
    """Never answers PRIORITY_GRANT — used to trip the RFC 4.2 deadline."""
    def step(self, cat):
        pdu = self.recv()
        self.events.append(pdu)
        t = pdu["type"]
        if t == "GAME_STATE_UPDATE":
            st = pdu.get("state", {})
            if st.get("phase") == "MULLIGAN" and self.me in st.get("hand", {}):
                self.send({"type": "MULLIGAN_CHOICE", "seq_num": pdu["seq_num"],
                           "keep": True, "cards_to_bottom": []})
        elif t == "GAME_OVER":
            self.done.set()
        # PRIORITY_GRANT: deliberately ignored


def game_c():
    port = 4779
    import test_combat
    test_combat.PORT = port
    srv = start_server(port, "--first", "0", "--time-limit", "1500")
    try:
        deck = ["mountain_%03d" % i for i in range(1, 11)]
        p1 = SilentBot("player_1", deck[:], script={})
        p2 = EdgeBot("player_2", deck[:], script={})
        p1.start()
        p1.connected.wait(10)
        time.sleep(0.3)
        p2.start()
        ok(p1.done.wait(30), "GAME C finished")
        over = [e for e in p1.events if e["type"] == "GAME_OVER"][0]
        ok(over["reason"] == "DISCONNECT" and over["winner_id"] == "player_2",
           "E10 unresponsive priority holder times out: DISCONNECT, "
           "opponent wins")
    finally:
        srv.terminate()


# ---------------------------------------------------------------------------
# GAME D — simultaneous death triggers exercise TRIGGER_ORDER (RFC 8.6.2)
# ---------------------------------------------------------------------------
D_DECK1 = (["mountain_%03d" % i for i in range(1, 10)]
           + ["reckless_wurm_001", "lightning_bolt_001", "shock_001"])
D_DECK2 = (["swamp_%03d" % i for i in range(1, 10)]
           + ["festering_imp_001", "festering_imp_002", "healing_salve_001"])


def deal_d(seed):
    random.seed(seed)
    a, b = D_DECK1[:], D_DECK2[:]
    random.shuffle(a)
    random.shuffle(b)
    return a, b


def prereq_d(a, b):
    # P1 plays a mountain on T1/3/5/7 and casts the Wurm on T7 (seen 10).
    p1 = (cnt(a, 7, "mountain") >= 1 and cnt(a, 8, "mountain") >= 2
          and cnt(a, 9, "mountain") >= 3 and cnt(a, 10, "mountain") >= 4
          and "reckless_wurm_001" in a[:10])
    # P2 plays a swamp + an imp on T2 and T4 (seen 8 / 9).
    p2 = (cnt(b, 8, "swamp") >= 1 and cnt(b, 8, "festering_imp") >= 1
          and cnt(b, 9, "swamp") >= 2 and cnt(b, 9, "festering_imp") >= 2)
    return p1 and p2


class OrderBot(EdgeBot):
    """EdgeBot that also answers TRIGGER_ORDER with the offered order."""
    def step(self, cat):
        pdu = self.recv()
        if pdu["type"] == "TRIGGER_ORDER":
            self.events.append(pdu)
            self.send({"type": "TRIGGER_ORDER_RESPONSE",
                       "seq_num": pdu["seq_num"],
                       "ordered_trigger_ids": pdu["trigger_ids"]})
            return
        # replay the already-received PDU through the parent's handler
        real_recv = self.recv
        self.recv = lambda: pdu
        try:
            EdgeBot.step(self, cat)
        finally:
            self.recv = real_recv


def game_d():
    port = 4780
    import test_combat
    test_combat.PORT = port
    for s in range(5000):
        if prereq_d(*deal_d(s)):
            seed = s
            break
    print(f"[game D] using seed {seed}")
    srv = start_server(port, "--seed", str(seed), "--first", "0")
    try:
        p1 = OrderBot("player_1", D_DECK1,
                      script={(1, "PRECOMBAT_MAIN"): [("play", "mountain")],
                              (3, "PRECOMBAT_MAIN"): [("play", "mountain")],
                              (5, "PRECOMBAT_MAIN"): [("play", "mountain")],
                              (7, "PRECOMBAT_MAIN"): [("play", "mountain"),
                                                      ("cast", "reckless_wurm",
                                                       None)]},
                      attacks={9: ["reckless_wurm"]},
                      orders={9: ("reckless_wurm",
                                  ["festering_imp", "festering_imp"])})
        p2 = OrderBot("player_2", D_DECK2,
                      script={(2, "PRECOMBAT_MAIN"): [("play", "swamp"),
                                                      ("cast", "festering_imp",
                                                       None)],
                              (4, "PRECOMBAT_MAIN"): [("play", "swamp"),
                                                      ("cast", "festering_imp",
                                                       None)]},
                      blocks={9: [("festering_imp", "reckless_wurm"),
                                  ("festering_imp", "reckless_wurm")]})
        p1.start()
        p1.connected.wait(10)
        time.sleep(0.3)
        p2.start()
        deadline = time.time() + 120
        while time.time() < deadline and p1.turn < 10 and not p1.done.is_set():
            time.sleep(0.5)

        tord = [e for e in p2.events if e["type"] == "TRIGGER_ORDER"]
        ok(tord and len(tord[0]["trigger_ids"]) == 2,
           "E11 TRIGGER_ORDER sent for two simultaneous death triggers")
        pushes = [e for e in p1.events if e["type"] == "STACK_PUSH"
                  and e.get("item_type") == "TRIGGER_ABILITY"]
        ok(len(pushes) >= 2,
           "E12 both ordered triggers were pushed as TRIGGER_ABILITY items")
        drained = [e for e in p1.events if e["type"] == "GAME_STATE_UPDATE"
                   and e["state"].get("life_totals", {}).get("player_1") == 18]
        ok(bool(drained),
           "E13 both death triggers resolved: player_1 lost 1 life twice")
        died = set()
        for e in p1.events:
            if e["type"] == "COMBAT_DAMAGE_RESULT":
                died |= set(e.get("creatures_died", []))
        ok({"festering_imp_001", "festering_imp_002"} <= died,
           "E14 both Festering Imps died to the ordered damage assignment")
        p1.done.set()
        p2.done.set()
    finally:
        srv.terminate()


if __name__ == "__main__":
    game_a()
    game_b()
    game_c()
    game_d()
    print("\nALL EDGE-CASE TESTS PASSED")
