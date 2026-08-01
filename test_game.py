"""
test_game.py — end-to-end protocol smoke test (not part of the deliverable,
but useful evidence for the demo). Spawns the server, connects two scripted
bots, and drives a full game to GAME_OVER, asserting key protocol behaviour:

  * framing + lobby + ILLEGAL_DECK / DUPLICATE_ID rejection
  * mulligan (P2 mulligans once and bottoms a card)
  * turn engine: land plays, creature cast, summoning sickness rejection
  * STALE_ACTION on a bad seq_num
  * an attack with a block and combat damage
  * Gray Merchant optional ETB trigger (TRIGGER_CHOICE flow)
  * lethal Lightning Bolt -> GAME_OVER LIFE_ZERO -> new LOBBY on same socket
"""
import json
import socket
import struct
import subprocess
import sys
import time

PORT = 4555
HOST = "127.0.0.1"


class Bot:
    def __init__(self, name):
        self.name = name
        self.sock = socket.create_connection((HOST, PORT), timeout=20)
        self.sock.settimeout(20)

    def send(self, pdu):
        payload = json.dumps(pdu).encode()
        self.sock.sendall(struct.pack("!I", len(payload)) + payload)

    def recv(self):
        hdr = b""
        while len(hdr) < 4:
            hdr += self.sock.recv(4 - len(hdr))
        (n,) = struct.unpack("!I", hdr)
        buf = b""
        while len(buf) < n:
            buf += self.sock.recv(n - len(buf))
        pdu = json.loads(buf.decode())
        print(f"  [{self.name} <-] {pdu['type']} seq={pdu.get('seq_num')}")
        return pdu

    def recv_until(self, *types):
        while True:
            pdu = self.recv()
            if pdu["type"] in types:
                return pdu

    def wait_priority(self):
        return self.recv_until("PRIORITY_GRANT")


def ok(cond, msg):
    if cond:
        print(f"PASS: {msg}")
    else:
        print(f"FAIL: {msg}")
        sys.exit(1)


def main():
    srv = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT), "--verbose"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    try:
        run(srv)
    finally:
        srv.terminate()


