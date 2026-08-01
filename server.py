"""
server.py — MTGNP v1.0 authoritative Game Server (RFC 0001, CSNETWK).

Usage:
    python3 server.py [--port 4444] [--verbose] [--time-limit 60000]

Design overview
---------------
* One listening TCP socket on port 4444 (RFC 5.1). Exactly two clients are
  seated; further connection attempts are refused.
* One reader thread per client pushes decoded PDUs onto a single event queue.
  PING is answered with PONG directly from the reader thread (PONG echoes the
  client's own seq/timestamp and does not consume the server counter).
* The main thread runs the game-lifecycle state machine of RFC Section 6:
      LOBBY -> GAME_SETUP -> MULLIGAN -> IN_GAME -> GAME_OVER -> LOBBY ...
  All game logic lives here; clients are thin renderers (RFC 4.3).
* `self.seq` is the server's monotonically increasing PDU counter (RFC 5.4).
  The "priority token" a client must echo is the seq_num of the most recent
  PRIORITY_GRANT (or corresponding server request PDU) sent to that client.
"""

import argparse
import queue
import random
import socket
import threading
import time

from common import (DEFAULT_PORT, FramingError, base_name, card_def,
                    load_catalog, log_pdu, recv_pdu, send_pdu, set_verbose)


# ---------------------------------------------------------------------------
# Control-flow exceptions
# ---------------------------------------------------------------------------
class GameOver(Exception):
    """Raised anywhere inside IN_GAME to unwind to the GAME_OVER broadcast."""
    def __init__(self, winner_idx, loser_idx, reason):
        self.winner_idx = winner_idx
        self.loser_idx = loser_idx
        self.reason = reason


class ClientGone(Exception):
    """A TCP-level disconnect or heartbeat/priority timeout for one client."""
    def __init__(self, idx):
        self.idx = idx


# ---------------------------------------------------------------------------
# Per-connection wrapper: socket + reader thread + send lock
# ---------------------------------------------------------------------------
class ClientConn:
    def __init__(self, sock, addr, idx, event_q):
        self.sock = sock
        self.addr = addr
        self.idx = idx                 # seat index 0 or 1
        self.player_id = None          # set by PLAYER_READY
        self.send_lock = threading.Lock()
        self.alive = True
        self._q = event_q
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def label(self):
        return self.player_id or f"seat_{self.idx}"

    def send(self, pdu):
        try:
            send_pdu(self.sock, pdu, who=self.label(), lock=self.send_lock)
        except OSError:
            self.alive = False

    def _read_loop(self):
        """Reader thread: frame-decode PDUs and push them to the event queue.

        PING is a heartbeat with its own client-side counter (RFC 5.4); we
        answer it here so heartbeats keep flowing even while the main thread
        is blocked waiting for a specific player's action.
        """
        while self.alive:
            try:
                pdu = recv_pdu(self.sock, who=self.label())
            except FramingError as e:
                # RFC 11: INVALID_JSON — report and keep the connection.
                self.send({"type": "ERROR", "seq_num": 0, "code": "INVALID_JSON",
                           "message": str(e), "rejected_action": None})
                continue
            except (ConnectionError, OSError):
                self.alive = False
                self._q.put(("gone", self.idx, None))
                return

            if pdu.get("type") == "PING":
                # RFC 10.2.24/25: PONG echoes the PING's seq_num + timestamp.
                self.send({"type": "PONG",
                           "seq_num": pdu.get("seq_num", 0),
                           "timestamp": pdu.get("timestamp", 0)})
                continue

            self._q.put(("pdu", self.idx, pdu))


# ---------------------------------------------------------------------------
# The Game Server
# ---------------------------------------------------------------------------
PRIORITY_TYPES = {"CAST_SPELL", "ACTIVATE_ABILITY", "PRIORITY_PASS", "PLAY_LAND"}

