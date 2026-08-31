"""The Challenger - Stop hook entry point.

Reads the Stop hook payload from stdin. Non-enabled projects, short responses,
and non-Opus-5 sessions are waved through programmatically. Anything else goes
to a report editor (OpenAI gpt-5.6-sol via the codex CLI bridge by default,
Claude via CHALLENGER_CRITIC=claude) that rewrites the response into the
high-level report the user prefers. The editor may first ask the coding agent
clarification questions (bounded rounds). Its final report is delivered by
having the agent post it verbatim, or - with CHALLENGER_DISPLAY_EDIT on -
drawn by the display companion in place of the draft, leaving this hook
nothing to do but allow. Fails open: any error allows the stop.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_config(path):
    """Minimal KEY=VALUE reader: # comments, optional surrounding quotes.

    Settings only - no credentials live here; both editor backends authenticate
    through their own CLI. Values are pushed into the environment, so a real
    environment variable always wins over the file and any setting can be
    overridden for a single run without editing anything.

    No dependency on python-dotenv: a hook that runs on every stop should not
    need a virtualenv.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_config(os.path.join(PROJECT_DIR, "challenger.conf"))


def _env(name, default=""):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


def _env_path(name, default):
    """Like _env, but relative paths resolve against the repo, not the session.

    The hook runs with the *session's* working directory, so a relative path in
    challenger.conf would otherwise point at whatever project is being edited.
    """
    value = _env(name, default)
    return value if os.path.isabs(value) else os.path.join(PROJECT_DIR, value)


EDITOR_PROMPT_PATH = _env_path("CHALLENGER_PROMPT", "critic-prompt.md")
CRITIC_SETTINGS_PATH = os.path.join(PROJECT_DIR, "critic-settings.json")
LOG_PATH = _env_path("CHALLENGER_LOG", "hook-debug.log")

# The hook is registered globally (~/.claude/settings.json), so it runs for every
# session on the machine; this list is what actually decides which ones are edited.
# Set CHALLENGER_PROJECTS in challenger.conf to an os.pathsep-separated list of
# project roots (";" on Windows, ":" elsewhere). Empty list = the Challenger is
# installed but inert. Worktrees under an enabled root are covered by prefix match.
ENABLED_PROJECTS = [
    part.strip() for part in _env("CHALLENGER_PROJECTS").split(os.pathsep) if part.strip()
]

MIN_CHARS = _env_int("CHALLENGER_MIN_CHARS", 1750)   # shorter responses are never edited
MAX_ASK_ROUNDS = _env_int("CHALLENGER_MAX_ASK_ROUNDS", 2)  # clarification rounds before giving up
TARGET_MODEL_PREFIX = _env("CHALLENGER_TARGET_MODEL", "claude-opus-5")  # only edit this model
CRITIC_BACKEND = _env("CHALLENGER_CRITIC", "codex")  # "codex" (OpenAI) or "claude"
CRITIC_MODEL = _env("CHALLENGER_CRITIC_MODEL", "claude-fable-5")  # claude backend
CLAUDE_BIN = _env("CHALLENGER_CLAUDE_BIN", "claude")
CODEX_BRIDGE = _env_path("CHALLENGER_CODEX_BRIDGE", os.path.join("vendor", "ask_codex.py"))
CODEX_MODEL = _env("CHALLENGER_CODEX_MODEL", "gpt-5.6-sol")
CODEX_EFFORT = _env("CHALLENGER_CODEX_EFFORT", "high")  # try lowering once happy
CRITIC_NAME = CODEX_MODEL if CRITIC_BACKEND == "codex" else CRITIC_MODEL
CRITIC_TIMEOUT = _env_int("CHALLENGER_TIMEOUT", 300)  # seconds; keep below the hook's own timeout
# Let the display companion run the editor and draw the finished report in
# place of the draft, instead of blocking for the agent to repost it. Off by
# default: it only works if the MessageDisplay hook entry raises its timeout
# past the editor's, and at the platform default of 10s every draft would
# stall and then fail open to the raw text. See README.md.
DISPLAY_EDIT = _env("CHALLENGER_DISPLAY_EDIT", "0").strip().lower() not in ("0", "false", "no", "")
MAX_RESPONSE_CHARS = 100_000  # truncate pathological inputs to the editor

