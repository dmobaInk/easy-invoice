import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import current_user, database, owned, require_csrf
from app.schemas.requests import TitleInput

router = APIRouter(prefix='/api/titles')


@router.get('')
def titles(user=Depends(current_user), db=Depends(database)):
    rows = db.execute(
        "SELECT t.*, count(i.id) AS active_count, coalesce(sum(round(i.amount * 100)), 0) / 100.0 AS active_amount "
        "FROM invoice_titles t LEFT JOIN invoices i ON i.title_id = t.id AND i.user_id = t.user_id AND i.status = 'active' "
        'WHERE t.user_id = ? GROUP BY t.id ORDER BY t.id DESC', (user['id'],),
    ).fetchall()
    return {'items': [dict(row) for row in rows]}


@router.post('', dependencies=[Depends(require_csrf)], status_code=201)
def create_title(data: TitleInput, user=Depends(current_user), db=Depends(database)):
    try:
        with db:
            cursor = db.execute('INSERT INTO invoice_titles(user_id, title_name, tax_number) VALUES (?, ?, ?)',
                                (user['id'], data.title_name, data.tax_number))
    except sqlite3.IntegrityError:
        raise HTTPException(409, '该抬头已存在') from None
    return owned(db, 'invoice_titles', cursor.lastrowid, user['id'])


@router.put('/{title_id}', dependencies=[Depends(require_csrf)])
def update_title(title_id: int, data: TitleInput, user=Depends(current_user), db=Depends(database)):
    owned(db, 'invoice_titles', title_id, user['id'])
    try:
        with db:
            db.execute('UPDATE invoice_titles SET title_name = ?, tax_number = ? WHERE id = ? AND user_id = ?',
                       (data.title_name, data.tax_number, title_id, user['id']))
    except sqlite3.IntegrityError:
        raise HTTPException(409, '该抬头已存在') from None
    return owned(db, 'invoice_titles', title_id, user['id'])


@router.delete('/{title_id}', dependencies=[Depends(require_csrf)])
def delete_title(title_id: int, user=Depends(current_user), db=Depends(database)):
    owned(db, 'invoice_titles', title_id, user['id'])
    try:
        with db:
            db.execute('DELETE FROM invoice_titles WHERE id = ? AND user_id = ?', (title_id, user['id']))
    except sqlite3.IntegrityError:
        raise HTTPException(409, '该抬头仍关联发票或报销方案，暂不能删除') from None
    return {'message': '抬头已删除'}
