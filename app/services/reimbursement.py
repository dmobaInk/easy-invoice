"""Exact subset selection in integer cents, with explicit resource limits."""

from decimal import Decimal
from functools import reduce
from math import gcd
from time import monotonic


class OptimizationLimitError(Exception):
    pass


def cents(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal('0.01')) * 100)


def optimize(*args, **kwargs):
    for event in match_events(*args, **kwargs):
        if 'ids' in event:
            return event['ids']


def match_events(invoices, target_amount, allow_exceed=True, *, max_states=None, max_work=10000000):
    started = monotonic()

    def progress(done, count, best):
        return {'done': done, 'total': count, 'best_cents': best,
                'eta': (monotonic() - started) * (count - done) / done if done else None}

    target = cents(target_amount)
    if target <= 0:
        raise ValueError('Target must be positive')
    candidates = sorted([(row['id'], cents(row['amount'])) for row in invoices], key=lambda pair: pair[0])
    candidates = [(key, amount) for key, amount in candidates if amount >= 0 and (allow_exceed or amount <= target)]
    if not candidates:
        yield {'ids': []}
        return

    def rank(total, ids):
        # Meeting the target always outranks a shortfall.
        return total < target, abs(total - target), len(ids), ids

    best_total, best_ids = min(((amount, (key,)) for key, amount in candidates), key=lambda item: rank(*item))
    positive = [(key, amount) for key, amount in candidates if amount]
    total_amount = sum(amount for _, amount in positive)
    if not positive or best_total == target:
        yield {'ids': list(best_ids)}
        return
    if total_amount <= target:
        yield {'ids': [key for key, _ in positive]}
        return

    # Any feasible covering sum bounds the exact search from above.
    bound = target
    single_total = best_total
    if allow_exceed:
        # A covering single invoice cannot improve by adding positive amounts.
        below = [item for item in positive if item[1] < target]
        if sum(amount for _, amount in below) < target:
            yield {'ids': list(best_ids)}
            return
        subtotal = 0
        for _, amount in sorted(below, key=lambda item: item[1], reverse=True):
            subtotal += amount
            if subtotal >= target:
                break
        bound = min(subtotal, min((v for _, v in positive if v >= target), default=subtotal))
        if rank(bound, ()) < rank(best_total, ()):
            best_total = bound
    positive = [(key, amount) for key, amount in positive if amount <= bound]
    unit = reduce(gcd, (amount for _, amount in positive))
    width = bound // unit + 1
    count = len(positive)
    planes = count.bit_length()
    yield progress(0, count, best_total)
    # One traceback bitmap per invoice, plus bit planes and intermediate buffers.
    if max_states is None and width * (count + 3 * planes + 12) <= 536_870_912:
        mask = (1 << width) - 1
        reachable = 1
        costs = [0] * planes
        decisions = []
        for done, (_, value) in enumerate(reversed(positive), 1):
            shift = value // unit
            available = (reachable << shift) & mask
            # Each column encodes the fewest invoices for its sum in binary.
            # Add one to all shifted costs in parallel and compare low to high.
            carry = available
            take = available
            proposed = []
            for old in costs:
                shifted = (old << shift) & mask
                new = shifted ^ carry
                proposed.append(new)
                carry &= shifted
                take = (take & ~(new ^ old)) | (old & ~new)
            take = available & (take | ~reachable)
            costs = [(old & ~take) | (new & take) for old, new in zip(costs, proposed)]
            # On equal costs include the current (smaller) ID.
            decisions.append(take)
            reachable |= available
            threshold = (target + unit - 1) // unit
            above = reachable >> threshold
            local = (threshold + (above & -above).bit_length() - 1 if above
                     else reachable.bit_length() - 1) * unit
            if rank(local, ()) < rank(best_total, ()):
                best_total = local
            yield progress(done, count, best_total)
        remaining = best_total // unit
        ids = []
        for (key, amount), decision in zip(positive, reversed(decisions)):
            if (decision >> remaining) & 1:
                ids.append(key)
                remaining -= amount // unit
        yield {'ids': ids}
        return

    # Sparse amounts can span too wide a range for a dense bitmap.
    initial_best = best_total
    best_total = single_total
    max_states = 200000 if max_states is None else max_states
    states = {0: ()}
    work = 0
    for done, (key, amount) in enumerate(positive, 1):
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
        reported = min((initial_best, best_total), key=lambda total: rank(total, ()))
        yield progress(done, count, reported)
    yield {'ids': list(best_ids)}
