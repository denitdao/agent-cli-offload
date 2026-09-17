#!/usr/bin/env python3
"""Install both skills for the current user; preview by default. No overwrites."""
import argparse
import datetime
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

NAMES = ('codex-offload', 'claude-offload')


def digest_tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def install(home, apply=False, update=False):
    source_root = Path(__file__).resolve().parent / 'skills'
    canonical_root = home / '.agents' / 'skills'
    claude_root = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(home / '.claude'))) / 'skills'
    actions = []
    for name in NAMES:
        source = source_root / name
        target = canonical_root / name
        link = claude_root / name
        if os.path.lexists(target):
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f'Refusing to replace existing skill: {target}')
            if digest_tree(target) != digest_tree(source):
                if not update:
                    raise ValueError(f'Refusing to replace existing skill without --update: {target}')
                actions.append(('update', source, target))
        else:
            actions.append(('copy', source, target))
        if os.path.lexists(link):
            if not link.is_symlink() or link.resolve() != target.resolve():
                raise ValueError(f'Refusing to replace existing Claude entry: {link}')
        else:
            actions.append(('link', target, link))
    for kind, source, target in actions:
        print(f'{kind}: {source} -> {target}')
    if not apply:
        print('Preview only. Use --apply to install.')
        return
    for kind, source, target in actions:
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind in ('copy', 'update'):
            staging = Path(tempfile.mkdtemp(prefix='.offload-install-', dir=target.parent))
            try:
                shutil.copytree(source, staging / 'skill', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                backup = None
                if kind == 'update':
                    backup_root = home / '.agents' / 'offload-skill-backups'
                    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                    backup = backup_root / (target.name + '-' + stamp)
                    target.rename(backup)
                try:
                    (staging / 'skill').rename(target)
                except OSError:
                    if backup is not None:
                        backup.rename(target)
                    raise
                if backup is not None:
                    print(f'Preserved previous skill: {backup}')
            finally:
                shutil.rmtree(staging)
        else:
            target.symlink_to(source, target_is_directory=True)
    for name in NAMES:
        assert digest_tree(source_root / name) == digest_tree(canonical_root / name)
        assert (claude_root / name).resolve() == (canonical_root / name).resolve()
    print('Verified: both skills installed in ~/.agents/skills and linked for Claude Code.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--update', action='store_true', help='Back up and replace differing skill directories')
    args = parser.parse_args()
    try:
        install(Path.home(), args.apply, args.update)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
