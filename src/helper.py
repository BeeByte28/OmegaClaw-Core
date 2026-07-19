from collections import deque
import json
import re
from datetime import datetime

TS_RE = re.compile(r'^\("(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"')
# Must stay in sync with the skills advertised in src/skills.metta (getSkills):
# an advertised command missing here is treated as speech and wrapped in `send`
# (see _coerce_speech_lines) or swallowed into a preceding send, instead of run.
LLM_COMMANDS = {
    "append-file",
    "episodes",
    "metta",
    "pin",
    "query",
    "read-file",
    "remember",
    "remind",
    "send",
    "shell",
    "tavily-search",
    "technical-analysis",
    "websearch",
    "write-file",
}


def extract_timestamp(line):
    m = TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def around_time(needle_time_str, k):
    needle_time_str = needle_time_str.replace(r'\"', '').replace('"', '').strip()
    filename = "repos/OmegaClaw-Core/memory/history.metta"
    target = datetime.strptime(needle_time_str, "%Y-%m-%d %H:%M:%S")
    best_lineno = None
    best_line = None
    best_diff = None
    buffer = []
    best_idx = None
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            buffer.append((lineno, line))
            ts = extract_timestamp(line)
            if ts is None:
                continue
            diff = abs((ts - target).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_lineno = lineno
                best_line = line
                best_idx = len(buffer) - 1
    if best_lineno is None:
        return
    start = max(0, best_idx - k)
    end = min(len(buffer), best_idx + k + 1)
    ret = ""
    for lineno, line in buffer[start:end]:
        ret += f"{lineno}:{line}"
    return ret


def _strip_outer_parens(line):
    if line.startswith("(") and line.endswith(")"):
        return line[1:-1].strip()
    return line


def _get_command_name(line):
    normalized = line.strip()
    while normalized.startswith("("):
        normalized = normalized[1:].lstrip()
    while normalized.endswith(")"):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        return ""
    return normalized.split(maxsplit=1)[0]


def _is_known_command(line):
    return _get_command_name(line) in LLM_COMMANDS


def _decode_quoted_arg(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _merge_send_continuations(lines):
    merged = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if _get_command_name(line) != "send":
            merged.append(line)
            idx += 1
            continue

        send_wrapped = line.strip().startswith("(")
        head = line.strip()
        while head.startswith("("):
            head = head[1:].lstrip()
        parts = head.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        decoded_payload = _decode_quoted_arg(payload) if payload.startswith('"') else None
        text = decoded_payload if decoded_payload is not None else payload

        idx += 1
        continuations = []
        while idx < len(lines) and not _is_known_command(lines[idx]):
            continuation = lines[idx].strip()
            if send_wrapped and continuation.endswith(")"):
                continuation = continuation[:-1].rstrip()
                continuations.append(continuation)
                idx += 1
                break
            continuations.append(continuation)
            idx += 1

        if continuations:
            if text:
                text = text + "\n" + "\n".join(continuations)
            else:
                text = "\n".join(continuations)
            merged.append(f"send {json.dumps(text, ensure_ascii=False)}")
        else:
            merged.append(line)
    return merged


def _coerce_speech_lines(lines):
    """A line whose leading token is not a known command is speech the LLM
    forgot to wrap in `send`; wrap it so it still reaches the user instead of
    evaluating to a silent no-op. Run AFTER _merge_send_continuations so genuine
    multi-line sends are already coalesced. Paren-only lines (no command name)
    and the pin shorthand ('-...') are left for the main loop to handle."""
    coerced = []
    for line in lines:
        stripped = line.strip()
        name = _get_command_name(line)
        if (not name or name in LLM_COMMANDS
                or stripped.startswith("-") or stripped.startswith("(-")):
            coerced.append(line)
        else:
            coerced.append("send " + stripped)
    return coerced


def balance_parentheses(s):
    s = s.replace("_quote_", '"').replace("_newline_", "\n")
    sexprs = []
    special_two_arg_cmds = {"write-file", "append-file"}
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    lines = _merge_send_continuations(lines)
    lines = _coerce_speech_lines(lines)
    for line in lines:
        if line.startswith("(-"):
            line = "(pin -" + line[2:]
        elif line.startswith("-"):
            line = "pin " + line
        # remove one outer (...) if present
        line = _strip_outer_parens(line)
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        cmd = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in special_two_arg_cmds:
            if not rest:
                sexprs.append(f"({cmd})")
                continue
            # filename is first token unless already quoted
            if rest.startswith('"'):
                end = 1
                escaped = False
                while end < len(rest):
                    ch = rest[end]
                    if ch == '"' and not escaped:
                        break
                    escaped = (ch == '\\' and not escaped)
                    if ch != '\\':
                        escaped = False
                    end += 1
                if end < len(rest) and rest[end] == '"':
                    filename = rest[:end+1]
                    content = rest[end+1:].strip()
                else:
                    filename = '"' + rest[1:].replace('"', '\\"') + '"'
                    content = ""
            else:
                split_rest = rest.split(maxsplit=1)
                filename = '"' + split_rest[0].replace('"', '\\"') + '"'
                content = split_rest[1].strip() if len(split_rest) > 1 else ""
            if content:
                if content.startswith('"') and content.endswith('"'):
                    sexprs.append(f"({cmd} {filename} {content})")
                else:
                    content = content.replace('"', '\\"')
                    sexprs.append(f'({cmd} {filename} "{content}")')
            else:
                sexprs.append(f"({cmd} {filename})")
            continue
        if rest:
            if rest.startswith('"') and rest.endswith('"'):
                sexprs.append(f"({cmd} {rest})")
            else:
                rest = rest.replace('"', '\\"')
                sexprs.append(f'({cmd} "{rest}")')
        else:
            sexprs.append(f"({cmd})")
    ret = " ".join(sexprs)
    return "(" + ret + ")"


def normalize_string(x):
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", errors="ignore")
        return str(x).encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return str(x)


def test_balance_parenthesis():
    assert balance_parentheses('(write-file test.txt hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(append-file test.txt hello world)') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file "test.txt" hello world)') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file "test.txt" "hello world")') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(write-file test.txt "hello world")') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('(send test.xt hello world)') == '((send "test.xt hello world"))'
    assert balance_parentheses('write-file test.txt hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('append-file test.txt hello world') == '((append-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file "test.txt" hello world') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file "test.txt" "hello world"') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('write-file test.txt "hello world"') == '((write-file "test.txt" "hello world"))'
    assert balance_parentheses('send test.xt hello world') == '((send "test.xt hello world"))'
    assert balance_parentheses('send Here are the planets:\n1. Mercury\n2. Venus') == '((send "Here are the planets:\\n1. Mercury\\n2. Venus"))'
    assert balance_parentheses('send Here are the options:\n- MacBook Air\n- ThinkPad X1\npin done') == '((send "Here are the options:\\n- MacBook Air\\n- ThinkPad X1") (pin "done"))'
    assert balance_parentheses('send "Plain text version:"\n**Mars** - red planet\nNote: Pluto is a dwarf planet') == '((send "Plain text version:\\n**Mars** - red planet\\nNote: Pluto is a dwarf planet"))'
    assert balance_parentheses('(send Here are the planets:\n1. Mercury\n2. Venus)') == '((send "Here are the planets:\\n1. Mercury\\n2. Venus"))'
    assert balance_parentheses('send "hello" world') == '((send "\\"hello\\" world"))'
    # bare "()" lines yield no tokens after _strip_outer_parens and must be skipped, not crash
    assert balance_parentheses('()') == '()'
    assert balance_parentheses('') == '()'
    assert balance_parentheses('   ') == '()'
    assert balance_parentheses('()\nsend hello') == '((send "hello"))'
    # bare speech the LLM forgot to wrap in `send` must still reach the user
    assert balance_parentheses('Huh-heh-heh! |smile,1.5,0.7| |yes_once|') == '((send "Huh-heh-heh! |smile,1.5,0.7| |yes_once|"))'
    assert balance_parentheses('Already got that one\nremember foo') == '((send "Already got that one") (remember "foo"))'
    assert balance_parentheses('Coming over now |@come_to_me|') == '((send "Coming over now |@come_to_me|"))'
    # real commands and pin shorthand are untouched by the coercion
    assert balance_parentheses('send hi\nremember x\npin y') == '((send "hi") (remember "x") (pin "y"))'
    assert balance_parentheses('- note this') == '((pin "- note this"))'
    # every advertised skill must be recognized, not coerced to send
    assert balance_parentheses('websearch weather in tokyo') == '((websearch "weather in tokyo"))'
    assert balance_parentheses('send checking\nwebsearch tokyo') == '((send "checking") (websearch "tokyo"))'


if __name__ == "__main__":
    test_balance_parenthesis()