CHALLENGER_TAG = "[The Challenger]"
ECHO_INSTRUCTION = (
    f"{CHALLENGER_TAG} A report editor has rewritten your report for the user. "
    "Post the edited report below verbatim as your next message - the full text, "
    "no additions, no commentary, no mention of the editing process. A trailing "
    "link line, where the text below carries one, is part of the report: post "
    "that too. "
    "One exception: if the rewrite gets facts wrong - invented details, dropped "
    "caveats, broken citations - post your original report verbatim instead, "
    "also without commentary. Style is the editor's call and is not grounds "
    "for rejection.\n\n"
)
ASK_INSTRUCTION = (
    f"{CHALLENGER_TAG} A report editor is preparing your report for the user and "
    "needs clarification first. Answer these questions directly and concisely:\n\n"
)
REPOST_INSTRUCTION = (
    f"{CHALLENGER_TAG} The report editing round could not complete, and your "
    "report had already been hidden from the user's screen pending the edit. "
    "Post your report again verbatim as your next message - the full text, "
    "no additions, no commentary:\n\n"
)


def draft_link(label, target):
    """The line that links a draft the display companion stashed away."""
    return f"\n\n[{label}]({target})" if target else ""


def log(message):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512_000:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                kept = f.read()[-100_000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write("[log trimmed]\n" + kept)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def project_enabled(cwd):
    try:
        cwd = os.path.normcase(os.path.abspath(cwd))
    except (OSError, ValueError, TypeError):
        return False
    for root in ENABLED_PROJECTS:
        root = os.path.normcase(os.path.abspath(root))
        if cwd == root or cwd.startswith(root + os.sep):
            return True
    return False


def state_path(session_id):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"challenger-{safe}.json")


def load_state(session_id):
    try:
        with open(state_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(session_id, state):
    try:
        with open(state_path(session_id), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def clear_state(session_id):
    try:
        os.remove(state_path(session_id))
    except OSError:
        pass


# challenger_display_hook.py (the optional MessageDisplay companion) hides
# would-be-edited drafts as they render and keeps its per-turn bookkeeping in a
# second temp file. The Stop hook reads it to learn whether the draft the user
# was supposed to see is currently hidden, and clears it when a turn resolves.
# If the companion is not registered the file never exists and nothing changes.

def display_state_path(session_id):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"challenger-display-{safe}.json")


def load_display_state(session_id):
    try:
        with open(display_state_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_display_state(session_id, state):
    try:
        with open(display_state_path(session_id), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def clear_display_state(session_id):
    try:
        os.remove(display_state_path(session_id))
    except OSError:
        pass


def allow(message=None) -> NoReturn:
    # ensure_ascii (the default) keeps stdout pure ASCII regardless of the
    # console codepage Windows gives the hook process.
    if message:
        print(json.dumps({"systemMessage": message}))
    sys.exit(0)


def block(reason, message=None) -> NoReturn:
    out = {"decision": "block", "reason": reason}
    if message:
        out["systemMessage"] = message
    print(json.dumps(out))
    sys.exit(0)


def read_transcript_tail(transcript_path):
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - 500_000))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def session_model(tail):
    """Best-effort read of the session's model from the transcript tail.

    Assistant entries in the transcript JSONL carry message.model. Returns the
    most recent one (skipping subagent sidechains), or None if undeterminable.
    """
    for line in reversed(tail.splitlines()):
        if '"model"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("isSidechain"):
            continue
        message = obj.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            return message["model"]
    return None


def last_user_text(tail):
    """Best-effort read of the user's most recent real prompt from the transcript.

    Skips tool results, sidechains, meta entries, command XML, and the
    Challenger's own injected instructions. Returns None if nothing usable.
    """
    for line in reversed(tail.splitlines()):
        if '"user"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "user" or obj.get("isSidechain") or obj.get("isMeta"):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        else:
            continue
        text = "\n".join(t for t in texts if t).strip()
        if not text or text.startswith("<") or text.startswith(CHALLENGER_TAG):
            continue
        return text
    return None


def run_editor(original, user_context=None, exchange=None, note=None):
    """Returns {"action": ..., "message": ...} or None on any failure (fail open).

    `note` is appended to the editor's instructions for this call alone - the
    display companion uses it to warn that the text it is handing over may be
    a mid-turn progress message rather than a finished report.
    """
    with open(EDITOR_PROMPT_PATH, encoding="utf-8") as f:
        prompt = f.read()
    if note:
        prompt += "\n\n" + note
    if user_context:
        prompt += "\n\n--- USER'S REQUEST ---\n\n" + user_context[:4000]
    if exchange:
        lines = ["\n\n--- CLARIFICATION EXCHANGE (your earlier questions and the coding agent's answers) ---\n"]
        for i, qa in enumerate(exchange, 1):
            lines.append(f"\nYour questions (round {i}):\n{qa.get('q', '')[:8000]}")
            if qa.get("a"):
                lines.append(f"\nThe coding agent's answers (round {i}):\n{qa['a'][:20000]}")
        prompt += "\n".join(lines)
    prompt += "\n\n--- RESPONSE TO EDIT ---\n\n" + original[:MAX_RESPONSE_CHARS]

    raw = _run_codex(prompt) if CRITIC_BACKEND == "codex" else _run_claude(prompt)
    if raw is None:
        return None
    try:
        obj = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except ValueError as e:
        log(f"could not parse editor output: {e}; text={raw[:500]}")
        return None
    action, text = obj.get("action"), obj.get("message")
    if action not in ("echo_to_user", "ask_model") or not isinstance(text, str) or not text.strip():
        log(f"invalid editor output: action={action!r}")
        return None
    return obj


def _run_claude(prompt):
    cmd = (
        f'{CLAUDE_BIN} -p --model {CRITIC_MODEL} --output-format json '
        f'--settings "{CRITIC_SETTINGS_PATH}"'
    )
    try:
        result = subprocess.run(
            cmd, shell=True, input=prompt, capture_output=True,
            text=True, encoding="utf-8", timeout=CRITIC_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"claude editor failed: {e}")
        return None
    if result.returncode != 0:
        log(f"claude editor exited {result.returncode}: {result.stderr[:500]}")
        return None
    try:
        return json.loads(result.stdout)["result"]
    except (ValueError, KeyError, TypeError) as e:
        log(f"could not parse claude -p wrapper: {e}; stdout={result.stdout[:500]}")
        return None


def _run_codex(prompt):
    fd, path = tempfile.mkstemp(suffix=".md", prefix="challenger-editor-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = [sys.executable, CODEX_BRIDGE, "--prompt-file", path,
               "--effort", CODEX_EFFORT, "--timeout", str(CRITIC_TIMEOUT)]
        if CODEX_MODEL:
            cmd += ["--model", CODEX_MODEL]
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            timeout=CRITIC_TIMEOUT + 30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"codex editor failed: {e}")
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if result.returncode != 0:
        log(f"codex editor exited {result.returncode}: {(result.stderr or result.stdout)[:500]}")
        return None
    return result.stdout


