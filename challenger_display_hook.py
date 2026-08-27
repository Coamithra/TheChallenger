"""The Challenger - MessageDisplay companion hook (optional).

Hides would-be-edited Opus 5 drafts from the screen so the user reads one
report - the editor's - instead of the draft followed by the rewrite. This is
display-only by platform design: the transcript and the model's context keep
the original, and verbose mode still shows it.

MessageDisplay fires once per flush of newly completed lines while a message
renders (one flush per message carries final=true). Since a draft cannot be
un-rendered once shown, this hook buffers instead: for messages that pass the
same cheap gates as the Stop hook (project, model, phase) every delta is
swallowed, and on the final flush the whole message either appears at once
(too short to be edited) or collapses to a one-line placeholder (the Stop
hook's editor takes it from there). The cost in enabled projects is that
messages appear at end-of-message rather than streaming line by line.

If the editor round then fails open, the Stop hook notices the hidden draft
(via the shared display-state file) and has the agent repost it, so the
placeholder is never the end of the story.

Fails open like everything here: any error displays the original delta.
"""

import json
import os
import re
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

import challenger_hook as ch  # noqa: E402  (shared config, gates, state, logging)

PLACEHOLDER = f"{ch.CHALLENGER_TAG} Draft report withheld; the edited version follows."
ASK_PLACEHOLDER = (
    f"{ch.CHALLENGER_TAG} (answering the report editor's clarification questions)"
)


def foldout(summary, text):
    """The hidden text, collapsed behind a click. The desktop app renders
    <details> folded; the terminal TUI ignores displayContent altogether, so
    nothing needs to degrade gracefully there. A literal closing tag inside
    the text would end the foldout early - defuse it with a space."""
    text = re.sub(r"(?i)</(\s*)details", r"</ \1details", text)
    return f"<details><summary>{summary}</summary>\n\n{text}\n\n</details>"


def emit(text):
    # ensure_ascii keeps stdout safe for whatever codepage the console uses.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "MessageDisplay", "displayContent": text}}))
    sys.exit(0)


def show():
    sys.exit(0)  # no output = display the original delta


def classify(payload, session_id):
    """First flush of a message: decide its fate once, cache it in the state.

    "show"      - stream normally (edited report being delivered, wrong model,
                  or model undeterminable - fail open).
    "defer"     - buffer; a fresh draft that may go to the editor.
    "defer-ask" - buffer; the agent is answering the editor's questions.
    """
    phase = ch.load_state(session_id).get("phase")
    if phase == "echo":
        return "show"
    tail = ch.read_transcript_tail(payload.get("transcript_path", ""))
    model = ch.session_model(tail)
    if not model or not model.startswith(ch.TARGET_MODEL_PREFIX):
        return "show"
    return "defer-ask" if phase == "ask" else "defer"


def main():
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    if payload.get("agent_id"):  # subagent output is not a user-facing report
        show()
    if not ch.project_enabled(payload.get("cwd", "")):
        show()

    session_id = payload.get("session_id", "unknown")
    turn_id = payload.get("turn_id", "")
    message_id = payload.get("message_id", "")
    delta = payload.get("delta", "")
    final = bool(payload.get("final"))

    state = ch.load_display_state(session_id)
    if state.get("turn_id") != turn_id:
        state = {"turn_id": turn_id}  # new turn: drop everything, hidden_any included
    if state.get("message_id") != message_id:
        state.update({"message_id": message_id, "verdict": None, "buffer": ""})

    if state.get("verdict") is None:
        state["verdict"] = classify(payload, session_id)
        ch.save_display_state(session_id, state)
    if state["verdict"] == "show":
        show()

    state["buffer"] = state.get("buffer", "") + delta
    if not final:
        ch.save_display_state(session_id, state)
        emit("")

    text = state["buffer"]
    if state["verdict"] == "defer-ask":
        state["hidden_any"] = True
        ch.save_display_state(session_id, state)
        ch.log(f"display: collapsed ask-round answer, {len(text)} chars (session {session_id})")
        emit(ASK_PLACEHOLDER + "\n\n" + foldout("Show the answers", text))
    if len(text) >= ch.MIN_CHARS:
        state["hidden_any"] = True
        ch.save_display_state(session_id, state)
        ch.log(f"display: withheld draft, {len(text)} chars (session {session_id})")
        emit(PLACEHOLDER + "\n\n" + foldout("Show the original draft", text))
    ch.save_display_state(session_id, state)
    emit(text)  # too short to be edited: release it, just all at once


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail open: show the original delta
        ch.log(f"display: unexpected error: {e!r}")
        sys.exit(0)
