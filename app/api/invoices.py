import json
from datetime import date
from dataclasses import asdict
from decimal import Decimal
import asyncio
from pathlib import Path
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import current_user, database, managed_file, owned, require_csrf
from app.integrations.feishu.files import MAX_PDF_BYTES
from app.schemas.requests import InvoiceStatus, InvoiceTag, InvoiceUpdate
from app.services.storage import InvalidPdfError, LocalStorage
from app.db.repositories.invoices import InvoiceRepository
from app.integrations.aliyun.invoice_ocr import AliyunInvoiceOcr
from app.services.invoice_ingestion import UploadIngestion
from app.services.pdf_preview import preview_response

router = APIRouter(prefix='/api/invoices')


def public_invoice(row):
    return {key: value for key, value in dict(row).items() if key != 'file_path'}


@router.get('/summary')
def summary(user=Depends(current_user), db=Depends(database)):
    rows = db.execute('SELECT status, count(*) AS count, coalesce(sum(round(amount * 100)), 0) / 100.0 AS amount '
                      'FROM invoices WHERE user_id = ? GROUP BY status', (user['id'],)).fetchall()
    return {row['status']: {'count': row['count'], 'amount': row['amount']} for row in rows}


@router.get('/tags')
def tag_choices(user=Depends(current_user), db=Depends(database)):
    rows = db.execute("SELECT t.name, coalesce(usage.n, 0) AS frequency FROM user_invoice_tags t "
                      "LEFT JOIN (SELECT a.name, count(*) n FROM invoice_tag_assignments a "
                      "JOIN invoices i ON i.id = a.invoice_id WHERE i.user_id = ? GROUP BY a.name) usage "
                      "ON usage.name = t.name WHERE t.user_id = ? ORDER BY frequency DESC, t.name",
                      (user['id'], user['id']))
    return {'items': ['未分类', *(row['name'] for row in rows)]}


@router.put('/{invoice_id}/tag', dependencies=[Depends(require_csrf)])
def set_tag(invoice_id: int, data: InvoiceTag, user=Depends(current_user), db=Depends(database)):
    with db:
        db.execute('BEGIN IMMEDIATE')
        owned(db, 'invoices', invoice_id, user['id'])
        db.execute('DELETE FROM invoice_tag_assignments WHERE invoice_id = ?', (invoice_id,))
        db.executemany('INSERT OR IGNORE INTO user_invoice_tags VALUES (?, ?)',
                       [(user['id'], name) for name in data.names])
        db.executemany('INSERT INTO invoice_tag_assignments VALUES (?, ?)',
                       [(invoice_id, name) for name in data.names])
    return {'tags': data.names}


