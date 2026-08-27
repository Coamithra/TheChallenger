# The Challenger

An automatic report editor for Claude Code. A global `Stop` hook intercepts the end of an Opus 5 turn, sends the response to an editor model (OpenAI `gpt-5.6-sol` by default), and blocks the stop with an instruction to post the editor's rewrite verbatim. The result is that long Opus 5 reports reach the user as release-note-style summaries — what changed, why it matters, how it was verified, what still needs attention — instead of dense implementation narration.

This is a public repo (`Coamithra/TheChallenger`, MIT). Anything you add here ships to strangers: no personal paths, no project names, no session excerpts in tracked files.

`plan.md` is the design document and running changelog: read it before changing behavior, and add a numbered Status entry when you do. `README.md` is the public front door and `InstallMe.md` is a runbook another agent executes on someone else's machine — when behavior changes, those two usually need the same edit.

## Layout

```
challenger_hook.py     the whole implementation - Stop hook entry point (command hook)
critic-prompt.md       the editor model's system prompt (the "Report Editor" instructions)
smoke_test.py          fake Stop payload -> real editor call; the end-to-end check
schema.json            the editor's decision shape - documentation only, not loaded at runtime
critic-settings.json   {"disableAllHooks": true}, passed to the Claude backend so it can't recurse
vendor/ask_codex.py    vendored Codex CLI bridge (upstream: the standalone CodexCLI project)
challenger.conf        local settings, gitignored (challenger.conf.example is the documented copy)
critic-prompt.local.md optional local override of the prompt; gitignored via *.local.md
plan.md                design doc, architecture rationale, Status log, open considerations
README.md              public overview
InstallMe.md           install runbook written for a coding agent to execute
hook-debug.log         every decision from every enabled project; gitignored, self-trims past 512KB
research/              archived prompts and harvested source material; gitignored, never read at runtime
```

## Configuration

Everything is env-driven, read from `challenger.conf` next to the hook by a ten-line KEY=VALUE parser (no python-dotenv — a hook that runs on every stop should not need a virtualenv). Values are pushed into the environment, so a real environment variable wins over the file. It is deliberately not called `.env`: no credentials go in it, since both backends authenticate through their own CLI.

`CHALLENGER_PROJECTS` is the only setting with no useful default: an `os.pathsep`-separated list of absolute project roots. Empty means the Challenger is installed but inert. `challenger.conf.example` documents the rest and is the file to update when adding a knob.

Path settings (`CHALLENGER_PROMPT`, `CHALLENGER_LOG`, `CHALLENGER_CODEX_BRIDGE`) go through `_env_path`, which resolves relative values against the repo — the hook runs with the *session's* cwd, so a bare `critic-prompt.local.md` would otherwise be looked up inside whatever project is being edited.

`critic-prompt.md` is tracked, so users who want their own house style copy it to `critic-prompt.local.md` (gitignored via `*.local.md`) and point `CHALLENGER_PROMPT` at it. Keep that escape hatch working when touching prompt loading.

## How it runs

The hook is registered once in `~/.claude/settings.json` (`Stop`, 360s timeout) and therefore fires for every session on the machine. The project list is the real switch: a cwd-prefix match decides whether a session is edited, and worktrees nested under an enabled root are covered automatically. Sessions elsewhere pay ~70ms for interpreter startup and exit at the first gate.

## Flow

Gates run in order, cheapest first, and every one of them fails open:

1. **Project** — cwd not under an enabled root, allow.
2. **Length** — under `MIN_CHARS` (1000) and not a continuation, allow.
3. **Model** — session model sniffed from the transcript tail; anything not `claude-opus-5*` is allowed untouched.

Past the gates the response goes to the editor along with the user's last real prompt (extracted from the transcript, for register and altitude judgment). The editor returns one of two actions:

- `echo_to_user` — the message is the finished report. The hook blocks with "post this verbatim", records phase `echo` in the state file, and allows the next stop unreviewed.
- `ask_model` — the message is clarification questions. The hook blocks with them, stores the exchange, and re-invokes the editor with the agent's answers on the next stop. Bounded at `MAX_ASK_ROUNDS` (2); past that the original ships.

State lives in `%TEMP%\challenger-<session_id>.json` and is cleared whenever a turn resolves.

## Editor backends

`CHALLENGER_CRITIC=codex` (default) runs `vendor/ask_codex.py` with the interpreter already running the hook. The vendor split is deliberate: a Claude editor shares Claude's stylistic blind spots, and the codex path also runs 2-4x faster. It depends on a valid `codex login`; auth failure fails open. `CHALLENGER_CRITIC=claude` switches to `claude -p` with `CHALLENGER_CRITIC_MODEL` (default `claude-fable-5`), which must be passed `critic-settings.json` or the headless editor re-triggers this same hook.

## Design notes

**Subagents are excluded by design.** Background and farmed agents end their turns with `SubagentStop`, not `Stop`, and the hook is deliberately registered only on `Stop`. Their output is a machine-to-machine handoff to an orchestrating agent, not a report for a human — rewriting it into release-note register would strip exactly the detail the overseer needs, and blocking would inject echo turns into agents nobody is reading. The overseer session's own response is challenged, which is where the user actually reads.

**Fail open, always.** Any exception, timeout, unparseable editor output, or missing dependency allows the stop. A broken Challenger must never brick a session; `main()` is wrapped in a bare `except` that exits 0. This is the one invariant to protect when changing anything here.

**Hooks cannot replace a rendered response.** The original is already on screen by the time the hook runs; the edited report arrives as a follow-up message. This is a platform constraint, not a choice.

**Echo fidelity is unverified.** The stop that delivers the rewrite is allowed without review (phase `echo`), so an agent that appends commentary to the "verbatim" report is not caught. Watch for it when calibrating.

**Transcript parsing is best-effort.** Model sniffing and user-prompt extraction read the session JSONL, whose format is internal to Claude Code. Both return `None` on anything unexpected: the model gate then skips editing, and the editor simply loses the request context.

## Working on it

`python smoke_test.py` is the end-to-end check — it builds a fake Stop payload and transcript, runs the hook as Claude Code would, and prints the decision. It makes a real editor call, so it costs a request and 5-30 seconds.

For gate-level work, pipe a payload in directly:

```bash
echo '{"session_id":"test","cwd":"...","last_assistant_message":"<long text>","transcript_path":"<real .jsonl>","stop_hook_active":false}' | python challenger_hook.py
```

Exit code is always 0; the decision is the JSON on stdout (nothing = allow, `{"decision":"block",...}` = edit). `hook-debug.log` records the reason for every path, including the cheap-gate allows, and every enabled project logs there, which is what makes cross-project debugging possible.

Keep stdout pure ASCII (`json.dumps` default `ensure_ascii=True`) — the hook process inherits whatever console codepage Windows hands it. Run any Python you write against this project with `PYTHONIOENCODING=utf-8`.
