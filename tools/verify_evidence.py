#!/usr/bin/env python3
"""Verify an Agent Gateway evidence bundle offline.

    python3 tools/verify_evidence.py evidence.json

Checks, with no network, that the bundle agrees with itself:
  1. The Intent Mandate signature (Ed25519) verifies, and the signed string is
     exactly CONTEXT + canonical JSON of the mandate record.
  2. The key id is the SHA-256 prefix of the public key, and it is the mandate's principal.
  3. The Cart Mandate string matches its record and points to this Intent Mandate (id and hash).
  4. If the traveler approved (human present): that signature verifies, it was made by the
     same device key, and the approved cart is the same cart.
  5. The purchase fits the signed constraints (category, budget, auto-approve limit or approval).
  6. The ledger hash chain is intact from genesis to the head, and it records the mandate,
     the decision for exactly this cart (same cart hash and amount) and the PayPal capture
     (same order id, capture id and amount).
What it cannot show: that the public key belongs to the traveler, that PayPal really moved
the money (the PayPal part is what the gateway recorded), or that the holder of the ledger
did not rebuild it or cut entries off its end. Those need information from outside the bundle.
A bundle comes from someone else. Text from it is printed with control characters shown as
escapes, and a file that is not built like a bundle gives NOT VERIFIED with the reason.
Exit code 0 when every check passes, 1 otherwise. Python 3.9+ standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import ed25519  # noqa: E402
from app.ledger import GENESIS, verify_entries  # noqa: E402
from app.util import CART_CONTEXT, INTENT_CONTEXT, b64url_decode, canonical_json, to_minor  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _same_amount(value, total: int, currency: str) -> bool:
    return value is not None and to_minor(value, currency) == total


STRUCTURE = "Bundle: built as expected"
PARTS = ("intent_mandate", "cart_mandate", "cart_approval", "paypal", "ledger")
# Control, formatting (bidi, zero-width), surrogate and line-separator characters.
UNSAFE = ("Cc", "Cf", "Cs", "Zl", "Zp")


def show(value, limit: int = 200) -> str:
    """Text from a bundle, made safe to print.

    Escape sequences, carriage returns and bidi marks in a title or an id could redraw the
    terminal: a forged "VERIFIED" line, or hidden text. They are printed as escapes such as
    \\x1b, and a long value is cut.
    """
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return "".join(ch.encode("unicode_escape").decode("ascii") if unicodedata.category(ch) in UNSAFE else ch
                   for ch in text)


def shape_problem(bundle) -> str:
    """Why a bundle cannot be checked at all. Empty when its outline is as expected."""
    if not isinstance(bundle, dict):
        return "the file does not hold a JSON object"
    for part in PARTS:
        if bundle.get(part) is not None and not isinstance(bundle[part], dict):
            return "%s is not an object" % part
    entries = (bundle.get("ledger") or {}).get("entries")
    if entries is not None and not (isinstance(entries, list) and all(isinstance(e, dict) for e in entries)):
        return "ledger.entries is not a list of objects"
    return ""


def verify_bundle(bundle) -> list:
    """Return a list of (ok, label, detail), one per check.

    It never raises. A bundle that is not built as expected gives one failed result that
    says what is wrong, so the answer is "not verified" and not a traceback.
    """
    problem = shape_problem(bundle)
    at = ["the bundle"]
    if not problem:
        try:
            return _checks(bundle, at)
        except Exception as e:  # noqa: BLE001 - whatever is wrong with the input, it is not verified
            problem = "%s is not built as expected (%s: %s)" % (at[0], type(e).__name__, e)
    return [(False, STRUCTURE, show(problem))]


def _checks(bundle: dict, at: list) -> list:
    """The checks. `at` names the part being read, for the message when it cannot be read."""
    results = []

    def check(ok, label, detail=""):
        results.append((bool(ok), label, show(detail) if detail else ""))
        return bool(ok)

    at[0] = "intent_mandate"
    im = bundle.get("intent_mandate") or {}
    rec = im.get("record") or {}
    try:
        pub = b64url_decode(im.get("public_key") or "")
        sig = b64url_decode(im.get("signature") or "")
    except (ValueError, TypeError):
        pub, sig = b"", b""
    check(im.get("signed_string") == INTENT_CONTEXT + canonical_json(rec),
          "Intent Mandate: signed string matches the record")
    check(ed25519.verify(pub, (im.get("signed_string") or "").encode("utf-8"), sig),
          "Intent Mandate: Ed25519 signature verifies", "key %s" % im.get("key_id"))
    kid = hashlib.sha256(pub).hexdigest()[:16]
    check(kid == im.get("key_id") == (rec.get("principal") or {}).get("device_key_id"),
          "Intent Mandate: key id = SHA-256(public key) and is the principal", kid)

    at[0] = "cart_mandate"
    cm = bundle.get("cart_mandate") or {}
    crec = cm.get("record") or {}
    check(cm.get("signed_string") == CART_CONTEXT + canonical_json(crec), "Cart Mandate: string matches the record")
    check(crec.get("intent_mandate_id") == rec.get("id") and
          crec.get("intent_mandate_hash") == _sha(im.get("signed_string") or ""),
          "Cart Mandate: points to this Intent Mandate (id and hash)", str(rec.get("id")))

    approval = bundle.get("cart_approval")
    c = rec.get("constraints") or {}
    cur = c.get("currency", "USD")
    total = to_minor((crec.get("total") or {}).get("value") or 0, cur)
    if approval:
        at[0] = "cart_approval"
        arec = approval.get("record") or {}
        try:
            apub = b64url_decode(approval.get("public_key") or "")
            asig = b64url_decode(approval.get("signature") or "")
        except (ValueError, TypeError):
            apub, asig = b"", b""
        check(approval.get("signed_string") == CART_CONTEXT + canonical_json(arec),
              "Approval: signed string matches the record")
        check(ed25519.verify(apub, (approval.get("signed_string") or "").encode("utf-8"), asig),
              "Approval: Ed25519 signature verifies (human present)")
        check(apub == pub and arec.get("approver_key_id") == im.get("key_id"),
              "Approval: signed by the same device that signed the mandate")
        skip = ("presence", "approved_at", "approver_key_id")
        same = {k: v for k, v in arec.items() if k not in skip} == {k: v for k, v in crec.items() if k not in skip}
        check(same and arec.get("presence") == "HUMAN_PRESENT",
              "Approval: the traveler signed this exact cart, HUMAN_PRESENT")

    at[0] = "the constraints or the cart's items"
    items = crec.get("items") or []
    cats = set(c.get("allowed_categories") or [])
    check(items and all(i.get("category") in cats for i in items), "Constraints: category allowed",
          ", ".join(i.get("category", "?") for i in items))
    check(total <= to_minor(c.get("total_budget") or 0, cur), "Constraints: amount within total budget",
          "%s %s" % ((crec.get("total") or {}).get("value"), cur))
    under = total <= to_minor(c.get("auto_approve_limit") or 0, cur)
    check(under or approval, "Constraints: under the auto-approve limit, or approved by the traveler",
          "auto-approved" if under else "human approval")

    at[0] = "ledger"
    led = bundle.get("ledger") or {}
    entries = led.get("entries") or []
    v = verify_entries(entries)
    check(v.get("ok"), "Ledger: hash chain intact from genesis", "%d entries" % len(entries) if v.get("ok") else
          "broken at #%s: %s" % (v.get("broken_at"), v.get("why")))
    check((entries[-1]["hash"] if entries else GENESIS) == led.get("head"), "Ledger: head hash matches",
          str(led.get("head"))[:16])
    cart_id = bundle.get("cart_id")
    kinds = [(e.get("kind"), e.get("data") or {}) for e in entries]
    check(any(k == "MANDATE_SIGNED" and d.get("mandate_id") == rec.get("id") and
              d.get("signed_hash") == _sha(im.get("signed_string") or "") for k, d in kinds),
          "Ledger: records this signed mandate")
    # An auto-approved cart is signed by nobody. What ties the cart in this bundle to the cart the
    # gateway decided on and paid is the cart hash and the amount the gateway wrote into the ledger.
    cart_hash = _sha(cm.get("signed_string") or "")
    decisions = [d for k, d in kinds if k == "CART_DECISION" and d.get("cart_id") == cart_id]
    decided = [d for d in decisions if d.get("cart_hash") == cart_hash and _same_amount(d.get("amount"), total, cur)]
    check(decided, "Ledger: records the gateway decision for this cart",
          "" if decided else "no decision entry for this cart" if not decisions else
          "the cart in the bundle is not the cart that was decided on: hash or amount differs")
    at[0] = "paypal"
    pp = bundle.get("paypal") or {}
    if pp.get("capture_id"):
        check(any(k == "PAYPAL_CAPTURE" and d.get("cart_id") == cart_id and d.get("capture_id") == pp["capture_id"]
                  and d.get("order_id") == pp.get("order_id") and _same_amount(d.get("amount"), total, cur)
                  for k, d in kinds),
              "Ledger: records the PayPal capture", "%s / capture %s" % (pp.get("order_id"), pp.get("capture_id")))
        back = pp.get("readback") or {}
        check(pp.get("order_status") == "COMPLETED" and pp.get("capture_status") == "COMPLETED"
              and back.get("order_id") in (None, pp.get("order_id")),
              "PayPal: order and capture COMPLETED", "%s (%s)" % (pp.get("order_status"), pp.get("env")))
    if approval:
        check(any(k == "CART_SIGNED" and d.get("cart_id") == cart_id and
                  d.get("signed_hash") == _sha(approval.get("signed_string") or "") for k, d in kinds),
              "Ledger: records the traveler's approval signature")
    return results


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[0])
        print("usage: python3 tools/verify_evidence.py evidence.json")
        return 2
    try:
        with open(argv[0], "r", encoding="utf-8") as f:
            bundle = json.load(f)
    except (OSError, ValueError, RecursionError) as e:
        print("NOT VERIFIED: %s cannot be read as JSON (%s)" % (show(argv[0]), show(e)))
        return 1
    results = verify_bundle(bundle)
    if [label for _, label, _ in results] == [STRUCTURE]:
        print("Evidence bundle %s" % show(bundle.get("cart_id") if isinstance(bundle, dict) else None))
        print("  NOT VERIFIED: %s" % results[0][2])
        return 1
    crec = (bundle.get("cart_mandate") or {}).get("record") or {}
    item = (crec.get("items") or [{}])[0]
    total = crec.get("total") or {}
    print("Evidence bundle %s" % show(bundle.get("cart_id")))
    print("  %s · %s %s · status %s" % (show(item.get("title")), show(total.get("value")),
                                         show(total.get("currency")), show(bundle.get("status"))))
    print("")
    for ok, label, detail in results:
        print("  %s %s%s" % ("PASS" if ok else "FAIL", label, ("  (%s)" % detail) if detail else ""))
    passed = sum(1 for r in results if r[0])
    print("")
    all_ok = passed == len(results)
    print("  %s: %d of %d checks passed" % ("VERIFIED" if all_ok else "NOT VERIFIED", passed, len(results)))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
