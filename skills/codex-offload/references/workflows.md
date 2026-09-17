# Codex handoff workflows

**Advanced/direct-call reference.** Normal offloads use `scripts/offload.py` and [the helper contract](helper.md). The direct calls and legacy `run.py` recorder below do not provide leases or a watchdog. Keep the model preset, use a finite timeout, and retain a host process handle when using them.

Verified CLI surface: **codex-cli 0.154.0**, 2026-09-17. Local help is authoritative for accepted flags; refresh it after upgrades. The [official noninteractive guide](https://learn.chatgpt.com/docs/non-interactive-mode) explains the supported execution contract. Some older examples use removed `codex mcp-server` or deprecated `--full-auto`; neither is the recommended handoff here.

CLI 0.154.0 local verification: root `-a never` alone left `on-request` in exec turn metadata on this host. The lifecycle helper also passes exec-level `-c 'approval_policy="never"'`; a live resumed turn confirmed `never` with the read-only sandbox unchanged. This rejects unavailable escalations; it does not grant them.

## Required model and speed

The user wants **GPT-6 Astra**, **high** reasoning effort, and **standard speed** for every offload. Pass `-m gpt-6-astra -c 'model_reasoning_effort="high"' -c 'service_tier="default"' -c 'approval_policy="never"' --disable fast_mode` explicitly on initial calls, reviews, resumes and forks. Keep these exec-level options before the subcommand. Do not inherit a cheaper model or Fast/priority tier from a profile. Do not downgrade or retry with another model when Astra is unavailable; report the blocker. Change this preset only when the user explicitly requests a different one. Verify effective model/effort/tier when runtime metadata is available; a model mismatch is not a valid completed offload.

## Prompt contract

Write a prompt using a quoted heredoc or a file-writing tool, never an unescaped shell interpolation. Adapt this template; omit irrelevant fields.

```text
Objective: ...
Workspace: ...
Relevant files and existing changes: ...
Instructions inherited from the parent: ...
Allowed changes: ...
Out of scope: ...
Acceptance checks: ...
Deliverables: summary, files changed, checks and outcomes, unresolved blockers.
Complete this task yourself. Do not invoke another agentic CLI or offload skill.
Preserve unrelated changes. Do not commit, push, publish, or message others
unless the task explicitly authorizes that action. Report missing permissions.
```

For a second opinion, pass the raw problem and evidence without leading the child toward your favored conclusion. For implementation, pass the relevant decisions and exact acceptance criteria. Do not copy the entire parent conversation unless it is actually needed.

## Direct calls

Use the recorder for work that needs progress/status tracking. A short foreground call can be simpler:

```bash
codex -a never exec -m gpt-6-astra \
  -c 'model_reasoning_effort="high"' -c 'service_tier="default"' -c 'approval_policy="never"' \
  --disable fast_mode -C "$TARGET_DIR" -s read-only - < "$PROMPT_FILE"
```

Without `--json`, the final answer goes to stdout and progress to stderr. With `--json`, stdout is JSONL. Always keep stderr separate so JSON parsing survives diagnostic messages. `-o` provides a separate final-message artifact alongside JSONL. Preserve the CLI's exit code; a pipeline to `tee` or `jq` can hide it unless the shell uses `pipefail`.

## Permissions and configuration

| Need | Choice |
|---|---|
| Inspect files / review | `-s read-only` |
| Implement within the workspace | `-s workspace-write` |
| Add an authorized writable directory | Repeat `--add-dir /absolute/path` |
| Unattended refusal of escalation | Root option `-a never` |
| Live web search | Root `--search`, only if relevant |
| Configured profile | `-p NAME`; 0.154.0 loads `$CODEX_HOME/NAME.config.toml` |
| Required model and effort | `-m gpt-6-astra -c 'model_reasoning_effort="high"'` |
| Standard speed / no premium tier | `-c 'service_tier="default"' -c 'approval_policy="never"' --disable fast_mode` |
| Discard resumable session history | `--ephemeral` |
| Intentionally non-Git directory | `--skip-git-repo-check` |

Use explicit approval and sandbox settings because user/project configuration can change defaults. `never` denies escalation rather than granting full access. The child still inherits outer OS restrictions: a restricted parent shell can prevent networking, Keychain access, or writes to CLI state. Use the host's normal permission mechanism for a justified launch; do not relax child permissions to work around an unrelated parent restriction.

The shell sandbox does not automatically confine every MCP/app operation. Review enabled external tools when the handoff must be strictly read-only. A prompt boundary alone is not a technical isolation boundary.

`--ignore-user-config` changes model/provider/MCP defaults but still uses `CODEX_HOME` for auth. `--ignore-rules` discards execution rules. Do not use either as a routine fix for a denial. Retain normal auth/config for ordinary personal offloads. Never copy `auth.json` into the run folder or pass credentials in CLI arguments.

## Resume and fork

Capture `thread_id` from `thread.started` (the recorder calls it `session_id`). Wait until the current call has stopped. Supply a fresh `RUN_DIR` and a follow-up prompt file.

```bash
python3 "$SKILL_DIR/scripts/run.py" run \
  --tool codex --cwd "$TARGET_DIR" \
  --prompt-file "$FOLLOWUP_FILE" --run-dir "$NEXT_RUN_DIR" \
  -- codex -a never exec -m gpt-6-astra \
  -c 'model_reasoning_effort="high"' -c 'service_tier="default"' -c 'approval_policy="never"' \
  --disable fast_mode -C "$TARGET_DIR" -s read-only \
  resume "$SESSION_ID" --json -o "$NEXT_RUN_DIR/final.md" -
```

Keep exec-level `-C` and `-s` **before** `resume` or `fork`; they are not listed as subcommand-local flags. Root `-a never` goes before `exec`. Use `workspace-write` for an authorized implementation continuation. Never assume a previous run's permissions are the ones you want now.

Use `codex ... exec ... fork "$SESSION_ID" ... -` for an independent conversation derived from the saved one. It gets a new UUID; capture it again. `--last` can select an unrelated recent task; use IDs. Resuming an active UUID is not a status check and can launch more model work.

`codex agents` in 0.154.0 is an interactive app-server session browser, not a JSON status endpoint for arbitrary `exec` processes. `codex queue` targets sessions on the shared server; do not assume it can steer a standalone exec run. Prefer saved events plus the process handle; use app-server only when building an actual persistent orchestration application.

## Review

```bash
codex -a never exec -m gpt-6-astra \
  -c 'model_reasoning_effort="high"' -c 'service_tier="default"' -c 'approval_policy="never"' \
  --disable fast_mode -C "$TARGET_DIR" -s read-only \
  review --uncommitted --json
```

Other targets: `review --base "$BASE_BRANCH"` or `review --commit "$COMMIT_SHA"`. Choose one target. The dedicated review interface rejects combining a positional custom prompt with target selectors on the tested version. For custom review instructions, use regular `exec` with the desired target described in its prompt, or use `exec review` without a selector. Do not silently drop user instructions to fit the review command.

## Structured output

```bash
codex -a never exec -m gpt-6-astra \
  -c 'model_reasoning_effort="high"' -c 'service_tier="default"' -c 'approval_policy="never"' \
  --disable fast_mode -C "$TARGET_DIR" -s read-only \
  --json --output-schema "$SCHEMA_FILE" \
  -o "$RESULT_FILE" - < "$PROMPT_FILE"
```

`--output-schema` takes a **file path**. Supply an object schema with explicit required fields and `additionalProperties: false` where required by the selected provider. Parse and validate the final JSON separately. JSONL events and schema-shaped final output are different layers.

## Status, cancellation, and recovery

The recorder's `run.json` contains owned process IDs, command, cwd, timestamps, and exit code. It passes argv directly (no shell evaluation), sends stdin from a file, and creates a private new run directory. A PID-exists check only provides liveness evidence; PID reuse is possible. The recorded process exit plus a terminal event is stronger evidence.

- `thread.started`: record the UUID.
- `item.started`, `item.updated`, `item.completed`: task progress; inspect command/file/tool items as needed.
- `turn.completed`: model turn finished; inspect results and validations.
- `turn.failed`, nonzero process exit: investigate failure.
- `error`: inspect context; a transient retry event need not mean final failure.

Keep log parsing tolerant of additional fields/event types and an unfinished last line. A silent interval can be model reasoning, authentication, MCP startup, or a long command. Inspect stderr and the parent process handle before taking action. Do not launch duplicates merely because output is quiet.

On timeout/interruption, inspect the workspace before retrying. The child may already have edited files. On an auth/model/rate-limit error, fix that cause; do not repeatedly rerun the same failure. No resumable UUID means a fresh handoff is needed. Missing final output with exit 0 still requires investigation.

On macOS, a pending Xcode license can make `/usr/bin/git` unusable. This is a machine prerequisite; report it and let the user resolve it. Do not accept licenses, reinstall Git, or alter authentication as an incidental side effect.

## Source map

- [OpenAI noninteractive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI CLI commands](https://learn.chatgpt.com/docs/developer-commands)
- [OpenAI skill discovery](https://developers.openai.com/codex/skills)
- [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- Exact accepted options: [locally captured help](cli-reference.md).

Model preset sources: installed model catalogue (`gpt-6-astra` supports `high`), [speed controls](https://learn.chatgpt.com/docs/agent-configuration/speed), and [configuration](https://learn.chatgpt.com/docs/config-file/config-reference). Verified 2026-09-17.
