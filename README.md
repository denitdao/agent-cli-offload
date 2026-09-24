# Codex Offload and Claude Offload

Two self-contained agent skills for delegating work between Codex and Claude Code.

- **Codex Offload** (`codex-offload`): delegate a bounded task to Codex, inspect progress, and continue its saved conversation.
- **Claude Offload** (`claude-offload`): delegate a bounded task to Claude Code with the same lifecycle interface.

The name identifies the destination. A common workflow is Claude → Codex Offload or Codex → Claude Offload. The calling agent checks the delegated result.

## Install through skills.sh

Install each skill for the agent that will call it: Codex Offload in Claude Code, Claude Offload in Codex.

```bash
npx skills add denitdao/agent-cli-offload --skill codex-offload --agent claude-code --global
npx skills add denitdao/agent-cli-offload --skill claude-offload --agent codex --global
```

List the available skills without installing:

```bash
npx skills add denitdao/agent-cli-offload --list
```

Refresh the agent's skill discovery or start a new session if needed. Invoke `$claude-offload` in Codex or `/codex-offload` in Claude Code, or just ask ("get a second opinion from Codex on Sol"). Codex Offload also works from other agents. Claude Offload refuses to start inside a Claude Code session, because nested Claude sessions misbehave.

## Requirements and defaults

- macOS or Linux and Python 3. macOS is tested; Linux is not yet verified. Windows is unsupported.
- The destination CLI installed on PATH and authenticated through its normal login.
- Access to the chosen destination model (see below).
- Node.js/npm is needed for the `npx skills` installation command, not for the Python helper itself.

### Model selection

Ask for a model by family and the skill uses its newest release: "use Opus" → the latest Claude Opus, "use Sol" → the newest `gpt-<version>-sol` in the installed Codex catalogue. Because lineups change, the agent can run `offload.py models --tool codex|claude` to see what is available. For Codex this is the full installed catalogue. For Claude it uses the Models API when `ANTHROPIC_API_KEY` is set, or the alias list plus an optional one-call `--probe` that shows the exact release an alias selects. Naming a version ("Opus 5.5", "GPT-5.6 Sol") pins that exact model, and effort can be requested the same way. Without a preference, the calling agent picks a model and effort that fit the task; the helper defaults to the flagship families (**Fable** for Claude, **Astra** for Codex) at **high effort**.

Every managed start and continuation uses **standard speed with Fast disabled**, and a continuation stays on the release that answered the first run. Unavailable models are reported rather than replaced with other models. Claude may use auxiliary models internally. Offloads consume the destination account's usage allowance.

No Python packages, permanent service, MCP bridge, or separate process manager installation is required. Each skill folder includes its own helper and references.

## Agent-facing lifecycle

The bundled `scripts/offload.py` exposes JSON commands:

| Operation | Purpose |
|---|---|
| `start` | Launch a bounded task and return an offload ID |
| `status` / `list` | Inspect progress and active/completed work |
| `models` | List available models and families, the newest release per family, and (Codex) supported efforts |
| `read` | Read incremental JSONL events with a byte cursor |
| `wait` | Wait up to 60 seconds for completion or attention |
| `send` | Continue the exact saved session after its current run exits |
| `stop` | Request graceful cancellation, then escalate if necessary |
| `touch` | Renew responsibility for a running task |

The skill teaches the agent how to select permissions, prepare context, invoke these commands, and verify the result. See [Codex Offload](skills/codex-offload/SKILL.md), [Claude Offload](skills/claude-offload/SKILL.md), and the [helper contract](skills/codex-offload/references/helper.md).

### If the calling app disappears

By default, a run stops after **five minutes without a caller lease renewal**, with a separate **thirty-minute maximum per run**. The caller normally checks or waits every 15–60 seconds. These bounds are configurable positive finite values.

An optional stable caller PID allows faster cancellation when that process dies. A temporary worker and watchdog supervise each run and exit with it. The watchdog handles a worker crash. Force-killing both supervisors, permanent OS inspection/signalling failures, or deliberately detached descendants can defeat cleanup.

### Boundaries

- `send` is a saved-session follow-up, not live steering or an automatic queue.
- The caller polls or waits; a skill cannot guarantee native push notifications or integration into the host's subagent list.
- Stop/expiry can leave partial edits. A successful CLI turn does not establish task correctness.
- Codex uses a read-only or workspace-write OS sandbox with unavailable escalations denied. Claude's tool permissions are not an OS filesystem sandbox; Bash needs an explicit grant.
- Advanced direct CLI workflows remain documented, but bypass the managed helper's lease/watchdog guarantees.

## Verification

From the repository root:

```bash
python3 -B -m unittest -v test_offload.py test_lifecycle.py
```

The 71 offline tests use fake CLI executables and temporary directories, never paid models. They cover exact-session continuation, concurrent requests, event cursors, models and permissions, caller leases, owner death, worker crashes, signal escalation, process-group cleanup and installation. On macOS they need ordinary process-inspection permissions; a restrictive host sandbox may block `ps`.

Additional local verification included 20 consecutive worker-crash cleanup trials and real Codex/Claude handoff, recall, interruption and caller-abandonment tests. Claude also used Codex Offload as a real caller to delegate a small code fix, continue the same session and independently verify its tests.

Tested CLI versions: the lifecycle was live-tested with Codex 0.154.0 and Claude Code 2.1.274. Help snapshots and model selection were rechecked against Codex 0.156.1 and Claude Code 2.1.281, including a live `--probe opus`. Parameter references are versioned snapshots; consult current CLI help after upgrades.

## Manual installation

Each folder under `skills/` is portable. The optional `install.py` installs both skills for the current user, previews by default, and preserves backups on explicit updates:

```bash
python3 install.py
python3 install.py --apply
python3 install.py --apply --update
```

The skills.sh installation command is the recommended sharing path. Runtime state stays outside the repository by default in `~/.local/state/agent-offload`.

## License

Original code and skill instructions are MIT licensed. Included CLI help snapshots describe their respective tools and retain any upstream rights.
