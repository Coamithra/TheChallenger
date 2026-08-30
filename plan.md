# The Challenger — Plan

## What It Is

An automatic report editor that sits between Claude Opus 5's output and the user. A `Stop` hook hands the response to an editor model (OpenAI gpt-5.6-sol by default, configurable) that rewrites it for clarity in the author's own voice — surfacing a buried result, opening up dense sentences, replacing local jargon, without flattening it into a house register — and may first ask the coding agent targeted clarification questions. The edited report is delivered by having the agent post it verbatim. (Earlier iterations used a critique-and-revise loop with a severity gate; see Status for that history.)

## Why

Opus 5 has a well-documented set of communication problems: invented jargon ("Claudish"), over-elaboration, register mismatch, formulaic scaffolding, instruction resistance, and more. Users shouldn't have to paste output into ChatGPT to understand it, maintain 100-phrase blocklists, or constantly repeat style instructions. The Challenger automates that pressure.

## Architecture (as built)

```
User prompt
    │
    ▼
┌──────────┐
│  Opus 5  │  ← does the actual work
└────┬─────┘
     │ response (already shown to user — hooks can't replace it)
     ▼
 Stop hook fires → challenger_hook.py (command hook)
     │
     ├─ cwd not in ENABLED_PROJECTS? ─► allow (~70ms)
     ├─ < 1750 chars? ────────────────► allow
     ├─ session model not Opus 5? ────► allow (sniffed from transcript tail)
     ▼
 editor call (backend-selectable)       ← default: OpenAI gpt-5.6-sol via the
     │  critic-prompt.md + user's        ask_codex.py bridge; CHALLENGER_CRITIC=claude
     │  request (from transcript)        switches to claude -p (claude-fable-5,
     │  [+ clarification exchange]       hooks disabled via critic-settings.json)
     │  + response to edit
     ▼
 JSON decision {action, message}
     │
     ├─ action: echo_to_user ────────► block: agent is told to post the edited
     │                                 report verbatim; the next Stop is allowed
     │                                 (phase "echo" in the state file)
     ▼ action: ask_model
 block: the editor's questions go to the agent as its next instruction
     │
     ▼
 agent answers → Stop fires again → editor re-invoked with the full
 clarification exchange (max 2 ask rounds, then the original ships)
```

Key facts from the hooks docs that shaped this (verified against live docs, Aug 2026):

- The `Stop` payload includes `last_assistant_message` and `stop_hook_active` — no transcript parsing needed for the response text. It does **not** include the session model, which is why `challenger_hook.py` sniffs it from the transcript tail (assistant entries carry `message.model`).
- On block, the reason is fed to the **same** Opus 5 session as its next instruction — the "reviser" comes for free, no separate workflow/agent needed.
- Hooks cannot replace the displayed response *retroactively*; by the time `Stop` fires the original is rendered and the revision appears as a follow-up message. But since v2.1.152 there is a `MessageDisplay` hook event that transforms text *as it displays*: it fires once per flush of newly completed lines while a message renders (exactly one flush per message carries `final: true`; in `-p` mode the whole message arrives as one final flush), and returning `hookSpecificOutput.displayContent` replaces that delta on screen. Display-only — the transcript and the model's context keep the original, and verbose mode still shows it. Verified against the installed 2.1.220 binary and live in `-p` mode. Known platform caveats: the interactive terminal TUI silently drops `displayContent` (anthropics/claude-code#83957, #85773), and the desktop app reportedly applies it without awaiting the verdict, so the draft may flash before being replaced.
- Stop hooks have no matcher expressions (no `response_length > 200` syntax) — all gating happens inside the hook script.
- All hooks under one matcher run in parallel, so a command hook can't gate a separate agent hook; that's why everything lives in one script.
- Claude Code hard-caps at 8 consecutive Stop blocks, a backstop under our own 2-round limit.

## Components

### `challenger_hook.py` — the hook (command type)

Reads the Stop payload from stdin. Programmatic gates: project allowlist (`ENABLED_PROJECTS`, cwd-prefix match), length (`MIN_CHARS` = 1750 by default, skipped on continuation stops), and session model (`TARGET_MODEL_PREFIX = "claude-opus-5"` — Fable/Sonnet/older Opus are never touched). Past the gates it invokes the editor, then acts on its decision: `echo_to_user` blocks with "post this verbatim" and lets the next Stop through (state phase "echo"); `ask_model` blocks with the questions, stores the exchange in the state file, and re-invokes the editor with the answers on the next Stop (max `MAX_ASK_ROUNDS = 2`, then the original ships). Fails open on any error/timeout/unparseable output, logs every decision to `hook-debug.log`.

