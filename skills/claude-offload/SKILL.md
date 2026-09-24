---
name: claude-offload
description: Delegate a bounded task to, or get a second opinion from, the installed Claude Code CLI (Anthropic Claude models such as Fable, Opus, Sonnet and Haiku), on a model the user names or one chosen for the task; poll progress, read the result, and continue the same Claude session. Use from Codex or another non-Claude agent whenever the user asks to offload, hand off, consult, cross-check or review something with Claude, Claude Code or Anthropic, wants "another model" to look at code, or names a Claude model ("ask Opus", "let Sonnet write the tests"). Not for use inside Claude Code itself.
---

# Claude Offload

Run a task in the user's installed `claude` CLI as a separate agent with its own conversation, login and usage, then verify its result. Requires macOS/Linux, Python 3, and a logged-in `claude`. The bundled helper `scripts/offload.py` owns launching, supervision and resume, so use it rather than calling `claude` directly.

Run this from Codex or another non-Claude host. The helper refuses to start inside a Claude Code session (`CLAUDECODE` is set) because nested sessions misbehave; inside Claude Code, use its own subagents instead.

In every command, replace `$SKILL_DIR` (the directory containing this file), `$OFFLOAD_ID` and other placeholders with literal absolute values: host shells often do not keep variables between calls. Every command prints JSON.

## 1. Preflight

Run `claude --version` and `claude auth status --text` once. If the host sandbox blocks process inspection, Keychain or login access, networking or writes to `~/.local/state/agent-offload`, rerun through the host's normal approval mechanism instead of weakening the offload's own settings.

## 2. Choose the model and effort

Lineups change, so don't assume the names below are current:

```bash
python3 "$SKILL_DIR/scripts/offload.py" models --tool claude
python3 "$SKILL_DIR/scripts/offload.py" models --tool claude --probe opus   # one tiny paid call
```

Claude Code has no offline model list. With `ANTHROPIC_API_KEY` set, `models` reads the Models API for that key's account and reports `latest_by_family`; otherwise it returns the family aliases. `--probe ALIAS` shows the exact release an alias selects; use it only when the exact version matters. Current IDs are also listed in the [models overview](https://docs.claude.com/en/docs/about-claude/models/overview).

- **User named a family** ("ask Opus"): `--model opus` passes Claude Code's alias for the newest Opus. A context-window variant such as `opus[1m]` also works.
- **User named a version** ("Opus 5.5"): pass the exact ID, `--model claude-opus-5-5`.
- **Name you can't confirm:** a new family word (for example a future `--model lyra`) is passed through and checked against the model that answers. If the name is neither a family nor a `claude-…` ID, ask the user rather than guessing.
- **No preference:** at the time of writing, use `fable` (the default) for the hardest reasoning, architecture, security, subtle debugging and independent reviews; `opus` for demanding coding and review; `sonnet` for routine, well-specified coding; `haiku` for quick lookups and mechanical edits. When unsure, prefer the stronger model: a wrong answer costs more than the extra usage.
- **Effort** is one of `low`, `medium`, `high` (default), `xhigh`, `max`. Use `xhigh`/`max` for long, hard problems and `medium`/`low` for simple ones, or whatever the user asked for.

