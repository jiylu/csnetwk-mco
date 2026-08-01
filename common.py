"""
common.py — shared MTGNP plumbing (RFC 0001, CSNETWK).

Implements Section 5 of the RFC:
  * 4-byte big-endian unsigned length prefix (Section 5.2)
  * UTF-8 JSON payload, max 65,535 bytes (Sections 5.2 / 5.3)
plus the shared card catalog loader and the verbose PDU logger required by
the machine-problem rubric.
"""

import json
import os
import struct
import sys
import threading
import time

MAX_PDU_BYTES = 65535        # RFC 5.2: "A PDU MUST NOT exceed 65,535 bytes."
DEFAULT_PORT = 4444          # RFC 5.1: default server port.

# ---------------------------------------------------------------------------
# Verbose mode (rubric prerequisite): toggled at startup with --verbose / -v.
# When on, every PDU sent or received is printed, clearly labelled.
# ---------------------------------------------------------------------------
VERBOSE = False
_log_lock = threading.Lock()


def set_verbose(flag: bool) -> None:
    global VERBOSE
    VERBOSE = flag


def log_pdu(direction: str, who: str, pdu: dict) -> None:
    """Print a PDU in a readable, labelled format when verbose mode is on.

    direction: 'SEND' or 'RECV'
    who:       peer label, e.g. 'player_1' or 'server'
    """
    if not VERBOSE:
        return
    stamp = time.strftime("%H:%M:%S")
    with _log_lock:
        print(f"[{stamp}] {direction:4s} {who:>9s} | "
              f"{json.dumps(pdu, separators=(', ', ': '))}")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Message framing (RFC Section 5.2)
# ---------------------------------------------------------------------------
class FramingError(Exception):
    pass


def send_pdu(sock, pdu: dict, who: str = "peer", lock: threading.Lock = None) -> None:
    """Encode a PDU as UTF-8 JSON and send it with a 4-byte BE length prefix."""
    payload = json.dumps(pdu).encode("utf-8")
    if len(payload) > MAX_PDU_BYTES:
        raise FramingError(f"PDU exceeds {MAX_PDU_BYTES} bytes")
    frame = struct.pack("!I", len(payload)) + payload
    if lock:
        with lock:
            sock.sendall(frame)
    else:
        sock.sendall(frame)
    log_pdu("SEND", who, pdu)


def _recv_exact(sock, n: int) -> bytes:
    """Read exactly n bytes from the socket (RFC 5.2: 'read exactly that many
    bytes before attempting JSON parsing'). Raises ConnectionError on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf += chunk
    return buf


def recv_pdu(sock, who: str = "peer") -> dict:
    """Receive one length-prefixed PDU. Returns the parsed JSON object.

    Raises FramingError with reason 'INVALID_JSON' semantics if the payload is
    not valid UTF-8 JSON — callers decide whether to send an ERROR PDU.
    """
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack("!I", header)
    if length > MAX_PDU_BYTES:
        raise FramingError(f"frame length {length} exceeds {MAX_PDU_BYTES}")
    payload = _recv_exact(sock, length)
    try:
        pdu = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FramingError(f"INVALID_JSON: {e}")
    if not isinstance(pdu, dict):
        raise FramingError("INVALID_JSON: top-level value is not an object")
    log_pdu("RECV", who, pdu)
    return pdu


# ---------------------------------------------------------------------------
# Card catalog (RFC Section 1 NOTE: pre-loaded out-of-band by both sides)
# ---------------------------------------------------------------------------
def load_catalog(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards.json")
    with open(path, "r", encoding="utf-8") as f:
        cat = json.load(f)
    cat.pop("_comment", None)
    return cat


def base_name(card_instance_id: str) -> str:
    """'lightning_bolt_001' -> 'lightning_bolt'. The trailing _NNN suffix
    distinguishes copies; the base is the catalog key."""
    parts = card_instance_id.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return card_instance_id


def card_def(catalog: dict, card_instance_id: str):
    return catalog.get(base_name(card_instance_id))
