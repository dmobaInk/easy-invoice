from contextlib import closing
import hmac
from pathlib import Path
import secrets

from fastapi import Depends, HTTPException, Request

from app.db.connection import connect
from app.security.auth import auth_tag


def database(request: Request):
    with closing(connect(request.app.state.settings.database_path)) as db:
        yield db


def csrf_token(request: Request) -> str:
    if 'csrf' not in request.session:
        request.session['csrf'] = secrets.token_urlsafe(32)
    return request.session['csrf']


def require_csrf(request: Request):
    expected = request.session.get('csrf')
    received = request.headers.get('X-CSRF-Token', '')
    if not isinstance(expected, str) or not hmac.compare_digest(expected, received):
        raise HTTPException(403, '请求验证失败，请刷新页面后重试')


def current_user(request: Request, db=Depends(database)):
    user_id = request.session.get('user_id')
    row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone() if type(user_id) is int else None
    if row is None or not row['is_active'] or request.session.get('auth_tag') != auth_tag(row['password_hash']):
        raise HTTPException(401, '请先登录')
    return dict(row)


def administrator(user=Depends(current_user)):
    if user['role'] != 'admin':
        raise HTTPException(403, '需要管理员权限')
    return user


def public_user(user):
    return {key: user[key] for key in ('id', 'username', 'name', 'role', 'is_active', 'feishu_open_id', 'created_at')}


def owned(db, table: str, record_id: int, user_id: int):
    # table is exclusively a constant supplied by application code.
    row = db.execute(f'SELECT * FROM {table} WHERE id = ? AND user_id = ?', (record_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, '记录不存在')
    return dict(row)


def managed_file(path: str, root: Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise HTTPException(404, '文件不存在')
    return resolved