@router.get('')
def list_invoices(title_id: Optional[int] = Query(default=None, gt=0), status: Optional[InvoiceStatus] = None,
                  seller_name: Optional[str] = None, tag: Optional[str] = None,
                  amount_min: Optional[Decimal] = Query(default=None, ge=0),
                  amount_max: Optional[Decimal] = Query(default=None, ge=0),
                  date_from: Optional[date] = None, date_to: Optional[date] = None,
                  page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                  user=Depends(current_user), db=Depends(database)):
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(400, '金额区间无效')
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, '日期区间无效')
    clauses, params = ['i.user_id = ?'], [user['id']]
    for field, operator, value in [('title_id', '=', title_id), ('status', '=', status),
                                    ('amount', '>=', float(amount_min) if amount_min is not None else None),
                                    ('amount', '<=', float(amount_max) if amount_max is not None else None),
                                    ('invoice_date', '>=', date_from.isoformat() if date_from else None),
                                    ('invoice_date', '<=', date_to.isoformat() if date_to else None)]:
        if value is not None:
            clauses.append(f'i.{field} {operator} ?')
            params.append(value)
    if seller_name and seller_name.strip():
        clauses.append("instr(lower(i.seller_name), lower(?)) > 0")
        params.append(seller_name.strip())
    source = ' FROM invoices i '
    if tag and tag.strip():
        condition = ("EXISTS (SELECT 1 FROM invoice_tag_assignments a WHERE a.invoice_id = i.id "
                     "AND instr(lower(a.name), lower(?)) > 0)")
        if tag.strip() in '未分类':
            condition = '(' + condition + ' OR NOT EXISTS (SELECT 1 FROM invoice_tag_assignments a WHERE a.invoice_id = i.id))'
        clauses.append(condition)
        params.append(tag.strip())
    where = ' AND '.join(clauses)
    total = db.execute('SELECT count(*)' + source + 'WHERE ' + where, params).fetchone()[0]
    rows = db.execute("SELECT i.*, t.title_name, d.reimbursed_at, (SELECT json_group_array(name) FROM invoice_tag_assignments WHERE invoice_id = i.id) tags" + source +
                      'LEFT JOIN reimbursement_dates d ON d.invoice_id = i.id LEFT JOIN invoice_titles t '
                      'ON t.id = i.title_id AND t.user_id = i.user_id WHERE ' + where +
                      ' ORDER BY i.created_at DESC, i.id DESC LIMIT ? OFFSET ?',
                      params + [page_size, (page - 1) * page_size]).fetchall()
    return {'items': [{**public_invoice(row), 'tags': json.loads(row['tags'])} for row in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.post('/upload', dependencies=[Depends(require_csrf)], status_code=201)
async def upload(request: Request, file: UploadFile, user=Depends(current_user)):
    try:
        if not (file.filename or '').lower().endswith('.pdf'):
            raise HTTPException(422, '仅支持 PDF 文件')
        binary = await file.read(MAX_PDF_BYTES + 1)
    finally:
        await file.close()
    if len(binary) > MAX_PDF_BYTES:
        raise HTTPException(413, 'PDF 超过 10 MiB')
    root = request.app.state.settings.database_path.parent
    try:
        storage = LocalStorage(root / 'invoices')
        path = await asyncio.to_thread(storage.save_invoice, user['id'], 'web_' + uuid.uuid4().hex, binary)
    except InvalidPdfError:
        raise HTTPException(422, 'PDF 无效、已加密或无法读取') from None
    try:
        settings = request.app.state.settings
        processor = UploadIngestion(InvoiceRepository(settings.database_path), AliyunInvoiceOcr(settings))
        saved = await asyncio.to_thread(processor, user['id'], path)
        if saved.code == 'duplicate_same_user':
            raise HTTPException(409, '该发票已存在，未重复收录')
        if saved.code == 'duplicate_other_user':
            raise HTTPException(409, '该发票已被其他用户收录，未重复入库')
        if saved.code == 'account_disabled':
            raise HTTPException(403, '账号已停用，无法收录发票')
        return public_invoice(asdict(saved.invoice))
    except Exception:
        path.unlink(missing_ok=True)
        raise


@router.get('/{invoice_id}')
def detail(invoice_id: int, user=Depends(current_user), db=Depends(database)):
    return public_invoice(owned(db, 'invoices', invoice_id, user['id']))


@router.get('/{invoice_id}/file')
def original(invoice_id: int, request: Request, user=Depends(current_user), db=Depends(database)):
    row = owned(db, 'invoices', invoice_id, user['id'])
    root = request.app.state.settings.database_path.parent / 'invoices' / str(user['id'])
    path = managed_file(row['file_path'], root)
    return FileResponse(path, media_type='application/pdf', filename=f'invoice-{invoice_id}.pdf')


@router.get('/{invoice_id}/preview')
def preview(invoice_id: int, request: Request, page: int = Query(default=1, ge=1),
            user=Depends(current_user), db=Depends(database)):
    row = owned(db, 'invoices', invoice_id, user['id'])
    root = request.app.state.settings.database_path.parent / 'invoices' / str(user['id'])
    return preview_response(managed_file(row['file_path'], root), page)


@router.put('/{invoice_id}', dependencies=[Depends(require_csrf)])
def update(invoice_id: int, data: InvoiceUpdate, user=Depends(current_user), db=Depends(database)):
    changes = data.model_dump(exclude_unset=True)
    if changes.get('status', 'active') is None:
        raise HTTPException(422, '状态不能为空')
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = owned(db, 'invoices', invoice_id, user['id'])
        for field in ('buyer_name', 'seller_name', 'invoice_type'):
            if field in changes and changes[field] is not None:
                changes[field] = changes[field].strip() or None
        merged = {**row, **changes}
        if merged['status'] in ('active', 'reimbursed'):
            required = ('invoice_number', 'invoice_date', 'buyer_name', 'seller_name', 'invoice_type', 'amount')
            if any(merged[field] is None or merged[field] == '' for field in required):
                raise HTTPException(422, '请补全发票号码、日期、买卖方、类型和金额后再确认或报销')
        if merged['invoice_number']:
            duplicate = db.execute('SELECT user_id FROM invoices WHERE invoice_code IS ? AND invoice_number = ? AND id != ?',
                                   (merged['invoice_code'], merged['invoice_number'], invoice_id)).fetchone()
            if duplicate:
                raise HTTPException(409, '该发票已存在' if duplicate['user_id'] == user['id'] else '该发票已被其他用户收录')
        if 'title_id' in changes and changes['title_id'] is not None:
            owned(db, 'invoice_titles', changes['title_id'], user['id'])
        elif 'title_id' not in changes and 'buyer_name' in changes:
            if changes['buyer_name']:
                db.execute('INSERT INTO invoice_titles(user_id, title_name) VALUES (?, ?) '
                           'ON CONFLICT(user_id, title_name) DO NOTHING', (user['id'], changes['buyer_name']))
                changes['title_id'] = db.execute('SELECT id FROM invoice_titles WHERE user_id = ? AND title_name = ?',
                                                (user['id'], changes['buyer_name'])).fetchone()[0]
            else:
                changes['title_id'] = None
        if not changes:
            return public_invoice(row)
        if isinstance(changes.get('invoice_date'), date):
            changes['invoice_date'] = changes['invoice_date'].isoformat()
        if changes.get('amount') is not None:
            changes['amount'] = float(changes['amount'])
        if merged['status'] in ('active', 'reimbursed'):
            changes['ocr_error'] = None
        assignments = ', '.join(f'{field} = ?' for field in changes)
        db.execute('UPDATE invoices SET ' + assignments + ", updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                   'WHERE id = ? AND user_id = ?', list(changes.values()) + [invoice_id, user['id']])
    return public_invoice(owned(db, 'invoices', invoice_id, user['id']))


@router.delete('/{invoice_id}', dependencies=[Depends(require_csrf)])
def delete(invoice_id: int, user=Depends(current_user), db=Depends(database)):
    with db:
        db.execute('BEGIN IMMEDIATE')
        owned(db, 'invoices', invoice_id, user['id'])
        db.execute('DELETE FROM invoices WHERE id = ? AND user_id = ?', (invoice_id, user['id']))
    return {'message': '发票已删除'}
