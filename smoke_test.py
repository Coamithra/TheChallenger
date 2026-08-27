#!/usr/bin/env python
"""End-to-end check that The Challenger is wired up and the editor answers.

Builds a fake Stop payload (with a fake transcript claiming an Opus 5 session),
feeds it to challenger_hook.py the way Claude Code would, and reports what came
back. This makes a real editor call, so it costs one request and 5-30 seconds.

    python smoke_test.py                 # uses the first enabled project as cwd
    python smoke_test.py /path/to/proj   # or check a specific project root

Exit code 0 means the hook produced an edit decision; non-zero means it fell
through (which, since the hook fails open, is also what a misconfiguration
looks like - the reason is printed and logged to hook-debug.log).
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "challenger_hook.py")

SAMPLE = (
    "Refactored the ingest path. The dedupe key is now a tuple of (source_id, "
    "content_hash) computed in `_normalize()` before the batch writer sees it, "
    "instead of the previous per-row hash computed downstream in `flush()`. This "
    "removes the double-hashing on the retry path and means a retried batch no "
    "longer produces phantom rows. I also moved the retry backoff out of "
    "`writer.py` into `retry.py` since it was the only stateful thing left in the "
    "writer and made the writer hard to unit test. Tests: added four cases to "
    "`test_ingest.py` covering retry-after-partial-flush, which is the case that "
    "was broken; the existing suite passes (62 tests, 1.4s). Not verified against "
    "the staging feed, because the staging credentials in `.env` are expired - "
    "someone with access needs to rotate them before this can be checked against "
    "real traffic. There is one behavioral change worth flagging: rows whose "
    "source_id is null now raise instead of being silently assigned a synthetic "
    "id. I believe nothing in production emits null source_ids, but I could not "
    "confirm that from the code alone."
) * 2


def main():
    sys.path.insert(0, HERE)
    import challenger_hook as ch

    if len(sys.argv) > 1:
        cwd = sys.argv[1]
    elif ch.ENABLED_PROJECTS:
        cwd = ch.ENABLED_PROJECTS[0]
    else:
        sys.exit("No projects configured. Set CHALLENGER_PROJECTS in .env first.")

    print(f"cwd:      {cwd}")
    print(f"enabled:  {ch.project_enabled(cwd)}")
    print(f"backend:  {ch.CRITIC_BACKEND} ({ch.CRITIC_NAME})")
    print(f"length:   {len(SAMPLE)} chars (gate is {ch.MIN_CHARS})")
    if not ch.project_enabled(cwd):
        sys.exit("\nThat path is not under any enabled project root - nothing would be edited.")

    fd, transcript = tempfile.mkstemp(suffix=".jsonl", prefix="challenger-smoke-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Fix the duplicate rows in the ingest path."}]}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {
            "role": "assistant", "model": ch.TARGET_MODEL_PREFIX + "-test",
            "content": [{"type": "text", "text": SAMPLE}]}}) + "\n")

    payload = {
        "session_id": "challenger-smoke-test",
        "cwd": cwd,
        "transcript_path": transcript,
        "last_assistant_message": SAMPLE,
        "stop_hook_active": False,
    }

    print("\ncalling the editor (this is a real request; 5-30s is normal)...")
    try:
        result = subprocess.run(
            [sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", timeout=ch.CRITIC_TIMEOUT + 60,
        )
    finally:
        try:
            os.remove(transcript)
        except OSError:
            pass
        ch.clear_state("challenger-smoke-test")

    if result.stderr.strip():
        print(f"stderr: {result.stderr.strip()[:2000]}")
    out = (result.stdout or "").strip()
    if not out:
        sys.exit("\nFAIL: the hook allowed the stop silently. See hook-debug.log for the reason.")
    try:
        decision = json.loads(out)
    except ValueError:
        sys.exit(f"\nFAIL: unparseable hook output:\n{out[:2000]}")

    if decision.get("decision") != "block":
        sys.exit(f"\nFAIL: hook allowed the stop: {decision}\nSee hook-debug.log for the reason.")

    print("\nOK - the hook blocked and asked the agent to deliver this:\n")
    print("-" * 70)
    print(decision["reason"])
    print("-" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
