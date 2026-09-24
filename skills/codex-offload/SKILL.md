---
name: codex-offload
description: Delegate a bounded task to, or get a second opinion from, the installed Codex CLI (OpenAI GPT models such as Astra, Sol and Luna), on a model the user names or one chosen for the task; poll progress, read the result, and continue the same Codex session. Use whenever the user asks to offload, hand off, consult, cross-check or review something with Codex, GPT or OpenAI, wants "another model" to look at code, or names a Codex model ("use Sol", "have GPT-6 fix the tests"), even without mentioning a skill.
---

# Codex Offload

Run a task in the user's installed `codex` CLI as a separate agent with its own conversation, login and usage, then verify its result. Requires macOS/Linux, Python 3, and a logged-in `codex`. The bundled helper `scripts/offload.py` owns launching, supervision and resume, so use it rather than calling `codex` directly.

In every command, replace `$SKILL_DIR` (the directory containing this file), `$OFFLOAD_ID` and other placeholders with literal absolute values: host shells often do not keep variables between calls. Every command prints JSON.

## 1. Preflight

Run `codex --version` and `codex login status` once. If the host sandbox blocks process inspection, login, networking or writes to `~/.local/state/agent-offload`, rerun through the host's normal approval mechanism instead of weakening the offload's own settings.

## 2. Choose the model and effort

Lineups change, so check what is installed instead of trusting names you remember:

```bash
python3 "$SKILL_DIR/scripts/offload.py" models --tool codex
```

It lists each model's ID, description, family and supported efforts, plus `latest_by_family`. No model is called (the CLI may refresh its catalogue online).

- **User named a family** ("use Sol"): `--model sol` resolves to the newest listed release, e.g. `gpt-6-sol` over `gpt-5.6-sol`.
- **User named a version** ("GPT-5.6 Sol"): pass the exact ID, `--model gpt-5.6-sol`.
- **Name matches nothing:** pick the closest entry by ID or display name and pass its exact ID; ask the user if it is ambiguous. Models outside the `gpt-<version>-<family>` pattern (`family: null`) work by exact ID.
- **No preference:** match the catalogue descriptions to the task. At the time of writing: `astra` (the default) for the hardest reasoning, architecture, security, subtle debugging and independent reviews; `sol` for everyday, well-specified coding; `luna` for quick lookups and mechanical edits. When unsure, prefer the stronger model: a wrong answer costs more than the extra usage.
- **Effort** defaults to `high` (the catalogue's own default is lower). Use the model's higher levels (`xhigh`, `max`, `ultra` where listed) for long, hard problems and `medium`/`low` for simple ones, or whatever the user asked for.

The helper always runs at the standard service tier with Fast disabled. It never substitutes a model: an unknown model or unsupported effort makes `start` exit 2 with the available choices and launches nothing. Tell the user rather than silently picking something else.

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

For a second opinion, give the evidence without your hypothesis, so the answer is independent. Codex's `workspace-write` sandbox usually has no network, so say so if the checks need to install dependencies or reach services. The helper adds its own boundary text (stay in scope, no nested agents, begin with `NEEDS_INPUT:` if blocked), so don't repeat it. Never use an offload to get around a permission the user or host denied.

## 4. Start

```bash
python3 "$SKILL_DIR/scripts/offload.py" start --tool codex --cwd "$TARGET_DIR" \
  --prompt-file "$PROMPT_FILE" --mode read --model sol --effort high
```

- **Modes:** `--mode read` (default) uses a read-only sandbox; Codex can still run commands that don't write files, such as most test runs. `--mode edit` uses `workspace-write`. Asking Codex to fix or change something is authorization to edit; otherwise stay read-only. Add `--allow-non-git` for an intentional non-Git workspace.
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

- **`done`** with `transport_success: true` means the CLI turn finished cleanly, not that the work is right. Check the answer against the acceptance criteria, inspect changed files (`git status`, `git diff`) and rerun the relevant tests yourself. Don't report the child's claimed checks as your own verification.
- **`needs_input`:** the child is blocked. If the user's existing instructions answer it, reply with `send`; otherwise ask the user.
- **`failed`, `timed_out`, `expired` or `interrupted`:** read `stop_reason`, `last_error` and `stderr.log`. Inspect partial edits before retrying, and fix the cause (login, model, rate limit) instead of rerunning the same failure.

`status` also reports `resolved_model` and `effort`. Codex's event stream doesn't reliably include the effective model, so this is the request, not independent confirmation. Report the outcome, your own verification, any blockers, and the offload ID if a follow-up is likely.

## 7. Follow up in the same session

```bash
python3 "$SKILL_DIR/scripts/offload.py" send "$OFFLOAD_ID" --prompt-file "$FOLLOWUP_FILE" --request-id followup-1
```

`send` resumes the exact Codex session once the current run has exited, with the same model, effort, mode and limits. Use a new `--request-id` for each new message; reusing one returns the earlier run (safe for retries). It cannot change the model or permissions: for that, `start` a new offload and include the context it needs. Then poll as in step 5.

## References

- [Helper contract](references/helper.md): every option, state and exit code, `--owner-pid`, crash recovery, cursors and concurrency.
- [Advanced workflows](references/workflows.md): direct `codex exec` calls, code review, structured output, forks and troubleshooting. These bypass the helper's lease and watchdog.
- [CLI parameters](references/cli-reference.md): captured `codex` help. Prefer current local `--help` after an upgrade.
