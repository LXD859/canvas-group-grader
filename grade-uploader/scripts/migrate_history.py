"""Finish the one-time relocation of a history database that was open during a move."""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
from pathlib import Path
from uuid import uuid4


def port_in_use():
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', 8000)) == 0


def migrate(root, is_running=port_in_use):
    root = Path(root).resolve()
    marker = root / 'data/migration-pending.json'
    if not marker.is_file():
        return False
    if is_running():
        raise RuntimeError('Please close the old uploader on port 8000, then start again. The original history is preserved.')
    source = root.parent / 'data/history.sqlite3'
    target = root / 'data/history.sqlite3'
    if not source.is_file() or not target.is_file():
        raise RuntimeError('A migration history file is missing. Both original and new history must be retained.')
    expected = json.loads(marker.read_text(encoding='utf-8'))['target_sha256']
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise RuntimeError('New history has changed since relocation. Automatic overwrite stopped; preserve both databases for reconciliation.')
    suffix = uuid4().hex
    staging = root / 'data' / ('history-migrating-' + suffix + '.sqlite3')
    reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    writer = sqlite3.connect(staging)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    shutil.copy2(target, root / 'data' / ('history-before-migration-' + suffix + '.sqlite3'))
    os.replace(staging, target)
    marker.rename(root / 'data' / ('migration-completed-' + suffix + '.json'))
    return True


if __name__ == '__main__':
    try:
        changed = migrate(Path(__file__).resolve().parents[1])
        if changed:
            print('Latest legacy history copied successfully. Original and pre-migration snapshots are retained.')
    except (RuntimeError, OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(str(exc)) from None
