# The Challenger

An automatic report editor for [Claude Code](https://claude.com/claude-code).

Claude Opus 5 writes excellent code and dense, jargon-heavy reports about it. The Challenger sits at the end of every turn, hands the response to a second model, and gets back the report you should have received: what changed, why it matters, how it was verified, and what still needs your attention. In release-note register, not implementation narration.

It is one Python file, one prompt file, and a `Stop` hook entry. No service, no daemon, no dependencies outside the standard library.

## What it actually does

```
You ask for something
        │
        ▼
   Claude Opus 5  ── does the work, writes its report ──► rendered on screen
        │
        ▼
   Stop hook fires
        │
        ├─ project not enabled?  ─► allow (~70ms)
        ├─ response under 1000 chars? ─► allow
        ├─ session model isn't Opus 5? ─► allow
        ▼
   editor model reads the report + your original request
        │
        ├─ needs context? ─► asks the coding agent targeted questions,
        │                    reads the answers, then decides again (max 2 rounds)
        ▼
   returns the finished report ─► the agent posts it as its next message
```

Hooks cannot replace a response that has already been rendered, so the edited report arrives as a follow-up message rather than in place of the original. That is a platform constraint and the main thing to know before installing.

## Install

Point your local Claude Code at [InstallMe.md](InstallMe.md):

```bash
claude "read InstallMe.md in this repo and set The Challenger up for me"
```

It will check your prerequisites, ask which projects you want edited, write your `challenger.conf`, register the hook, and run a smoke test. Manual install is in the same file if you would rather do it yourself.

## Configuration

Settings live in `challenger.conf` next to the hook, copied from [challenger.conf.example](challenger.conf.example). No credentials go in it — both backends authenticate through their own CLI — and it is gitignored, so `git pull` never disturbs your setup. Two settings matter:

`CHALLENGER_PROJECTS` is a path-separated list of absolute project roots. The hook is registered globally, so it runs for every session on your machine, but only sessions whose cwd is under one of these roots are edited. Everything else exits at the first gate. Adding a project is one line; there is nothing to configure inside the project itself, and git worktrees beneath a listed root are covered automatically.

`CHALLENGER_CRITIC` picks the editor backend, below. Everything else — the length gate, the model gate, timeouts, model ids — has a tested default and only needs an entry if you want to change it.

## Editor backends

**codex** (default) runs OpenAI's `gpt-5.6-sol` through the vendored `vendor/ask_codex.py` bridge. Requires the [Codex CLI](https://github.com/openai/codex) on PATH and a valid `codex login`. The default is deliberately cross-vendor: an editor from the same family as the model it edits shares its stylistic blind spots, and in testing the Claude editor was slower for no gain in catch rate.

**claude** runs `claude -p` headlessly with `claude-fable-5`. No extra install if you already have Claude Code. It is passed `critic-settings.json` (`{"disableAllHooks": true}`) so the editor cannot re-trigger this same hook.

Set `CHALLENGER_CRITIC=claude` to switch.

## House style

The editor's entire personality is [critic-prompt.md](critic-prompt.md), in plain prose. It currently asks for high-level, outcome-oriented reports that preserve caveats, verification status, remaining manual actions, and useful references, while dropping implementation mechanics you did not ask for. If you want a different register — terser, more technical, a different language — change it there.

That file is tracked, though, so editing it in place will conflict the next time you `git pull`. Copy it alongside as `critic-prompt.local.md` (gitignored) and set `CHALLENGER_PROMPT=critic-prompt.local.md` in your config; your house style then survives every update.

## Design decisions worth knowing

**Subagents are excluded on purpose.** Background and farmed agents end their turns with `SubagentStop`, and the hook is registered only on `Stop`. A subagent's output is a handoff to an orchestrating agent, not a report for a human: rewriting it into release-note register would strip exactly the detail the orchestrator needs, and blocking would inject echo turns into agents nobody is reading. The orchestrating session's own response is edited, which is where you actually read.

**It fails open, always.** Any error, timeout, missing CLI, auth failure, or unparseable editor output allows the stop and ships the original. A broken Challenger costs you the edit, never the session.

**It costs a turn.** Every edited response is one editor call (roughly 5-15 seconds) plus one extra turn in which the agent posts the rewrite. There is no "this one was already fine" pass-through action; the editor always rewrites. If that feels wasteful, raise `CHALLENGER_MIN_CHARS`.

**Echo fidelity is unverified.** The stop that delivers the rewrite is allowed without review, so an agent that appends its own commentary to the "verbatim" report is not caught.

**Transcript parsing is best-effort.** Detecting the session model and your original request means reading Claude Code's session JSONL, whose format is internal and may change between versions. Both reads fail soft: the model gate skips editing, and the editor simply loses your request as context.

## Uninstall

Delete the `Stop` entry from `~/.claude/settings.json`. Nothing else on your machine was touched.

## License

MIT.
