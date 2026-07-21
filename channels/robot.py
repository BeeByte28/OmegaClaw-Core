"""Robot channel: a robot-agnostic text bridge for embodied deployments.

OmegaClaw acts as the server; a robot's voice pipeline connects as a TCP
client and exchanges newline-delimited JSON messages:

  robot -> agent:  {"type": "hello", "robot": "<name>", "auth": "<secret>"}
                   {"type": "utterance", "text": "...", "speaker": "user",
                    "system": "<system prompt: persona + animation +
                                inline-skill grammar + live places + nav mode>"}
                   {"type": "cancel"}
                   {"type": "ping"}
                   {"type": "look_result", "id": 7, "ok": true, "text": "..."}
                   {"type": "look_result", "id": 7, "ok": false,
                    "code": "NO-CAMERA", "error": "..."}
  agent -> robot:  {"type": "hello_ack", "ok": true, "auth": "ok"|"off"}
                   {"type": "say", "text": "..."}
                   {"type": "pong"}
                   {"type": "look_request", "id": 7, "question": "..."}

The agent cannot see: the camera frame lives in the robot's pipeline, and the
agent's model is not necessarily a vision model. ``look_request`` asks the robot
to describe what its camera sees right now and answer back in TEXT, so vision is
a skill the agent invokes when it needs it rather than an image bolted onto every
prompt. It is the only request the agent makes of the robot, hence the only place
an id is needed -- to stop a late reply satisfying a newer request.

There are no turn ids. The agent is a continuous loop that speaks and acts on
its own schedule, so every ``say`` is delivered on arrival: into the person's
open reply stream if there is one, otherwise spoken/acted on as unprompted
speech. Keeping the robot quiet on idle cycles is a prompt concern, not a
transport one.

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
import re
import socket
import threading
import time

import auth

_running = False
_server_sock = None
_client_sock = None
_client_lock = threading.Lock()
_last_message = ""
_msg_lock = threading.Lock()
_last_system = ""
_system_lock = threading.Lock()
_reminders = []          # (due_epoch_seconds, text), earliest not necessarily first
_reminders_lock = threading.Lock()
_look_pending = {}       # request id -> [threading.Event, result string or None]
_look_seq = 0
_look_lock = threading.Lock()
# The robot round-trips to a vision model to answer; a couple of seconds is
# normal. Matches the pipeline's own llm_timeout.
_LOOK_TIMEOUT_SEC = 15.0


# One line per message crossing the channel, in both directions, so a single
# grep over the compose log reconstructs a whole turn:
#
#   ROBOT SENT      pipeline forwarded the person's utterance   (pipeline)
#   OMEGACLAW RECV  the agent received it                       (here)
#   OMEGACLAW SENT  the agent emitted a say, with its outcome    (here)
#   ROBOT RECV      the pipeline received that say              (pipeline)
#   ROBOT SAYS      the sentence reached TTS                     (pipeline)
#
# A missing link names the hop that dropped it. Text is truncated: these are
# for correlating, and a full say would bury the surrounding log.
_LOG_TEXT_MAX = 200


def _log_channel(tag, text, detail=""):
    suffix = f" ({detail})" if detail else ""
    print(f"[ROBOT] {tag}{suffix}: {str(text)[:_LOG_TEXT_MAX]!r}", flush=True)


def _set_last(msg):
    global _last_message
    with _msg_lock:
        if _last_message == "":
            _last_message = msg
        else:
            _last_message = _last_message + " | " + msg


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Unit words that must never be swallowed as message text: "remind 1 hour call
# mum" would otherwise parse as 1 second saying "hour call mum" -- a reminder
# that looks scheduled, fires almost immediately, and says something garbled.
_UNIT_WORDS = ("s", "sec", "secs", "second", "seconds",
               "m", "min", "mins", "minute", "minutes",
               "h", "hr", "hrs", "hour", "hours",
               "d", "day", "days")


def _dedupe_key(text):
    """Loose key for spotting a re-issued reminder: ignores case, spacing and
    trailing punctuation. Deliberately not fuzzy -- two genuinely different
    reminders set seconds apart ("check the oven" / "take it out") must both
    survive, so only near-identical text is treated as a repeat."""
    return " ".join(str(text).lower().split()).strip(" .!?,")


def _parse_delay(token):
    """Seconds from "90" or "30s"/"5m"/"1h"/"2d". None if it is not a delay."""
    token = token.strip().lower()
    if not token:
        return None
    try:
        return float(token)          # bare number == seconds
    except ValueError:
        pass
    value, unit = token[:-1], token[-1:]
    if unit not in _UNIT_SECONDS:
        return None
    try:
        return float(value) * _UNIT_SECONDS[unit]
    except ValueError:
        return None


def remind(spec):
    """Schedule a reminder from "<delay> <text>", delivered as input when due.

    Delay is one token: seconds ("90") or a unit suffix ("30s", "5m", "1h",
    "2d"). It must not be split across tokens -- see _UNIT_WORDS.

    Timing cannot live in the agent's reasoning: the loop only calls the LLM
    while it has budget, so between turns it is idle for wakeupInterval and a
    short deadline would silently pass unnoticed. Here a real clock owns the
    deadline and the due text is surfaced by getLastMessage, which the loop
    polls every tick -- that counts as new input, which refills the budget, so
    the agent is thinking again at the moment the reminder comes due.
    """
    parts = str(spec).strip().split(None, 1)
    if len(parts) < 2:
        return "REMIND-FAILED-EXPECTED: remind <delay> <what to say>, e.g. remind 5m call mum"
    seconds = _parse_delay(parts[0])
    if seconds is None:
        return ("REMIND-FAILED-BAD-DELAY: first token must be seconds or "
                "<number><s|m|h|d>, e.g. 90 or 5m or 1h")
    if seconds < 0:
        return "REMIND-FAILED-DELAY-MUST-NOT-BE-NEGATIVE"
    text = parts[1].strip()
    # Reject a unit written as its own word, rather than reading it as message
    # text and scheduling a wildly wrong deadline.
    first_word = text.split(None, 1)[0].lower().rstrip(".,") if text else ""
    if first_word in _UNIT_WORDS:
        return ("REMIND-FAILED-AMBIGUOUS-DELAY: write the delay as one token, "
                f"e.g. remind {parts[0]}{first_word[0]} <what to say>")
    with _reminders_lock:
        # Idle cycles make the agent re-issue commands it already ran, and a
        # repeated reminder is worse than a repeated pin: the robot says the
        # same thing to the person twice. Only pending ones are compared, so
        # the same reminder can be set again once it has fired.
        key = _dedupe_key(text)
        if any(_dedupe_key(pending) == key for _, pending in _reminders):
            return "REMIND-ALREADY-SCHEDULED: an identical reminder is still pending, do not set it again"
        _reminders.append((time.time() + seconds, text))
    return "REMIND-SCHEDULED"


def _due_reminders():
    """Pop every reminder whose deadline has passed."""
    now = time.time()
    with _reminders_lock:
        due = [text for due_at, text in _reminders if due_at <= now]
        _reminders[:] = [(d, t) for d, t in _reminders if d > now]
    return due


def getLastMessage():
    global _last_message
    with _msg_lock:
        tmp = _last_message
        _last_message = ""
    if tmp:
        print(f"[ROBOT] getLastMessage: {tmp}", flush=True)
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


_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"))


def _is_bracketed(text):
    """True if the whole string sits inside one matching bracket pair."""
    for opener, closer in _BRACKET_PAIRS:
        if len(text) > 2 and text.startswith(opener) and text.endswith(closer):
            depth = 0
            for i, ch in enumerate(text):
                depth += (ch == opener) - (ch == closer)
                # Closed before the end: two groups, e.g. "(a) and (b)".
                if depth == 0 and i < len(text) - 1:
                    return False
            return depth == 0
    return False


_INLINE_TAG_RE = re.compile(r"\|[^|]*\|")
# Every <...> tag, not just a lone one: the agent emits "<response>" but also
# "<reply></reply>", which a single-tag pattern misses.
_MARKUP_RE = re.compile(r"<[^>]*>")


def _is_speakable(text):
    """False for scaffolding that must never reach the robot's voice.

    The agent sometimes emits raw generation artefacts as a say -- "_", or a
    lone "<response>" tag -- and anything sent here is read out loud in the
    room. A say carrying only inline |tags| is legitimate: that is a gesture
    with no speech.
    """
    without_tags = _INLINE_TAG_RE.sub("", text).strip()
    if not without_tags:
        return bool(_INLINE_TAG_RE.search(text))
    # Only used to decide; the original text is what gets sent.
    without_markup = _MARKUP_RE.sub("", without_tags).strip()
    if not without_markup:
        return False
    # Wholly parenthesised text is a stage direction, never speech. Told to
    # reply with nothing, the LLM narrates the absence instead -- "(empty
    # reply)", "(no output)" -- and the robot reads that out loud. Speech is
    # never entirely inside brackets, so this needs no list of phrases.
    if _is_bracketed(without_markup):
        return False
    return any(ch.isalnum() for ch in without_markup)


def _say(text):
    text = str(text)
    # Logged on every path including the drops: a say the agent believes it made
    # is exactly what we need to line up against the robot's ROBOT RECV.
    if not _is_speakable(text):
        _log_channel("OMEGACLAW SENT", text, "dropped, not speakable")
        return "SEND-SKIPPED-NOT-SPEAKABLE: that was not words to say out loud"
    with _client_lock:
        sock = _client_sock
    if sock is None:
        _log_channel("OMEGACLAW SENT", text, "dropped, no robot connected")
        return "NO-ROBOT-CONNECTED"
    msg = {"type": "say", "text": text}
    if _send_json(sock, msg):
        _log_channel("OMEGACLAW SENT", text)
        return "SEND-SUCCESS"
    _log_channel("OMEGACLAW SENT", text, "send failed, robot likely disconnected")
    return "SEND-FAILURE"


def send_message(text):
    """Say something to the person; delivered whether or not they are waiting."""
    return _say(text)


def look(question=""):
    """Ask the robot what its camera sees right now; returns a description.

    Blocks until the robot answers, because the agent has nothing useful to do
    with a promise -- the next thing it does is talk about what it saw.

    Every failure returns a loud LOOK-FAILED-* string rather than an empty one.
    An empty result reads as "nothing to report", and the agent will describe a
    scene it never saw: exactly the confabulation this skill exists to remove.
    """
    global _look_seq
    with _client_lock:
        sock = _client_sock
    if sock is None:
        return "NO-ROBOT-CONNECTED"
    done = threading.Event()
    with _look_lock:
        _look_seq += 1
        req_id = _look_seq
        _look_pending[req_id] = [done, None]
    request = {"type": "look_request", "id": req_id, "question": str(question or "").strip()}
    if not _send_json(sock, request):
        with _look_lock:
            _look_pending.pop(req_id, None)
        return "LOOK-FAILED-SEND: the robot disconnected"
    if not done.wait(_LOOK_TIMEOUT_SEC):
        # Leave nothing behind for a late reply to fill in.
        with _look_lock:
            _look_pending.pop(req_id, None)
        return f"LOOK-FAILED-TIMEOUT: no answer from the camera within {int(_LOOK_TIMEOUT_SEC)}s"
    with _look_lock:
        result = _look_pending.pop(req_id, [None, None])[1]
    return result or "LOOK-FAILED-VISION-ERROR: the robot returned an empty description"


def _resolve_look(msg):
    """Hand a look_result to the waiting look() call, if it is still waiting.

    Unmatched ids are dropped: a reply that arrives after its request timed out
    must not satisfy the next one, which would describe the room as it was
    seconds ago and look like a working answer.
    """
    req_id = msg.get("id")
    with _look_lock:
        slot = _look_pending.get(req_id)
        if slot is None:
            print(f"[ROBOT] Stale look_result id={req_id!r} ignored", flush=True)
            return
        if msg.get("ok"):
            slot[1] = str(msg.get("text", "")).strip()
        else:
            code = str(msg.get("code", "VISION-ERROR")).strip() or "VISION-ERROR"
            slot[1] = f"LOOK-FAILED-{code}: {msg.get('error', 'no detail given')}"
        slot[0].set()


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
    global _client_sock
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
                # Logged on arrival, not when the loop polls it: getLastMessage
                # only prints once the loop drains it, so a message that arrives
                # and is never picked up looks identical to one that never came.
                if text:
                    _set_last(f"{speaker}: {text}")
                    _log_channel("OMEGACLAW RECV", f"{speaker}: {text}")
                else:
                    _log_channel("OMEGACLAW RECV", "",
                                 f"no text, system prompt only, {len(str(system or ''))} chars")
            elif mtype == "cancel":
                # The person stopped listening (barge-in). A reply may
                # already be in flight; nothing to reconcile, so just log it.
                print("[ROBOT] Reply cancelled by robot")
            elif mtype == "look_result":
                _resolve_look(msg)
            elif mtype == "ping":
                _send_json(sock, {"type": "pong"})
    # Forget the socket on the way out, or every later say is written into a
    # dead one: sendall succeeds against a closed peer until TCP gives up, so
    # the agent reports SEND-SUCCESS for words nobody hears. Identity-checked
    # because a newly accepted robot may already have taken the slot.
    with _client_lock:
        if _client_sock is sock:
            _client_sock = None
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
