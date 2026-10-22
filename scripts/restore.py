"""Restore only into a new directory; rewrite managed absolute file paths."""

import argparse
from contextlib import closing
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3

from scripts.backup import (BackupError, DIRECTORIES, checked_file, checksum,
                            copy_private, readonly_database, stored_relative, validate_database)


def restore_backup(snapshot: Path, destination: Path, *, service_stopped=False) -> Path:
    if not service_stopped:
        raise BackupError('请先停止全部服务，再指定 --service-stopped')
    snapshot, destination = snapshot.resolve(), destination.absolute()
    if destination.exists():
        raise BackupError('恢复目标已存在，拒绝覆盖')
    if destination.resolve().is_relative_to(snapshot) or snapshot.is_relative_to(destination.resolve()):
        raise BackupError('恢复目录和备份目录不能互相包含')
    manifest_path = checked_file(snapshot, 'manifest.json')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['format_version'] != 1 or not isinstance(manifest['files'], dict):
            raise ValueError
        source_root = Path(manifest['source_data_directory'])
        if not source_root.is_absolute():
            raise ValueError
        entries = manifest['files']
        if 'database.sqlite3' not in entries:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise BackupError('备份清单格式无效') from None
    for relative, metadata in entries.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise BackupError('备份清单内容无效')
        parts = PurePosixPath(relative).parts
        if relative != 'database.sqlite3' and not (
            len(parts) >= 2 and parts[0] == 'files' and
            (parts[1] in DIRECTORIES or (len(parts) == 2 and parts[1] == 'session.key'))
        ):
            raise BackupError('备份清单包含不支持的文件')
        path = checked_file(snapshot, relative)
        if metadata.get('size') != path.stat().st_size or metadata.get('sha256') != checksum(path):
            raise BackupError('备份文件校验失败，未执行恢复')
    with closing(readonly_database(snapshot / 'database.sqlite3')) as db:
        validate_database(db)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700)
    try:
        database = destination / 'app.db'
        copy_private(snapshot / 'database.sqlite3', database)
        for relative in entries:
            if relative.startswith('files/'):
                target_relative = PurePosixPath(relative).relative_to('files')
                copy_private(checked_file(snapshot, relative), destination / target_relative)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('PRAGMA foreign_keys = ON')
            for table, column, category in (('invoices', 'file_path', 'invoices'), ('print_jobs', 'pdf_path', 'prints')):
                rows = db.execute(f'SELECT id, user_id, {column} FROM {table}').fetchall()
                for record_id, user_id, value in rows:
                    relative = stored_relative(value, category, user_id, source_root)
                    target = destination / relative
                    if not target.is_file():
                        raise BackupError('恢复时发现数据库引用文件未包含在备份中')
                    db.execute(f'UPDATE {table} SET {column} = ? WHERE id = ?', (str(target.resolve()), record_id))
            validate_database(db)
        return database
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description='停服后恢复至不存在的新目录，数据库文件名为 app.db')
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service-stopped', action='store_true')
    args = parser.parse_args()
    try:
        restore_backup(args.snapshot, args.output, service_stopped=args.service_stopped)
    except (BackupError, OSError, sqlite3.Error):
        parser.exit(1, '恢复失败：请检查已停服、备份完整且目标目录不存在；不会覆盖已有目录。\n')
    print('恢复完成，请将 DATABASE_PATH 指向恢复目录下的 app.db，再按部署说明启动服务。')


if __name__ == '__main__':
    main()
