import json
from decimal import Decimal

from fastapi.responses import StreamingResponse
from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import current_user, database, owned, require_csrf
from app.schemas.requests import Optimize
from app.services.reimbursement import OptimizationLimitError, cents, match_events

router = APIRouter(prefix='/api/reimbursements')


@router.get('/fixed-options')
def fixed_options(title_id: int, user=Depends(current_user), db=Depends(database)):
    owned(db, 'invoice_titles', title_id, user['id'])
    rows = db.execute("SELECT i.id, i.invoice_number, i.buyer_name, i.seller_name, i.invoice_date, i.amount, r.remarks, "
                      "(SELECT json_group_array(name) FROM invoice_tag_assignments WHERE invoice_id = i.id) tags "
                      "FROM invoices i LEFT JOIN invoice_remarks r ON r.invoice_id = i.id "
                      "WHERE i.user_id = ? AND i.title_id = ? AND i.status = 'active' "
                      "ORDER BY i.invoice_date DESC, i.id DESC", (user['id'], title_id))
    return {'items': [{**dict(row), 'tags': json.loads(row['tags'])} for row in rows]}


@router.post('/optimize', dependencies=[Depends(require_csrf)])
def recommend(data: Optimize, stream: bool = False, user=Depends(current_user), db=Depends(database)):
    owned(db, 'invoice_titles', data.title_id, user['id'])
    rows = [dict(row) for row in db.execute("SELECT * FROM invoices WHERE user_id = ? AND title_id = ? AND status = 'active'",
                                          (user['id'], data.title_id)).fetchall()]
    fixed = set(data.fixed_invoice_ids)
    fixed_rows = [row for row in rows if row['id'] in fixed]
    if len(fixed_rows) != len(data.fixed_invoice_ids):
        raise HTTPException(409, '固定发票已变更或选择重复，请重新选择')
    base = sum(cents(row['amount']) for row in fixed_rows)
    remaining = cents(data.target_amount) - base
    if remaining < 0 and not data.allow_exceed:
        raise HTTPException(422, '固定发票合计已超过目标，请调整选择、提高目标或允许超额')

    def matches():
        if remaining <= 0:
            yield {'ids': sorted(fixed)}
            return
        optional = [row for row in rows if row['id'] not in fixed and (not fixed or cents(row['amount']) > 0)]
        for event in match_events(optional, Decimal(remaining) / 100, data.allow_exceed):
            if 'ids' in event:
                yield {'ids': sorted(fixed.union(event['ids']))}
            else:
                yield {**event, 'best_cents': base + event['best_cents']}

    if stream:
        def events():
            try:
                for event in matches():
                    if 'ids' in event:
                        event = {'result': save_result(db, user, data, rows, event['ids'])}
                    yield json.dumps(event) + '\n'
            except OptimizationLimitError as exc:
                yield json.dumps({'error': str(exc)}) + '\n'
        return StreamingResponse(events(), media_type='application/x-ndjson',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
    try:
        ids = next(event['ids'] for event in matches() if 'ids' in event)
    except OptimizationLimitError as exc:
        raise HTTPException(422, str(exc)) from None
    return save_result(db, user, data, rows, ids)


def save_result(db, user, data, rows, ids):
    indexed = {row['id']: row for row in rows}
    selected = [indexed[key] for key in ids]
    total = sum(cents(row['amount']) for row in selected) / 100
    difference = (sum(cents(row['amount']) for row in selected) - cents(data.target_amount)) / 100
    message = None
    if difference < 0:
        available = sum(cents(row['amount']) for row in rows) / 100
        if not rows:
            message = '该抬头下暂无可用发票，请先上传发票，或将已确认的发票设为可用后重试。'
        elif available < float(data.target_amount):
            message = (f'当前抬头下的可用发票合计 ¥{available:,.2f}，不足目标金额 '
                       f'¥{float(data.target_amount):,.2f}。已列出当前可用清单，仍差 ¥{-difference:,.2f}，'
                       '请补充发票或调整目标金额后重试。')
        else:
            message = (f'现有发票无法恰好组成目标金额，且尚未允许超额。已列出不超过目标的最接近清单，'
                       f'合计 ¥{total:,.2f}，仍差 ¥{-difference:,.2f}。'
                       '可勾选“允许超过目标金额，选择最接近的组合”后重新推荐。')
    if not selected:
        return {'id': None, 'title_id': data.title_id, 'allow_exceed': data.allow_exceed, 'fixed_invoice_ids': data.fixed_invoice_ids,
                'invoices': [], 'total_amount': 0, 'target_amount': float(data.target_amount),
                'difference': difference, 'target_met': False, 'message': message}
    with db:
        cursor = db.execute('INSERT INTO reimbursement_plans(user_id, title_id, target_amount, selected_invoice_ids, '
                            'total_amount, allow_exceed) VALUES (?, ?, ?, ?, ?, ?)',
                            (user['id'], data.title_id, float(data.target_amount), json.dumps(ids), total, int(data.allow_exceed)))
    return {'id': cursor.lastrowid, 'title_id': data.title_id, 'target_amount': float(data.target_amount),
            'allow_exceed': data.allow_exceed, 'fixed_invoice_ids': data.fixed_invoice_ids, 'total_amount': total,
            'difference': difference, 'target_met': difference >= 0, 'message': message,
            'invoices': [{key: row[key] for key in ('id', 'invoice_number', 'amount', 'invoice_date', 'buyer_name')} for row in selected]}


@router.get('')
def history(user=Depends(current_user), db=Depends(database)):
    return {'items': [dict(row) for row in db.execute('SELECT * FROM reimbursement_plans WHERE user_id = ? '
                                                     'ORDER BY id DESC LIMIT 100', (user['id'],))]}
