"""One definition of "what the seller actually charges for this pricing option".

Both the planner (choosing an option) and the guard (re-stamping the authoritative price
before the ceiling check) have to decide whether a pricing option is bookable and at what
price. When those two decisions were written separately they disagreed: the planner skipped
``supported is False`` and the guard did not, so a product carrying two options under the
SAME ``pricing_option_id`` — an unsupported one with a junk price, then a supported one —
planned at the good price and re-stamped from the junk one. Keeping the predicate in one
place is what makes that class of divergence impossible rather than merely fixed.
"""

from __future__ import annotations

import math
from typing import Any


def buyable_price(pricing_option: dict[str, Any], currency: str) -> float | None:
    """The bookable fixed CPM of one option, or None when it cannot be booked as-is.

    None (not an exception, not a sentinel number) for every reason an option is
    unbookable, so a caller cannot accidentally arithmetic on a non-price:

    * the seller marked it unsupported
    * it is quoted in a currency other than the brief's — comparing a foreign CPM against
      the ceiling is a real overspend
    * it has no ``fixed_price`` (floor/auction options need a bid_price we do not send)
    * the price is not a finite, non-negative number

    That last clause is load-bearing. ``float("NaN")`` is reachable from a plain
    RFC-8259-valid JSON string, and every ``>`` comparison against NaN is False — so an
    unknown price sailing through here would pass a ceiling check of any size.
    """
    if pricing_option.get("supported") is False:
        return None
    if (pricing_option.get("currency") or "USD") != currency:
        return None
    raw = pricing_option.get("fixed_price")
    if raw is None:
        return None
    try:
        cpm = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cpm) or cpm < 0:
        return None
    return cpm
