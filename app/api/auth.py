from collections import defaultdict, deque
import logging
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.dependencies import csrf_token, current_user, database, public_user, require_csrf
from app.schemas.requests import Login, PasswordChange, Registration
from app.security.auth import auth_tag, hash_password, verify_password

router = APIRouter(prefix='/api')
logger = logging.getLogger(__name__)
_attempts = defaultdict(deque)
_lock = threading.Lock()


def rate_limit(request, action, limit, seconds):
    key = (action, request.client.host if request.client else 'unknown')
    now = time.monotonic()
    with _lock:
        if len(_attempts) > 10000:
            for old in list(_attempts):
                if not _attempts[old] or _attempts[old][-1] < now - 3600:
                    del _attempts[old]
        queue = _attempts[key]
        while queue and queue[0] <= now - seconds:
            queue.popleft()
        if len(queue) >= limit:
            raise HTTPException(429, '操作过于频繁，请稍后再试')
        queue.append(now)


def establish_session(request, user):
    request.session.clear()
    request.session.update(user_id=user['id'], auth_tag=auth_tag(user['password_hash']),
                           csrf=secrets.token_urlsafe(32))
    return {'user': public_user(user), 'csrf_token': request.session['csrf']}


@router.get('/auth/session')
def session(request: Request, db=Depends(database)):
    token = csrf_token(request)
    try:
        user = current_user(request, db)
    except HTTPException:
        user = None
    return {'user': public_user(user) if user else None, 'csrf_token': token}


@router.post('/auth/register', dependencies=[Depends(require_csrf)], status_code=201)
def register(data: Registration, request: Request, db=Depends(database)):
    rate_limit(request, 'register', 20, 3600)
    password_hash = hash_password(data.password)
    try:
        with db:
            cursor = db.execute('INSERT INTO users(username, password_hash, name) VALUES (?, ?, ?)',
                                (data.username, password_hash, data.name.strip()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, '用户名已存在') from None
    user = dict(db.execute('SELECT * FROM users WHERE id = ?', (cursor.lastrowid,)).fetchone())
    logger.info('User registered: user_id=%d', user['id'])
    return establish_session(request, user)


@router.post('/auth/login', dependencies=[Depends(require_csrf)])
def login(data: Login, request: Request, db=Depends(database)):
    rate_limit(request, 'login', 20, 300)
    row = db.execute('SELECT * FROM users WHERE username = ?', (data.username,)).fetchone()
    # Always perform a password derivation even for unknown accounts.
    encoded = row['password_hash'] if row else 'pbkdf2_sha256$600000$' + '00' * 16 + '$' + '00' * 32
    valid = verify_password(data.password, encoded)
    if not row or not valid or not row['is_active']:
        raise HTTPException(401, '用户名或密码错误，或账号已停用')
    logger.info('User logged in: user_id=%d', row['id'])
    return establish_session(request, dict(row))


@router.post('/auth/logout', dependencies=[Depends(require_csrf)])
def logout(request: Request):
    request.session.clear()
    return {'csrf_token': csrf_token(request)}


@router.get('/users/me')
def me(user=Depends(current_user)):
    return public_user(user)


@router.put('/users/me/password', dependencies=[Depends(require_csrf)])
def change_password(data: PasswordChange, request: Request, user=Depends(current_user), db=Depends(database)):
    if not verify_password(data.old_password, user['password_hash']):
        raise HTTPException(400, '原密码不正确')
    with db:
        db.execute("UPDATE users SET password_hash = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                   (hash_password(data.new_password), user['id']))
    request.session.clear()
    return {'message': '密码已修改，请重新登录', 'csrf_token': csrf_token(request)}


@router.post('/auth/feishu/bind', dependencies=[Depends(require_csrf)])
def bind_instructions(user=Depends(current_user)):
    return {'username': user['username'], 'bound': bool(user['feishu_open_id']),
            'message': '请在飞书机器人单聊中发送完整用户名：' + user['username']}


@router.delete('/auth/feishu/bind', dependencies=[Depends(require_csrf)])
def unbind(user=Depends(current_user), db=Depends(database)):
    with db:
        db.execute("UPDATE users SET feishu_open_id = NULL, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                   (user['id'],))
    logger.info('Feishu unbound: user_id=%d', user['id'])
    return {'message': '已解绑飞书'}
