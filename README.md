# The Challenger

An automatic report editor for [Claude Code](https://claude.com/claude-code).

Claude Opus 5 writes excellent code and dense, jargon-heavy prose about it. The Challenger sits at the end of every turn, hands the response to a second model, and gets back a better-written version of the same response: the main point surfaced, dense sentences opened up, local jargon replaced. In the agent's own voice, with its caveats, verification, and open questions intact — it edits the writing, not the kind of thing being written.

It is one Python file, one prompt file, and a `Stop` hook entry. No service, no daemon, no dependencies outside the standard library.

## What it actually does

```
You ask for something
        │
        ▼
   Claude Opus 5  ── does the work, writes its report ──► rendered on screen
        │
        ▼
   the display companion holds the draft back as it renders
        │
        ├─ project not enabled?  ─► show it (~70ms)
        ├─ response under 1750 chars? ─► show it
        ├─ session model isn't Opus 5? ─► show it
        ▼
   editor model reads the draft + your original request
        │
        ├─ needs context? ─► parks its questions for the Stop hook, which
        │                    asks the agent and re-runs (max 2 rounds)
        ▼
   the finished report is drawn in place of the draft,
   with a link to the original underneath it
        │
        ▼
   Stop hook fires ─► nothing left to deliver, allows
```

That is the path with both hooks registered, and it is the one to install. A hook cannot replace a response that has already been rendered, so the Stop hook on its own can only deliver its report as a follow-up message — the same text, a message later. Holding the draft back while it renders is what buys you a single message instead of two, and it is the main thing to understand before installing.

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

## The display companion

`challenger_display_hook.py` is a second hook, on the `MessageDisplay` event (Claude Code 2.1.152+), registered separately from the Stop hook. It is where the editing happens. It runs the same cheap gates as the Stop hook while a response is still rendering, buffers messages that might be edited, and on the message's last flush either shows the whole thing at once (too short to edit) or hands it to the editor and draws the finished report in its place, with a link to the original underneath it.

That original is written to `.claude/challenger-drafts/` in the project. It is stashed before the editor runs, because replacing a draft outright leaves no placeholder to carry a link and you should always be one click from what the agent actually wrote. The directory gets a `.gitignore` containing `*`, which ignores the whole directory including itself, so it never shows up in `git status` and your own ignore rules are left alone; stashed drafts are swept after three days. This is display-only: the transcript and the model's context keep the original too, and verbose mode still shows it.

When the editor cannot deliver — it asked for clarification, it was unavailable, or its rewrite failed the fidelity check below — the draft collapses to a one-line placeholder carrying the same link, and the Stop hook takes it from there: it re-runs the editor and has the agent post the report, or reposts the hidden draft verbatim if the editor is still down. You never end up with just the placeholder.

It needs the `MessageDisplay` entry's `timeout` set to `360`, matching the Stop entry. The platform default for that event is 10 seconds — measured in the 2.1.247 build, and a per-hook `timeout` does override it — which is less than the editor needs; leave it at 10 and every draft stalls and then shows raw.

Two consequences of editing while the message renders. Nothing available while a message renders can tell the turn's final report from a long mid-turn message: the payload's `final` flag marks the last flush of *that message*, the transcript does not yet contain the message being displayed, and the Stop hook runs strictly after display — a display hook that waits for it deadlocks until it times out. So every message over `CHALLENGER_MIN_CHARS` is edited, mid-turn ones included, and the editor is told the text may be a progress note rather than a report. And the agent never reads the rewrite, so its standing permission to reject one that invented details no longer applies; in its place the companion checks mechanically that every citation marker, file path, and backticked identifier in the report also appears in the draft or your request, and falls back to the placeholder-and-echo path when one does not. That check cannot catch invented prose.

Clarification rounds keep the old shape: questions cannot be answered while a message is rendering, so the companion parks them for the Stop hook, which asks the agent without paying for a second editor call. Both halves of such a round are hidden from you - the draft behind its placeholder, the agent's answers behind another - so when one happens the questions and answers are appended to the stashed draft file, and the delivered report ends with a link to it saying how many questions were asked.

Register it alongside the Stop entry (same merge rules, same absolute-path convention):

```json
{
  "hooks": {
    "MessageDisplay": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"/absolute/path/to/challenger_display_hook.py\"",
            "timeout": 360
          }
        ]
      }
    ]
  }
}
```

Platform caveats, current as of Claude Code 2.1.246: the message stream renders no raw HTML and offers no collapsible syntax, so the placeholder links the draft out to a file rather than folding it inline — a `<details>` block arrives as visible markup wrapped around the very draft it was meant to hide. The app also only opens links that resolve inside the session's working directory, which is why the stash lives in the project instead of a temp directory. The interactive terminal silently ignores `displayContent` ([#83957](https://github.com/anthropics/claude-code/issues/83957)), so there the companion changes nothing; the desktop app applies it but may flash the draft briefly before replacing it. Print mode (`-p`) honors it fully. Also, in enabled projects messages appear when they finish instead of streaming line by line, and the first response of a brand-new session is never hidden (the session's model cannot be read yet, so the hook fails open).

## House style

The editor's entire personality is [critic-prompt.md](critic-prompt.md), in plain prose. It currently asks for clearer, tighter writing that preserves the author's voice, point of view, and level of formality along with caveats, verification status, remaining manual actions, and useful references. If you want something else — a house register imposed regardless of the original, terser output, a different language — change it there.

That file is tracked, though, so editing it in place will conflict the next time you `git pull`. Copy it alongside as `critic-prompt.local.md` (gitignored) and set `CHALLENGER_PROMPT=critic-prompt.local.md` in your config; your house style then survives every update.

## Design decisions worth knowing

**Subagents are excluded on purpose.** Background and farmed agents end their turns with `SubagentStop`, and the hook is registered only on `Stop`. A subagent's output is a handoff to an orchestrating agent, not a report for a human: prose that reads well buys nothing there, and blocking would inject echo turns into agents nobody is reading. The orchestrating session's own response is edited, which is where you actually read.

**It fails open, always.** Any error, timeout, missing CLI, auth failure, or unparseable editor output allows the stop and ships the original. A broken Challenger costs you the edit, never the session.

**It costs an editor call.** Every response over the length gate is one call, roughly 5-15 seconds, added to the end of the turn. There is no "this one was already fine" pass-through action; the editor always rewrites. If that feels wasteful, raise `CHALLENGER_MIN_CHARS`. Falling back to the Stop hook costs a second call and an extra turn on top, which is the other reason to keep the companion registered.

**Echo fidelity is unverified.** When the fallback fires, the stop that delivers the rewrite is allowed without review, so an agent that appends its own commentary to the "verbatim" report is not caught. The instruction does sanction one deviation: an agent that catches the editor getting facts wrong is told to post its original report verbatim instead — a fact-check of last resort, not a style veto.

**Transcript parsing is best-effort.** Detecting the session model and your original request means reading Claude Code's session JSONL, whose format is internal and may change between versions. Both reads fail soft: the model gate skips editing, and the editor simply loses your request as context.

## Uninstall

Delete the `Stop` entry — and the `MessageDisplay` entry if you added the display companion — from `~/.claude/settings.json`. Nothing else on your machine was touched.

## License

MIT.
