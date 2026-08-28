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
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

import challenger_hook as ch  # noqa: E402  (shared config, gates, state, logging)

PLACEHOLDER = f"{ch.CHALLENGER_TAG} Draft report withheld; the edited version follows."
ASK_PLACEHOLDER = (
    f"{ch.CHALLENGER_TAG} (answering the report editor's clarification questions)"
)


DRAFT_DIR = os.path.join(".claude", "challenger-drafts")
DRAFT_MAX_AGE = 3 * 86_400  # seconds a stashed draft survives before the sweep


def stash(label, text, cwd, session_id, message_id):
    """Park the hidden text in a file and return a link line pointing at it.

    There is no foldout to hide it behind: the message stream renders no raw
    HTML and has no collapsible syntax, so an earlier <details> attempt simply
    printed the markup around the draft it was meant to hide. A link keeps the
    draft one click away instead - but the app only opens files under the
    session's working directory, so the stash lives in the project rather than
    the temp dir. The directory carries a "*" .gitignore, which ignores the
    whole directory including itself: nothing appears in `git status` and the
    project's own ignore rules are never touched.

    Returns "" if the file cannot be written - the placeholder then stands
    alone, and the original is still in the transcript and in verbose mode.
    """
    try:
        directory = os.path.join(cwd, DRAFT_DIR)
        os.makedirs(directory, exist_ok=True)
        ignore = os.path.join(directory, ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as f:
                f.write("*\n")
        safe = "".join(c for c in f"{session_id}-{message_id}" if c.isalnum() or c in "-_")
        name = f"{safe[:80] or 'draft'}.md"
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            f.write(text)
        _prune_drafts(directory)
        return f"\n\n[{label}]({DRAFT_DIR.replace(os.sep, '/')}/{name})"
    except OSError as e:
        ch.log(f"display: could not stash the draft: {e!r}")
        return ""


def _prune_drafts(directory):
    """Stashed drafts are read once if at all; sweep the stale ones."""
    cutoff = time.time() - DRAFT_MAX_AGE
    try:
        entries = os.scandir(directory)
    except OSError:
        return
    with entries:
        for entry in entries:
            if not entry.name.endswith(".md"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    os.remove(entry.path)
            except OSError:
                pass


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
    cwd = payload.get("cwd", "")
    if not ch.project_enabled(cwd):
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
        emit(ASK_PLACEHOLDER + stash("Show the answers", text, cwd, session_id, message_id))
    if len(text) >= ch.MIN_CHARS:
        state["hidden_any"] = True
        ch.save_display_state(session_id, state)
        ch.log(f"display: withheld draft, {len(text)} chars (session {session_id})")
        emit(PLACEHOLDER + stash("Show the original draft", text, cwd, session_id, message_id))
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
