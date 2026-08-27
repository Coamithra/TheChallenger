# Install The Challenger

**This file is a runbook for a coding agent.** Point Claude Code at it (`claude "read InstallMe.md and set The Challenger up for me"`) and it will do the work. A human can follow the same steps by hand; the commands are all real.

## For the agent: what you are installing

The Challenger is a `Stop` hook that sends the user's long Opus 5 responses to a second model, which rewrites them into a high-level report. It is registered once, globally, and gates on a list of project roots the user chooses. Your job is to get it configured, registered, and smoke-tested — and to leave the user's existing setup intact.

Rules for this install:

1. **Never guess which projects to enable.** Step 3 is a mandatory stop; you must have the user's explicit answer before writing `challenger.conf`.
2. **Never overwrite `~/.claude/settings.json`.** It almost certainly contains the user's other hooks and settings. Merge, and back it up first.
3. **Never display or commit the contents of the user's `challenger.conf`** beyond confirming the paths they gave you.
4. If a step fails, stop and tell the user what failed. A half-installed hook is not dangerous — the hook fails open — but a confused user is.

Work through the steps in order and report at the end.

---

## Step 0 — Preflight

Confirm the basics and record the answers; later steps need them.

```bash
python --version
```

Python 3.8+ with no third-party packages. If `python` is not the right interpreter on this machine (some systems need `python3`, and on Windows `python3` may hit the Microsoft Store alias), find the one that works and use it consistently for the rest of this runbook.

Check that Claude Code's settings file exists — `~/.claude/settings.json` on every platform (`C:\Users\<you>\.claude\settings.json` on Windows). If it does not exist yet, you will create it in Step 5.

Note the platform's path separator: `;` on Windows, `:` on macOS and Linux. Step 4 needs it.

## Step 1 — Place the repo

The hook reads its prompt, settings, and vendored bridge relative to its own location, so the repo can live anywhere — but it must live somewhere permanent, because the hook is registered by absolute path. If the user cloned it into a temporary or throwaway directory, say so and agree on a home for it before continuing.

Record the absolute path to `challenger_hook.py`; Steps 4-6 all need it.

## Step 2 — Choose the editor backend

Two backends, and the choice depends on what is already installed.

**codex (default, recommended).** Uses OpenAI's `gpt-5.6-sol`. Check that the CLI is installed and logged in:

```bash
codex --version
codex login status
```

A missing CLI is fixed with `npm install -g @openai/codex`. Authentication is separate and prints something like `Logged in using ChatGPT` when it is good; anything else means the user must run `codex login` themselves. **You cannot log them in — do not try.** Ask them to do it, wait, and re-run the status check before continuing.

**claude (fallback).** Uses `claude -p` with `claude-fable-5`. Nothing extra to install if Claude Code is on this machine. Choose this if the Codex CLI is missing and the user does not want to install it.

Tell the user which backend you are configuring and why. The default exists because an editor from a different vendor does not share the stylistic habits of the model it is editing — worth one sentence if they ask, not worth a debate.

## Step 3 — Ask which projects (mandatory — do not skip)

**Stop here and ask the user this question. Do not infer the answer, do not enable everything you can find, and do not proceed on a maybe.**

> Which projects should The Challenger edit?

Give them what they need to answer well:

- The hook is registered globally but only acts on sessions whose working directory is inside one of the roots they name. Everything else is untouched, at a cost of about 70ms per response.
- Inside an enabled project, **every** Opus 5 response over 1750 characters costs one editor call (5-15 seconds) and one extra turn while the agent posts the rewrite. That is the real price, and it is per response, not per session.
- Git worktrees under a listed root are covered automatically — they do not need separate entries.
- This is trivially reversible: enabling or disabling a project later is one line in `challenger.conf`.

If they are unsure, recommend starting with **one** project where they read the reports carefully, living with it for a few days, and expanding from there. If it helps them decide, you may list plausible candidates from directories you can already see — but they must confirm; a listing is not an answer.

What you need out of this step is a list of **absolute paths to project roots**. Verify each one exists before continuing, and ask about any that do not.

## Step 4 — Write `challenger.conf`

Copy `challenger.conf.example` to `challenger.conf` next to `challenger_hook.py`, then set `CHALLENGER_PROJECTS` to the roots from Step 3, joined by the platform separator from Step 0. Set `CHALLENGER_CRITIC=claude` as well if Step 2 landed on the claude backend; the codex default needs no entry.

Leave every other setting commented out. They are documented in the example file, the defaults are the tested ones, and nothing secret belongs in this file — both backends authenticate through their own CLI.

Confirm the file parses the way the hook will read it, from the repo directory:

```bash
python -c "import challenger_hook as c; print(c.ENABLED_PROJECTS); print(c.CRITIC_BACKEND, c.CRITIC_NAME)"
```