def fail_open(session_id, original=None, note=None) -> NoReturn:
    """Allow the stop - unless the display hook hid a draft that now has no
    edited replacement coming, in which case have the agent repost it. Without
    this, an editor failure after the draft was hidden would leave the user
    with nothing but the placeholder."""
    if original and load_display_state(session_id).get("hidden_any"):
        save_state(session_id, {"phase": "echo"})
        log(f"repost: editor round failed but draft was hidden (session {session_id})")
        block(
            REPOST_INSTRUCTION + original,
            "The Challenger: editor unavailable; asking the agent to repost the hidden report.",
        )
    clear_state(session_id)
    clear_display_state(session_id)
    allow(note)


def count_questions(exchange):
    """How many questions the editor asked, across every round.

    It asks in prose rather than on a form, so the question marks are the
    count; a round that asks for something without one still counts as one.
    """
    return sum(qa.get("q", "").count("?") or 1 for qa in exchange)


def deliver_exchange(state):
    """Fold the clarification round into the stashed draft; return its link.

    The display companion hides the draft behind a placeholder and the agent's
    answers behind another, so without this the questions that shaped the
    report are only readable in verbose mode. Returns the line to put under the
    report - the link plus how many questions were asked - or "" when there was
    no exchange, no stashed draft, or the append failed, leaving the report to
    stand alone as it did before.
    """
    exchange = [qa for qa in state.get("exchange", []) if qa.get("q")]
    link = draft_link("Show the original draft", state.get("draft_link"))
    if not exchange or not link or not state.get("draft_file"):
        return ""
    section = ["\n\n---\n\n## The report editor's clarification round\n"]
    for i, qa in enumerate(exchange, 1):
        section.append(f"\n### The editor asked (round {i})\n\n{qa['q'].strip()}\n")
        if qa.get("a"):
            section.append(f"\n### The agent answered (round {i})\n\n{qa['a'].strip()}\n")
    try:
        with open(state["draft_file"], "a", encoding="utf-8") as f:
            f.write("".join(section))
    except OSError as e:
        log(f"could not append the clarification round to the draft: {e!r}")
        return ""
    asked = count_questions(exchange)
    return link + (f" - and the {asked} clarification question"
                   f"{'s' if asked != 1 else ''} the editor asked about it.")


