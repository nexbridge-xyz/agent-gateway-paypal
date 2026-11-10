"""Mandate rules: turn a draft into a clean Intent Mandate, and check a purchase against it.

The checks are plain code, not a model. A model can be fooled by text it reads;
this function only looks at the signed mandate, the catalog price and the ledger.
"""
from __future__ import annotations

import datetime as _dt

from .catalog import CATEGORIES
from .util import display, parse_date, to_minor, today, value_str

HARD = "hard"   # failing it blocks the purchase
SOFT = "soft"   # failing it sends the purchase to the user for a signature


def normalize_fields(raw: dict, currency: str, scenario_end: _dt.date | None = None) -> dict:
    """Clean up a mandate draft from the AI or from the edit form."""
    raw = raw or {}
    notes = [str(n)[:160] for n in (raw.get("notes") or []) if str(n).strip()][:3]

    total = max(0, to_minor(raw.get("total_budget") or 0, currency))
    total = min(total, to_minor(1_000_000, currency))
    limit = max(0, to_minor(raw.get("auto_approve_limit") or 0, currency))
    if limit > total:
        limit = total
        notes.append("Auto-approve limit was higher than the budget, so it was lowered to the budget.")

    cats = []
    for c in raw.get("allowed_categories") or []:
        c = str(c).strip().lower()
        if c in CATEGORIES and c not in cats:
            cats.append(c)

    try:
        max_purchases = int(raw.get("max_purchases") or 0)
    except (TypeError, ValueError):
        max_purchases = 0
    if max_purchases <= 0:
        max_purchases = 6
    max_purchases = max(1, min(max_purchases, 20))

    day0 = today()
    valid_until = parse_date(raw.get("valid_until"))
    if valid_until is None:
        valid_until = scenario_end or (day0 + _dt.timedelta(days=7))
    if valid_until < day0:
        valid_until = scenario_end if scenario_end and scenario_end >= day0 else day0 + _dt.timedelta(days=7)
        notes.append("The end date was in the past, so it was moved to %s." % valid_until.isoformat())
    if valid_until > day0 + _dt.timedelta(days=90):
        valid_until = day0 + _dt.timedelta(days=90)
        notes.append("Mandates last at most 90 days.")

    trip_raw = raw.get("trip") or {}
    trip = {}
    for key in ("origin", "destination", "start_date", "end_date"):
        val = str(trip_raw.get(key) or "").strip()
        if val:
            trip[key] = val[:60]
    try:
        travelers = int(trip_raw.get("travelers") or 0)
        if travelers > 0:
            trip["travelers"] = min(travelers, 20)
    except (TypeError, ValueError):
        pass

    verified_only = raw.get("verified_merchants_only")
    verified_only = True if verified_only is None else bool(verified_only)

    summary = str(raw.get("summary") or "").strip()[:240]
    if not summary:
        summary = "Spend up to %s on %s; ask me before anything over %s." % (
            display(total, currency), ", ".join(cats) or "nothing yet", display(limit, currency))

    return {
        "currency": currency,
        "total_budget": value_str(total, currency),
        "auto_approve_limit": value_str(limit, currency),
        "allowed_categories": cats,
        "max_purchases": max_purchases,
        "valid_until": valid_until.isoformat(),
        "verified_merchants_only": verified_only,
        "trip": trip,
        "summary": summary,
        "notes": notes[:4],
    }


def evaluate(mandate: dict, status: str, item: dict, usage: dict, currency: str) -> dict:
    """Check one purchase against the signed mandate.

    usage = {"committed": minor units already paid, "reserved": minor units waiting
             for approval (not counting this cart), "count": purchases so far,
             "skus": skus already bought}
    """
    c = mandate["constraints"]
    price = int(item["price_minor"])
    total = to_minor(c["total_budget"], currency)
    limit = to_minor(c["auto_approve_limit"], currency)
    used = int(usage.get("committed", 0)) + int(usage.get("reserved", 0))
    checks = []

    def add(key, ok, kind, label, detail):
        checks.append({"key": key, "ok": bool(ok), "kind": kind, "label": label, "detail": detail,
                       "fail_label": _fail_text({"key": key, "label": label})})

    add("mandate_active", status == "active", HARD, "Mandate is signed and active",
        "status: %s" % status)
    valid_until = parse_date(c["valid_until"])
    add("time_window", valid_until is not None and today() <= valid_until, HARD,
        "Within the mandate dates", "valid until %s" % c["valid_until"])
    add("category", item["category"] in c["allowed_categories"], HARD,
        "Category is allowed", "%s · allowed: %s" % (item["category"], ", ".join(c["allowed_categories"]) or "none"))
    add("merchant", item["verified"] or not c.get("verified_merchants_only", True), HARD,
        "Merchant is verified", "%s%s" % (item["merchant"], "" if item["verified"] else " · not verified"))
    add("budget", used + price <= total, HARD, "Fits the total budget",
        "%s used + %s vs %s budget" % (display(used, currency), display(price, currency), display(total, currency)))
    add("count", int(usage.get("count", 0)) < int(c["max_purchases"]), HARD, "Under the purchase count limit",
        "%d of %d used" % (int(usage.get("count", 0)), int(c["max_purchases"])))
    add("auto_limit", price <= limit, SOFT, "At or under the auto-approve limit",
        "%s vs limit %s" % (display(price, currency), display(limit, currency)))
    add("repeat", item["sku"] not in set(usage.get("skus") or []), SOFT, "Not a repeat purchase",
        item["sku"])

    hard_fail = [x for x in checks if not x["ok"] and x["kind"] == HARD]
    soft_fail = [x for x in checks if not x["ok"] and x["kind"] == SOFT]
    if hard_fail:
        result = "DENIED"
        reason = "; ".join(_fail_text(x) + " (%s)" % x["detail"] for x in hard_fail)
    elif soft_fail:
        result = "NEEDS_APPROVAL"
        reason = "; ".join(_fail_text(x) + " (%s)" % x["detail"] for x in soft_fail)
    else:
        result = "APPROVED"
        reason = "All %d checks passed" % len(checks)
    return {"result": result, "reason": reason, "checks": checks, "price_minor": price}


def _fail_text(check: dict) -> str:
    return {
        "mandate_active": "Mandate is not active",
        "time_window": "Outside the mandate dates",
        "category": "Category is not allowed",
        "merchant": "Merchant is not verified",
        "budget": "Over the total budget",
        "count": "Too many purchases",
        "auto_limit": "Above the auto-approve limit",
        "repeat": "Repeat purchase",
    }.get(check["key"], check["label"])
