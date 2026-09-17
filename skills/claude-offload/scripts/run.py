#!/usr/bin/env python3
"""Capture a foreground CLI run, or inspect its durable progress. Python 3 stdlib."""
import argparse
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def inspect(directory):
    record = json.loads((directory / 'run.json').read_text())
    result = dict(record)
    result.update(event_count=0, malformed_lines=0, session_id=None,
                  terminal_event=None, agent_success=None, permission_denials=[])
    events = directory / 'events.jsonl'
    if events.exists():
        with events.open(errors='replace') as stream:
            for line in stream:
                # A writer may still be finishing the last line.
                if not line.endswith('\n'):
                    result['partial_tail'] = True
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    result['malformed_lines'] += 1
                    continue
                if not isinstance(event, dict):
                    result['malformed_lines'] += 1
                    continue
                result['event_count'] += 1
                kind = event.get('type')
                result['last_event'] = kind
                if record['tool'] == 'codex':
                    if kind == 'thread.started':
                        result['session_id'] = event.get('thread_id')
                    elif kind == 'turn.started':
                        result['terminal_event'] = None
                        result['agent_success'] = None
                    elif kind in ('turn.completed', 'turn.failed'):
                        result['terminal_event'] = kind
                        result['agent_success'] = kind == 'turn.completed'
                    if kind == 'error' or event.get('error'):
                        result['last_error'] = event.get('error', event.get('message'))
                    item = event.get('item') or {}
                    if isinstance(item, dict):
                        if item.get('type') == 'agent_message':
                            result['last_message'] = str(item.get('text', ''))[-1600:]
                        if item.get('type') == 'command_execution':
                            result['last_command'] = item.get('command')
                            result['last_command_status'] = item.get('status')
                else:
                    if event.get('parent_tool_use_id'):
                        continue
                    if kind == 'system' and event.get('subtype') == 'init':
                        result['session_id'] = event.get('session_id')
                    if kind == 'result':
                        result['session_id'] = event.get('session_id', result['session_id'])
                        result['terminal_event'] = event.get('subtype', 'result')
                        result['agent_success'] = (event.get('is_error') is False
                                                   and event.get('subtype') == 'success')
                        result['permission_denials'] = event.get('permission_denials', [])
                        result['last_message'] = str(event.get('result', ''))[-1600:]
                        for key in ('errors', 'total_cost_usd', 'num_turns', 'structured_output'):
                            if key in event:
                                result[key] = event[key]
        result['log_bytes'] = events.stat().st_size
        result['log_updated_at'] = datetime.datetime.fromtimestamp(
            events.stat().st_mtime, datetime.timezone.utc).isoformat()
    if record['phase'] == 'running':
        try:
            os.kill(record['runner_pid'], 0)
            result['runner_pid_exists'] = True
        except ProcessLookupError:
            result['runner_pid_exists'] = False
            result['phase'] = 'interrupted_without_exit_record'
        except PermissionError:
            result['runner_pid_exists'] = None
    # A successful model turn does not prove that the requested work succeeded.
    result['transport_success'] = (record.get('exit_code') == 0
                                   and result['agent_success'] is True
                                   and not result.get('partial_tail')
                                   and result['malformed_lines'] == 0)
    return result


def run(args):
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise ValueError('Supply a CLI command after --')
    cwd = Path(args.cwd).expanduser().resolve(strict=True)
    prompt = Path(args.prompt_file).expanduser().resolve(strict=True)
    directory = Path(args.run_dir).expanduser().absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    record = dict(tool=args.tool, phase='starting', started_at=now(), cwd=str(cwd),
                  runner_pid=os.getpid(), command=command)
    save(directory / 'run.json', record)
    child = None
    reason = None
    stop_at = None

    def stop(signum, _frame):
        nonlocal reason, stop_at
        if reason is None:
            reason, stop_at = 'signal:' + str(signum), time.monotonic()
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    code = 1
    try:
        with prompt.open('rb') as source, (directory / 'events.jsonl').open('wb') as out, \
                (directory / 'stderr.log').open('wb') as err:
            child = subprocess.Popen(command, cwd=cwd, stdin=source, stdout=out,
                                     stderr=err, start_new_session=True)
            record.update(phase='running', child_pid=child.pid)
            save(directory / 'run.json', record)
            began = time.monotonic()
            while child.poll() is None:
                if args.timeout and time.monotonic() - began >= args.timeout and reason is None:
                    stop(signal.SIGTERM, None)
                    reason = 'timeout'
                if stop_at is not None and time.monotonic() - stop_at >= 5:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                time.sleep(0.1)
            raw = child.wait()
            code = raw if raw >= 0 else 128 - raw
            if reason == 'timeout':
                code = 124
            elif reason and reason.startswith('signal:'):
                code = 128 + int(reason.split(':')[1])
    except OSError as error:
        record['launch_error'] = str(error)
        code = 127
    finally:
        record.update(phase='exited', exit_code=code, ended_at=now(), stop_reason=reason)
        save(directory / 'run.json', record)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps(inspect(directory), indent=2))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    start = commands.add_parser('run', help='Run in the foreground; use the host job handle to wait')
    start.add_argument('--tool', choices=('codex', 'claude'), required=True)
    start.add_argument('--cwd', required=True)
    start.add_argument('--prompt-file', required=True)
    start.add_argument('--run-dir', required=True, help='A new directory, never an existing run')
    start.add_argument('--timeout', type=float, default=0, help='Seconds; 0 means no deadline')
    start.add_argument('command', nargs=argparse.REMAINDER)
    status = commands.add_parser('status', help='Read status without launching or resuming a model')
    status.add_argument('run_dir')
    args = parser.parse_args()
    try:
        if args.action == 'status':
            print(json.dumps(inspect(Path(args.run_dir).expanduser()), indent=2))
            return 0
        if args.timeout < 0:
            parser.error('--timeout must be nonnegative')
        return run(args)
    except (OSError, ValueError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    sys.exit(main())
