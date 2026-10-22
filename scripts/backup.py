"""Complete offline snapshot. The caller must stop Web and standalone listeners."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3

DIRECTORIES = ('invoices', 'prints', 'feishu-events', 'logs')


class BackupError(Exception):
    pass


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def readonly_database(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def validate_database(db):
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise BackupError('数据库完整性检查失败')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise BackupError('数据库外键检查失败')
    if db.execute('PRAGMA user_version').fetchone()[0] not in (1, 2):
        raise BackupError('不支持该数据库版本')
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {'users', 'invoice_titles', 'invoices', 'reimbursement_plans', 'print_jobs'}.issubset(tables):
        raise BackupError('数据库缺少必要业务表')


def stored_relative(value, category, user_id, source_root):
    path = Path(value)
    if not path.is_absolute():
        raise BackupError('数据库中存在非绝对存储路径，请先修正后备份')
    root = source_root / category / str(user_id)
    if not path.resolve().is_relative_to(root.resolve()):
        raise BackupError('数据库中存在用户目录以外的文件路径')
    return path.resolve().relative_to(source_root.resolve())


def copy_private(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def checked_file(root, relative):
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or any(part in ('..', '.') for part in pure.parts):
        raise BackupError('备份清单中存在无效路径')
    path = root.joinpath(*pure.parts)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise BackupError('备份文件不存在或超出备份目录')
    current = path
    while current != root:
        if current.is_symlink():
            raise BackupError('备份不支持符号链接')
        current = current.parent
    return path


def create_backup(database: Path, destination: Path, *, service_stopped=False) -> Path:
    if not service_stopped:
        raise BackupError('请先停止 Web 和飞书接收进程，再指定 --service-stopped')
    database, destination = database.resolve(), destination.absolute()
    if not database.is_file():
        raise BackupError('数据库不存在；不会自动创建空数据库')
    source_root = database.parent
    if destination.resolve().is_relative_to(source_root):
        raise BackupError('备份目录必须位于运行数据目录之外')
    if destination.exists():
        raise BackupError('备份目标已存在，拒绝覆盖')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700)
    try:
        target_db = destination / 'database.sqlite3'
        with closing(readonly_database(database)) as source, closing(sqlite3.connect(target_db)) as target:
            source.backup(target)
            validate_database(target)
            # Package a self-contained database, independent of WAL/SHM sidecars.
            if target.execute('PRAGMA journal_mode = DELETE').fetchone()[0] != 'delete':
                raise BackupError('无法将备份数据库转换为独立快照')
            for table, column, category in (('invoices', 'file_path', 'invoices'), ('print_jobs', 'pdf_path', 'prints')):
                for user_id, value in target.execute(f'SELECT user_id, {column} FROM {table}'):
                    relative = stored_relative(value, category, user_id, source_root)
                    if not (source_root / relative).is_file():
                        raise BackupError('数据库引用的原件或打印文件缺失')
        target_db.chmod(0o600)
        # A read-only SQLite connection may leave an unused shared-memory file.
        # DELETE mode above guarantees that neither auxiliary file is required.
        for suffix in ('-wal', '-shm'):
            Path(str(target_db) + suffix).unlink(missing_ok=True)
        for directory in DIRECTORIES:
            source_dir = source_root / directory
            if source_dir.is_symlink():
                raise BackupError('运行数据中存在符号链接，不执行备份')
            if not source_dir.exists():
                continue
            for path in sorted(source_dir.rglob('*')):
                if path.is_symlink():
                    raise BackupError('运行数据中存在符号链接，不执行备份')
                if path.is_file():
                    relative = path.relative_to(source_root)
                    copy_private(path, destination / 'files' / relative)
        key = source_root / 'session.key'
        if key.exists():
            copy_private(checked_file(source_root, 'session.key'), destination / 'files' / 'session.key')
        files = {}
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                files[path.relative_to(destination).as_posix()] = {'sha256': checksum(path), 'size': path.stat().st_size}
        manifest = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'source_data_directory': str(source_root), 'files': files}
        manifest_path = destination / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest_path.chmod(0o600)
        return destination
    except BaseException:
        # This directory was created exclusively above; no pre-existing data is removed.
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description='停服后备份数据库、PDF、事件收据及会话密钥，不复制 .env')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='必须是不存在的新目录，且位于运行数据目录之外')
    parser.add_argument('--service-stopped', action='store_true', help='确认已停止全部 Web 与飞书进程')
    args = parser.parse_args()
    try:
        create_backup(args.database, args.output, service_stopped=args.service_stopped)
    except (BackupError, OSError, sqlite3.Error):
        parser.exit(1, '备份失败：请检查已停服、源文件完整、目标不存在且权限正确。\n')
    print('备份完成，清单和文件校验值已保存；.env 未包含在备份中。')


if __name__ == '__main__':
    main()
