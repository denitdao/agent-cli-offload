# Claude Code handoff workflows

**Advanced/direct-call reference.** Normal offloads use `scripts/offload.py` and [the helper contract](helper.md). The direct calls and legacy `run.py` recorder below do not provide leases or a watchdog. Pass the chosen model/effort and the standard-speed preset explicitly, use a finite timeout, and retain a host process handle when using them.

Verified CLI surface: **Claude Code 2.1.274**, 2026-09-17; help re-checked against 2.1.281 on 2026-09-24 (no changes to the flags used here). Prefer current local help when it disagrees with an example. For the execution contract, see [programmatic usage](https://code.claude.com/docs/en/headless).

## Model, effort and speed

Choose the model and effort as described in SKILL.md; the examples use `$MODEL` and `$EFFORT` for those values. `--model` takes a family alias (`fable`, `opus`, `sonnet`, `haiku`) for its newest release or a full ID for an exact one. Environment overrides such as `ANTHROPIC_DEFAULT_OPUS_MODEL` can redirect an alias, so check the reported model. Always use **standard speed**. Set `CLAUDE_CODE_DISABLE_FAST_MODE=1` and `CLAUDE_CODE_EFFORT_LEVEL` to the chosen effort on the child invocation, and pass `--settings '{"fastMode":false,"ultracode":false}'` (merge these keys into any task-required settings). Apply the same preset to resumes, forks, and native background launches; on resume, pass the full ID the session reported so it stays on the same release. Ultracode is off so it cannot raise effort beyond the chosen level. Do not add `--fallback-model` or switch models when the chosen one is unavailable. Report the blocker instead. Check `system/init.model`, assistant message model IDs and final `modelUsage` when available; report provider-imposed fallback rather than presenting it as a result from the requested model. Aggregate usage can include auxiliary Haiku calls from Claude Code itself; those are not evidence that the main task switched models.

## Prompt contract

Use a prompt file created through a file-writing tool or a quoted heredoc. Include objective, workspace, relevant files, applicable parent instructions, permitted edits, acceptance checks, and deliverables. Explain existing uncommitted changes the child must preserve.

Include this boundary, adapted to existing authorization:

```text
Complete this task yourself. Do not invoke another agentic CLI or offload skill.
Preserve unrelated changes. Do not commit, push, publish, or message others
unless this task explicitly authorizes it. Report missing permissions.
Return your findings/changes, checks performed with outcomes, and blockers.
```

For an independent opinion, present the evidence without supplying the answer you want. A new CLI process has no access to the parent's conversation unless you supply it. Claude normally loads its own `CLAUDE.md`; explicitly supply relevant `AGENTS.md` requirements as needed.

## Direct calls and output

For a short result without tools:

```bash
env CLAUDE_CODE_DISABLE_FAST_MODE=1 CLAUDE_CODE_EFFORT_LEVEL="$EFFORT" \
  claude -p --model "$MODEL" --effort "$EFFORT" \
  --settings '{"fastMode":false,"ultracode":false}' --tools '' --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --permission-mode dontAsk --permission-prompts none \
  --output-format json < "$PROMPT_FILE" > "$RESULT_FILE"
```

Run in `TARGET_DIR` through the host tool's working-directory option or a quoted subshell `cd`. Claude has no root `--cwd` equivalent; `claude agents --cwd` filters listings and is a different command.

- `text`: final text.
- `json`: one result envelope; includes result/session metadata. Check `is_error`, `subtype`, and permission denials.
- `stream-json`: JSONL progress and final result. Pair with `--verbose`; `--include-partial-messages` adds token deltas when useful.

Keep stderr separate. Check the actual CLI exit code rather than the last command in a pipeline. With JSON Schema output, read `structured_output`, not an assumed plain `result` string.

## Implementation

For ordinary authorized edits/tests, use `dontAsk` plus specific tools/rules. Example for a project whose verified test command is `npm test`:

```bash
env CLAUDE_CODE_DISABLE_FAST_MODE=1 CLAUDE_CODE_EFFORT_LEVEL="$EFFORT" \
  claude -p --model "$MODEL" --effort "$EFFORT" \
  --settings '{"fastMode":false,"ultracode":false}' --output-format stream-json --verbose \
  --permission-mode dontAsk --permission-prompts none \
  --tools 'Read,Glob,Grep,Edit,Write,Bash' \
  --allowedTools 'Read,Glob,Grep,Edit,Write,Bash(npm test),Bash(npm test *)' \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' < "$PROMPT_FILE"
```

Replace the test rule with the exact project command. Running a test script executes project code; the permission rule is not proof that the script is harmless. If broad shell work is actually required, choose the appropriate authorized permissions/sandbox rather than accumulating misleadingly broad prefix rules.

`acceptEdits` is convenient when broad file editing is intended; it also auto-approves common filesystem commands. `auto` delegates permission decisions to Claude's classifier where available. `dontAsk` denies calls that still require approval. `manual` and `plan` do not make an unattended run interactive. `--permission-prompts none` explicitly avoids waiting on an external approval host. `bypassPermissions` / `--dangerously-skip-permissions` remove checks and are not a routine offload preset.

`--tools` chooses available built-in tools; `--allowedTools` preapproves matching calls; `--disallowedTools` denies matching tools. MCP configuration is separate. Claude permission modes are separate from OS sandboxing. See [permission controls](https://code.claude.com/docs/en/permissions).

## Resume and fork

Read the full `session_id` from the final envelope or `system/init`. Once the current run has ended, reuse the recorder with a fresh directory and add `--resume "$SESSION_ID"` to the same Claude options. Feed the follow-up via stdin. Reapply the model/effort/standard-speed preset and intended tool/permission configuration; it is part of the new invocation.

`--fork-session --resume "$SESSION_ID"` continues under a new UUID. `--continue` picks the most recent conversation and is ambiguous during concurrent work. `--session-id UUID` names a new conversation; it is not a replacement for `--resume`. `--no-session-persistence` disables later resumption.

The installed `--system-prompt-snapshot on` default may preserve the original system prompt across resume even when new prompt flags are supplied. Put ordinary new task requirements in the follow-up user prompt. If intentionally revising system instructions, inspect that flag's current help and consider a fresh session.

## Native background mode

For work that must survive terminal detachment:

```bash
env CLAUDE_CODE_DISABLE_FAST_MODE=1 CLAUDE_CODE_EFFORT_LEVEL="$EFFORT" \
  claude --bg --model "$MODEL" --effort "$EFFORT" \
  --settings '{"fastMode":false,"ultracode":false}' --name 'bounded-review' --permission-mode dontAsk \
  'Review the requested files. Report findings only; do not edit, commit, push, or open a PR.'
claude agents --json --all
claude logs "$BACKGROUND_ID"
claude stop "$BACKGROUND_ID"
```

Adapt the launch's tools and MCP options to the task. Do not combine `--bg` with `-p` or print-only flags. The short background `id` is for logs/stop/attach; the full `sessionId` is for `--resume`. Filter JSON to the captured ID. Poll `state`: `working`, `blocked`, `done`, `failed`, or `stopped`; `status` and `waitingFor` add detail. Use `--all` to retain completed entries. `idle` process status alone is not completion. Do not parse internal jobs files.

Background sessions may create worktrees and automatically perform commit/push/PR completion flows. Use print mode for tightly bounded local work unless that behavior is intended, and preserve explicit authorization restrictions. `claude rm` can remove a managed worktree; it is not a harmless stop command. See [official background lifecycle](https://code.claude.com/docs/en/agent-view).

## Structured output and limits

`--json-schema` takes a JSON **string**, unlike Codex's schema file flag. Pass it as one argv element. Example:

```bash
env CLAUDE_CODE_DISABLE_FAST_MODE=1 CLAUDE_CODE_EFFORT_LEVEL="$EFFORT" \
  claude -p --model "$MODEL" --effort "$EFFORT" \
  --settings '{"fastMode":false,"ultracode":false}' --output-format json --tools '' \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --permission-mode dontAsk --permission-prompts none \
  --json-schema '{"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"],"additionalProperties":false}' \
  < "$PROMPT_FILE"
```

`--max-turns N` is documented and accepted by the tested parser although absent from its normal help. It caps agent turns, not wall time or shell-command count. `--max-budget-usd AMOUNT` is a print-mode API spending limit, not a universal limit on subscription usage. Choose limits appropriate to the task; arbitrary small caps can truncate useful work. Use the recorder's `--timeout` only for an intentional wall-clock deadline. A cap hit is an incomplete/error result, not a successful answer.

## Configuration and authentication

Ordinary `-p` loads local customization. Tool restrictions do not disable configured startup hooks or plugins. Run only in a trusted target directory. `--strict-mcp-config` with an explicit empty server map suppresses unsolicited MCP servers; supply needed servers explicitly.

- Normal mode: preserves user/project context and existing CLI authentication; preferred for personal offloads.
- `--safe-mode`: disables customizations while preserving normal authentication/permissions. Useful for diagnosis or controlled supplied-context tasks; it also removes skills and project instructions, so it changes the task context.
- `--bare`: a different minimal mode. Local help says Anthropic auth must come from `ANTHROPIC_API_KEY` or an explicit `apiKeyHelper`; OAuth/Keychain are not read. Other providers use their normal credentials. Do not switch a subscription user into this mode accidentally.
- `--setting-sources`: choose user/project/local settings; `--settings` supplies explicit settings. Neither is an all-purpose isolation guarantee.

Use `claude auth status --text` to diagnose auth. A sandboxed status read can fail to access macOS Keychain; distinguish that from a confirmed expired login using the host's approved execution mechanism. Do not log keys/tokens or broadly dump environment variables. Existing API-key environment variables can change which account/provider pays.

Billing is account- and date-dependent. As checked on 2026-09-17, the June 15 update at the top of Anthropic's [subscription SDK article](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) says the announced separate SDK-credit changes were paused: subscription-authenticated SDK/`-p` usage still draws from subscription limits. The older announcement preserved below it is not in effect. API-key usage is billed separately. Check the current update and account usage; a subscription is not unlimited/free automation. Do not change auth methods to conceal or evade limits.

## Troubleshooting and verification

- No progress: inspect stderr, the last complete event, process state, and MCP/hook startup; do not immediately start another run.
- Permission denial: inspect the requested tool/arguments and existing authorization. A successful final envelope can still report denials and incomplete work.
- Expired login: ask the user to reauthenticate, then retry once. Do not loop.
- Unknown option: compare local help/version with the reference. Do not automatically drop a protective flag.
- Nested session error: investigate inherited session markers and intended topology; do not automatically unset `CLAUDECODE` or recurse through the other CLI.
- Interrupted run: inspect partial edits before resume/retry. A killed recorder can leave an unconfirmed state; use the parent job handle and verify ownership before signaling a PID.

After completion inspect modified/untracked files, the relevant tests, and the user's acceptance criteria. Do not report the child's claimed checks as your own independent verification. Keep the captured session ID for follow-up.

## Source map

- [Programmatic usage](https://code.claude.com/docs/en/headless)
- [CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Skills and symlinks](https://code.claude.com/docs/en/skills)
- [Permission controls](https://code.claude.com/docs/en/permissions)
- [Background sessions](https://code.claude.com/docs/en/agent-view)
- Exact exposed flags: [locally captured help](cli-reference.md).

Model selection sources: [model aliases and effort](https://code.claude.com/docs/en/model-config), [disabling Fast mode](https://code.claude.com/docs/en/fast-mode). Verified 2026-09-24.
