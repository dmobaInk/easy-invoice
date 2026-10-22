import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.api.dependencies import current_user, database, managed_file, owned, require_csrf
from app.schemas.requests import PrintGenerate
from app.services.pdf_printing import PdfGenerationError, generate_a4
from app.services.reimbursement import cents

router = APIRouter(prefix='/api/print')
logger = logging.getLogger(__name__)


@router.post('/generate', dependencies=[Depends(require_csrf)], status_code=201)
def generate(data: PrintGenerate, request: Request, user=Depends(current_user), db=Depends(database)):
    plan = owned(db, 'reimbursement_plans', data.reimbursement_plan_id, user['id'])
    selected = [owned(db, 'invoices', key, user['id']) for key in data.invoice_ids]
    if any(row['title_id'] != plan['title_id'] or row['status'] != 'active' for row in selected):
        raise HTTPException(409, '所选发票已变更、已报销或不属于该抬头，请刷新后重试')
    root = request.app.state.settings.database_path.parent
    paths = [managed_file(row['file_path'], root / 'invoices' / str(user['id'])) for row in selected]
    target = root / 'prints' / str(user['id']) / (uuid.uuid4().hex + '.pdf')
    try:
        generate_a4(paths, target)
    except PdfGenerationError as exc:
        raise HTTPException(422, str(exc)) from None
    total = sum(cents(row['amount']) for row in selected) / 100
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            # Rendering does not hold the DB write lock; verify input snapshots again.
            current_plan = owned(db, 'reimbursement_plans', data.reimbursement_plan_id, user['id'])
            if current_plan != plan:
                raise HTTPException(409, '报销方案已变更，请重新推荐')
            current = [owned(db, 'invoices', key, user['id']) for key in data.invoice_ids]
            if current != selected:
                raise HTTPException(409, '生成期间发票已被修改，请刷新后重试')
            # Save an immutable export selection, including any checkbox changes.
            snapshot = db.execute('INSERT INTO reimbursement_plans(user_id, title_id, target_amount, selected_invoice_ids, '
                                  'total_amount, allow_exceed) VALUES (?, ?, ?, ?, ?, ?)',
                                  (user['id'], plan['title_id'], plan['target_amount'], json.dumps(data.invoice_ids),
                                   total, plan['allow_exceed']))
            cursor = db.execute('INSERT INTO print_jobs(user_id, reimbursement_plan_id, pdf_path, auto_reimburse) VALUES (?, ?, ?, ?)',
                                (user['id'], snapshot.lastrowid, str(target), int(data.auto_reimburse)))
            if data.auto_reimburse:
                placeholders = ','.join('?' for _ in data.invoice_ids)
                db.execute("UPDATE invoices SET status = 'reimbursed', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                           f'WHERE user_id = ? AND id IN ({placeholders})', [user['id']] + data.invoice_ids)
        logger.info('Print generated: user_id=%d job_id=%d', user['id'], cursor.lastrowid)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {'id': cursor.lastrowid, 'download_url': f'/api/print/{cursor.lastrowid}/download',
            'total_amount': total, 'invoice_count': len(selected), 'auto_reimburse': data.auto_reimburse}


@router.get('')
def jobs(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
         user=Depends(current_user), db=Depends(database)):
    total = db.execute('SELECT count(*) FROM print_jobs WHERE user_id = ?', (user['id'],)).fetchone()[0]
    rows = db.execute('SELECT j.id, j.created_at, j.auto_reimburse, j.reimbursement_plan_id, p.total_amount, '
                      'p.selected_invoice_ids, t.title_name FROM print_jobs j JOIN reimbursement_plans p '
                      'ON p.id = j.reimbursement_plan_id AND p.user_id = j.user_id '
                      'JOIN invoice_titles t ON t.id = p.title_id AND t.user_id = p.user_id '
                      'WHERE j.user_id = ? ORDER BY j.id DESC LIMIT ? OFFSET ?',
                      (user['id'], page_size, (page - 1) * page_size)).fetchall()
    return {'items': [{**dict(row), 'invoice_count': len(json.loads(row['selected_invoice_ids'])),
                       'download_url': f"/api/print/{row['id']}/download"} for row in rows],
            'total': total, 'page': page, 'page_size': page_size}


@router.get('/{job_id}/download')
def download(job_id: int, request: Request, user=Depends(current_user), db=Depends(database)):
    row = owned(db, 'print_jobs', job_id, user['id'])
    path = managed_file(row['pdf_path'], request.app.state.settings.database_path.parent / 'prints' / str(user['id']))
    return FileResponse(path, media_type='application/pdf', filename=f'invoices-a4-{job_id}.pdf')