### `challenger_display_hook.py` — the display companion (optional, `MessageDisplay` type)

Hides would-be-edited drafts so the user reads one report instead of draft-plus-rewrite. A draft cannot be un-rendered once shown, so the hook buffers instead: for messages passing the same cheap gates as the Stop hook (project, model via transcript sniff, phase from the shared state file) it swallows every delta into `%TEMP%\challenger-display-<session_id>.json` and on the final flush either releases the whole message (under `MIN_CHARS` — appears at once instead of streaming) or collapses it to a one-line placeholder (the editor takes it from there). Echo-phase deliveries stream untouched; ask-round answers collapse to a note. Imports `challenger_hook` for all config and helpers, and fails open per flush — any error shows the original delta.

With `CHALLENGER_DISPLAY_EDIT=1` the editor call moves into this hook: on the final flush it edits the message itself and emits the report as `displayContent`, so the user reads one message and the Stop hook has nothing to do but allow (phase `delivered`) or ask the questions the editor parked for it (phase `ask_pending`). This requires the `MessageDisplay` entry's `timeout` raised to 360 — the event's default budget is 10s (`Ei=1e4`), and a per-hook `timeout` overrides it (measured: a hook registered at 90 stalled 20s and its `displayContent` was still honored).

The coupling back: when the display hook collapses something it sets `hidden_any` in its state file. If the editor round then fails open, the Stop hook's `fail_open()` sees the flag and blocks once with `REPOST_INSTRUCTION` (phase `echo`) so the agent reposts the hidden draft — the placeholder is never the end of the story. Without the companion registered the flag never exists and the Stop hook behaves exactly as before.

### `critic-prompt.md` — the editor's instructions

The user's own "Response Editor" prompt: improve the writing while preserving voice, tone, formality, and point of view — surface a buried result, simplify dense sentences, cut repetition, replace local jargon — keeping technical detail, caveats, verification status, remaining actions, and useful references; ask the coding agent targeted clarification questions only when the answer materially affects the rewrite. Two-action JSON protocol (`echo_to_user` / `ask_model`). Historical prompts are archived under `research/` (`critic-prompt-detailed.md` — the 10-category critique taxonomy; `checklist-draft.md` — the first draft); nothing reads them at runtime.

### `schema.json` — the editor decision shape

Documents `{action: echo_to_user | ask_model, message}`. Documentation only — the hook validates the two fields itself.

### `challenger.conf` — the settings

Read by the hook at import from the file next to `challenger_hook.py`; real environment variables win over it. `CHALLENGER_PROJECTS` (os.pathsep-separated absolute roots) is the allowlist and `CHALLENGER_CRITIC` picks the backend; everything else — gates, models, timeout, log and prompt paths — has a tested default and needs no entry. Not named `.env` because nothing secret goes in it: both backends authenticate through their own CLI. `challenger.conf.example` is the documented copy; the real file is gitignored, as is `*.local.md`, so a user's own `critic-prompt.local.md` survives updates too.

### The wiring

One command hook on `Stop` in `~/.claude/settings.json` running `challenger_hook.py` with a 360s timeout (editor subprocess capped at 300s). `critic-settings.json` (`{"disableAllHooks": true}`) is passed to the headless Claude editor so it can't recursively trigger this same hook.

## Deployment scope

Deployed globally since 2026-08-26: one `Stop` hook entry in `~/.claude/settings.json` runs the script for every session on the machine, and `CHALLENGER_PROJECTS` in `challenger.conf` decides by cwd-prefix match which projects are actually challenged. To enable another project, add its root to that line — no settings files needed anywhere, and worktrees under an enabled root are covered automatically (prefix match). Sessions in non-enabled projects pay ~70ms per stop (interpreter startup; the check itself is a string comparison, first gate, before any file IO). The live list lives in the local `challenger.conf`, which is gitignored, so it is not recorded here.

Earlier per-project wirings (`TheChallenger/.claude/settings.json`, `RotEA26/.claude/settings.local.json`) were removed as redundant.

Known coverage limit: farmed background card agents end with `SubagentStop`, not `Stop`, so they're never challenged regardless of configuration — deliberate, since their output goes to the overseer agent, not the user. The overseer session's own responses are challenged.

