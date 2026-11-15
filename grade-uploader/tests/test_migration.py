import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[1] / 'scripts/migrate_history.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def fixture(root):
    source = root.parent / 'data/history.sqlite3'
    target = root / 'data/history.sqlite3'
    for path, value in [(source, 'latest'), (target, 'snapshot')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('create table history (value text)')
            conn.execute('insert into history values (?)', (value,))
    (root / 'data/migration-pending.json').write_text(json.dumps({'target_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))
    return source, target


def test_one_time_backup_preserves_sources_and_stops_for_running_server(tmp_path):
    root = tmp_path / 'uploader'
    source, target = fixture(root)
    with pytest.raises(RuntimeError, match='close'):
        migration.migrate(root, lambda: True)
    assert migration.migrate(root, lambda: False)
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('select value from history').fetchone()[0] == 'latest'
    assert source.exists()
    assert list((root / 'data').glob('history-before-migration-*.sqlite3'))
    assert migration.migrate(root, lambda: False) is False


def test_does_not_overwrite_modified_new_history(tmp_path):
    root = tmp_path / 'uploader'
    source, target = fixture(root)
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("insert into history values ('new work')")
    with pytest.raises(RuntimeError, match='changed'):
        migration.migrate(root, lambda: False)
    with closing(sqlite3.connect(target)) as db:
        assert db.execute('select count(*) from history').fetchone()[0] == 2
