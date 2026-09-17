---
name: codex-offload
description: Delegate a bounded task to the installed Codex CLI, capture progress and results, and resume the exact session. Use when the user asks to offload work to Codex, consult Codex, or get an independent Codex review from Claude Code or another agent.
---

# Codex Offload

Delegate through the actual installed `codex` CLI. The child has its own conversation, authentication, configuration, and usage. This folder is self-contained: POSIX (macOS/Linux), Python 3, and an authenticated destination CLI are the runtime requirements. No service installation or MCP bridge is needed.

## Start and follow work

Resolve `SKILL_DIR` to the directory containing this file. Check `codex --version` and authentication status before first use. The tested versions and advanced flags are in [CLI reference](references/cli-reference.md).

Write the handoff to a prompt file: objective, working directory, relevant files and instructions, authorized edits, constraints, acceptance checks, and deliverables. The child does not inherit your conversation. Pass applicable parent instructions explicitly. Do not broaden permission or offload to evade a denied action. Nested delegation requires an explicit task request.

```bash
python3 "$SKILL_DIR/scripts/offload.py" start \
  --tool codex --cwd "$TARGET_DIR" --prompt-file "$PROMPT_FILE" --mode read
```

Save the returned `offload_id`. Use `--mode edit` for authorized edits. For an intentionally non-Git edit workspace also pass `--allow-non-git`. Commands return JSON. The helper applies **GPT-6 Astra, high effort, standard speed** to both starts and continuations; there is no smaller-model fallback. Codex uses `read-only` or `workspace-write` sandbox with approvals set to `never`. The helper pins `gpt-6-astra`, high reasoning, standard tier, and disables Fast. Exec JSONL does not consistently expose the effective model; the helper reports the requested preset without claiming independent model verification.

```bash
python3 "$SKILL_DIR/scripts/offload.py" status "$OFFLOAD_ID"
python3 "$SKILL_DIR/scripts/offload.py" read "$OFFLOAD_ID"
python3 "$SKILL_DIR/scripts/offload.py" wait "$OFFLOAD_ID" --timeout 30
python3 "$SKILL_DIR/scripts/offload.py" send "$OFFLOAD_ID" \
  --prompt-file "$FOLLOWUP_FILE" --request-id followup-1
python3 "$SKILL_DIR/scripts/offload.py" stop "$OFFLOAD_ID"
python3 "$SKILL_DIR/scripts/offload.py" list
```

`read` returns a cursor; pass it to the next `read --cursor "$CURSOR"` to avoid rereading events. `send` resumes the exact session after its current process exits. Reuse a request ID only when retrying the identical request; use a new ID for a new follow-up. Busy/lost runs refuse continuation. `stop` requests cancellation; follow with `wait` to confirm it stopped. There is no mid-turn send or automatic follow-up queue.

## Caller lifecycle — required

**Check or wait every 15–60 seconds while responsible for active offloads.** Each `status`, `read`, `wait`, `touch`, or `send` invocation renews the lease once. `list` and `status --no-touch` never renew it. A default run stops after **5 minutes without renewal**, or after **30 minutes total per run**, even if the caller disappears. Configure longer finite bounds at `start` only when the task needs them; neither accepts zero/unlimited. `wait` is capped at 60 seconds and never renews in a background loop.

When you know the stable caller agent/app PID, pass `--owner-pid PID` to stop sooner on its death. Never use the short-lived shell command's PID. Without an explicit owner, app death is detected by lease expiry, not immediately. Stop remaining children before ending your task, or clearly hand responsibility to an active caller. The temporary worker and watchdog both exit with their run. If both are forcibly killed, no standalone skill can guarantee cleanup; see recovery in [helper reference](references/helper.md).

Use the host's normal execution/approval mechanism if its sandbox blocks process inspection, auth, networking, or state writes. Never weaken destination settings to work around the host. Keep the helper ID, host shell-job handle, CLI session UUID, and run number distinct.

## Accept the result

`done` plus `transport_success: true` means a clean CLI turn and process exit, not correct work. Check the final answer, permission denials, changed files, and relevant tests. `needs_input` is a child-reported blocker; decide whether existing user authorization supplies the missing input before continuing. A denied optional tool alone does not mean the whole task failed. Stop/expiry can leave partial edits; inspect before retrying.

Full progress and final answers remain in the returned `run_dir/events.jsonl`; stderr is separate. Local inspection uses no model calls. Report outputs, verification, blockers, and a continuation ID when useful.

## References

- [Helper contract](references/helper.md): options, states, exit codes, cleanup, recovery, cursor and concurrency rules.
- [Advanced workflows](references/workflows.md): direct CLI calls, unusual parameters, structured output, native background modes and troubleshooting. These bypass the helper's lifecycle controls; use only when needed and manage their processes explicitly.
- [CLI parameters](references/cli-reference.md): full versioned CLI help snapshots. Consult current local help after an upgrade.