def dispatch(session_id, result, state):
    """Act on an editor decision. `state` carries original/user_context/exchange."""
    if result["action"] == "echo_to_user":
        report = result["message"] + deliver_exchange(state)
        save_state(session_id, {"phase": "echo"})
        log(f"echo: delivering edited report (session {session_id})")
        block(
            ECHO_INSTRUCTION + report,
            f"The Challenger: report edited by {CRITIC_NAME}; delivering.",
        )
    asks = len(state.get("exchange", [])) + 1
    state["phase"] = "ask"
    state.setdefault("exchange", []).append({"q": result["message"]})
    save_state(session_id, state)
    log(f"ask: clarification round {asks} (session {session_id})")
    block(
        ASK_INSTRUCTION + result["message"],
        f"The Challenger: {CRITIC_NAME} asked for clarification (round {asks}).",
    )


def main():
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    session_id = payload.get("session_id", "unknown")
    message = payload.get("last_assistant_message") or ""
    revising = payload.get("stop_hook_active", False)

    cwd = payload.get("cwd", "")
    if not project_enabled(cwd):
        log(f"allow: {cwd!r} not an enabled project (session {session_id})")
        allow()

    if len(message) < MIN_CHARS and not revising:
        log(f"allow: {len(message)} chars < {MIN_CHARS} (session {session_id})")
        clear_state(session_id)
        clear_display_state(session_id)
        allow()

    tail = read_transcript_tail(payload.get("transcript_path", ""))
    model = session_model(tail)
    if not model or not model.startswith(TARGET_MODEL_PREFIX):
        log(f"allow: session model {model!r} is not {TARGET_MODEL_PREFIX}* (session {session_id})")
        clear_state(session_id)
        clear_display_state(session_id)
        allow()

    # A continuation stop carries our own state forward. A fresh stop normally
    # ignores whatever is left lying around, but the display companion writes
    # these two phases before a stop has happened at all, so they are honoured
    # either way.
    state = load_state(session_id)
    phase = state.get("phase")
    if not revising and phase not in ("delivered", "ask_pending"):
        state, phase = {}, None

    if phase == "echo":
        log(f"allow: edited report delivered (session {session_id})")
        clear_state(session_id)
        clear_display_state(session_id)
        allow("The Challenger: edited report delivered.")

    if phase == "delivered":
        # The display companion already drew the edited report. There is no
        # echo turn to ask for and nothing left to review.
        log(f"allow: report edited in place at display time (session {session_id})")
        clear_state(session_id)
        clear_display_state(session_id)
        allow(f"The Challenger: report edited by {CRITIC_NAME} and shown in place.")

    if phase == "ask_pending":
        # The display companion got questions instead of a report. It cannot
        # answer them mid-render, so it parked them here: block with them now
        # rather than paying for a second editor call to be asked again.
        exchange = state.get("exchange", [])
        questions = exchange[-1].get("q") if exchange else None
        if not questions:
            fail_open(session_id, state.get("original"))
        state["phase"] = "ask"
        save_state(session_id, state)
        log(f"ask: clarification round 1, asked at display time (session {session_id})")
        block(
            ASK_INSTRUCTION + questions,
            f"The Challenger: {CRITIC_NAME} asked for clarification (round 1).",
        )

    if phase == "ask":
        exchange = state.get("exchange", [])
        if exchange:
            exchange[-1]["a"] = message  # the agent's answer to the last questions
        if len(exchange) >= MAX_ASK_ROUNDS:
            # The editor gets the answers but must now produce a report; if it
            # asks again anyway, give up and ship what stands.
            result = run_editor(state.get("original", message),
                                state.get("user_context"), exchange)
            if result is None or result["action"] != "echo_to_user":
                log(f"clarification limit reached (session {session_id})")
                fail_open(session_id, state.get("original"),
                          "The Challenger: clarification limit reached; showing the original report.")
            dispatch(session_id, result, state)
        result = run_editor(state.get("original", message),
                            state.get("user_context"), exchange)
        if result is None:
            fail_open(session_id, state.get("original"))
        dispatch(session_id, result, state)

    # Fresh response (or revising with no/foreign state): edit it.
    clear_state(session_id)
    display = load_display_state(session_id)
    state = {"original": message, "user_context": last_user_text(tail),
             # Where the companion stashed this draft, if it did: a
             # clarification round is folded back into that same file.
             "draft_file": display.get("draft_file"),
             "draft_link": display.get("draft_link")}
    result = run_editor(message, state["user_context"])
    if result is None:
        fail_open(session_id, message)
    dispatch(session_id, result, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail open, never brick the session
        log(f"unexpected error: {e!r}")
        sys.exit(0)
