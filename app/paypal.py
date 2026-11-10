"""PayPal REST client (sandbox) and a local simulator with the same interface.

Live calls used:
  POST /v1/oauth2/token                      access token
  POST /v3/vault/setup-tokens                save PayPal once (payer approves on PayPal)
  POST /v3/vault/payment-tokens              turn the approved setup token into a vault token
  POST /v2/checkout/orders  (vault_id)       pay later without the payer present -> COMPLETED
  GET  /v2/checkout/orders/{id}              read the order back for the evidence record
  POST /v2/payments/captures/{id}/refund     refund a booking
Every write sends PayPal-Request-Id, so a retry can never charge twice.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import value_str


# Shown after PayPal permission errors. Same wording as README → Troubleshooting.
VAULT_HINT = ("If Vault is not enabled for the sandbox app: open the app, check Save payment methods "
              "under Payment capabilities, click Save Changes, then restart the server and try again.")


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id=None, details=None):
        super().__init__("%s: %s" % (name, message))
        self.status = status
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.details = details or []

    def short(self) -> str:
        extra = ""
        if self.details:
            d = self.details[0]
            extra = " · %s %s" % (d.get("issue", ""), d.get("description", ""))
        dbg = " (debug id %s)" % self.debug_id if self.debug_id else ""
        return "%s: %s%s%s" % (self.name, self.message, extra, dbg)


def _parse_capture(res: dict) -> dict:
    units = res.get("purchase_units") or [{}]
    caps = ((units[0].get("payments") or {}).get("captures") or [])
    cap = caps[0] if caps else {}
    src = (res.get("payment_source") or {}).get("paypal") or {}
    payer = res.get("payer") or {}
    return {
        "order_id": res.get("id"),
        "order_status": res.get("status"),
        "capture_id": cap.get("id"),
        "capture_status": cap.get("status"),
        "payer_email": src.get("email_address") or payer.get("email_address"),
    }


class PayPalSandbox:
    live = True

    def __init__(self, client_id: str, client_secret: str, env: str = "sandbox", timeout: int = 40):
        self.env = "live" if env == "live" else "sandbox"
        self.base = "https://api-m.paypal.com" if self.env == "live" else "https://api-m.sandbox.paypal.com"
        self.label = "PayPal " + ("Live" if self.env == "live" else "Sandbox")
        self._id = client_id
        self._secret = client_secret
        self._timeout = timeout
        self._token = None
        self._token_exp = 0.0
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------
    def _open(self, req):
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"message": raw[:300].decode("utf-8", "replace")}
            raise PayPalError(e.code, body.get("name") or body.get("error") or "HTTP_%d" % e.code,
                              body.get("message") or body.get("error_description") or "",
                              body.get("debug_id"), body.get("details"))
        except urllib.error.URLError as e:
            raise PayPalError(0, "NETWORK_ERROR", str(e.reason))

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            auth = base64.b64encode(("%s:%s" % (self._id, self._secret)).encode()).decode()
            form = {"grant_type": "client_credentials"}
            if self._token is None:
                # First token after start: PayPal otherwise returns its cached token until it expires,
                # which may predate a change to the app's permissions (for example enabling Vault).
                form["ignoreCache"] = "true"
            req = urllib.request.Request(
                self.base + "/v1/oauth2/token",
                data=urllib.parse.urlencode(form).encode(),
                headers={"Authorization": "Basic " + auth, "Accept": "application/json",
                         "Content-Type": "application/x-www-form-urlencoded"},
                method="POST")
            body = self._open(req)
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3000))
            return self._token

    def _call(self, method: str, path: str, body=None, request_id=None) -> dict:
        headers = {"Authorization": "Bearer " + self._access_token(), "Accept": "application/json",
                   "Content-Type": "application/json", "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        data = json.dumps(body).encode() if body is not None else None
        return self._open(urllib.request.Request(self.base + path, data=data, headers=headers, method=method))

    def check_credentials(self) -> None:
        self._access_token()

    # -- vault: save PayPal once ------------------------------------------
    def create_setup_token(self, return_url: str, cancel_url: str, brand_name: str, request_id: str) -> dict:
        body = {"payment_source": {"paypal": {
            "description": "Lets your AI travel agent pay within the limits you sign.",
            "permit_multiple_payment_tokens": False,
            "usage_type": "MERCHANT",
            "customer_type": "CONSUMER",
            "experience_context": {"brand_name": brand_name[:127], "locale": "en-US",
                                   "return_url": return_url, "cancel_url": cancel_url},
        }}}
        res = self._call("POST", "/v3/vault/setup-tokens", body, request_id)
        approve = next((l.get("href") for l in res.get("links") or []
                        if l.get("rel") in ("approve", "payer-action")), None)
        return {"id": res.get("id"), "status": res.get("status"), "approve_url": approve}

    def create_payment_token(self, setup_token_id: str, request_id: str) -> dict:
        body = {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}}
        res = self._call("POST", "/v3/vault/payment-tokens", body, request_id)
        pp = (res.get("payment_source") or {}).get("paypal") or {}
        return {"vault_id": res.get("id"), "customer_id": (res.get("customer") or {}).get("id"),
                "payer_email": pp.get("email_address"), "payer_id": pp.get("payer_id")}

    # -- payments ------------------------------------------------------------
    def charge_vault(self, vault_id: str, amount_minor: int, currency: str, item_name: str, sku: str,
                     description: str, invoice_id: str, custom_id: str, request_id: str) -> dict:
        value = value_str(amount_minor, currency)
        money = {"currency_code": currency, "value": value}
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": invoice_id[:256],
                "description": description[:127],
                "custom_id": custom_id[:127],
                "invoice_id": invoice_id[:127],
                "amount": dict(money, breakdown={"item_total": dict(money)}),
                "items": [{"name": item_name[:127], "sku": sku[:127], "quantity": "1",
                           "unit_amount": dict(money), "category": "DIGITAL_GOODS"}],
            }],
            "payment_source": {"paypal": {"vault_id": vault_id}},
        }
        return _parse_capture(self._call("POST", "/v2/checkout/orders", body, request_id))

    def get_order(self, order_id: str) -> dict:
        return _parse_capture(self._call("GET", "/v2/checkout/orders/" + urllib.parse.quote(order_id)))

    def refund(self, capture_id: str, amount_minor: int, currency: str, request_id: str, note: str) -> dict:
        body = {"amount": {"currency_code": currency, "value": value_str(amount_minor, currency)},
                "note_to_payer": note[:255]}
        res = self._call("POST", "/v2/payments/captures/%s/refund" % urllib.parse.quote(capture_id),
                         body, request_id)
        return {"refund_id": res.get("id"), "status": res.get("status")}


class PayPalSimulated:
    """Same interface, no network. Used when no sandbox keys are set (demo mode)."""
    live = False

    def __init__(self):
        self.env = "simulated"
        self.label = "PayPal (simulated)"
        self._done = {}
        self._orders = {}

    @staticmethod
    def _hex(n: int) -> str:
        return secrets.token_hex(n).upper()

    def check_credentials(self) -> None:
        return None

    def create_setup_token(self, return_url, cancel_url, brand_name, request_id):
        token = "SIM-ST-" + self._hex(6)
        return {"id": token, "status": "PAYER_ACTION_REQUIRED",
                "approve_url": "/paypal/sim-approve?token=" + token}

    def create_payment_token(self, setup_token_id, request_id):
        return {"vault_id": "sim" + secrets.token_hex(4), "customer_id": "sim_customer_" + secrets.token_hex(3),
                "payer_email": "traveler@example.com", "payer_id": "SIMPAYER" + self._hex(2)}

    def charge_vault(self, vault_id, amount_minor, currency, item_name, sku, description,
                     invoice_id, custom_id, request_id):
        if request_id in self._done:  # same idempotency behaviour as PayPal
            return self._done[request_id]
        time.sleep(0.4)
        res = {"order_id": "SIM" + self._hex(8), "order_status": "COMPLETED",
               "capture_id": "SIMCAP" + self._hex(6), "capture_status": "COMPLETED",
               "payer_email": "traveler@example.com"}
        self._done[request_id] = res
        self._orders[res["order_id"]] = res
        return res

    def get_order(self, order_id):
        return dict(self._orders.get(order_id) or {"order_id": order_id, "order_status": "UNKNOWN"})

    def refund(self, capture_id, amount_minor, currency, request_id, note):
        if request_id in self._done:
            return self._done[request_id]
        res = {"refund_id": "SIMRF" + self._hex(6), "status": "COMPLETED"}
        self._done[request_id] = res
        return res