class Server:
    def __init__(self, port, time_limit_ms, seed=None, force_first=None):
        self.port = port
        self.time_limit_ms = time_limit_ms
        self.force_first = force_first          # test hook: 0, 1, or None
        if seed is not None:
            random.seed(seed)                   # test hook: reproducible runs
        self.catalog = load_catalog()
        self.events = queue.Queue()
        self.clients = [None, None]    # seat 0, seat 1
        self.seq = 0                   # server PDU counter (RFC 5.4)
        self.stk_counter = 0
        self.trg_counter = 0

    # ---------------- low-level send helpers ----------------
    def next_seq(self):
        self.seq += 1
        return self.seq

    def send_to(self, idx, pdu):
        pdu["seq_num"] = self.next_seq()
        self.clients[idx].send(pdu)
        return pdu["seq_num"]

    def send_all(self, pdu):
        """Broadcast one logical PDU. Per the RFC examples a broadcast
        consumes a single seq_num, delivered identically to both clients."""
        pdu["seq_num"] = self.next_seq()
        for c in self.clients:
            if c and c.alive:
                c.send(pdu)
        return pdu["seq_num"]

    def send_error(self, idx, code, message, rejected=None, echo_seq=None):
        self.clients[idx].send({
            "type": "ERROR",
            "seq_num": echo_seq if echo_seq is not None else self.seq,
            "code": code, "message": message,
            "rejected_action": rejected,
        })

    # ---------------- event-queue helpers ----------------
    def next_event(self, timeout=None):
        """Pop one event; raise ClientGone on disconnect notification."""
        try:
            kind, idx, pdu = self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError
        if kind == "gone":
            raise ClientGone(idx)
        return idx, pdu

    def expect(self, want_idx, allowed_types, expected_seq, regrant=None,
               timeout_ms=None):
        """Wait for a PDU of one of `allowed_types` from seat `want_idx`
        whose seq_num equals `expected_seq` (the current priority token).

        Everything else is rejected with the appropriate ERROR code:
          * wrong player            -> NOT_YOUR_PRIORITY
          * unknown 'type'          -> UNKNOWN_TYPE
          * wrong type / wrong time -> ILLEGAL_ACTION
          * stale seq_num           -> STALE_ACTION (+ optional re-grant)
        CONCEDE is legal from anyone at any time (RFC 5.4).
        On timeout the offending player is treated as disconnected (RFC 4.2).
        """
        deadline = None
        if timeout_ms:
            deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            tmo = None
            if deadline is not None:
                tmo = max(0.0, deadline - time.monotonic())
            try:
                idx, pdu = self.next_event(timeout=tmo)
            except TimeoutError:
                # RFC 4.2: enforce time_limit_ms -> GAME_OVER DISCONNECT.
                raise ClientGone(want_idx)

            ptype = pdu.get("type")
            if ptype == "CONCEDE":
                raise GameOver(1 - idx, idx, "CONCEDE")
            if ptype not in KNOWN_CLIENT_TYPES:
                self.send_error(idx, "UNKNOWN_TYPE",
                                f"Unknown PDU type '{ptype}'.", pdu)
                continue
            if idx != want_idx:
                self.send_error(idx, "NOT_YOUR_PRIORITY",
                                "You do not hold priority.", pdu)
                continue
            if ptype not in allowed_types:
                self.send_error(idx, "ILLEGAL_ACTION",
                                f"{ptype} is not legal right now.", pdu)
                continue
            if expected_seq is not None and pdu.get("seq_num") != expected_seq:
                self.send_error(idx, "STALE_ACTION",
                                f"Priority token mismatch. Expected seq_num "
                                f"{expected_seq}, got {pdu.get('seq_num')}.",
                                pdu, echo_seq=pdu.get("seq_num"))
                if regrant:
                    # Re-issue the current PRIORITY_GRANT (RFC 5.4 example)
                    # and validate future PDUs against the fresh token.
                    expected_seq = regrant()
                continue
            return pdu

    # =======================================================================
    # Connection acceptance
    # =======================================================================
    def accept_players(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        print(f"[server] listening on port {self.port}")
        self.listener = srv
        while None in self.clients:
            sock, addr = srv.accept()
            seat = self.clients.index(None)
            self.clients[seat] = ClientConn(sock, addr, seat, self.events)
            print(f"[server] seat {seat} connected from {addr}")
        # Refuse any further connections in the background (RFC 5.1).
        threading.Thread(target=self._refuse_loop, daemon=True).start()

    def _refuse_loop(self):
        while True:
            try:
                sock, addr = self.listener.accept()
                print(f"[server] refusing extra connection from {addr}")
                sock.close()
            except OSError:
                return

    # =======================================================================
    # Game / player state
    # =======================================================================
    def reset_game_state(self):
        self.players = []
        for i in range(2):
            self.players.append({
                "id": None, "deck": None,      # filled during LOBBY
                "library": [], "hand": [], "graveyard": [],
                "battlefield": [],             # list of permanent dicts
                "life": 20, "mulligans": 0, "land_played": False,
            })
        self.turn = 0
        self.active = 0
        self.stack = []                        # index 0 = bottom (RFC 8.3)
        self.phase = "LOBBY"

    def pid(self, idx):
        return self.players[idx]["id"]

    def idx_of(self, player_id):
        for i in (0, 1):
            if self.players[i]["id"] == player_id:
                return i
        return None

    def find_perm(self, perm_id):
        for i in (0, 1):
            for p in self.players[i]["battlefield"]:
                if p["id"] == perm_id:
                    return i, p
        return None, None

    def new_perm(self, card_id, creature):
        perm = {"id": card_id, "tapped": False}
        if creature:
            d = card_def(self.catalog, card_id)
            perm.update({"damage": 0, "base_power": d["power"],
                         "base_toughness": d["toughness"],
                         "pump_p": 0, "pump_t": 0,
                         "summoning_sick": not d.get("haste", False),
                         "creature": True})
        else:
            perm["creature"] = False
        return perm

    def power(self, perm):
        return perm["base_power"] + perm["pump_p"]

    def toughness(self, perm):
        return perm["base_toughness"] + perm["pump_t"]

    # ---------------- visible-state construction (RFC 4.2 last bullet) -----
    def visible_state(self, for_idx):
        me, opp = self.players[for_idx], self.players[1 - for_idx]
        def bf(pl):
            out = []
            for p in pl["battlefield"]:
                if p["creature"]:
                    out.append({"id": p["id"], "tapped": p["tapped"],
                                "damage": p["damage"],
                                "power": self.power(p),
                                "toughness": self.toughness(p),
                                "summoning_sick": p["summoning_sick"]})
                else:
                    out.append({"id": p["id"], "tapped": p["tapped"]})
            return out
        return {
            "turn": self.turn, "phase": self.phase,
            "active_player": self.pid(self.active),
            "life_totals": {self.pid(0): self.players[0]["life"],
                            self.pid(1): self.players[1]["life"]},
            "stack": [dict(s) for s in self.stack],
            "battlefield": {self.pid(0): bf(self.players[0]),
                            self.pid(1): bf(self.players[1])},
            "graveyard": {self.pid(0): list(self.players[0]["graveyard"]),
                          self.pid(1): list(self.players[1]["graveyard"])},
            "hand": {self.pid(for_idx): list(me["hand"])},
            "hand_counts": {self.pid(1 - for_idx): len(opp["hand"])},
            "library_counts": {self.pid(0): len(self.players[0]["library"]),
                               self.pid(1): len(self.players[1]["library"])},
            "land_played_this_turn": self.players[self.active]["land_played"],
        }

    def broadcast_state(self):
        """Send a personalized GAME_STATE_UPDATE to each player. Returns a
        map seat -> seq_num sent (used when a state update doubles as a
        server request PDU, e.g. mulligan or cleanup discard)."""
        seqs = {}
        for i in (0, 1):
            seqs[i] = self.send_to(i, {"type": "GAME_STATE_UPDATE",
                                       "state": self.visible_state(i)})
        return seqs

    # =======================================================================
    # LOBBY (RFC 6.2)
    # =======================================================================
    def run_lobby(self):
        self.phase = "LOBBY"
        for p in self.players:
            p["id"], p["deck"] = None, None   # player IDs reset each lobby
        print("[server] LOBBY: waiting for PLAYER_READY from both seats")
        while not all(p["deck"] for p in self.players):
            try:
                idx, pdu = self.next_event()
            except ClientGone as e:
                self.handle_lobby_disconnect(e.idx)
                continue
            if pdu.get("type") != "PLAYER_READY":
                self.send_error(idx, "ILLEGAL_ACTION",
                                "Only PLAYER_READY is accepted in LOBBY.", pdu)
                continue
            self.handle_player_ready(idx, pdu)

    def handle_player_ready(self, idx, pdu):
        player_id = pdu.get("player_id")
        deck = pdu.get("deck_list")
        other = self.players[1 - idx]["id"]
        if not isinstance(player_id, str) or not player_id:
            self.send_error(idx, "ILLEGAL_ACTION",
                            "player_id must be a non-empty string.", pdu)
            return
        if player_id == other:
            self.send_error(idx, "DUPLICATE_ID",
                            f"player_id '{player_id}' already claimed.", pdu)
            return
        if (not isinstance(deck, list) or not (1 <= len(deck) <= 50)
                or any(card_def(self.catalog, c) is None for c in deck)):
            n = len(deck) if isinstance(deck, list) else "?"
            self.send_error(idx, "ILLEGAL_DECK",
                            f"Deck invalid: {n} cards or unknown card IDs "
                            f"(1-50 cards from the fixed set required).", pdu)
            return
        # Re-submission before both ready replaces the earlier deck (RFC 6.2).
        self.players[idx]["id"] = player_id
        self.players[idx]["deck"] = list(deck)
        self.clients[idx].player_id = player_id
        ready = sum(1 for p in self.players if p["deck"])
        waiting = [] if ready == 2 else \
            [self.players[1 - idx]["id"] or f"seat_{1 - idx}"]
        self.send_to(idx, {"type": "GAME_STATE_UPDATE",
                           "state": {"phase": "LOBBY", "players_ready": ready,
                                     "waiting_for": waiting}})

    def handle_lobby_disconnect(self, idx):
        print(f"[server] seat {idx} disconnected in LOBBY; awaiting new client")
        try:
            self.clients[idx].sock.close()
        except OSError:
            pass
        sock, addr = self.listener.accept()
        self.clients[idx] = ClientConn(sock, addr, idx, self.events)
        self.players[idx]["id"], self.players[idx]["deck"] = None, None
        print(f"[server] seat {idx} reconnected from {addr}")

    # =======================================================================
    # GAME_SETUP (RFC 6.3)
    # =======================================================================
    def run_setup(self):
        self.phase = "GAME_SETUP"
        self.send_all({"type": "GAME_STATE_UPDATE",
                       "state": {"phase": "GAME_SETUP", "players_ready": 2,
                                 "waiting_for": []}})
        for p in self.players:
            p["life"] = 20
            p["library"] = list(p["deck"])
            random.shuffle(p["library"])
            p["hand"] = [p["library"].pop(0) for _ in range(min(7, len(p["library"])))]
        # Coin flip for first player (RFC 6.3 step 5); --first overrides it
        # for reproducible tests/demos.
        self.active = (self.force_first if self.force_first is not None
                       else random.randint(0, 1))
        print(f"[server] coin flip: {self.pid(self.active)} goes first")
        self.phase = "MULLIGAN"
        return self.broadcast_state()        # seq map -> mulligan request seqs

    # =======================================================================
    # MULLIGAN (RFC 6.4, London Mulligan)
    # =======================================================================
    def run_mulligan(self, request_seqs):
        pending = {0, 1}
        while pending:
            try:
                idx, pdu = self.next_event()
            except ClientGone as e:
                raise GameOver(1 - e.idx, e.idx, "DISCONNECT")
            if pdu.get("type") == "CONCEDE":
                raise GameOver(1 - idx, idx, "CONCEDE")
            if idx not in pending or pdu.get("type") != "MULLIGAN_CHOICE":
                self.send_error(idx, "ILLEGAL_ACTION",
                                "Expecting MULLIGAN_CHOICE.", pdu)
                continue
            if pdu.get("seq_num") != request_seqs[idx]:
                self.send_error(idx, "STALE_ACTION",
                                f"Expected seq_num {request_seqs[idx]}, got "
                                f"{pdu.get('seq_num')}.", pdu,
                                echo_seq=pdu.get("seq_num"))
                continue
            pl = self.players[idx]
            if pdu.get("keep"):
                bottoms = pdu.get("cards_to_bottom", [])
                if (len(bottoms) != pl["mulligans"]
                        or any(c not in pl["hand"] for c in bottoms)):
                    self.send_error(idx, "ILLEGAL_ACTION",
                                    f"cards_to_bottom must contain exactly "
                                    f"{pl['mulligans']} cards from your hand.",
                                    pdu)
                    continue
                for c in bottoms:                 # bottom N cards (London rule)
                    pl["hand"].remove(c)
                    pl["library"].append(c)
                pending.discard(idx)
            else:
                # Redraw a fresh 7 and send a new state (= new request PDU).
                pl["mulligans"] += 1
                pl["library"].extend(pl["hand"])
                pl["hand"] = []
                random.shuffle(pl["library"])
                pl["hand"] = [pl["library"].pop(0)
                              for _ in range(min(7, len(pl["library"])))]
                request_seqs[idx] = self.send_to(
                    idx, {"type": "GAME_STATE_UPDATE",
                          "state": self.visible_state(idx)})

    # =======================================================================
    # IN_GAME: turn engine (RFC Section 7)
    # =======================================================================
    def phase_transition(self, frm, to):
        self.phase = to
        self.send_all({"type": "PHASE_TRANSITION", "from_phase": frm,
                       "to_phase": to, "active_player": self.pid(self.active),
                       "turn": self.turn})
        return self.seq    # seq of the PHASE_TRANSITION (combat request token)

    def run_game(self):
        self.turn = 1
        first = True
        prev = "MULLIGAN"
        while True:
            ap = self.players[self.active]
            # ---- Untap (7.2): automatic, no priority ----
            self.phase_transition(prev, "UNTAP")
            for perm in ap["battlefield"]:
                perm["tapped"] = False
            ap["land_played"] = False
            self.broadcast_state()
            # ---- Upkeep (7.3) ----
            self.phase_transition("UNTAP", "UPKEEP")
            self.priority_window()
            # ---- Draw (7.4): first player skips draw on turn 1 ----
            self.phase_transition("UPKEEP", "DRAW")
            if not first:
                self.draw_cards(self.active, 1)
                self.broadcast_state()
            first = False
            self.priority_window()
            # ---- Precombat Main (7.5) ----
            self.phase_transition("DRAW", "PRECOMBAT_MAIN")
            self.priority_window(main_phase=True)
            # ---- Combat (Section 9) ----
            last = self.run_combat()
            # ---- Postcombat Main ----
            self.phase_transition(last, "POSTCOMBAT_MAIN")
            self.priority_window(main_phase=True)
            # ---- End Step (7.7) ----
            self.phase_transition("POSTCOMBAT_MAIN", "END_STEP")
            self.priority_window()
            # ---- Cleanup (7.8) ----
            self.phase_transition("END_STEP", "CLEANUP")
            self.run_cleanup()
            self.turn += 1
            self.active = 1 - self.active
            prev = "CLEANUP"

    def draw_cards(self, idx, n):
        for _ in range(n):
            if not self.players[idx]["library"]:
                # RFC 6.5: drawing from an empty library loses the game.
                raise GameOver(1 - idx, idx, "DECK_EMPTY")
            self.players[idx]["hand"].append(
                self.players[idx]["library"].pop(0))

    def run_cleanup(self):
        ap_idx = self.active
        ap = self.players[ap_idx]
        while len(ap["hand"]) > 7:
            req = self.broadcast_state()[ap_idx]   # request PDU (RFC 7.8)
            def regrant():
                pass
            pdu = self.expect(ap_idx, {"DISCARD"}, req,
                              timeout_ms=self.time_limit_ms)
            cards = pdu.get("card_ids", [])
            if any(c not in ap["hand"] for c in cards) or not cards:
                self.send_error(ap_idx, "ILLEGAL_ACTION",
                                "DISCARD lists cards not in your hand.", pdu)
                continue
            for c in cards:
                ap["hand"].remove(c)
                ap["graveyard"].append(c)
        # Clear damage and until-end-of-turn effects; no priority (RFC 7.8).
        for i in (0, 1):
            for p in self.players[i]["battlefield"]:
                if p["creature"]:
                    p["damage"] = 0
                    p["pump_p"] = p["pump_t"] = 0
                    if i == ap_idx:
                        pass
        # Summoning sickness wears off for the player whose turn is starting;
        # simplest faithful model: clear it for the AP's creatures now, since
        # they have been under AP's control since before their next turn.
        for p in ap["battlefield"]:
            if p["creature"]:
                p["summoning_sick"] = False
        self.broadcast_state()

    # =======================================================================
    # Priority & the Stack (RFC Section 8)
    # =======================================================================
    def grant_priority(self, idx):
        return self.send_to(idx, {"type": "PRIORITY_GRANT",
                                  "player_id": self.pid(idx),
                                  "time_limit_ms": self.time_limit_ms})

    def resend_grant(self, idx, token):
        """RFC Section 11 item 3: after rejecting an illegal action, if the
        player still holds priority, re-issue PRIORITY_GRANT with the SAME
        seq_num so the player may try again (no counter increment)."""
        self.clients[idx].send({"type": "PRIORITY_GRANT",
                                "player_id": self.pid(idx),
                                "seq_num": token,
                                "time_limit_ms": self.time_limit_ms})

    def priority_window(self, main_phase=False):
        """One full priority window (RFC 8.1 / 8.2). Returns when both players
        pass consecutively with an empty stack (step ends)."""
        holder = self.active
        passes = 0
        while True:
            token = self.grant_priority(holder)
            acted = self.await_action(holder, token, main_phase)
            if acted == "PASS":
                passes += 1
                if passes == 2:
                    if self.stack:
                        self.resolve_top()
                        holder, passes = self.active, 0
                    else:
                        return                     # step advances
                else:
                    holder = 1 - holder
            else:
                passes = 0
                # After casting/land the same player retains priority except
                # when the action was a pass; RFC 8.1 rule 3.
                holder = acted if isinstance(acted, int) else holder

    def await_action(self, idx, token, main_phase):
        """Wait for one legal priority action. Returns 'PASS' or the seat
        index that retains priority after a successful non-pass action."""
        def regrant():
            nonlocal token
            token = self.grant_priority(idx)
            return token
        while True:
            pdu = self.expect(idx, PRIORITY_TYPES, token, regrant,
                              timeout_ms=self.time_limit_ms)
            t = pdu["type"]
            if t == "PRIORITY_PASS":
                return "PASS"
            if t == "PLAY_LAND":
                if self.try_play_land(idx, pdu, main_phase):
                    self.broadcast_state()
                    return idx        # AP retains priority (RFC 7.5)
                self.resend_grant(idx, token)
                continue
            if t == "CAST_SPELL":
                if self.try_cast(idx, pdu, main_phase):
                    return idx        # caster retains priority (RFC 8.1)
                self.resend_grant(idx, token)
                continue
            if t == "ACTIVATE_ABILITY":
                # MTGNP 1.0 catalog defines no activated non-mana abilities;
                # mana is paid implicitly inside CAST_SPELL (RFC 7.5).
                self.send_error(idx, "ILLEGAL_ACTION",
                                "No activated abilities in the card set.", pdu)
                self.resend_grant(idx, token)
                continue

    # ---------------- land plays (RFC 7.5) ----------------
    def try_play_land(self, idx, pdu, main_phase):
        card = pdu.get("card_id")
        d = card_def(self.catalog, card) if card else None
        pl = self.players[idx]
        if not main_phase or idx != self.active or self.stack:
            self.send_error(idx, "WRONG_PHASE",
                            "Lands may only be played in your own Main Phase "
                            "with an empty stack.", pdu)
            return False
        if pl["land_played"]:
            self.send_error(idx, "ILLEGAL_ACTION",
                            "Already played a land this turn.", pdu)
            return False
        if card not in pl["hand"] or not d or d["kind"] != "land":
            self.send_error(idx, "ILLEGAL_ACTION",
                            "That is not a land card in your hand.", pdu)
            return False
        pl["hand"].remove(card)
        pl["battlefield"].append(self.new_perm(card, creature=False))
        pl["land_played"] = True
        return True

    # ---------------- mana payment (RFC 7.5, implicit mana) ----------------
    def check_and_pay_mana(self, idx, cost, payment, pdu):
        """Validate the declared payment against the cost and tap lands.
        Colored cost keys must be paid in-color; 'X' (generic) may be paid
        with any color. Returns True and taps sources on success."""
        cost = dict(cost or {})
        payment = dict(payment or {})
        pool = {}   # color -> list of untapped land perms
        for perm in self.players[idx]["battlefield"]:
            d = card_def(self.catalog, perm["id"])
            if d["kind"] == "land" and not perm["tapped"]:
                pool.setdefault(d["produces"], []).append(perm)
        # 1) payment must total the cost and cover each colored requirement
        need_total = sum(cost.values())
        pay_total = sum(payment.values())
        colored_ok = all(payment.get(c, 0) >= n
                         for c, n in cost.items() if c != "X")
        if pay_total != need_total or not colored_ok:
            self.send_error(idx, "INSUFFICIENT_MANA",
                            "Declared mana_payment does not satisfy the "
                            "spell's cost.", pdu)
            return False
        # 2) payment must be producible by untapped lands
        for color, n in payment.items():
            if len(pool.get(color, [])) < n:
                self.send_error(idx, "INSUFFICIENT_MANA",
                                f"Not enough untapped sources of {color}.", pdu)
                return False
        for color, n in payment.items():
            for perm in pool[color][:n]:
                perm["tapped"] = True
        return True

    # ---------------- casting (RFC 8.1 / 8.3) ----------------
    def try_cast(self, idx, pdu, main_phase):
        card = pdu.get("card_id")
        d = card_def(self.catalog, card) if card else None
        pl = self.players[idx]
        if card not in pl["hand"] or d is None or d["kind"] == "land":
            self.send_error(idx, "ILLEGAL_ACTION",
                            "Card is not a castable card in your hand.", pdu)
            return False
        sorcery_speed = d["kind"] in ("creature", "sorcery", "enchantment",
                                      "artifact")
        if sorcery_speed and not (main_phase and idx == self.active
                                  and not self.stack):
            self.send_error(idx, "WRONG_PHASE",
                            f"{d['name']} may only be cast in your own Main "
                            f"Phase with an empty stack.", pdu)
            return False
        targets = pdu.get("targets", [])
        if d.get("needs_target"):
            if len(targets) != 1 or not self.target_legal(d, targets[0]):
                self.send_error(idx, "ILLEGAL_TARGET",
                                "Missing or illegal target.", pdu)
                return False
        if not self.check_and_pay_mana(idx, d.get("cost"),
                                       pdu.get("mana_payment"), pdu):
            return False
        pl["hand"].remove(card)
        self.stk_counter += 1
        item = {"stack_item_id": f"stk_{self.stk_counter:02d}",
                "item_type": "SPELL", "source": card,
                "targets": targets, "controller": self.pid(idx)}
        self.stack.append(item)
        self.send_all({"type": "STACK_PUSH", **item})
        return True

    def target_legal(self, spell_def, target):
        if spell_def.get("targets_stack"):
            return any(s["stack_item_id"] == target for s in self.stack)
        if spell_def.get("targets_creature"):
            _, perm = self.find_perm(target)
            return perm is not None and perm["creature"]
        # 'any target' style (bolt/shock) or player-only (healing salve):
        if self.idx_of(target) is not None:
            return True
        if spell_def.get("effect") == "lifegain":
            return False
        _, perm = self.find_perm(target)
        return perm is not None and perm["creature"]

    # ---------------- resolution (RFC 8.4) ----------------
    def resolve_top(self):
        item = self.stack.pop()
        d = card_def(self.catalog, item["source"])
        changes = []
        # Re-check target legality; fizzle if all targets are now illegal.
        if d.get("needs_target"):
            if not all(self.target_legal(d, t) for t in item["targets"]):
                self.send_all({"type": "STACK_RESOLVE",
                               "stack_item_id": item["stack_item_id"],
                               "result": "FIZZLE", "state_changes": []})
                d_owner = self.idx_of(item["controller"])
                if item["item_type"] == "SPELL":
                    self.players[d_owner]["graveyard"].append(item["source"])
                self.after_event()
                return
        controller = self.idx_of(item["controller"])
        etb_perm = None
        if item["item_type"] == "TRIGGER_ABILITY":
            changes += self.apply_trigger_effect(item, controller)
        elif d["kind"] == "creature":
            perm = self.new_perm(item["source"], creature=True)
            self.players[controller]["battlefield"].append(perm)
            changes.append({"change_type": "PERMANENT_ENTERS",
                            "card_id": item["source"],
                            "controller": item["controller"]})
            etb_perm = perm
        else:
            changes += self.apply_spell_effect(d, item, controller)
            self.players[controller]["graveyard"].append(item["source"])
        self.send_all({"type": "STACK_RESOLVE",
                       "stack_item_id": item["stack_item_id"],
                       "result": "RESOLVED", "state_changes": changes})
        self.after_event(etb_source=etb_perm,
                         etb_controller=controller if etb_perm else None)

    def apply_spell_effect(self, d, item, controller):
        changes = []
        eff = d.get("effect")
        tgt = item["targets"][0] if item["targets"] else None
        if eff == "damage":
            changes += self.deal_damage(item["source"], tgt, d["amount"])
        elif eff == "lifegain":
            t = self.idx_of(tgt)
            self.players[t]["life"] += d["amount"]
            changes.append({"change_type": "LIFE_GAIN", "target": tgt,
                            "amount": d["amount"]})
        elif eff == "pump":
            _, perm = self.find_perm(tgt)
            perm["pump_p"] += d["power"]
            perm["pump_t"] += d["toughness"]
            changes.append({"change_type": "PUMP", "target": tgt,
                            "power": d["power"], "toughness": d["toughness"]})
        elif eff == "counter":
            for i, s in enumerate(self.stack):
                if s["stack_item_id"] == tgt:
                    countered = self.stack.pop(i)
                    self.send_all({"type": "STACK_RESOLVE",
                                   "stack_item_id": countered["stack_item_id"],
                                   "result": "FIZZLE", "state_changes": []})
                    own = self.idx_of(countered["controller"])
                    if countered["item_type"] == "SPELL":
                        self.players[own]["graveyard"].append(
                            countered["source"])
                    changes.append({"change_type": "COUNTERED",
                                    "target": tgt})
                    break
        elif eff == "draw":
            self.draw_cards(controller, d["amount"])
            changes.append({"change_type": "DRAW",
                            "target": self.pid(controller),
                            "amount": d["amount"]})
        return changes

    def deal_damage(self, source, target, amount):
        t_idx = self.idx_of(target)
        if t_idx is not None:
            self.players[t_idx]["life"] -= amount
        else:
            _, perm = self.find_perm(target)
            if perm:
                perm["damage"] += amount
        return [{"change_type": "DAMAGE", "target": target, "amount": amount}]

    # ---------------- state-based actions + triggers (RFC 8.4 / 8.6) -------
    def check_sbas(self):
        """Apply state-based actions repeatedly until none remain. Returns a
        list of (owner_idx, card_id) for creatures that died, so callers can
        collect death triggers (RFC 8.6.1)."""
        deaths = []
        while True:
            acted = False
            # Simultaneous death: AP loses ties (RFC 8.4).
            l0, l1 = self.players[0]["life"], self.players[1]["life"]
            if l0 <= 0 and l1 <= 0:
                raise GameOver(1 - self.active, self.active, "LIFE_ZERO")
            if l0 <= 0:
                raise GameOver(1, 0, "LIFE_ZERO")
            if l1 <= 0:
                raise GameOver(0, 1, "LIFE_ZERO")
            for i in (0, 1):
                for perm in list(self.players[i]["battlefield"]):
                    if perm["creature"] and (
                            self.toughness(perm) <= 0
                            or perm["damage"] >= self.toughness(perm)):
                        self.players[i]["battlefield"].remove(perm)
                        self.players[i]["graveyard"].append(perm["id"])
                        deaths.append((i, perm["id"]))
                        acted = True
            if not acted:
                return deaths

    def after_event(self, etb_source=None, etb_controller=None):
        """SBA check, then trigger detection/placement, then fresh state to
        both players (RFC 8.4 step 3 + 8.6.1). Called after every event.

        Triggers are collected from the event (ETB) and from the SBA sweep
        (death triggers), then placed on the stack in APNAP order. If one
        player has several simultaneous triggers, the TRIGGER_ORDER /
        TRIGGER_ORDER_RESPONSE flow of RFC 8.6.2 chooses their order."""
        deaths = self.check_sbas()
        pending = []               # (controller_idx, source_card_id, trig)
        if etb_source is not None:
            d = card_def(self.catalog, etb_source["id"])
            if d.get("etb_trigger"):
                pending.append((etb_controller, etb_source["id"],
                                d["etb_trigger"]))
        for owner, cid in deaths:
            d = card_def(self.catalog, cid)
            if d.get("death_trigger"):
                pending.append((owner, cid, d["death_trigger"]))
        self.dispatch_triggers(pending)
        self.broadcast_state()

    def dispatch_triggers(self, pending):
        """Place simultaneous triggers in APNAP order: the active player's
        triggers go on the stack first (RFC 8.6.2)."""
        for idx in (self.active, 1 - self.active):
            mine = [(src, trig) for (o, src, trig) in pending if o == idx]
            if not mine:
                continue
            if len(mine) == 1:
                self.place_trigger(idx, *mine[0])
                continue
            # Multiple simultaneous triggers for one player: the player
            # chooses their stack order via TRIGGER_ORDER (RFC 8.6.2 /
            # 10.2.10-11).
            by_id = {}
            for src, trig in mine:
                self.trg_counter += 1
                by_id[f"trg_{self.trg_counter:02d}"] = (src, trig)
            req = self.send_to(idx, {"type": "TRIGGER_ORDER",
                                     "player_id": self.pid(idx),
                                     "trigger_ids": list(by_id)})
            while True:
                pdu = self.expect(idx, {"TRIGGER_ORDER_RESPONSE"}, req,
                                  timeout_ms=self.time_limit_ms)
                order = pdu.get("ordered_trigger_ids", [])
                if sorted(order) != sorted(by_id):
                    self.send_error(idx, "TRIGGER_ORDER_INVALID",
                                    "ordered_trigger_ids must be a "
                                    "permutation of the offered trigger_ids.",
                                    pdu)
                    continue
                break
            # Triggers are pushed in the listed order, so the LAST listed
            # trigger ends up on top of the stack and resolves first.
            for tid in order:
                src, trig = by_id[tid]
                self.place_trigger(idx, src, trig, trig_id=tid)

    def place_trigger(self, controller, source_id, trig, trig_id=None):
        """Put one trigger on the stack. Optional ('you may') triggers first
        go through the TRIGGER_CHOICE flow (RFC 8.6.3); declining discards
        the trigger with no effect."""
        if trig_id is None:
            self.trg_counter += 1
            trig_id = f"trg_{self.trg_counter:02d}"
        if trig.get("optional"):
            req = self.send_to(controller, {
                "type": "TRIGGER_CHOICE", "trigger_id": trig_id,
                "source_id": source_id, "effect_summary": trig["summary"],
                "requires_target": False, "legal_targets": []})
            pdu = self.expect(controller, {"TRIGGER_CHOICE_RESPONSE"}, req,
                              timeout_ms=self.time_limit_ms)
            if pdu.get("trigger_id") != trig_id:
                self.send_error(controller, "TRIGGER_CHOICE_INVALID",
                                "Unknown trigger_id.", pdu)
                return
            if not pdu.get("accept"):
                return                   # declined: silently discarded
        self.stk_counter += 1
        item = {"stack_item_id": f"stk_{self.stk_counter:02d}",
                "item_type": "TRIGGER_ABILITY", "source": source_id,
                "targets": [], "controller": self.pid(controller),
                "trigger_effect": trig}
        self.stack.append(item)
        pub = {k: v for k, v in item.items() if k != "trigger_effect"}
        self.send_all({"type": "STACK_PUSH", **pub})

    def apply_trigger_effect(self, item, controller):
        trig = item["trigger_effect"]
        changes = []
        if trig["effect"] == "drain":
            n = trig["amount"]
            self.players[1 - controller]["life"] -= n
            self.players[controller]["life"] += n
            changes.append({"change_type": "DAMAGE",
                            "target": self.pid(1 - controller), "amount": n})
            changes.append({"change_type": "LIFE_GAIN",
                            "target": self.pid(controller), "amount": n})
        elif trig["effect"] == "opp_lose":
            n = trig["amount"]
            self.players[1 - controller]["life"] -= n
            changes.append({"change_type": "DAMAGE",
                            "target": self.pid(1 - controller), "amount": n})
        return changes

    # =======================================================================
    # Combat (RFC Section 9)
    # =======================================================================
    def run_combat(self):
        """Runs BEGIN_COMBAT through END_OF_COMBAT. Returns the name of the
        phase we ended in ('END_OF_COMBAT') for the next PHASE_TRANSITION."""
        ap_i, nap_i = self.active, 1 - self.active
        self.phase_transition(self.phase, "BEGIN_COMBAT")
        self.priority_window()

        # ---- Declare Attackers (9.3): token = PHASE_TRANSITION seq ----
        token = self.phase_transition("BEGIN_COMBAT", "DECLARE_ATTACKERS")
        pdu = self.expect(ap_i, {"DECLARE_ATTACKERS"}, token,
                          timeout_ms=self.time_limit_ms)
        attackers = self.validate_attackers(ap_i, pdu)
        while attackers is None:
            pdu = self.expect(ap_i, {"DECLARE_ATTACKERS"}, token,
                              timeout_ms=self.time_limit_ms)
            attackers = self.validate_attackers(ap_i, pdu)
        if not attackers:
            # RFC 9.3: no attackers -> skip straight to End of Combat.
            self.phase_transition("DECLARE_ATTACKERS", "END_OF_COMBAT")
            self.priority_window()
            self.clear_combat()
            return "END_OF_COMBAT"
        for a in attackers:              # attacking taps the creature (9.3)
            _, perm = self.find_perm(a)
            perm["tapped"] = True
        self.broadcast_state()
        self.priority_window()

        # ---- Declare Blockers (9.4): NAP echoes PHASE_TRANSITION seq ----
        token = self.phase_transition("DECLARE_ATTACKERS", "DECLARE_BLOCKERS")
        pdu = self.expect(nap_i, {"DECLARE_BLOCKERS"}, token,
                          timeout_ms=self.time_limit_ms)
        blocks = self.validate_blockers(nap_i, pdu, attackers)
        while blocks is None:
            pdu = self.expect(nap_i, {"DECLARE_BLOCKERS"}, token,
                              timeout_ms=self.time_limit_ms)
            blocks = self.validate_blockers(nap_i, pdu, attackers)
        self.broadcast_state()
        self.priority_window()

        # ---- Assign Damage Order (9.5): only for multi-blocked attackers ---
        order = {}   # attacker_id -> [blocker ids in damage order]
        multi = {a: bs for a, bs in blocks.items() if len(bs) > 1}
        if multi:
            token = self.phase_transition("DECLARE_BLOCKERS",
                                          "ASSIGN_DAMAGE_ORDER")
            needed = set(multi)
            while needed:
                pdu = self.expect(ap_i, {"ASSIGN_DAMAGE_ORDER"}, token,
                                  timeout_ms=self.time_limit_ms)
                atk = pdu.get("attacker_id")
                bo = pdu.get("blocker_order", [])
                if atk not in needed or sorted(bo) != sorted(multi[atk]):
                    self.send_error(ap_i, "ILLEGAL_ACTION",
                                    "blocker_order must list exactly that "
                                    "attacker's blockers.", pdu)
                    continue
                order[atk] = bo
                needed.discard(atk)
            self.priority_window()
            prev = "ASSIGN_DAMAGE_ORDER"
        else:
            prev = "DECLARE_BLOCKERS"
        for a, bs in blocks.items():
            order.setdefault(a, bs)

        # ---- First Strike Damage (9.6): only if FS/DS creatures present ----
        def has_fs(cid):
            d = card_def(self.catalog, cid)
            return d.get("first_strike") or d.get("double_strike")
        fs_present = any(has_fs(a) for a in attackers) or \
            any(has_fs(b) for bs in blocks.values() for b in bs)
        if fs_present:
            self.phase_transition(prev, "FIRST_STRIKE_DAMAGE")
            self.resolve_combat_damage(attackers, blocks, order,
                                       first_strike_step=True)
            self.priority_window()
            prev = "FIRST_STRIKE_DAMAGE"

        # ---- Combat Damage (9.7) ----
        self.phase_transition(prev, "COMBAT_DAMAGE")
        self.resolve_combat_damage(attackers, blocks, order,
                                   first_strike_step=False)
        # ---- End of Combat (9.8) ----
        self.phase_transition("COMBAT_DAMAGE", "END_OF_COMBAT")
        self.priority_window()
        self.clear_combat()
        return "END_OF_COMBAT"

    def validate_attackers(self, ap_i, pdu):
        out = []
        for a in pdu.get("attackers", []):
            cid = a.get("creature_id")
            owner, perm = self.find_perm(cid)
            if (owner != ap_i or perm is None or not perm["creature"]
                    or perm["tapped"] or perm["summoning_sick"]
                    or a.get("target") != self.pid(1 - ap_i)):
                self.send_error(ap_i, "ILLEGAL_ACTION",
                                f"'{cid}' cannot attack (tapped, summoning-"
                                f"sick, missing, or bad target).", pdu)
                return None
            out.append(cid)
        return out

    def validate_blockers(self, nap_i, pdu, attackers):
        blocks = {a: [] for a in attackers}
        seen = set()
        for b in pdu.get("blockers", []):
            cid, tgt = b.get("creature_id"), b.get("blocking_id")
            owner, perm = self.find_perm(cid)
            # A creature may block only one attacker; tapped creatures cannot
            # block; blocking does not tap (RFC 9.4).
            if (owner != nap_i or perm is None or not perm["creature"]
                    or perm["tapped"] or cid in seen or tgt not in blocks):
                self.send_error(nap_i, "ILLEGAL_ACTION",
                                f"'{cid}' is not a legal block.", pdu)
                return None
            seen.add(cid)
            blocks[tgt].append(cid)
        return blocks

    def resolve_combat_damage(self, attackers, blocks, order, first_strike_step):
        """Assign and apply combat damage simultaneously (RFC 9.6 / 9.7)."""
        cat = self.catalog
        ap_i, nap_i = self.active, 1 - self.active

        def deals_now(cid):
            d = card_def(cat, cid)
            fs, ds = d.get("first_strike"), d.get("double_strike")
            if first_strike_step:
                return fs or ds
            return ds or not fs      # FS-only creatures already dealt damage

        events = []
        # Attacker damage
        for atk in attackers:
            _, aperm = self.find_perm(atk)
            if aperm is None or not deals_now(atk):
                continue
            dmg = self.power(aperm)
            bs = [b for b in order.get(atk, blocks.get(atk, []))
                  if self.find_perm(b)[1] is not None]
            if not bs:
                if not blocks.get(atk):          # unblocked -> player
                    events.append((atk, self.pid(nap_i), dmg))
                continue                          # blocked, blockers all dead
            # Damage order: lethal to each blocker in order, overflow to next
            # blocker only (no trample in MTGNP 1.0 — never to the player).
            for b in bs:
                if dmg <= 0:
                    break
                _, bperm = self.find_perm(b)
                lethal = max(0, self.toughness(bperm) - bperm["damage"])
                assign = min(dmg, lethal) if b != bs[-1] else dmg
                events.append((atk, b, assign))
                dmg -= assign
        # Blocker damage (simultaneous)
        for atk, bs in blocks.items():
            _, aperm = self.find_perm(atk)
            for b in bs:
                _, bperm = self.find_perm(b)
                if bperm is None or aperm is None or not deals_now(b):
                    continue
                events.append((b, atk, self.power(bperm)))

        for src, tgt, amt in events:
            if amt > 0:
                self.deal_damage(src, tgt, amt)
        died = []
        for i in (0, 1):
            for perm in self.players[i]["battlefield"]:
                if perm["creature"] and perm["damage"] >= self.toughness(perm):
                    died.append(perm["id"])
        self.send_all({"type": "COMBAT_DAMAGE_RESULT",
                       "damage_events": [{"source": s, "target": t,
                                          "amount": a} for s, t, a in events],
                       "life_totals": {self.pid(0): self.players[0]["life"],
                                       self.pid(1): self.players[1]["life"]},
                       "creatures_died": died})
        self.after_event()                       # SBAs move the dead, win check

    def clear_combat(self):
        # Combat damage markers persist until Cleanup (RFC 7.8); attacker /
        # blocker assignments are transient locals, so nothing else to do.
        pass

    # =======================================================================
    # GAME_OVER (RFC 6.6) and the outer session loop
    # =======================================================================
    def run_forever(self):
        self.accept_players()
        while True:
            self.reset_game_state()
            try:
                self.run_lobby()
                seqs = self.run_setup()
                self.run_mulligan(seqs)
                self.phase = "IN_GAME"
                self.run_game()
            except GameOver as g:
                self.announce_game_over(g)
            except ClientGone as e:
                g = GameOver(1 - e.idx, e.idx, "DISCONNECT")
                self.announce_game_over(g)
                self.replace_dead_seats()
            # Loop: back to LOBBY on the same TCP connections (RFC 6.6).

    def announce_game_over(self, g):
        win = self.pid(g.winner_idx) or f"seat_{g.winner_idx}"
        lose = self.pid(g.loser_idx) or f"seat_{g.loser_idx}"
        print(f"[server] GAME_OVER: {win} beats {lose} ({g.reason})")
        self.send_all({"type": "GAME_OVER", "winner_id": win,
                       "loser_id": lose, "reason": g.reason})
        # Drain stale events so a new lobby starts clean.
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def replace_dead_seats(self):
        for i in (0, 1):
            if not self.clients[i].alive:
                try:
                    self.clients[i].sock.close()
                except OSError:
                    pass
                print(f"[server] waiting for a new client on seat {i} ...")
                sock, addr = self.listener.accept()
                self.clients[i] = ClientConn(sock, addr, i, self.events)
                print(f"[server] seat {i} reconnected from {addr}")


KNOWN_CLIENT_TYPES = {
    "PLAYER_READY", "MULLIGAN_CHOICE", "PRIORITY_PASS", "CAST_SPELL",
    "ACTIVATE_ABILITY", "TRIGGER_ORDER_RESPONSE", "TRIGGER_CHOICE_RESPONSE",
    "DECLARE_ATTACKERS", "DECLARE_BLOCKERS", "ASSIGN_DAMAGE_ORDER",
    "PLAY_LAND", "DISCARD", "CONCEDE", "PING",
}


def main():
    ap = argparse.ArgumentParser(description="MTGNP v1.0 Game Server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print every PDU sent and received")
    ap.add_argument("--time-limit", type=int, default=60000,
                    help="priority response deadline in ms (RFC 4.2)")
    ap.add_argument("--seed", type=int, default=None,
                    help="debug: seed the RNG for reproducible shuffles")
    ap.add_argument("--first", type=int, default=None, choices=(0, 1),
                    help="debug: force which seat goes first")
    args = ap.parse_args()
    set_verbose(args.verbose)
    Server(args.port, args.time_limit, args.seed, args.first).run_forever()


if __name__ == "__main__":
    main()