All decisions from every project land in this project's `hook-debug.log` (self-rotating past 512KB). To gate Opus 5 output everywhere, copy the same `Stop` hook entry into `~/.claude/settings.json` (user-level settings merge with project-level ones); the script already uses absolute paths for everything it reads, so it works from any cwd. Costs to know before going global: every Opus 5 response over the length gate in any project pays an Opus 4.6 critique (~10-45s after the response renders, subscription-billed), and blocks trigger revision turns in the main session. Roll back by deleting the entry again.

## Status

1. ~~Checklist as evaluation criteria~~ — done (`critic-prompt.md`)
2. ~~Critique JSON schema~~ — done (`schema.json`)
3. ~~Research Stop hook capabilities~~ — done (see Architecture)
4. ~~Hook implementation with programmatic gating~~ — done (`challenger_hook.py`)
5. ~~Test: short-circuit paths, block path (Claudish sample), pass path (clean sample)~~ — done, all verified 2026-08-26
6. ~~Critic prompt enriched from the Reddit research~~ — done: real harvested examples per category, expanded stock-phrase list, pseudo-concession / false-importance / emphasis-inversion / tangential-warning patterns added
7. ~~User-question context for the critic~~ — done: the hook extracts the user's last real prompt from the transcript tail (fail-open) so verbosity/register are judged against the actual question; first test caught a lede-burying answer that context-free evaluation passed
8. ~~Pass visibility~~ — done: `systemMessage` surfaces critic outcomes ("passed clean", "passed with N low-severity notes", "blocked (round N)", "revision limit reached"); cheap-gate allows stay silent
9. ~~Revision context~~ — done: the state file carries the prior round's critique, which the critic receives on revision rounds; binary oscillation is now actually judgeable, and round 2 verifies fixes instead of re-critiquing blind (revisions also skip the length gate, since a meaning-stripped rewrite is typically short)
10. ~~Buried-actionability category~~ — done: added from live calibration after a review of this very project passed the critic clean while its reader couldn't extract what "apply 2–5" meant; re-tested on that exact review, both candidate critics now block it with the same core finding (the ask is unapprovable: one item forks into two options, another states no fix)
11. ~~Critic model A/B~~ — done: Opus 4.6 vs Fable 5 on the escaped review — detection tied, Fable's suggestions resolved decision forks instead of restating them; default switched to `claude-fable-5`, override with the `CHALLENGER_CRITIC_MODEL` env var
12. ~~Polish pass~~ — done: a pass with only low-severity findings triggers one revision carrying the notes as minimal-edit polish instructions ("change nothing else"), and the polished version ships without another critique — one extra turn, no extra critic call
13. ~~Insider-density / altitude category~~ — done: tenth category from Source 11 (ChatGPT's review of a real RotEA26 agent PR summary + the user's stated preference for release-note-altitude reports with details on request). Validated on that exact pair: the dense original now blocks (HIGH insider-density on the same sentence ChatGPT singled out, suggestions converging on ChatGPT's literal rewrites) while ChatGPT's accepted high-level rewrite passes with zero findings
14. ~~Cross-vendor critic backend~~ — done: OpenAI (gpt-5.6-sol via the codex CLI bridge) is the default critic, on the reasoning that a non-Claude judge can't share Claude's stylistic blind spots; validated on the dense/high-level pair (blocks the dense original on the same passages the Claude critics flagged, passes the rewrite, 2-4x faster); Claude backend retained behind `CHALLENGER_CRITIC=claude`
15. ~~Prompt slimmed to ChatGPT register~~ — done: the 110-line rubric replaced by a short "what do you think of the writing?" ask with the user's altitude preference baked in; validated on the dense/high-level pair on both backends — verdicts unchanged, findings sharper (both critics caught issues the rubric runs missed), categories now critic-chosen free labels
16. ~~Editor architecture~~ — done: replaced critique-and-revise with the user's "Report Editor" design — the editor model rewrites the response into the final report directly (`echo_to_user`, delivered by the agent posting it verbatim) and may first ask the agent clarification questions (`ask_model`, bounded at 2 rounds, exchange threaded back to the editor). Severity gates and the polish pass are gone with the old flow. Validated: the dense RotEA26 report came back as a release-note rewrite that preserved every caveat (10s); a fabricated ambiguous report's clarification round produced a final report correctly incorporating all three answers (6s)
17. Live tuning: threshold (raised 1000 -> 1750 on 2026-08-27, short status updates were not worth an editor round trip), max rounds (2), severity calibration, critic latency (~10-30s pass / ~45s block per invocation) — pending real Opus 5 sessions
18. ~~Display companion prototype~~ — done 2026-08-27: `challenger_display_hook.py` (optional `MessageDisplay` hook) buffers candidate drafts per flush and collapses long ones to a placeholder, so the user reads only the edited report; the Stop hook gained `fail_open()` repost — an editor failure after a hide blocks once to make the agent repost the draft. Schema recovered from the installed 2.1.220 binary (per-flush whole-line deltas, one `final` flush per message, `hookSpecificOutput.displayContent`, ~10s per-flush budget). Verified: 16 offline checks (buffer/collapse/echo/ask/gates/repost loop), live `-p` runs (a 1139-char draft collapsed to the placeholder end-to-end), and a real desktop session on 2026-08-27 — the desktop app honors `displayContent`: draft withheld, edited report delivered as the only readable reply. Known limits: first turn of a fresh session fails open (no assistant entry in the transcript yet, model sniff returns None); interactive TUI drops `displayContent` upstream (#83957); desktop app may flash the draft before replacing; long *mid-turn* messages collapse too but only the turn's final message is edited/repostable — watch in calibration
19. ~~Foldout for the withheld draft~~ — done 2026-08-27: the placeholder now carries the full original inside a collapsed `<details>` block (the desktop renderer supports it — verified by rendering one in a live session), so the draft is one click away instead of a ctrl+o trip; ask-round answers get the same treatment; literal `</details>` inside a draft is defused with a space. Same session also produced the first live run of the repost path in the real harness (draft withheld -> stub editor failed -> repost block -> echo delivered) and the first live echo deviation: the agent refused to post an editor rewrite that had fabricated a list entry and stripped `[SN]` citations — the unreviewed echo turn doubling as a fact-check on the editor. Calibration lead: teach `critic-prompt.md` to preserve citation markers and never alter checkable lists
20. ~~Sanctioned echo refusal~~ — done 2026-08-27: `ECHO_INSTRUCTION` now tells the agent that if the rewrite gets facts wrong (invented details, dropped caveats, broken citations) it should post its original report verbatim instead, without commentary — channeling the fact-check observed in item 19 into a clean repost instead of an improvised complaint; style is explicitly not grounds for rejection, to keep Opus's instruction resistance from turning the escape hatch into a veto
21. ~~Foldout replaced by a link~~ — done 2026-08-28: item 19's `<details>` foldout never folded. The message stream renders no raw HTML, so the placeholder shipped the markup *and* the full draft it was meant to hide, putting the original back on screen — draft-then-edit, exactly what the companion exists to prevent. It went unnoticed because `hook-debug.log` records that a draft was hidden, not what the renderer then did with the replacement; item 19's "verified by rendering one in a live session" claim was wrong, most likely checked in a surface that does render HTML. Replaced with `stash()`: the hidden text goes to a file the placeholder links to, swept after three days, degrading to the bare placeholder if the file cannot be written. First attempt put it in `%TEMP%` — the app refuses to open links that resolve outside the session's working directory ("Couldn't find this file... or it lives outside the working directory", with only a Show in Explorer fallback), so the stash moved into the project as `.claude/challenger-drafts/<session>-<message>.md`, linked by relative path. That directory writes itself a `.gitignore` containing `*`, which ignores the directory including the ignore file itself: enabled projects gain a hidden folder but nothing in `git status`, and their own ignore rules are never edited. Verified offline (relative link emitted, file round-trips the draft byte for byte, `.gitignore` written, `git status` clean, short messages still pass through untouched) and live in the session that made the change — a stashed draft linked in the placeholder form was clicked open in the desktop app, which is the check item 19 skipped. Also corrected: the desktop app runs Claude Code 2.1.246, not the 2.1.220 recorded above

22. ~~Editing at display time~~ — done 2026-08-30: `CHALLENGER_DISPLAY_EDIT` (default off) runs the editor inside the display companion and draws its report in place of the draft, ending the echo turn — one message per report instead of two, and one fewer Opus turn. Three measurements shaped it, all against the desktop app's 2.1.247 build: (a) the `MessageDisplay` budget is `Ei=1e4` but `W.timeout ? W.timeout*1000 : i` means a per-hook `timeout` overrides it — a hook registered at 90 stalled 20s and its `displayContent` was still applied; (b) the display hook cannot identify the turn's last message — the payload's `final` marks the last flush of *that message*, and the transcript at display time holds only the *previous* message (whose `stop_reason` of `tool_use` vs `end_turn` is the signal, arriving one message too late); (c) the tandem workaround does not exist — a display hook that waits for the Stop hook to signal deadlocks, measured as both flushes timing out at 20s with the Stop hook running one second after the display hook gave up. So every message over `MIN_CHARS` is edited, `MIDTURN_NOTE` tells the editor the text may be a progress note, and `ask_model` is parked for the Stop hook rather than answered mid-render. `check_fidelity()` replaces item 20's escape hatch mechanically — citation markers, file paths, and backticked identifiers in the report must appear in the draft or the user's request — and falls back to the placeholder path (where the agent does see the rewrite) when it trips. Verified by 14 offline checks driving both hooks as subprocesses against a stub editor bridge: delivery, guard rejection, editor failure, the repost path, the parked clarification round, short messages, the flag off, and non-Opus sessions. Not yet exercised in a live session

23. ~~Voice preservation~~ — done 2026-08-30: the "Report Editor" prompt asked for release-note register unconditionally, so every response was flattened into the same kind of document regardless of what it was. Measured on a design-deliberation turn: first-person judgment was laundered into institutional recommendation voice ("the one I'd build" -> "the recommended fix is"), the closing question to the user was demoted to a status line ("the remaining decision is..."), the reasoning that made a judgment checkable was dropped (a three-way taxonomy of fixes, and an explicit "so it's not fatal" rebuttal of the objection to one of them), and the template's empty verification slot produced "No implementation or verification has been completed yet" on a turn that had never claimed any. Replaced with the user's "Response Editor" prompt: preserve voice, tone, formality and point of view, improve clarity rather than convert register, do not shorten for its own sake. Validated on both cases against gpt-5.6-sol — the deliberation turn came back in first person with its question intact and the verdict promoted to the opening sentence, and a dense completed-work report still led with the result and kept its first-person ownership, its untested-backend gap, and an incidental bug it had declined to fix. Also corrected while transcribing: the new prompt documented a `--- PRIOR CRITIQUE ---` block the hook has never sent, in place of the `--- CLARIFICATION EXCHANGE ---` block it does.

## Remaining considerations

- **Cost/latency**: every long Opus 5 report now costs one editor call (~5-15s on gpt-5.6-sol) plus one echo turn from the agent — the editor always rewrites; there is no "already fine, pass through" action in the protocol. If live usage makes that feel wasteful on already-good reports, either add a third `keep_as_is` action to the prompt/hook, or accept that a rewrite of a good report is nearly identical to it.
- **Echo fidelity**: the agent is instructed to post the edited report verbatim, and that stop ships unreviewed (phase "echo" allows it). An agent that appends commentary or "improves" the report won't be caught — watch early sessions for deviation. `CHALLENGER_DISPLAY_EDIT` removes this failure mode by removing the echo turn, at the cost of the fact-check that came with it (item 22).
- **Mid-turn editor cost**: with display-time editing on, every message over `MIN_CHARS` pays an editor call, including long mid-turn ones, because the turn's last message is not identifiable at display time (item 22). How often that actually happens is unmeasured. `stop_reason` in the transcript settles it one message late, which is useless for prevention but enough for accounting: if the cost turns out to matter, log per turn how many threshold-crossing messages later turn out to have been mid-turn before considering anything cleverer.
- **Transcript coupling**: model sniffing and question extraction parse the transcript JSONL, whose format is internal and may change between Claude Code versions. Both fail open (model gate skips the critique; context is omitted).
- **Which model for the critic**: OpenAI `gpt-5.6-sol` by default (env `CHALLENGER_CRITIC=codex`, the default) via `C:\Programming\CodexCLI\ask_codex.py` — chosen for vendor independence (a Claude critic shares training biases with the Claude it judges) and speed (11-24s vs Fable's 14-59s on the same pair). `CHALLENGER_CRITIC=claude` switches to `claude -p` with `CHALLENGER_CRITIC_MODEL` (default `claude-fable-5`); the prompted Fable critic performed comparably on the validation pair, so both backends are credible — worth comparing on live traffic. Codex-backend note: requires the user's `codex login` to be valid; auth failure fails open (allows).
