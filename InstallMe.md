# Install The Challenger

**This file is a runbook for a coding agent.** Point Claude Code at it (`claude "read InstallMe.md and set The Challenger up for me"`) and it will do the work. A human can follow the same steps by hand; the commands are all real.

## For the agent: what you are installing

The Challenger is a `Stop` hook that sends the user's long Opus 5 responses to a second model, which rewrites them for clarity while keeping the agent's own voice. It is registered once, globally, and gates on a list of project roots the user chooses. There is a second hook too, on the `MessageDisplay` event: it holds the draft back while it renders and draws the edited report in its place, which is what makes the user read one message instead of two. Steps 0-6 install the `Stop` hook alone and Step 6b adds the companion, in that order, so a working install exists before anything depends on display behaviour the user's client may not support. Your job is to get it configured, registered, and smoke-tested — and to leave the user's existing setup intact.

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

If Step 1 will be cloning, check `git --version` too. If git is missing, install it or tell the user how — do not work around it by downloading a zip of the repo, which would leave them with no `git pull` to update with later.

Check that Claude Code's settings file exists — `~/.claude/settings.json` on every platform (`C:\Users\<you>\.claude\settings.json` on Windows). If it does not exist yet, you will create it in Step 5.

Note the platform's path separator: `;` on Windows, `:` on macOS and Linux. Step 4 needs it.

## Step 1 — Place the repo

If you were pointed at this file by URL and the repo is not on this machine yet, clone it first. Every later step runs commands inside the repo directory, not against the page you are reading:

```bash
git clone https://github.com/Coamithra/TheChallenger.git
```

The hook reads its prompt, settings, and vendored bridge relative to its own location, so the repo can live anywhere — but it must live somewhere permanent, because the hook is registered by absolute path. Ask the user where it should live rather than leaving it wherever this session happens to be running, and if it was already cloned into a temporary or throwaway directory, say so and agree on a home for it before continuing.

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
- Inside an enabled project, **every** Opus 5 response over 1750 characters costs one editor call (5-15 seconds) and one extra turn while the agent posts the rewrite. That is the real price, and it is per response, not per session. (Step 6b's companion removes the extra turn on clients that support it — do not offer it here; the editor call remains either way.)
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

## Step 6b — Register the display companion (recommended)

`challenger_display_hook.py` registers on the `MessageDisplay` event. It holds back a draft that is about to be edited, runs the editor while the message is still rendering, and draws the finished report in place of the draft with a link to the original underneath — so the user reads one message rather than the draft followed by its rewrite, and the agent never gets the chance to paraphrase a report it was told to post verbatim. Where it cannot deliver, the draft collapses to a one-line placeholder and the Stop hook you just installed takes over, so this is additive: nothing from Steps 0-6 stops working.

Check both prerequisites before offering it:

- Claude Code 2.1.152 or newer (`claude --version`).
- Tell the user the honest platform status: print mode honors it fully; the desktop app applies it but may briefly flash the draft first; **the interactive terminal currently ignores it entirely** (anthropics/claude-code#83957) — a terminal-only user gains nothing today and should skip this step.

The draft is not thrown away: it is written to `.claude/challenger-drafts/` inside the enabled project and linked under the report. Mention that directory — it is the only thing this install leaves inside their own projects. It ignores itself with a `.gitignore` containing `*`, so nothing reaches `git status` and their own ignore rules are untouched, and stashed drafts are swept after three days.

If they want it, extend `register.py` from Step 5 with the same idempotent merge for a `MessageDisplay` entry running `challenger_display_hook.py`, by absolute path and with `"timeout": 360` to match the Stop entry. That timeout is not optional: the platform default for this event is 10 seconds, less than an editor round, and at that value every draft stalls and then shows raw. Everything else is shared — it reads the same `challenger.conf`, logs to the same `hook-debug.log`, and fails open on any error by showing the original text.

Tell them the four costs:

- In enabled projects, responses appear when they finish rather than streaming line by line.
- The first response of a brand-new session is never hidden (fails open — the session's model cannot be read yet).
- Every message over `CHALLENGER_MIN_CHARS` is edited, long mid-turn ones included, because nothing available while a message renders identifies the turn's final report. The editor is told the text may be a progress note rather than a report.
- The agent no longer reads the rewrite before it reaches them, so its standing permission to reject an editor that invented details does not apply. In its place a mechanical check — citation markers, file paths, and backticked identifiers in the report must appear in the draft or the user's request — hands the rewrite back to the Stop hook when it trips, where the agent does see it. That check cannot catch invented prose.

Clarification rounds still resolve at the Stop hook: questions cannot be answered while a message is rendering, so the companion parks them and the Stop hook asks the agent without paying for a second editor call. Both halves of such a round are hidden from the user, so the questions and the agent's answers are appended to the stashed draft file, and the report's link line says how many questions were asked.

## Step 7 — Hand over

Tell the user:

- which projects are enabled, and which backend is running;
- that the hook applies to **new** sessions — sessions already open when you registered it will not pick it up;
- what a delivered report looks like: with the companion from Step 6b, it is drawn in place of the draft with a link to the original underneath. Without it — or in the interactive terminal, which drops `displayContent` — the report arrives as a **follow-up message** instead, because a hook cannot replace a response that has already been rendered. The first time that fires it looks like the agent repeated itself in plainer language. That is the feature working.
- how to change their mind: edit `CHALLENGER_PROJECTS` in `challenger.conf` to add or drop a project, and delete the `Stop` entry — plus the `MessageDisplay` entry, if you registered one — from `~/.claude/settings.json` to remove it entirely;
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
