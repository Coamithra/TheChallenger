#!/usr/bin/env python
"""Bridge that lets Claude Code hand a task to OpenAI's Codex CLI and get a clean answer back.

Vendored into The Challenger from the standalone CodexCLI project so this repo has
no external file dependencies. Upstream is a general-purpose "ask Codex a question"
bridge; the Challenger only ever uses --prompt-file, --model, --effort and --timeout,
and never lets it out of the read-only sandbox.

Why this exists
---------------
`codex exec` streams a noisy event log to stdout (every tool call, every reasoning
chunk). Piping that straight back into a Claude Code transcript wastes a huge number of
tokens for a few paragraphs of actual answer. This wrapper:

  * feeds the prompt via **stdin**, so prompts containing quotes/newlines/backticks
    never have to survive a shell-quoting round trip (the whole reason this is a
    script and not a one-liner in the skill);
  * captures the event stream to a log file instead of the terminal;
  * prints ONLY Codex's final message (via `--output-last-message`);
  * on failure, prints a diagnosis plus the tail of the log so the caller can act.

Usage
-----
    echo "your question" | python ask_codex.py [options]
    python ask_codex.py --prompt-file prompt.txt [options]

Options
-------
    --model M          default DEFAULT_MODEL below. Pass "" to defer to Codex's own
                       default (~/.codex/config.toml, else whatever it ships with).
    --effort E         reasoning effort, default DEFAULT_EFFORT below. Codex's own
                       default is "none", which is the wrong tradeoff when you've paid
                       the latency of an external call to get a second opinion.
    --image FILE       attach an image to the prompt (screenshot, diagram, mockup).
                       Repeatable. Paths are resolved to absolute BEFORE handing them
                       to Codex, because --cd moves Codex's working root and a relative
                       path would otherwise resolve somewhere surprising.
    --write            workspace-write sandbox (Codex may edit files). Default is
                       read-only: it can read the repo but not change anything.
    --full-access      danger-full-access sandbox. Escape hatch; requires --write too.
    --cd DIR           working root for Codex (default: cwd).
    --timeout S        seconds before giving up (default 900).
    --log FILE         where to keep the raw event stream (default: temp file, path
                       is reported on failure).
    --keep-log         always report the log path, even on success.

Exit codes: 0 = Codex answered, non-zero = something went wrong (message on stderr).
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# Defaults for the bridge. These deliberately override Codex's own defaults, which are
# tuned for interactive chat rather than "one expensive question, make it count".
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "high"


def build_argv(args, out_path):
    codex = shutil.which("codex")
    if not codex:
        sys.exit(
            "ask_codex: `codex` not found on PATH.\n"
            "  Install it with:  npm install -g @openai/codex"
        )

    if args.full_access:
        sandbox = "danger-full-access"
    elif args.write:
        sandbox = "workspace-write"
    else:
        sandbox = "read-only"

    argv = [
        codex,
        "exec",
        "--sandbox", sandbox,
        "--cd", args.cd,
        "--skip-git-repo-check",   # C:\Programming and friends aren't all git repos
        "--color", "never",
        "--output-last-message", out_path,
        # Non-interactive: never block waiting for an approval nobody is there to give.
        # The sandbox, not the approval prompt, is the real boundary here.
        "-c", 'approval_policy="never"',
    ]
    if args.model:
        argv += ["--model", args.model]
    if args.effort:
        argv += ["-c", f'model_reasoning_effort="{args.effort}"']
    for img in args.image or []:
        argv += ["--image", img]
    return argv, sandbox


def main():
    p = argparse.ArgumentParser(
        prog="ask_codex",
        description="Ask OpenAI Codex a question and print only its final answer.",
    )
    p.add_argument("--prompt-file", help="read the prompt from this file instead of stdin")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f'model (default {DEFAULT_MODEL}; pass "" to use Codex\'s own default)')
    p.add_argument("--effort", default=DEFAULT_EFFORT,
                   help=f'reasoning effort (default {DEFAULT_EFFORT}; pass "" to use Codex\'s own default)')
    p.add_argument("--image", action="append", metavar="FILE",
                   help="attach an image to the prompt; repeatable")
    p.add_argument("--write", action="store_true", help="allow Codex to edit files (workspace-write)")
    p.add_argument("--full-access", action="store_true", help="danger-full-access sandbox; needs --write")
    p.add_argument("--cd", default=os.getcwd(), help="working root for Codex (default: cwd)")
    p.add_argument("--timeout", type=int, default=900, help="seconds before giving up (default 900)")
    p.add_argument("--log", help="path for the raw event stream")
    p.add_argument("--keep-log", action="store_true", help="report the log path even on success")
    args = p.parse_args()

    if args.full_access and not args.write:
        p.error("--full-access also requires --write (belt and braces: two flags to leave the sandbox)")

    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as fh:
            prompt = fh.read()
    else:
        prompt = sys.stdin.read()
    if not prompt.strip():
        p.error("empty prompt (pass --prompt-file or pipe text on stdin)")

    if not os.path.isdir(args.cd):
        p.error(f"--cd directory does not exist: {args.cd}")

    if args.image:
        # Resolve before Codex sees them: --cd moves Codex's working root, so it is
        # ambiguous whether a relative image path would resolve against our cwd or its.
        # Also fail here rather than 60s into a run that was never going to work.
        resolved = []
        for img in args.image:
            full = os.path.abspath(os.path.expanduser(img))
            if not os.path.isfile(full):
                p.error(f"--image file does not exist: {img}  (resolved to {full})")
            resolved.append(full)
        args.image = resolved

    out_fd, out_path = tempfile.mkstemp(prefix="codex_answer_", suffix=".md")
    os.close(out_fd)
    if args.log:
        log_path = args.log
    else:
        log_fd, log_path = tempfile.mkstemp(prefix="codex_events_", suffix=".log")
        os.close(log_fd)

    argv, sandbox = build_argv(args, out_path)

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            log.write(f"$ {' '.join(argv)}\n\n--- prompt ---\n{prompt}\n--- events ---\n")
            log.flush()
            proc = subprocess.run(
                argv,
                input=prompt,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout,
                env=env,
            )
    except subprocess.TimeoutExpired:
        sys.exit(
            f"ask_codex: Codex exceeded --timeout {args.timeout}s and was killed.\n"
            f"  Partial event log: {log_path}"
        )

    answer = ""
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            answer = fh.read().strip()
        os.unlink(out_path)

    if proc.returncode != 0 or not answer:
        tail = ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                tail = "".join(fh.readlines()[-40:])
        except OSError:
            pass
        hint = ""
        low = tail.lower()
        if "not logged in" in low or "unauthor" in low or "401" in tail:
            hint = "\n  Looks like an auth problem. THE USER must run `codex login` themselves.\n"
        elif "unknown" in low and "model" in low:
            hint = (
                f"\n  Model '{args.model}' may not be valid for this account. Try --model ''\n"
                f"  to fall back to Codex's own default.\n"
            )
        sys.exit(
            f"ask_codex: codex exec failed (exit {proc.returncode}, sandbox={sandbox}).{hint}\n"
            f"  Full log: {log_path}\n"
            f"--- last 40 lines ---\n{tail}"
        )

    sys.stdout.write(answer + "\n")
    if args.keep_log:
        sys.stderr.write(f"\n[event log: {log_path}]\n")
    elif not args.log:
        try:
            os.unlink(log_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