The printed list must match what the user asked for, exactly.

## Step 5 — Register the hook

This edits a file the user depends on. Back it up first, merge idempotently, and never rewrite the whole file by hand. Save this as `register.py` in the repo directory and run it there:

```python
import json, os, shutil, sys

settings = os.path.expanduser("~/.claude/settings.json")
hook_path = os.path.abspath("challenger_hook.py")
command = '%s "%s"' % (sys.executable, hook_path)

data = {}
if os.path.exists(settings):
    shutil.copy(settings, settings + ".backup-challenger")
    with open(settings, encoding="utf-8") as f:
        data = json.load(f)
else:
    os.makedirs(os.path.dirname(settings), exist_ok=True)

stops = data.setdefault("hooks", {}).setdefault("Stop", [])
already = any("challenger_hook.py" in h.get("command", "")
              for entry in stops for h in entry.get("hooks", []))
if already:
    print("already registered - nothing to do")
else:
    stops.append({"hooks": [{"type": "command", "command": command, "timeout": 360}]})
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print("registered: " + command)
```

The 360s timeout must stay comfortably above `CHALLENGER_TIMEOUT` (300s default), or Claude Code will kill the hook mid-edit.

Read the resulting file back and confirm the user's other hooks survived. If the original was not valid JSON the script raises before writing anything — restore from the `.backup-challenger` copy if needed, and tell the user. Delete `register.py` when you are done.

## Step 6 — Smoke test

```bash
python smoke_test.py
```

This feeds the hook a realistic fake response and makes one real editor call. Success prints the rewritten report the agent would have been told to post. It is a genuine end-to-end test: it exercises the project gate, the model gate, the backend, and the decision parsing.

If it reports that the hook allowed the stop, `hook-debug.log` in the repo directory has the reason on its last line. The usual causes:

- `not an enabled project` — the path in `CHALLENGER_PROJECTS` does not match; check for a typo or the wrong separator.
- `codex editor exited` with an auth message — the user needs to run `codex login` themselves.
- `codex: not found` / `claude: not found` — the backend CLI is not on PATH for non-interactive shells.

Every one of these fails open by design: the hook never blocks a session, it just stops editing.

## Step 6b — Optional: hide the drafts (display companion)

Offer this to the user, default off. `challenger_display_hook.py` registers on the `MessageDisplay` event and hides would-be-edited drafts as they render, so the edited report is the only version they read. Before offering, check both prerequisites:

- Claude Code 2.1.152 or newer (`claude --version`).
- Tell the user the honest platform status: print mode honors it fully; the desktop app applies it but may briefly flash the draft first; **the interactive terminal currently ignores it entirely** (anthropics/claude-code#83957) — a terminal-only user gains nothing today.

Also tell them the two visible costs: in enabled projects, responses appear when they finish rather than streaming line by line, and the first response of a brand-new session is never hidden (fails open — the session's model cannot be read yet).

If they want it, extend `register.py` from Step 5 with the same idempotent merge for a `MessageDisplay` entry running `challenger_display_hook.py` (absolute path, `"timeout": 10` — this hook runs per display flush and must stay fast). Everything else is shared: it reads the same `challenger.conf`, logs to the same `hook-debug.log`, and fails open on any error by showing the original text.

## Step 7 — Hand over

Tell the user:

- which projects are enabled, and which backend is running;
- that the hook applies to **new** sessions — sessions already open when you registered it will not pick it up;
- that the edited report arrives as a **follow-up message**, because hooks cannot replace a response that has already been rendered. The first time it fires it looks like the agent repeated itself in plainer language. That is the feature working.
- how to change their mind: edit `CHALLENGER_PROJECTS` in `challenger.conf` to add or drop a project, and delete the `Stop` entry from `~/.claude/settings.json` to remove it entirely;
- that updating is `git pull` in the repo directory — the clone *is* the installation, since the hook is registered by absolute path and nothing was copied elsewhere;
- that to change the register of the reports they should copy `critic-prompt.md` to `critic-prompt.local.md` and set `CHALLENGER_PROMPT=critic-prompt.local.md` in their config, rather than editing the tracked file, which would conflict on their next update.

---

## Manual install

Without an agent, the same install is five steps:

1. Clone this repo somewhere permanent.
2. `cp challenger.conf.example challenger.conf`, then set `CHALLENGER_PROJECTS` to your project roots, separated by `;` (Windows) or `:` (macOS/Linux).
3. Have either the Codex CLI installed and logged in (default backend), or set `CHALLENGER_CRITIC=claude` to use Claude Code itself.
4. Add this to the `hooks` object in `~/.claude/settings.json`, with the real absolute path, merging it into whatever is already there:

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "python \"/absolute/path/to/challenger_hook.py\"",
               "timeout": 360
             }
           ]
         }
       ]
     }
   }
   ```

5. `python smoke_test.py`
