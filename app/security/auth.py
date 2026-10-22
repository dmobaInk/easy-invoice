"""Password hashing, persistent session signing key, initial administrator."""

from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import secrets

from app.config import Settings
from app.db.connection import connect


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000)
    return f'pbkdf2_sha256$600000${salt}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != 600000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def auth_tag(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()


def signing_key(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'session.key'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text(encoding='ascii').strip()
        if len(key) != 64 or any(char not in '0123456789abcdef' for char in key):
            raise ValueError('Invalid session signing key file')
        return key
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        key = secrets.token_hex(32)
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return key


def bootstrap_admin(settings: Settings) -> None:
    from app.schemas.requests import Registration

    username, password = settings.bootstrap_admin_username, settings.bootstrap_admin_password
    if not username and not password:
        return
    if not username or not password:
        raise ValueError('INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD must both be configured')
    try:
        data = Registration(username=username, password=password, name='管理员')
    except ValueError:
        raise ValueError('Initial administrator configuration is invalid; check username and password length') from None
    with closing(connect(settings.database_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT role FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            if existing['role'] != 'admin':
                raise ValueError('Initial administrator username already belongs to an ordinary user')
            return
        db.execute('INSERT INTO users(username, password_hash, name, role) VALUES (?, ?, ?, ?)',
                   (data.username, hash_password(data.password), data.name, 'admin'))


class LocalSessionMiddleware:
    """Use the persistent secret created by lifespan, without import-time writes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        from starlette.middleware.sessions import SessionMiddleware
        state = scope['app'].state
        middleware = SessionMiddleware(
            self.app, secret_key=state.signing_key, session_cookie='easy_invoice_session',
            max_age=12 * 3600, same_site='lax', https_only=state.settings.cookie_secure,
        )
        await middleware(scope, receive, send)
