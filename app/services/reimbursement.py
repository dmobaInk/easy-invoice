"""Exact subset selection in integer cents, with explicit resource limits."""

from decimal import Decimal


class OptimizationLimitError(Exception):
    pass


def cents(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal('0.01')) * 100)


def optimize(invoices, target_amount, allow_exceed=True, *, max_states=200000, max_work=10000000):
    target = cents(target_amount)
    if target <= 0:
        raise ValueError('Target must be positive')
    candidates = sorted([(row['id'], cents(row['amount'])) for row in invoices], key=lambda pair: pair[0])
    candidates = [(key, amount) for key, amount in candidates if amount >= 0 and (allow_exceed or amount <= target)]
    if not candidates:
        return []

    def rank(total, ids):
        # Meeting the target always outranks a shortfall.
        return total < target, abs(total - target), len(ids), ids

    best_total, best_ids = min(((amount, (key,)) for key, amount in candidates), key=lambda item: rank(*item))
    bound = target + max(amount for _, amount in candidates) if allow_exceed else target
    states = {0: ()}
    work = 0
    for key, amount in candidates:
        if amount == 0:
            continue
        for total, selected in list(states.items()):
            work += 1
            if work > max_work:
                raise OptimizationLimitError('组合计算量较大，请缩小该抬头下的可用发票范围后重试')
            combined = total + amount
            if combined > bound:
                continue
            ids = selected + (key,)
            existing = states.get(combined)
            if existing is None or (len(ids), ids) < (len(existing), existing):
                states[combined] = ids
            if rank(combined, ids) < rank(best_total, best_ids):
                best_total, best_ids = combined, ids
            if len(states) > max_states:
                raise OptimizationLimitError('组合数量较大，请缩小该抬头下的可用发票范围后重试')
    return list(best_ids)
