"""Robot channel: a robot-agnostic text bridge for embodied deployments.

OmegaClaw acts as the server; a robot's voice pipeline connects as a TCP
client and exchanges newline-delimited JSON messages:

  robot -> agent:  {"type": "hello", "robot": "<name>", "auth": "<secret>"}
                   {"type": "utterance", "turn": 1, "text": "...", "speaker": "user",
                    "system": "<per-turn system prompt: persona + animation +
                                inline-skill grammar + live places + nav mode>"}
                   {"type": "cancel", "turn": 1}
                   {"type": "ping"}
  agent -> robot:  {"type": "hello_ack", "ok": true, "auth": "ok"|"off"}
                   {"type": "say", "turn": 1, "text": "..."}
                   {"type": "pong"}

Every ``say`` is delivered: to the person's open turn if there is one, otherwise
spoken/acted on as unprompted speech. ``turn`` is echoed back for correlation
and ``cancel`` only -- the adapter does not match on it, because the agent
stamps the last turn it received and never clears it, so gating on the id
silently swallowed anything emitted on the agent's own loop (a gesture, a
``|@skill|`` tag). Keeping the robot quiet on idle cycles is therefore a prompt
concern, not a transport one.

Robot ACTIONS (navigate, come here, move) are NOT a separate command channel:
the ``system`` prompt the robot forwards each turn carries the inline-skill
grammar (``|@go_to_place, kitchen|`` etc.) plus the live place list and nav
mode. The agent weaves those tags into its ``say`` text; the robot's voice
pipeline parses them and fires the ROS service. So the agent just talks, and
navigation rides inside the speech -- like inline animation tags.

One robot connection at a time: a newly authenticated connection replaces the
previous one (the agent is a single identity; two bodies sharing it mid-turn
would be incoherent). Auth reuses the proxy /auth/verify flow like the chat
channels: required only when the deployment has a secret configured.
"""

import json
import socket
import threading

import auth

_running = False
_server_sock = None
_client_sock = None
_client_lock = threading.Lock()
_last_message = ""
_msg_lock = threading.Lock()
_current_turn = None
_turn_lock = threading.Lock()
_last_system = ""
_system_lock = threading.Lock()


def _set_last(msg):
    global _last_message
    with _msg_lock:
        if _last_message == "":
            _last_message = msg
        else:
            _last_message = _last_message + " | " + msg


def getLastMessage():
    global _last_message
    with _msg_lock:
        tmp = _last_message
        _last_message = ""
        return tmp


def get_system():
    """Latest per-turn system prompt forwarded by the robot's voice pipeline.

    Carries the inline-skill grammar + live place list + nav mode. The agent's
    getPrompt folds it into context so the agent emits valid |@...| action
    tags. Persists between turns (not cleared) so idle wake cycles still see the
    last known robot state; empty when no robot has forwarded one yet.
    """
    with _system_lock:
        return _last_system


def _send_json(sock, obj):
    try:
        sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def _say(text):
    with _turn_lock:
        turn = _current_turn
    with _client_lock:
        sock = _client_sock
    if sock is None:
        print("[ROBOT] No robot connected; dropping say")
        return "NO-ROBOT-CONNECTED"
    msg = {"type": "say", "turn": turn, "text": str(text)}
    if _send_json(sock, msg):
        return "SEND-SUCCESS"
    print("[ROBOT] Send failed; robot likely disconnected")
    return "SEND-FAILURE"


def send_message(text):
    """Say something to the person; delivered whether or not their turn is open."""
    return _say(text)


def _handshake(sock, addr):
    """Read the hello line and authenticate. Returns robot name or None."""
    sock.settimeout(10)
    buf = b""
    while b"\n" not in buf:
        data = sock.recv(4096)
        if not data:
            return None
        buf += data
        if len(buf) > 65536:
            return None
    line = buf.split(b"\n", 1)[0]
    try:
        hello = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    if hello.get("type") != "hello":
        return None
    if auth.is_auth_enabled():
        if not auth.verify_token(hello.get("auth", "")):
            print(f"[ROBOT] Auth denied for {addr}")
            _send_json(sock, {"type": "hello_ack", "ok": False, "auth": "denied"})
            return None
        auth_state = "ok"
    else:
        auth_state = "off"
    _send_json(sock, {"type": "hello_ack", "ok": True, "auth": auth_state})
    return str(hello.get("robot", "robot"))


def _client_loop(sock, addr, robot_name):
    global _current_turn
    sock.settimeout(60)
    buf = b""
    while _running:
        try:
            data = sock.recv(4096)
            if not data:
                break
        except socket.timeout:
            continue
        except OSError:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                print("[ROBOT] Bad JSON line ignored")
                continue
            mtype = msg.get("type")
            if mtype == "utterance":
                global _last_system
                text = str(msg.get("text", "")).strip()
                speaker = str(msg.get("speaker", "user")) or "user"
                system = msg.get("system")
                if system is not None:
                    with _system_lock:
                        _last_system = str(system)
                if text:
                    with _turn_lock:
                        _current_turn = msg.get("turn")
                    _set_last(f"{speaker}: {text}")
            elif mtype == "cancel":
                # The robot stopped listening for this turn (barge-in). The
                # agent's reply may already be in flight; the adapter drops
                # stale says by turn id, so just log it here.
                print(f"[ROBOT] Turn {msg.get('turn')} cancelled by robot")
            elif mtype == "ping":
                _send_json(sock, {"type": "pong"})
    print(f"[ROBOT] {robot_name}@{addr} disconnected")


def _accept_loop(port):
    global _running, _server_sock, _client_sock
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", int(port)))
    srv.listen(2)
    _server_sock = srv
    print(f"[ROBOT] Robot channel listening on port {port}")
    while _running:
        try:
            sock, addr = srv.accept()
        except OSError:
            break
        try:
            robot_name = _handshake(sock, addr)
        except (OSError, ValueError):
            robot_name = None
        if robot_name is None:
            try:
                sock.close()
            except OSError:
                pass
            continue
        with _client_lock:
            old = _client_sock
            _client_sock = sock
        if old is not None:
            print("[ROBOT] New robot connection replaces the previous one")
            try:
                old.close()
            except OSError:
                pass
        print(f"[ROBOT] Robot '{robot_name}' connected from {addr}")
        threading.Thread(
            target=_client_loop, args=(sock, addr, robot_name), daemon=True
        ).start()


def start_robot(port):
    global _running
    if _running:
        return
    _running = True
    threading.Thread(target=_accept_loop, args=(port,), daemon=True).start()
