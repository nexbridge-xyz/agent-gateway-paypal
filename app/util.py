"""Small helpers: canonical JSON, encodings, ids, time and money."""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import secrets
from decimal import ROUND_HALF_UP, Decimal

# PayPal does not accept decimals for these currencies.
ZERO_DECIMAL = {"JPY", "HUF", "TWD"}
SYMBOLS = {"USD": "$", "JPY": "¥", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$",
           "SGD": "S$", "HKD": "HK$"}

# What the browser signs is always CONTEXT + canonical JSON, so a signature
# made for one kind of record can never be replayed as another kind.
INTENT_CONTEXT = "NexBridge Agent Gateway | Intent Mandate v1\n"
CART_CONTEXT = "NexBridge Agent Gateway | Cart Mandate v1\n"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj) -> bytes:
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_id(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(6)


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now_iso() -> str:
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> _dt.date:
    return now_utc().date()


def parse_date(text) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def decimals(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL else 2


def to_minor(value, currency: str) -> int:
    """'98.00' or 98 -> 9800 (USD); '15000' -> 15000 (JPY)."""
    d = decimals(currency)
    try:
        q = Decimal(str(value).replace(",", "").strip() or "0")
    except Exception:
        q = Decimal(0)
    q = q.quantize(Decimal(1) if d == 0 else Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(q * (10 ** d))


def value_str(minor: int, currency: str) -> str:
    """PayPal 'value' string: 9800 -> '98.00' (USD), 15000 -> '15000' (JPY)."""
    minor = int(minor)
    if decimals(currency) == 0:
        return str(minor)
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return "%s%d.%02d" % (sign, minor // 100, minor % 100)


def display(minor: int, currency: str) -> str:
    minor = int(minor)
    if decimals(currency) == 0:
        body = "{:,}".format(minor)
    else:
        body = "{:,.2f}".format(minor / 100)
    sym = SYMBOLS.get(currency)
    return sym + body if sym else body + " " + currency


def display_short(minor: int, currency: str) -> str:
    """Like display(), but drops '.00' on whole amounts: $600 instead of $600.00."""
    text = display(minor, currency)
    return text[:-3] if text.endswith(".00") else text


def mask(text, keep: int = 4) -> str:
    text = str(text or "")
    if len(text) <= keep * 2:
        return text[:2] + "…" if text else ""
    return text[:keep] + "…" + text[-keep:]


def mask_email(email) -> str:
    email = str(email or "")
    if "@" not in email:
        return mask(email)
    name, domain = email.split("@", 1)
    return (name[:2] + "…@" + domain) if name else "…@" + domain
