"""The gateway: devices, mandates, carts, approvals, payments, events and evidence.

Everything that moves money is here, in plain code. The agent (any model, or the
scripted one) can only call three tools; the price comes from the catalog, the
payee is fixed, and every purchase is checked against the mandate the traveler
signed on their own device.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import threading
import traceback

from . import ed25519, policy
from .catalog import Catalog
from .ledger import GENESIS, Ledger, verify_entries
from .llm import AIError
from .paypal import PAYMENT_TIMEOUT_TEXT, PAYMENT_UNKNOWN_HINT, VAULT_HINT, PayPalError
from .util import (CART_CONTEXT, INTENT_CONTEXT, b64url, b64url_decode, canonical_json, clean_text, display,
                   mask_email, mask_tail, new_id, now_iso, sha256_hex, to_minor, value_str)

AGENT = {"id": "tabi-travel-agent", "name": "Tabi · AI travel agent"}
MERCHANT_NAME = "Tabi Travel (demo merchant)"
BRAND_NAME = "Tabi Travel (demo)"
MAX_EVENTS = 600
MAX_HISTORY = 8  # user/agent turns kept for follow-ups

# Cart statuses that hold budget. PAYMENT_UNKNOWN: PayPal's answer was lost or unclear,
# so the money may have moved and the amount stays counted.
COMMITTED = ("PAYING", "PAID", "CAPTURE_PENDING", "PAYMENT_UNKNOWN")
RESERVED = ("PENDING_APPROVAL",)

# PayPal order statuses that mean nothing was captured.
NOT_CAPTURED = ("CREATED", "SAVED", "APPROVED", "PAYER_ACTION_REQUIRED", "VOIDED")
UNKNOWN_NOTE = "Do not retry this item. Tell the traveler to check it in PayPal."


def payment_status(order_status, capture_status) -> str:
    """Cart status for what PayPal reported. Only a clear "no money moved" frees the budget."""
    if order_status == "COMPLETED" and capture_status == "COMPLETED":
        return "PAID"
    if capture_status == "PENDING":
        return "CAPTURE_PENDING"
    if capture_status in ("DECLINED", "FAILED") or (capture_status is None and order_status in NOT_CAPTURED):
        return "NOT_EXECUTED"
    return "PAYMENT_UNKNOWN"


class GatewayError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def fingerprint(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))[:16]


def _empty_session() -> dict:
    return {"mandate": None, "prepared_mandates": {}, "carts": {}, "cart_order": [],
            "events": [], "event_seq": 0, "ledger": [], "history": []}


class Gateway:
    def __init__(self, paypal, ai, state_path: str | None = None, currency: str = "USD"):
        self.paypal = paypal
        self.ai = ai
        self.currency = currency
        self.state_path = state_path
        self.lock = threading.RLock()
        self.agent_thread = None
        self.agent_running = False
        self.state = {"version": 1, "devices": {}, "paypal": {}}
        self.state.update(_empty_session())
        self._load()
        self.ledger = Ledger(self.state["ledger"])

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict) and saved.get("version") == 1:
                self.state.update(saved)
        except (OSError, ValueError):
            pass  # a broken state file starts fresh; the old one is overwritten on the next save

    def save(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.remove(tmp)  # a half-written copy would keep the vault token readable
            except OSError:
                pass
            raise
        try:
            os.chmod(self.state_path, 0o600)  # the vault token lives here
        except OSError:
            pass

    @property
    def catalog(self) -> Catalog:
        # Rebuilt on demand so the trip dates move with the calendar.
        return Catalog(self.currency)

    # ------------------------------------------------------------------
    # events (what the UI shows in the chat)
    # ------------------------------------------------------------------
    def emit(self, kind: str, **data) -> dict:
        with self.lock:
            self.state["event_seq"] += 1
            ev = {"seq": self.state["event_seq"], "ts": now_iso(), "kind": kind}
            ev.update(data)
            self.state["events"].append(ev)
            if len(self.state["events"]) > MAX_EVENTS:
                del self.state["events"][:-MAX_EVENTS]
            self.save()
            return ev

    def events_after(self, after: int) -> list:
        with self.lock:
            return [e for e in self.state["events"] if e["seq"] > after]

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------
    def register_device(self, public_key_b64: str) -> dict:
        try:
            raw = b64url_decode(public_key_b64)
        except (ValueError, TypeError):
            raise GatewayError("Public key is not base64url.")
        if len(raw) != 32 or ed25519._decompress(raw) is None:
            raise GatewayError("Public key is not a valid Ed25519 key.")
        kid = key_id_for(raw)
        with self.lock:
            if kid not in self.state["devices"]:
                self.state["devices"][kid] = {"public_key": b64url(raw), "registered_at": now_iso()}
                self.save()
        return {"key_id": kid}

    def _device(self, key_id: str) -> dict:
        dev = self.state["devices"].get(str(key_id or ""))
        if not dev:
            raise GatewayError("This device key is not registered. Reload the page.")
        return dev

    def _verify(self, key_id: str, message: str, signature_b64: str) -> bytes:
        dev = self._device(key_id)
        try:
            sig = b64url_decode(signature_b64)
        except (ValueError, TypeError):
            raise GatewayError("Signature is not base64url.")
        if not ed25519.verify(b64url_decode(dev["public_key"]), message.encode("utf-8"), sig):
            raise GatewayError("Signature does not verify for this device key.")
        return sig

    # ------------------------------------------------------------------
    # PayPal connection (vault)
    # ------------------------------------------------------------------
    def _conn(self) -> dict:
        return self.state["paypal"].get(self.paypal.env) or {}

    def paypal_connected(self) -> bool:
        return self._conn().get("status") == "connected"

    def _vault_fingerprint(self, vault_id: str) -> str:
        """Fingerprint of the vault token for the ledger: HMAC-SHA256 with a random key.

        The ledger goes to the browser and into every evidence bundle. A plain hash of a
        short token would let anyone test guesses against it; without the key that is not
        possible. The key is made once and stays in the state file. Call with the lock held.
        """
        keys = self.state.setdefault("secrets", {})
        if not keys.get("fingerprint_key"):
            keys["fingerprint_key"] = b64url(os.urandom(32))
        key = b64url_decode(keys["fingerprint_key"])
        return hmac.new(key, vault_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def paypal_begin(self, base_url: str) -> str:
        """Create a setup token and return the URL the browser should go to."""
        rid = new_id("st")
        res = self.paypal.create_setup_token(base_url + "/paypal/return", base_url + "/paypal/cancel",
                                             BRAND_NAME, rid)
        if not res.get("approve_url") or not res.get("id"):
            raise GatewayError("PayPal did not return an approval link (setup token status: %s)."
                               % res.get("status"))
        with self.lock:
            conn = dict(self._conn())
            conn.update({"pending_setup_token": res["id"], "pending_since": now_iso(), "last_error": None})
            if conn.get("status") != "connected":
                conn["status"] = "pending"
            self.state["paypal"][self.paypal.env] = conn
            self.save()
        return res["approve_url"]

    def paypal_finish(self, query: dict) -> dict:
        """Called on return_url. Turns the approved setup token into a vault token."""
        with self.lock:
            conn = dict(self._conn())
            setup = conn.get("pending_setup_token")
        if not setup:
            raise GatewayError("No PayPal approval is in progress. Click Connect PayPal again.")
        given = query.get("approval_token_id") or query.get("token")
        if given and given != setup and self.paypal.live is False:
            raise GatewayError("The approval does not match the pending request.")
        res = self.paypal.create_payment_token(setup, "pt_" + setup)
        if not res.get("vault_id"):
            raise GatewayError("PayPal did not return a vault token.")
        with self.lock:
            conn.update({
                "status": "connected", "vault_id": res["vault_id"], "customer_id": res.get("customer_id"),
                "payer_email": res.get("payer_email"), "connected_at": now_iso(),
                "pending_setup_token": None, "last_error": None,
                # Names of the query parameters PayPal sent back (values are not stored).
                "return_params": sorted(query.keys()),
            })
            self.state["paypal"][self.paypal.env] = conn
            self.ledger.append("PAYPAL_CONNECTED", "PayPal saved for agent payments (%s)" % self.paypal.env, {
                "env": self.paypal.env, "payer": mask_email(res.get("payer_email")),
                "vault_token_fingerprint": self._vault_fingerprint(res["vault_id"]),
                "customer_id": res.get("customer_id"),
            })
            self.save()
        self.emit("system", text="PayPal connected. The vault token is held by the gateway; the agent never sees it.")
        return conn

    def paypal_cancel(self):
        with self.lock:
            conn = dict(self._conn())
            conn["pending_setup_token"] = None
            if conn.get("status") != "connected":
                conn["status"] = "disconnected"
            self.state["paypal"][self.paypal.env] = conn
            self.save()

    def paypal_error(self, message: str):
        with self.lock:
            conn = dict(self._conn())
            conn["last_error"] = message
            if conn.get("status") == "pending":
                conn["status"] = "disconnected"
            self.state["paypal"][self.paypal.env] = conn
            self.save()

    # ------------------------------------------------------------------
    # mandates
    # ------------------------------------------------------------------
    def draft_mandate(self, instruction: str) -> dict:
        instruction = clean_text(instruction or "").strip()[:1000]
        if not instruction:
            raise GatewayError("Write what the agent may do first.")
        cat = self.catalog
        from .util import today
        raw = self.ai.draft_mandate(instruction, today(), cat.start, cat.end)
        fields = policy.normalize_fields(raw, self.currency, cat.end)
        return {"fields": fields, "instruction": instruction, "source": self.ai.kind, "label": self.ai.label}

    def prepare_mandate(self, instruction: str, fields: dict, key_id: str) -> dict:
        self._device(key_id)
        instruction = clean_text(instruction or "").strip()[:1000]
        f = policy.normalize_fields(fields, self.currency, self.catalog.end)
        if to_minor(f["total_budget"], self.currency) <= 0:
            raise GatewayError("Set a total budget above zero.")
        if not f["allowed_categories"]:
            raise GatewayError("Allow at least one category.")
        trip = dict(f["trip"])
        record = {
            "type": "IntentMandate", "version": 1, "id": new_id("im"), "issued_at": now_iso(),
            "principal": {"device_key_id": key_id},
            "agent": dict(AGENT),
            "merchant": {"name": MERCHANT_NAME, "paypal_env": self.paypal.env},
            "presence": "HUMAN_NOT_PRESENT",
            "instruction": instruction,
            "constraints": {
                "currency": self.currency,
                "total_budget": f["total_budget"],
                "auto_approve_limit": f["auto_approve_limit"],
                "allowed_categories": f["allowed_categories"],
                "max_purchases": f["max_purchases"],
                "valid_until": f["valid_until"],
                "verified_merchants_only": f["verified_merchants_only"],
            },
            "trip": trip,
            "summary": f["summary"],
        }
        signing = INTENT_CONTEXT + canonical_json(record)
        with self.lock:
            prepared = self.state["prepared_mandates"]
            prepared.clear()  # only the latest prepared mandate can be signed
            prepared[record["id"]] = {"record": record, "signed_string": signing, "notes": f["notes"]}
            self.save()
        return {"mandate_id": record["id"], "record": record, "signing_string": signing,
                "key_id": key_id, "notes": f["notes"]}

    def activate_mandate(self, mandate_id: str, signature: str) -> dict:
        with self.lock:
            prep = self.state["prepared_mandates"].get(str(mandate_id or ""))
            if not prep:
                raise GatewayError("This mandate was not prepared, or a newer one replaced it.")
            record = prep["record"]
            key_id = record["principal"]["device_key_id"]
            self._verify(key_id, prep["signed_string"], signature)
            old = self.state["mandate"]
            if old and old["status"] == "active":
                old["status"] = "replaced"
                old["revoked_at"] = now_iso()
                self.ledger.append("MANDATE_REVOKED", "Mandate %s replaced by a new one" % old["record"]["id"],
                                   {"mandate_id": old["record"]["id"], "reason": "replaced"})
            dev = self._device(key_id)
            self.state["mandate"] = {
                "record": record, "signed_string": prep["signed_string"], "signature": signature,
                "public_key": dev["public_key"], "key_id": key_id, "alg": "Ed25519",
                "status": "active", "activated_at": now_iso(), "revoked_at": None, "notes": prep.get("notes") or [],
            }
            self.state["prepared_mandates"].clear()
            c = record["constraints"]
            self.ledger.append("MANDATE_SIGNED", "Intent Mandate signed: " + record["summary"][:120], {
                "mandate_id": record["id"], "key_id": key_id,
                "signed_hash": sha256_hex(prep["signed_string"].encode("utf-8")),
                "signature_fingerprint": fingerprint(signature),
                "total_budget": c["total_budget"], "auto_approve_limit": c["auto_approve_limit"],
                "currency": c["currency"], "allowed_categories": c["allowed_categories"],
                "valid_until": c["valid_until"],
            })
            self.save()
        self.emit("system", text="Intent Mandate signed on this device and active.")
        return self.mandate_view()

    def revoke_mandate(self) -> dict:
        with self.lock:
            m = self.state["mandate"]
            if not m or m["status"] != "active":
                raise GatewayError("There is no active mandate to revoke.")
            m["status"] = "revoked"
            m["revoked_at"] = now_iso()
            self.ledger.append("MANDATE_REVOKED", "Mandate revoked by the traveler",
                               {"mandate_id": m["record"]["id"], "reason": "revoked"})
            self.save()
        self.emit("system", text="Mandate revoked. Every later purchase will be denied.")
        return self.mandate_view()

    def _usage(self, exclude: str | None = None) -> dict:
        m = self.state["mandate"]
        mid = m["record"]["id"] if m else None
        committed = reserved = count = 0
        skus = []
        for cid in self.state["cart_order"]:
            cart = self.state["carts"][cid]
            if cart["mandate_id"] != mid or cid == exclude:
                continue
            price = cart["price_minor"]
            if cart["status"] in COMMITTED:
                committed += price
                count += 1
                skus.append(cart["sku"])
            elif cart["status"] in RESERVED:
                reserved += price
                count += 1
                skus.append(cart["sku"])
        return {"committed": committed, "reserved": reserved, "count": count, "skus": skus}

    def _mandate_status(self, m: dict | None) -> str:
        """The mandate's status as the checks see it.

        A mandate names the PayPal environment it was signed for. Each mode has its own state
        file, so the two normally match. A mandate for another environment (a copied file, or
        one from before the files were split) does not count as active here.
        """
        if not m:
            return "none"
        signed_for = (m["record"].get("merchant") or {}).get("paypal_env")
        if signed_for != self.paypal.env:
            return "signed for PayPal %s, not %s" % (signed_for, self.paypal.env)
        return m["status"]

    def mandate_view(self) -> dict | None:
        with self.lock:
            m = self.state["mandate"]
            if not m:
                return None
            usage = self._usage()
            total = to_minor(m["record"]["constraints"]["total_budget"], self.currency)
            left = max(0, total - usage["committed"] - usage["reserved"])
            return {
                "id": m["record"]["id"], "status": self._mandate_status(m), "record": m["record"],
                "key_id": m["key_id"], "signature_fingerprint": fingerprint(m["signature"]),
                "activated_at": m["activated_at"], "revoked_at": m["revoked_at"], "notes": m.get("notes") or [],
                "budget": {"total": value_str(total, self.currency),
                           "paid": value_str(usage["committed"], self.currency),
                           "waiting": value_str(usage["reserved"], self.currency),
                           "left": value_str(left, self.currency),
                           "count": usage["count"]},
            }

    # ------------------------------------------------------------------
    # agent tools
    # ------------------------------------------------------------------
    def tool_get_mandate(self) -> dict:
        with self.lock:
            m = self.state["mandate"]
            if not m:
                return {"active": False, "status": "none",
                        "message": "No signed mandate. The traveler must sign one before anything can be bought."}
            v = self.mandate_view()
            carts = [self.state["carts"][c] for c in self.state["cart_order"]]
            mine = [c for c in carts if c["mandate_id"] == m["record"]["id"]]
            status = self._mandate_status(m)
            return {
                "active": status == "active", "status": status,
                "summary": m["record"]["summary"], "constraints": m["record"]["constraints"],
                "trip": m["record"]["trip"],
                "budget_left": display(to_minor(v["budget"]["left"], self.currency), self.currency),
                "purchases": [self._agent_cart(c) for c in mine if c["status"] in COMMITTED],
                "pending_approvals": [self._agent_cart(c) for c in mine if c["status"] in RESERVED],
            }

    def _agent_cart(self, c: dict) -> dict:
        return {"sku": c["sku"], "title": c["item"]["title"], "category": c["item"]["category"],
                "price": display(c["price_minor"], self.currency), "status": c["status"]}

    def tool_search(self, category: str, query: str = "") -> dict:
        cat = self.catalog
        items = [cat.public_view(i) for i in cat.search(category, query)]
        return {"category": category, "count": len(items), "items": items}

    def call_tool(self, name: str, args: dict, actor: str = "Tabi agent") -> dict:
        # The tool name and its arguments are model output: make the text safe to store first.
        name = clean_text(name or "")
        args = args if isinstance(args, dict) else {}
        args = {k: clean_text(v) if isinstance(v, str) else v for k, v in args.items()}
        try:
            if name == "get_mandate":
                out = self.tool_get_mandate()
                self.emit("tool", name=name, actor=actor, summary="get_mandate · " + (
                    "active · %s left" % out["budget_left"] if out.get("active") else out.get("status", "none")))
            elif name == "search_inventory":
                category = str(args.get("category") or "").lower()
                out = self.tool_search(category, str(args.get("query") or ""))
                self.emit("tool", name=name, actor=actor,
                          summary="search_inventory · %s · %d results" % (category or "all", out["count"]),
                          items=[{"sku": i["sku"], "title": i["title"], "price": i["price"],
                                  "verified": i["merchant_verified"]} for i in out["items"]])
            elif name == "request_purchase":
                out = self.request_purchase(str(args.get("sku") or ""), str(args.get("reason") or ""), actor)
            else:
                out = {"error": "Unknown tool %s" % name}
                self.emit("tool", name=str(name), actor=actor, summary="unknown tool %s" % name)
        except GatewayError as e:
            out = {"status": "NOT_EXECUTED", "error": str(e)}
        return out

    # ------------------------------------------------------------------
    # purchases
    # ------------------------------------------------------------------
    def _cart_record(self, cart_id: str, item: dict, reason: str, actor: str, presence: str) -> dict:
        m = self.state["mandate"]
        return {
            "type": "CartMandate", "version": 1, "id": cart_id,
            "intent_mandate_id": m["record"]["id"] if m else None,
            "intent_mandate_hash": sha256_hex(m["signed_string"].encode("utf-8")) if m else None,
            "created_at": now_iso(), "presence": presence,
            "agent": dict(AGENT), "requested_by": actor,
            "payee": {"name": MERCHANT_NAME, "paypal_env": self.paypal.env},
            "merchant": {"name": item["merchant"], "verified": bool(item["verified"])},
            "items": [{"sku": item["sku"], "title": item["title"], "category": item["category"],
                       "detail": item["detail"], "quantity": 1,
                       "unit_price": value_str(item["price_minor"], self.currency)}],
            "total": {"currency": self.currency, "value": value_str(item["price_minor"], self.currency)},
            "reason": reason[:300],
        }

    def request_purchase(self, sku: str, reason: str, actor: str = "Tabi agent") -> dict:
        item = self.catalog.get(sku)
        if not item:
            self.emit("tool", name="request_purchase", actor=actor, summary="request_purchase · %s · unknown sku" % sku)
            return {"status": "DENIED", "why": "No item with sku %s in the catalog." % sku}
        with self.lock:
            m = self.state["mandate"]
            cart_id = new_id("cart")
            status = self._mandate_status(m)
            mandate = m["record"] if m else {"constraints": {
                "total_budget": "0", "auto_approve_limit": "0", "allowed_categories": [], "max_purchases": 0,
                "valid_until": "1970-01-01", "verified_merchants_only": True}}
            decision = policy.evaluate(mandate, status, item, self._usage(), self.currency)
            decision["decided_at"] = now_iso()
            record = self._cart_record(cart_id, item, reason, actor, "HUMAN_NOT_PRESENT")
            cart = {
                "id": cart_id, "created_at": record["created_at"], "actor": actor, "sku": item["sku"],
                "item": {"sku": item["sku"], "title": item["title"], "category": item["category"],
                         "merchant": item["merchant"], "verified": item["verified"], "detail": item["detail"]},
                "price_minor": item["price_minor"], "reason": reason[:300],
                "mandate_id": m["record"]["id"] if m else None,
                "decision": decision, "recheck": None,
                "cart_mandate": {"record": record, "signed_string": CART_CONTEXT + canonical_json(record)},
                "approval": None, "prepared_approval": None, "paypal": None, "refund": None, "error": None,
                "status": {"APPROVED": "PAYING", "NEEDS_APPROVAL": "PENDING_APPROVAL",
                           "DENIED": "DENIED"}[decision["result"]],
            }
            self.state["carts"][cart_id] = cart
            self.state["cart_order"].append(cart_id)
            self.ledger.append("CART_DECISION", "%s %s %s · %s" % (
                item["sku"], display(item["price_minor"], self.currency), decision["result"], actor), {
                "cart_id": cart_id, "sku": item["sku"], "amount": value_str(item["price_minor"], self.currency),
                "currency": self.currency, "result": decision["result"], "actor": actor,
                "mandate_id": cart["mandate_id"],
                "failed": [c["key"] for c in decision["checks"] if not c["ok"]],
                "cart_hash": sha256_hex(cart["cart_mandate"]["signed_string"].encode("utf-8")),
            })
            self.save()
        self.emit("decision", cart_id=cart_id, actor=actor, result=decision["result"], sku=item["sku"],
                  title=item["title"], price=display(item["price_minor"], self.currency), reason=reason[:300],
                  why=decision["reason"], checks=decision["checks"])
        if decision["result"] == "DENIED":
            return {"status": "DENIED", "sku": item["sku"], "why": decision["reason"],
                    "note": "Do not retry this item."}
        if decision["result"] == "NEEDS_APPROVAL":
            return {"status": "PENDING_APPROVAL", "sku": item["sku"], "why": decision["reason"],
                    "note": "The traveler must approve and sign this purchase. Do not retry it; continue."}
        return self._pay(cart_id)

    def _unpaid(self, cart: dict, status: str, msg: str, why: str, data: dict | None = None) -> dict:
        """Close a cart that was not paid (NOT_EXECUTED), or not clearly paid (PAYMENT_UNKNOWN,
        which keeps its amount in the budget). Call with the lock held."""
        unknown = status == "PAYMENT_UNKNOWN"
        cart["status"] = status
        cart["error"] = msg
        self.ledger.append("PAYMENT_UNKNOWN" if unknown else "PAYMENT_FAILED",
                           "%s %s: %s" % (cart["sku"], "payment result unknown" if unknown else "not paid", why),
                           dict(data or {}, cart_id=cart["id"], error=msg))
        self.save()
        self.emit("payment_failed", cart_id=cart["id"], sku=cart["sku"], error=msg, status=status)
        out = {"status": status, "sku": cart["sku"], "error": msg}
        if unknown:
            out["note"] = UNKNOWN_NOTE
        return out

    def _pay(self, cart_id: str) -> dict:
        with self.lock:
            cart = self.state["carts"][cart_id]
            conn = self._conn()
            if conn.get("status") != "connected" or not conn.get("vault_id"):
                return self._unpaid(cart, "NOT_EXECUTED", "PayPal is not connected. Connect PayPal first.",
                                    "PayPal not connected")
            vault_id = conn["vault_id"]
            cart["status"] = "PAYING"
            self.save()
            item = cart["item"]
            mandate_id = cart["mandate_id"] or ""
        # Network call outside the lock; the cart already holds its budget as PAYING.
        try:
            res = self.paypal.charge_vault(vault_id, cart["price_minor"], self.currency, item["title"], item["sku"],
                                           "%s · %s" % (item["title"], item["detail"]), cart_id, mandate_id,
                                           cart_id)
            error = None
        except PayPalError as e:
            res, error = None, e
        except Exception as e:  # noqa: BLE001 - shown to the traveler, never swallowed
            res, error = None, PayPalError(0, "GATEWAY_ERROR", str(e))
        readback = None
        if res and res.get("order_id"):
            try:
                readback = self.paypal.get_order(res["order_id"])
            except PayPalError as e:
                readback = {"error": e.short()}
            except Exception as e:  # noqa: BLE001 - the charge has its answer; a failed read-back must not lose it
                readback = {"error": "GATEWAY_ERROR: %s" % e}
        with self.lock:
            cart = self.state["carts"][cart_id]
            if error is not None:
                # "Not paid" needs a clear refusal from PayPal (4xx) or a request that never left.
                # Anything else may have been charged, so the cart keeps its amount in the budget.
                if error.sent is False or 400 <= error.status < 500:
                    msg = error.short()
                    if self.paypal.live and error.name in ("NOT_AUTHORIZED", "PERMISSION_DENIED") or \
                            "vault" in msg.lower():
                        msg += " · " + VAULT_HINT
                    return self._unpaid(cart, "NOT_EXECUTED", msg, error.name, {"debug_id": error.debug_id})
                msg = PAYMENT_TIMEOUT_TEXT if error.name == "TIMEOUT" else error.short() + " · " + PAYMENT_UNKNOWN_HINT
                return self._unpaid(cart, "PAYMENT_UNKNOWN", msg, error.name, {
                    "debug_id": error.debug_id, "amount": value_str(cart["price_minor"], self.currency),
                    "currency": self.currency})
            # With an order id, what PayPal says when the order is read back decides. Fields the
            # read-back does not report, and a read-back that failed, fall back to the first answer.
            keys = ("order_status", "capture_id", "capture_status")
            first = {k: res.get(k) for k in keys}
            pp = dict(res, env=self.paypal.env, readback=readback, at=now_iso())
            if isinstance(readback, dict) and not readback.get("error"):
                pp.update({k: readback[k] for k in keys if readback.get(k)})
                if any(pp.get(k) != first[k] for k in keys):
                    pp["first_answer"] = first
            cart["paypal"] = pp
            status = payment_status(pp.get("order_status"), pp.get("capture_status"))
            seen = "PayPal %s / %s" % (pp.get("order_status"), pp.get("capture_status"))
            data = {"cart_id": cart_id, "amount": value_str(cart["price_minor"], self.currency),
                    "currency": self.currency, "order_id": pp.get("order_id"), "capture_id": pp.get("capture_id"),
                    "order_status": pp.get("order_status"), "capture_status": pp.get("capture_status"),
                    "env": self.paypal.env}
            if status in ("NOT_EXECUTED", "PAYMENT_UNKNOWN"):
                msg = "PayPal order status %s, capture status %s: not marked as paid." % (
                    pp.get("order_status"), pp.get("capture_status"))
                if status == "PAYMENT_UNKNOWN":
                    msg += " " + PAYMENT_UNKNOWN_HINT
                return self._unpaid(cart, status, msg, seen, data)
            cart["status"] = status
            self.ledger.append("PAYPAL_CAPTURE", "%s %s · %s" % (
                cart["sku"], display(cart["price_minor"], self.currency), seen), data)
            self.save()
            left = self.mandate_view()["budget"]["left"]
        self.emit("receipt", cart_id=cart_id, sku=cart["sku"], title=item["title"],
                  price=display(cart["price_minor"], self.currency), order_id=pp.get("order_id"),
                  capture_id=pp.get("capture_id"), order_status=pp.get("order_status"),
                  capture_status=pp.get("capture_status"), env=self.paypal.env)
        return {"status": status, "sku": cart["sku"],
                "amount": display(cart["price_minor"], self.currency), "paypal_order_id": pp.get("order_id"),
                "budget_left": display(to_minor(left, self.currency), self.currency)}

    # ------------------------------------------------------------------
    # approvals (human present)
    # ------------------------------------------------------------------
    def _pending(self, cart_id: str) -> dict:
        cart = self.state["carts"].get(str(cart_id or ""))
        if not cart:
            raise GatewayError("Unknown purchase.", 404)
        if cart["status"] != "PENDING_APPROVAL":
            raise GatewayError("This purchase is not waiting for approval (status %s)." % cart["status"], 409)
        return cart

    def prepare_approval(self, cart_id: str, key_id: str) -> dict:
        with self.lock:
            cart = self._pending(cart_id)
            m = self.state["mandate"]
            if not m:
                raise GatewayError("There is no mandate.")
            self._device(key_id)
            if key_id != m["key_id"]:
                raise GatewayError("Approve on the device that signed the mandate (key %s)." % m["key_id"])
            record = copy.deepcopy(cart["cart_mandate"]["record"])
            record["presence"] = "HUMAN_PRESENT"
            record["approved_at"] = now_iso()
            record["approver_key_id"] = key_id
            signing = CART_CONTEXT + canonical_json(record)
            cart["prepared_approval"] = {"record": record, "signed_string": signing}
            # Show the traveler the checks as they are now, not as they were.
            item = dict(cart["item"], price_minor=cart["price_minor"])
            now = policy.evaluate(m["record"], self._mandate_status(m), item, self._usage(exclude=cart_id),
                                  self.currency)
            self.save()
            return {"cart_id": cart_id, "record": record, "signing_string": signing, "key_id": key_id,
                    "checks": now["checks"]}

    def approve(self, cart_id: str, signature: str) -> dict:
        with self.lock:
            cart = self._pending(cart_id)
            prep = cart.get("prepared_approval")
            if not prep:
                raise GatewayError("Prepare the approval first.")
            key_id = prep["record"]["approver_key_id"]
            self._verify(key_id, prep["signed_string"], signature)
            dev = self._device(key_id)
            cart["approval"] = {"record": prep["record"], "signed_string": prep["signed_string"],
                                "signature": signature, "public_key": dev["public_key"], "key_id": key_id,
                                "alg": "Ed25519"}
            cart["prepared_approval"] = None
            self.ledger.append("CART_SIGNED", "%s approved and signed by the traveler" % cart["sku"], {
                "cart_id": cart_id, "approver_key_id": key_id,
                "signed_hash": sha256_hex(prep["signed_string"].encode("utf-8")),
                "signature_fingerprint": fingerprint(signature)})
            # Run the hard checks again: the mandate may be revoked or the budget used up.
            m = self.state["mandate"]
            item = dict(cart["item"], price_minor=cart["price_minor"])
            status = self._mandate_status(m) if m and m["record"]["id"] == cart["mandate_id"] else "replaced"
            again = policy.evaluate(m["record"], status, item, self._usage(exclude=cart_id), self.currency)
            hard = [c for c in again["checks"] if not c["ok"] and c["kind"] == policy.HARD]
            again["decided_at"] = now_iso()
            again["result"] = "DENIED" if hard else "APPROVED"
            cart["recheck"] = again
            if hard:
                cart["status"] = "DENIED"
                why = "; ".join(policy._fail_text(c) + " (%s)" % c["detail"] for c in hard)
                again["reason"] = why
                self.ledger.append("CART_DECISION", "%s denied on re-check after approval" % cart["sku"], {
                    "cart_id": cart_id, "result": "DENIED", "stage": "recheck",
                    "failed": [c["key"] for c in hard]})
                self.save()
            else:
                cart["status"] = "PAYING"
                self.save()
        if hard:
            self.emit("decision", cart_id=cart_id, actor="traveler", result="DENIED", sku=cart["sku"],
                      title=cart["item"]["title"], price=display(cart["price_minor"], self.currency),
                      reason="Signed by the traveler, re-checked before payment", why=why, checks=again["checks"])
            return {"status": "DENIED", "why": why}
        self.emit("system", text="%s approved and signed on this device. Paying through PayPal." % cart["item"]["title"])
        return self._pay(cart_id)

    def decline(self, cart_id: str) -> dict:
        with self.lock:
            cart = self._pending(cart_id)
            cart["status"] = "DECLINED"
            self.ledger.append("CART_DECLINED", "%s declined by the traveler" % cart["sku"], {"cart_id": cart_id})
            self.save()
        self.emit("system", text="%s declined. The reserved budget is released." % cart["item"]["title"])
        return {"status": "DECLINED"}

    def refund(self, cart_id: str) -> dict:
        with self.lock:
            cart = self.state["carts"].get(str(cart_id or ""))
            if not cart:
                raise GatewayError("Unknown purchase.", 404)
            if cart["status"] != "PAID" or not (cart.get("paypal") or {}).get("capture_id"):
                raise GatewayError("Only paid purchases can be refunded (status %s)." % cart["status"], 409)
            capture_id = cart["paypal"]["capture_id"]
        try:
            res = self.paypal.refund(capture_id, cart["price_minor"], self.currency, "rf_" + cart_id,
                                     "Refund for %s (cancelled by the traveler)." % cart["item"]["title"])
        except PayPalError as e:
            raise GatewayError("PayPal refund failed: " + e.short(), 502)
        with self.lock:
            cart["refund"] = dict(res, at=now_iso())
            if res.get("status") in ("COMPLETED", "PENDING"):
                cart["status"] = "REFUNDED"
            self.ledger.append("PAYPAL_REFUND", "%s refunded %s · PayPal %s" % (
                cart["sku"], display(cart["price_minor"], self.currency), res.get("status")), {
                "cart_id": cart_id, "refund_id": res.get("refund_id"), "status": res.get("status"),
                "amount": value_str(cart["price_minor"], self.currency), "currency": self.currency,
                "capture_id": capture_id})
            self.save()
        self.emit("system", text="%s refunded through PayPal (%s). The budget is back." % (
            cart["item"]["title"], res.get("status")))
        return {"status": cart["status"], "refund": cart["refund"]}

    # ------------------------------------------------------------------
    # agent runs
    # ------------------------------------------------------------------
    def start_agent(self, text: str) -> dict:
        text = clean_text(text or "").strip()[:1000]
        if not text:
            raise GatewayError("Write a request for the agent.")
        with self.lock:
            if self.agent_running:
                raise GatewayError("The agent is still working on the last request.", 409)
            self.agent_running = True
        try:
            self.emit("user", text=text)
            self.emit("agent_status", running=True, label=self.ai.label)
            t = threading.Thread(target=self._run_agent, args=(text,), daemon=True)
            self.agent_thread = t
            t.start()
        except BaseException:
            # Nothing is running, so the gateway must not look busy for ever.
            with self.lock:
                self.agent_running = False
            raise
        return {"started": True}

    def _run_agent(self, text: str):
        try:
            with self.lock:
                history = list(self.state["history"])
            # Model output is made safe to store before it becomes an event or history.
            final = self.ai.run_agent(history, text, self.call_tool,
                                      lambda t: self.emit("agent_text", text=clean_text(t), interim=True))
            final = clean_text(final or "").strip()
            final = final or "(The agent finished without a summary. See the purchases panel.)"
            self.emit("agent_text", text=final, interim=False)
            with self.lock:
                self.state["history"] += [{"role": "user", "text": text}, {"role": "assistant", "text": final}]
                self.state["history"] = self.state["history"][-MAX_HISTORY * 2:]
                self.save()
        except AIError as e:
            detail = clean_text(e)
            print("  Agent stopped: AI error: %s" % detail)
            self.emit("error", text="Agent stopped. AI error: %s" % detail)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.emit("error", text="Agent error: %s" % clean_text(e))
        finally:
            with self.lock:
                self.agent_running = False
            self.emit("agent_status", running=False)

    def hijack(self) -> dict:
        """Red team: what a prompt-injected agent would do, through the same gateway."""
        actor = "hijacked agent (simulated)"
        self.emit("system", text="Red team: simulating an agent that obeyed the hidden instruction in listing H-399.")
        self.emit("tool", name="search_inventory", actor=actor, summary="search_inventory · hotel · reads H-399 listing",
                  items=[])
        return self.call_tool("request_purchase", {"sku": "H-399",
                                                    "reason": "Listing says the guest pre-approved a suite upgrade"},
                              actor=actor)

    # ------------------------------------------------------------------
    # ledger and evidence
    # ------------------------------------------------------------------
    def ledger_entries(self) -> list:
        with self.lock:
            return copy.deepcopy(self.state["ledger"])

    def verify_ledger(self) -> dict:
        with self.lock:
            return verify_entries(self.state["ledger"])

    def evidence(self, cart_id: str) -> dict:
        with self.lock:
            cart = self.state["carts"].get(str(cart_id or ""))
            if not cart:
                raise GatewayError("Unknown purchase.", 404)
            m = self.state["mandate"]
            if not m or m["record"]["id"] != cart["mandate_id"]:
                raise GatewayError("The mandate for this purchase is no longer held (a new mandate replaced it).", 410)
            entries = self.state["ledger"]
            last = 0
            for i, e in enumerate(entries):
                if (e.get("data") or {}).get("cart_id") == cart_id:
                    last = i + 1
            chain = copy.deepcopy(entries[:last])
            pp = copy.deepcopy(cart.get("paypal"))
            # Evidence is meant to be shared, so the payer's email is masked here
            # (also in the order read back from PayPal).
            for part in (pp, (pp or {}).get("readback")):
                if isinstance(part, dict) and part.get("payer_email"):
                    part["payer_email"] = mask_email(part["payer_email"])
            return {
                "format": "nexbridge-agent-gateway-evidence/v1",
                "generated_at": now_iso(),
                "cart_id": cart_id,
                "status": cart["status"],
                "intent_mandate": {"record": m["record"], "signed_string": m["signed_string"],
                                   "signature": m["signature"], "public_key": m["public_key"],
                                   "key_id": m["key_id"], "alg": "Ed25519", "status": m["status"],
                                   "revoked_at": m["revoked_at"]},
                "cart_mandate": copy.deepcopy(cart["cart_mandate"]),
                "cart_approval": copy.deepcopy(cart.get("approval")),
                "gateway_decision": copy.deepcopy(cart["decision"]),
                "gateway_recheck": copy.deepcopy(cart.get("recheck")),
                "paypal": pp,
                "refund": copy.deepcopy(cart.get("refund")),
                "ledger": {"entries": chain, "head": chain[-1]["hash"] if chain else GENESIS},
            }

    # ------------------------------------------------------------------
    # status and reset
    # ------------------------------------------------------------------
    def carts_view(self) -> list:
        out = []
        for cid in self.state["cart_order"]:
            c = self.state["carts"][cid]
            pp = c.get("paypal") or {}
            out.append({
                "id": cid, "created_at": c["created_at"], "actor": c["actor"], "sku": c["sku"], "item": c["item"],
                "price": display(c["price_minor"], self.currency), "reason": c["reason"], "status": c["status"],
                "decision": {"result": c["decision"]["result"], "reason": c["decision"]["reason"],
                             "checks": c["decision"]["checks"]},
                "recheck": c.get("recheck"),
                "approved": bool(c.get("approval")),
                "paypal": {"order_id": pp.get("order_id"), "capture_id": pp.get("capture_id"),
                           "order_status": pp.get("order_status"), "capture_status": pp.get("capture_status"),
                           "env": pp.get("env")} if pp else None,
                "refund": c.get("refund"), "error": c.get("error"),
                "has_evidence": bool(self.state["mandate"] and self.state["mandate"]["record"]["id"] == c["mandate_id"]),
            })
        return out

    def status(self) -> dict:
        with self.lock:
            conn = self._conn()
            cat = self.catalog
            mandate = self.mandate_view()
            return {
                "paypal": {
                    "mode": self.paypal.env, "label": self.paypal.label, "live": self.paypal.live,
                    "status": conn.get("status") or "disconnected",
                    "payer": mask_email(conn.get("payer_email")) if conn.get("payer_email") else None,
                    "vault_token": mask_tail(conn.get("vault_id")) if conn.get("vault_id") else None,
                    "connected_at": conn.get("connected_at"), "last_error": conn.get("last_error"),
                },
                "ai": {"kind": self.ai.kind, "label": self.ai.label, "model": getattr(self.ai, "model", None)},
                "agent_running": self.agent_running,
                "devices": len(self.state["devices"]),
                "mandate": mandate,
                "carts": self.carts_view(),
                "ledger": {"count": len(self.state["ledger"]),
                           "head": self.state["ledger"][-1]["hash"] if self.state["ledger"] else GENESIS},
                "event_seq": self.state["event_seq"],
                "scenario": cat.scenario(),
                "currency": self.currency,
            }

    def reset(self, everything: bool = False) -> dict:
        with self.lock:
            if self.agent_running:
                raise GatewayError("Wait for the agent to finish before resetting.", 409)
            seq = self.state["event_seq"]
            if everything:
                self.state = {"version": 1, "devices": {}, "paypal": {}}
            for k, v in _empty_session().items():
                self.state[k] = v
            # Event numbers keep counting up so open pages do not miss new events.
            self.state["event_seq"] = seq
            self.ledger = Ledger(self.state["ledger"])
            self.save()
        self.emit("reset", text="Demo reset. PayPal connection and device keys were kept.")
        return {"ok": True}