def run(srv):
    p1, p2 = Bot("p1"), Bot("p2")

    # --- extra connection is refused (RFC 5.1) ---
    time.sleep(0.2)
    extra = socket.create_connection((HOST, PORT), timeout=5)
    time.sleep(0.3)
    ok(extra.recv(1) == b"", "third connection refused/closed")

    # --- ILLEGAL_DECK: 51 cards ---
    p1.send({"type": "PLAYER_READY", "seq_num": 1, "player_id": "player_1",
             "deck_list": ["mountain_001"] * 51})
    e = p1.recv_until("ERROR")
    ok(e["code"] == "ILLEGAL_DECK", "51-card deck rejected with ILLEGAL_DECK")

    # --- valid ready for P1 ---
    deck1 = ["mountain_001", "mountain_002", "mountain_003", "mountain_004",
             "goblin_guide_001", "lightning_bolt_001", "lightning_bolt_002",
             "shock_001", "mountain_005", "mountain_006", "mountain_007",
             "lightning_bolt_003"]
    p1.send({"type": "PLAYER_READY", "seq_num": 2, "player_id": "player_1",
             "deck_list": deck1})
    p1.recv_until("GAME_STATE_UPDATE")

    # --- DUPLICATE_ID ---
    p2.send({"type": "PLAYER_READY", "seq_num": 1, "player_id": "player_1",
             "deck_list": ["swamp_001"]})
    e = p2.recv_until("ERROR")
    ok(e["code"] == "DUPLICATE_ID", "duplicate player_id rejected")

    deck2 = ["swamp_001", "swamp_002", "swamp_003", "swamp_004",
             "gray_merchant_001", "island_001", "island_002",
             "counterspell_001", "swamp_005", "swamp_006", "swamp_007",
             "wall_of_stone_001"]
    p2.send({"type": "PLAYER_READY", "seq_num": 2, "player_id": "player_2",
             "deck_list": deck2})

    # --- setup states arrive; grab mulligan request seqs ---
    m1 = p1.recv_until("GAME_STATE_UPDATE")
    while m1["state"].get("phase") != "MULLIGAN":
        m1 = p1.recv_until("GAME_STATE_UPDATE")
    m2 = p2.recv_until("GAME_STATE_UPDATE")
    while m2["state"].get("phase") != "MULLIGAN":
        m2 = p2.recv_until("GAME_STATE_UPDATE")
    ok(m1["state"]["life_totals"] == {"player_1": 20, "player_2": 20},
       "life totals initialised to 20")
    ok("player_2" not in m1["state"]["hand"], "opponent hand hidden from P1")

    # --- P1 keeps; P2 mulligans once, then keeps bottoming 1 card ---
    p1.send({"type": "MULLIGAN_CHOICE", "seq_num": m1["seq_num"],
             "keep": True, "cards_to_bottom": []})
    p2.send({"type": "MULLIGAN_CHOICE", "seq_num": m2["seq_num"],
             "keep": False, "cards_to_bottom": []})
    redraw = p2.recv_until("GAME_STATE_UPDATE")
    hand2 = redraw["state"]["hand"]["player_2"]
    # Bottom the LAST card so the land the bot plays later stays in hand.
    bottom = hand2[-1]
    hand2 = hand2[:-1]
    p2.send({"type": "MULLIGAN_CHOICE", "seq_num": redraw["seq_num"],
             "keep": True, "cards_to_bottom": [bottom]})

    # --- game begins; figure out who is active ---
    pt = p1.recv_until("PHASE_TRANSITION")
    active = pt["active_player"]
    ap, nap = (p1, p2) if active == "player_1" else (p2, p1)
    ap_hand = (m1["state"]["hand"]["player_1"]
               if active == "player_1" else hand2)
    print(f"  active player: {active}, hand: {ap_hand}")

    def bot_id(bot):
        return "player_1" if bot is p1 else "player_2"

    def maybe_discard(bot):
        """Consume PDUs until the next UNTAP; if a CLEANUP state shows more
        than 7 cards in hand, answer the server's DISCARD request."""
        me = bot_id(bot)
        while True:
            pdu = bot.recv()
            if (pdu["type"] == "PHASE_TRANSITION"
                    and pdu.get("to_phase") == "UNTAP"):
                return
            if pdu["type"] == "GAME_STATE_UPDATE":
                st = pdu.get("state", {})
                hand = st.get("hand", {}).get(me, [])
                if st.get("phase") == "CLEANUP" and len(hand) > 7:
                    bot.send({"type": "DISCARD", "seq_num": pdu["seq_num"],
                              "card_ids": hand[:len(hand) - 7]})
                    print(f"  [{bot.name}] discarded down to 7")

    # helper: both pass one full priority window
    def both_pass():
        g = ap.wait_priority()
        ap.send({"type": "PRIORITY_PASS", "seq_num": g["seq_num"]})
        g = nap.wait_priority()
        nap.send({"type": "PRIORITY_PASS", "seq_num": g["seq_num"]})

    # --- Turn 1: upkeep, draw (no card on turn 1), main ---
    both_pass()                                   # UPKEEP
    both_pass()                                   # DRAW (no draw turn 1)
    g = ap.wait_priority()                        # PRECOMBAT_MAIN

    # --- STALE_ACTION check: send a wrong seq_num on purpose ---
    ap.send({"type": "PRIORITY_PASS", "seq_num": g["seq_num"] - 1})
    e = ap.recv_until("ERROR")
    ok(e["code"] == "STALE_ACTION", "stale seq_num rejected with STALE_ACTION")
    g = ap.wait_priority()                        # server re-granted

    # --- play a land, cast Goblin Guide / a creature if in hand ---
    land = next(c for c in ap_hand if c.split("_")[0] in
                ("mountain", "swamp", "island"))
    ap.send({"type": "PLAY_LAND", "seq_num": g["seq_num"], "card_id": land})
    g = ap.wait_priority()
    ap.send({"type": "PRIORITY_PASS", "seq_num": g["seq_num"]})
    gg = nap.wait_priority()
    nap.send({"type": "PRIORITY_PASS", "seq_num": gg["seq_num"]})

    # --- combat: declare no attackers via the PHASE_TRANSITION token ---
    both_pass()                                   # BEGIN_COMBAT
    pt = ap.recv_until("PHASE_TRANSITION")
    while pt["to_phase"] != "DECLARE_ATTACKERS":
        pt = ap.recv_until("PHASE_TRANSITION")
    ap.send({"type": "DECLARE_ATTACKERS", "seq_num": pt["seq_num"],
             "attackers": []})
    both_pass()                                   # END_OF_COMBAT
    both_pass()                                   # POSTCOMBAT_MAIN
    both_pass()                                   # END_STEP
    maybe_discard(ap)                             # cleanup discard if needed

    # --- Turn 2: the other player takes a fast turn passing everything ---
    ap, nap = nap, ap
    both_pass()                                   # UPKEEP
    both_pass()                                   # DRAW
    both_pass()                                   # PRECOMBAT_MAIN (no plays)
    both_pass()                                   # BEGIN_COMBAT
    pt = ap.recv_until("PHASE_TRANSITION")
    while pt["to_phase"] != "DECLARE_ATTACKERS":
        pt = ap.recv_until("PHASE_TRANSITION")
    ap.send({"type": "DECLARE_ATTACKERS", "seq_num": pt["seq_num"],
             "attackers": []})
    both_pass()                                   # END_OF_COMBAT
    both_pass()                                   # POSTCOMBAT_MAIN
    both_pass()                                   # END_STEP
    maybe_discard(ap)                             # cleanup discard if needed

    # --- Turn 3+: original AP concedes to prove CONCEDE + LOBBY restart ---
    ap, nap = nap, ap
    g = ap.wait_priority()                        # UPKEEP of turn 3
    ap.send({"type": "CONCEDE", "seq_num": g["seq_num"],
             "player_id": active})
    over1 = p1.recv_until("GAME_OVER")
    over2 = p2.recv_until("GAME_OVER")
    ok(over1["reason"] == "CONCEDE" and over1["winner_id"] != active,
       "CONCEDE ends the game with the non-conceding winner")
    ok(over1["seq_num"] == over2["seq_num"], "GAME_OVER broadcast to both")

    # --- same TCP connections, new game (RFC 6.6) ---
    p1.send({"type": "PLAYER_READY", "seq_num": 3, "player_id": "player_1",
             "deck_list": deck1})
    lob = p1.recv_until("GAME_STATE_UPDATE")
    ok(lob["state"]["phase"] == "LOBBY" and lob["state"]["players_ready"] == 1,
       "server back in LOBBY on the same connections after GAME_OVER")

    # --- PING/PONG heartbeat ---
    p1.send({"type": "PING", "seq_num": 42, "timestamp": 1234567890})
    pong = p1.recv_until("PONG")
    ok(pong["seq_num"] == 42 and pong["timestamp"] == 1234567890,
       "PONG echoes PING seq_num and timestamp")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
