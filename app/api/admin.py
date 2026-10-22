from collections import deque
from pathlib import Path
from typing import Literal, Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.api.dependencies import administrator, database, managed_file, public_user, require_csrf
from app.api.invoices import public_invoice
from app.schemas.requests import AdminUserUpdate
from app.security.auth import hash_password
from app.services.pdf_preview import preview_response

router = APIRouter(prefix='/api/admin', dependencies=[Depends(administrator)])
logger = logging.getLogger(__name__)


@router.get('/users')
def users(page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100), db=Depends(database)):
    total = db.execute('SELECT count(*) FROM users').fetchone()[0]
    rows = db.execute('SELECT * FROM users ORDER BY id LIMIT ? OFFSET ?', (page_size, (page - 1) * page_size)).fetchall()
    return {'items': [public_user(dict(row)) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.put('/users/{user_id}', dependencies=[Depends(require_csrf)])
def edit_user(user_id: int, data: AdminUserUpdate, admin=Depends(administrator), db=Depends(database)):
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '用户不存在')
        changes = {key: value for key, value in data.model_dump(exclude_unset=True).items()
                   if key in ('name', 'role', 'is_active') and value is not None}
        merged = {**dict(row), **changes}
        if row['role'] == 'admin' and row['is_active'] and (merged['role'] != 'admin' or not merged['is_active']):
            count = db.execute("SELECT count(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]
            if count <= 1:
                raise HTTPException(409, '至少保留一名启用的管理员')
        if data.new_password:
            changes['password_hash'] = hash_password(data.new_password)
        if data.unbind_feishu:
            changes['feishu_open_id'] = None
        if changes:
            assignments = ', '.join(f'{field} = ?' for field in changes)
            db.execute('UPDATE users SET ' + assignments + ", updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                       list(changes.values()) + [user_id])
    logger.info('User administered: actor_id=%d user_id=%d', admin['id'], user_id)
    return public_user(dict(db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()))


@router.get('/invoices')
def all_invoices(user_id: Optional[int] = Query(default=None, gt=0),
                 page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100), db=Depends(database)):
    where, args = ('WHERE i.user_id = ?', [user_id]) if user_id else ('', [])
    total = db.execute('SELECT count(*) FROM invoices i ' + where, args).fetchone()[0]
    rows = db.execute('SELECT i.*, u.username FROM invoices i JOIN users u ON u.id = i.user_id ' + where +
                      ' ORDER BY i.id DESC LIMIT ? OFFSET ?', args + [page_size, (page - 1) * page_size]).fetchall()
    return {'items': [public_invoice(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.get('/invoices/{invoice_id}/file')
def invoice_file(invoice_id: int, request: Request, db=Depends(database)):
    row = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '发票不存在')
    root = request.app.state.settings.database_path.parent / 'invoices' / str(row['user_id'])
    return FileResponse(managed_file(row['file_path'], root), media_type='application/pdf', filename=f'invoice-{invoice_id}.pdf')


@router.get('/invoices/{invoice_id}/preview')
def invoice_preview(invoice_id: int, request: Request, page: int = Query(default=1, ge=1), db=Depends(database)):
    row = db.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '发票不存在')
    root = request.app.state.settings.database_path.parent / 'invoices' / str(row['user_id'])
    return preview_response(managed_file(row['file_path'], root), page)


@router.get('/logs')
def logs(request: Request, source: Literal['web', 'feishu'] = 'web', limit: int = Query(default=200, ge=1, le=1000)):
    path = request.app.state.settings.database_path.parent / 'logs' / (source + '.log')
    if not path.exists():
        return {'source': source, 'lines': []}
    with path.open(encoding='utf-8', errors='replace') as stream:
        lines = list(deque(stream, maxlen=limit))
    return {'source': source, 'lines': [line.rstrip() for line in lines]}