The helper disables Fast mode and Ultracode. It never substitutes a model: if the main model Claude reports is outside the requested family (or isn't the requested exact ID), the run fails with `stop_reason: model_mismatch`. Report that rather than retrying on another model. Aggregate usage may include Claude Code's own auxiliary models; those don't count as a mismatch. Composite aliases such as `default` or `opusplan` choose a model per mode, so they run unverified (`model_verified: false`).

## 3. Write the handoff

The child sees only the prompt file, not your conversation. Write it outside the repository (for example with `mktemp`) so it doesn't appear in the diff, and delete it once the offload is finished:

```text
Objective: <one sentence>
Workspace: <absolute path>. Uncommitted changes to preserve: <list or "none">
Context: <relevant files, error output, prior findings, applicable AGENTS.md rules>
Allowed changes: <none | specific files or directories>
Acceptance checks: <commands to run and the expected outcome>
Deliverables: answer or findings; files changed; checks run with results; open questions
```

For a second opinion, give the evidence without your hypothesis, so the answer is independent. Claude loads its own `CLAUDE.md` but not `AGENTS.md`, so pass on any rules that matter. The helper adds its own boundary text (stay in scope, no nested agents, begin with `NEEDS_INPUT:` if blocked), so don't repeat it. Never use an offload to get around a permission the user or host denied.

## 4. Start

```bash
python3 "$SKILL_DIR/scripts/offload.py" start --tool claude --cwd "$TARGET_DIR" \
  --prompt-file "$PROMPT_FILE" --mode read --model opus --effort high
```

Claude runs with `dontAsk`, no permission prompts and no MCP servers. These are tool permissions, not an OS sandbox.
- **`--mode read`** (default) allows Read, Glob and Grep (WebSearch and WebFetch can be added with `--tools`). There is no shell, so to review a diff, save it to a file first (`git diff main...HEAD > /tmp/review/changes.diff`, or `git show HEAD --format= > …` for the last commit) and name that file in the prompt, adding `--add-dir /tmp/review` if it is outside the workspace. A patch can still be requested as text in the answer.
- **`--mode edit`** also allows Write and Edit. Asking Claude to fix or change something is authorization to edit; otherwise stay read-only. For shell commands, select Bash and grant only what is needed, for example `--tools Read,Glob,Grep,Edit,Write,Bash --allow-tool 'Bash(npm test)'`. `--allow-tool` rules are added to the other selected tools; Bash itself is never granted wholesale.
- **Before an edit run:** record `git status --porcelain` so you can attribute the child's changes afterwards, and don't edit the same tree while it runs.
- **Save the returned `offload_id`.** Passing a short descriptive `--id` (for example `limiter-review`) makes it the offload ID, and a retry of the same request then returns the existing offload instead of starting another.
- **Mode and model are fixed for the whole conversation**, including follow-ups. `xhigh`/`max` effort on a large task can run long, so raise `--max-runtime` (seconds per run) if needed.

## 5. Follow it until it finishes

A run is stopped automatically after **5 minutes without a lease renewal** and after **30 minutes per run** (`--lease-seconds` and `--max-runtime` at `start` raise these; both must be finite). `status`, `read`, `wait`, `touch` and `send` each renew the lease once; `list` and `status --no-touch` don't. So poll at least every minute or two while you are responsible for the run:

```bash
# One shell call covering about 9 minutes; each wait renews the lease. Exit code 124 means still running.
for i in 1 2 3 4 5 6 7 8 9; do
  python3 "$SKILL_DIR/scripts/offload.py" wait "$OFFLOAD_ID" --timeout 60; rc=$?
  [ "$rc" -ne 124 ] && break
done
```

Keep each shell call below your host's command timeout and repeat until the exit code is no longer 124. `read "$OFFLOAD_ID" [--cursor C]` returns new progress events and the next cursor. `stop "$OFFLOAD_ID"` requests cancellation; confirm with `wait`. Before ending your own task, stop any runs you still own.

Exit codes: 0 finished; 2 invalid input or setup (see `error`); 3 finished but needs attention; 4 busy or conflicting request; 124 `wait` timed out while the run is still active. Codes 3 and 124 are normal outcomes, not helper failures, so read the JSON.

## 6. Accept the result

The child's final answer is `last_message` in the `status`/`wait` JSON (the last 8000 characters). The full log is `run_dir/events.jsonl` and CLI errors are in `run_dir/stderr.log`.

- **`done`** with `transport_success: true` means the CLI turn finished cleanly, not that the work is right. Check the answer against the acceptance criteria, look at `permission_denials`, inspect changed files and rerun the relevant tests yourself. Don't report the child's claimed checks as your own verification. A denied optional tool doesn't by itself mean the task failed.
- **`needs_input`:** the child is blocked. If the user's existing instructions answer it, reply with `send`; otherwise ask the user.
- **`failed`, `timed_out`, `expired` or `interrupted`:** read `stop_reason`, `observed_models` and `stderr.log`. Inspect partial edits before retrying, and fix the cause (login, model, rate limit) instead of rerunning the same failure.

`status` also shows `requested_model`, `observed_model` and `effort`. Report the outcome, your own verification, any blockers, and the offload ID if a follow-up is likely.

## 7. Follow up in the same session

```bash
python3 "$SKILL_DIR/scripts/offload.py" send "$OFFLOAD_ID" --prompt-file "$FOLLOWUP_FILE" --request-id followup-1
```

`send` resumes the exact Claude session once the current run has exited. It uses the same effort, mode and limits, and stays on the release that answered the first run even if the alias has since moved. Use a new `--request-id` for each new message; reusing one returns the earlier run (safe for retries). It cannot change the model or permissions: for that, `start` a new offload and include the context it needs. Then poll as in step 5.

## References

- [Helper contract](references/helper.md): every option, state and exit code, `--owner-pid`, crash recovery, cursors and concurrency.
- [Advanced workflows](references/workflows.md): direct `claude -p` calls, structured output, native background sessions, authentication and billing, and troubleshooting. These bypass the helper's lease and watchdog.
- [CLI parameters](references/cli-reference.md): captured `claude` help. Prefer current local `--help` after an upgrade.
