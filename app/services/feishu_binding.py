"""Bind existing Web accounts atomically; never create accounts from events."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.db.connection import connect


@dataclass(frozen=True)
class BindingResult:
    code: str
    text: str
    user_id: Optional[int] = None


class BindingService:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def bind(self, open_id: str, username: str) -> BindingResult:
        username = username.strip()
        if not open_id.strip() or not username:
            raise ValueError('open_id and username are required')
        with closing(connect(self.database_path)) as db, db:
            # Serialize lookup and update to protect against concurrent bindings.
            db.execute('BEGIN IMMEDIATE')
            user = db.execute(
                'SELECT id, feishu_open_id, is_active FROM users WHERE username = ?',
                (username,),
            ).fetchone()
            if user is None:
                return BindingResult('user_not_found', '用户名不存在，请先在 Web 端注册后再发送用户名。')
            if not user['is_active']:
                return BindingResult('user_disabled', '该账号已停用，无法绑定飞书。')
            if user['feishu_open_id'] is not None:
                if user['feishu_open_id'] == open_id:
                    return BindingResult('already_bound', '该账号已绑定当前飞书，无需重复绑定。', user['id'])
                return BindingResult('account_bound', '该账号已关联其他飞书，请先在 Web 端解绑。')
            existing = db.execute('SELECT id FROM users WHERE feishu_open_id = ?', (open_id,)).fetchone()
            if existing is not None:
                return BindingResult('sender_bound', '当前飞书已关联其他账号，请先在 Web 端解绑。')
            db.execute(
                "UPDATE users SET feishu_open_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                'WHERE id = ? AND feishu_open_id IS NULL', (open_id, user['id']),
            )
            return BindingResult('bound', '绑定成功，可以发送 PDF 发票。', user['id'])

    def find_active_user(self, open_id: str) -> Optional[int]:
        with closing(connect(self.database_path)) as db:
            row = db.execute(
                'SELECT id FROM users WHERE feishu_open_id = ? AND is_active = 1', (open_id,),
            ).fetchone()
            return row['id'] if row is not None else None
