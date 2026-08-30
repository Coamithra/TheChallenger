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
(too short to be edited) or is handed to the editor.

With CHALLENGER_DISPLAY_EDIT on, that editor call happens here and its report
is drawn in place of the draft, with the original stashed and linked beneath
it, so the user reads one message and no echo turn is needed; the Stop hook
then only has to allow. Off (the default), the draft collapses to a one-line
placeholder and the Stop hook's editor takes it from there. The cost in
enabled projects either way is that messages appear at end-of-message rather
than streaming line by line.

If the editor round then fails open, the Stop hook notices the hidden draft
(via the shared display-state file) and has the agent repost it, so the
placeholder is never the end of the story.

Fails open like everything here: any error displays the original delta.
"""

import json
import os
import re
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

# Handed to the editor when it runs here. Nothing available at display time can
# tell the turn's final report from a long mid-turn message: the payload's
# "final" marks the last flush of *this* message, the transcript does not carry
# this message yet (only the previous one, whose stop_reason arrives a message
# too late), and the Stop hook runs strictly after display, so waiting for it
# deadlocks. Every message over the threshold is therefore edited, and the
# editor is told that this is what may have happened.
MIDTURN_NOTE = (
    "## About this particular message\n\n"
    "You are running while this message is still being displayed, so it may not "
    "be the end of the turn: it can be a progress note the agent wrote before "
    "carrying on working, rather than a finished report. Edit one of those as a "
    "progress note - short, about what is happening now - rather than dressing "
    "it up as a result, and do not ask for clarification about one, since there "
    "is nothing to report yet."
)

# A citation marker, file path, or backticked identifier that the report states
# and the draft never did is something the editor invented; see check_fidelity.
CITATION_RE = re.compile(r"\[[A-Z]{1,4}\d{0,3}\]")
CODE_SPAN_RE = re.compile(r"`([^`\n]{3,80})`")
PATH_RE = re.compile(r"(?:[\w.\-]*[\\/])?[\w\-]{2,}\.[A-Za-z]{2,4}\b")


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


def check_fidelity(report, sources):
    """Tokens the report states that nothing it was written from ever did.

    Until now the agent was the last check on the editor: it read the rewrite
    before posting it and could refuse one that had invented details (plan.md
    item 19 caught exactly that). Editing at display time removes that reader,
    so this is the mechanical remainder of the check. It cannot judge prose -
    only that every citation marker, file path, and backticked identifier in
    the report also appears in the draft or the user's request. Anything it
    flags falls back to the placeholder path, where the agent sees the rewrite
    again and the old escape hatch still applies.
    """
    haystack = "\n".join(s for s in sources if s).replace("\\", "/").lower()
    unsupported = []
    for pattern in (CITATION_RE, CODE_SPAN_RE, PATH_RE):
        for match in pattern.finditer(report):
            token = match.group(1) if pattern is CODE_SPAN_RE else match.group(0)
            if token.replace("\\", "/").lower() not in haystack:
                unsupported.append(token)
    return sorted(set(unsupported))[:5]


def edit_in_place(text, payload, session_id):
    """Run the editor now and return the report to draw, or None to fall back.

    Also sets up the Stop hook, which runs a moment later: `delivered` tells it
    the user has already read the report, `ask_pending` hands it the questions
    the editor asked instead (they cannot be answered mid-render). Falling back
    clears the state so a stale phase from an earlier message in the same turn
    cannot make Stop allow a placeholder.
    """
    tail = ch.read_transcript_tail(payload.get("transcript_path", ""))
    user_context = ch.last_user_text(tail)
    result = ch.run_editor(text, user_context, note=MIDTURN_NOTE)
    if result is None:
        ch.clear_state(session_id)
        ch.log(f"display: editor unavailable, falling back to the placeholder "
               f"(session {session_id})")
        return None
    if result["action"] == "ask_model":
        ch.save_state(session_id, {
            "phase": "ask_pending", "original": text,
            "user_context": user_context, "exchange": [{"q": result["message"]}],
        })
        ch.log(f"display: editor asked for clarification, parked for the Stop "
               f"hook (session {session_id})")
        return None
    report = result["message"]
    unsupported = check_fidelity(report, (text, user_context))
    if unsupported:
        ch.clear_state(session_id)
        ch.log(f"display: rewrite introduced {unsupported}; falling back so the "
               f"agent reviews it (session {session_id})")
        return None
    ch.save_state(session_id, {"phase": "delivered"})
    return report


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
        if ch.load_state(session_id).get("phase") == "delivered":
            # A turn that was interrupted between the report and its Stop left
            # that phase behind; leaving it would make this turn's Stop allow
            # without editing anything.
            ch.clear_state(session_id)
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
        if ch.DISPLAY_EDIT:
            report = edit_in_place(text, payload, session_id)
            if report is not None:
                # The report is on screen, so the Stop hook has nothing to
                # repost - but the draft it replaced left no placeholder to
                # carry a link, so the original is stashed from here instead.
                state["hidden_any"] = False
                ch.save_display_state(session_id, state)
                link = stash("Show the original draft", text, cwd,
                             session_id, message_id)
                ch.log(f"display: edited in place, {len(text)} -> {len(report)} "
                       f"chars (session {session_id})")
                emit(report + link)
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
