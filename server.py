#!/usr/bin/env python3
"""Agent Gateway for PayPal: HTTP server.

    python3 server.py --demo        simulated PayPal + scripted agent, no keys needed
    python3 server.py               uses .env: PayPal sandbox and/or an AI key when set
    python3 server.py --reset-all   wipe saved state (PayPal connection, device keys, ledger)

Python 3.9+ standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import sys
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from app import llm  # noqa: E402
from app.gateway import Gateway, GatewayError  # noqa: E402
from app.paypal import VAULT_HINT, PayPalError, PayPalSandbox, PayPalSimulated  # noqa: E402

STATIC = os.path.join(ROOT, "static")
MAX_BODY = 64 * 1024
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def load_env(path: str) -> dict:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Real env vars win."""
    values = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                values[key] = val
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    for key, val in os.environ.items():
        values.setdefault(key, val)
    return values


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentGateway/1.0"
    gateway: Gateway = None
    base_url: str = ""

    # -- helpers -------------------------------------------------------------
    def log_message(self, fmt, *args):
        # Short log lines; query strings are dropped so tokens never reach the log.
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/events") or path.startswith("/api/status"):
            return
        sys.stderr.write("  %s %s %s\n" % (self.command, path, args[1] if len(args) > 1 else ""))

    def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None, api: bool = True):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store" if api else "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, obj, status: int = 200, extra: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), extra)
        self.wfile.write(body)

    def _html(self, text: str, status: int = 200):
        body = text.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _error(self, message: str, status: int = 400):
        self._json({"error": message}, status)

    def _base(self) -> str:
        if self.base_url:
            return self.base_url
        host = self.headers.get("Host") or "localhost:8000"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return "%s://%s" % (proto, host)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            # Browsers send Origin on every cross-site POST; a missing one is same-origin or a non-browser client.
            return True
        host = self.headers.get("Host") or ""
        allowed = {urllib.parse.urlsplit(self._base()).netloc, host}
        return urllib.parse.urlsplit(origin).netloc in allowed

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise GatewayError("Request body too large.", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("Body must be JSON.")
        if not isinstance(data, dict):
            raise GatewayError("Body must be a JSON object.")
        return data

    # -- static ----------------------------------------------------------------
    def _static(self, rel: str):
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self._error("Not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        self._headers(200, ctype, len(body), api=False)
        self.wfile.write(body)

    # -- routing -----------------------------------------------------------------
    def do_GET(self):
        try:
            self._get()
        except GatewayError as e:
            self._error(str(e), e.status)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error("Server error: %s" % e, 500)

    def do_POST(self):
        try:
            if not self._origin_ok():
                return self._error("Cross-origin request refused.", 403)
            self._post()
        except GatewayError as e:
            self._error(str(e), e.status)
        except PayPalError as e:
            self._error("PayPal error: " + e.short(), 502)
        except llm.AIError as e:
            self._error("AI error: %s" % e, 502)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error("Server error: %s" % e, 500)

    def _get(self):
        g = self.gateway
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/favicon.ico":
            return self._static("favicon.svg")
        if path.startswith("/static/"):
            return self._static(urllib.parse.unquote(path[len("/static/"):]))
        if path == "/healthz":
            return self._json({"ok": True})
        if path == "/api/status":
            return self._json(g.status())
        if path == "/api/events":
            try:
                after = int(query.get("after") or 0)
            except ValueError:
                after = 0
            return self._json({"events": g.events_after(after), "agent_running": g.agent_running})
        if path == "/api/ledger":
            return self._json({"entries": g.ledger_entries()})
        if path == "/api/ledger/verify":
            return self._json(g.verify_ledger())
        if path.startswith("/api/evidence/"):
            cart_id = path.rsplit("/", 1)[-1]
            bundle = g.evidence(cart_id)
            extra = {}
            if query.get("download"):
                extra["Content-Disposition"] = 'attachment; filename="evidence-%s.json"' % cart_id
            return self._json(bundle, extra=extra)
        if path == "/paypal/connect":
            try:
                return self._redirect(g.paypal_begin(self._base()))
            except PayPalError as e:
                msg = e.short()
                if e.status in (401, 403) or "vault" in msg.lower() or e.name in ("NOT_AUTHORIZED", "PERMISSION_DENIED"):
                    msg += " · " + VAULT_HINT
                g.paypal_error(msg)
                return self._redirect("/?paypal=error")
        if path == "/paypal/return":
            # Log only the parameter names on the way back from PayPal (values may be tokens).
            sys.stderr.write("  PayPal return parameters: %s\n" % (", ".join(sorted(query)) or "none"))
            try:
                g.paypal_finish(query)
            except PayPalError as e:
                g.paypal_error(e.short())
                return self._redirect("/?paypal=error")
            except GatewayError as e:
                g.paypal_error(str(e))
                return self._redirect("/?paypal=error")
            return self._redirect("/?paypal=connected")
        if path == "/paypal/cancel":
            g.paypal_cancel()
            return self._redirect("/?paypal=cancelled")
        if path == "/paypal/sim-approve":
            if g.paypal.live:
                return self._error("Not found", 404)
            return self._html(sim_approve_page(query.get("token") or ""))
        return self._error("Not found", 404)

    def _post(self):
        g = self.gateway
        path = urllib.parse.urlsplit(self.path).path
        body = self._body()
        parts = path.strip("/").split("/")
        if path == "/api/device/register":
            return self._json(g.register_device(body.get("public_key")))
        if path == "/api/mandate/draft":
            return self._json(g.draft_mandate(body.get("instruction")))
        if path == "/api/mandate/prepare":
            return self._json(g.prepare_mandate(body.get("instruction"), body.get("fields") or {}, body.get("key_id")))
        if path == "/api/mandate/activate":
            return self._json({"mandate": g.activate_mandate(body.get("mandate_id"), body.get("signature"))})
        if path == "/api/mandate/revoke":
            return self._json({"mandate": g.revoke_mandate()})
        if path == "/api/agent":
            return self._json(g.start_agent(body.get("text")))
        if len(parts) == 4 and parts[:2] == ["api", "approvals"]:
            cart_id, action = parts[2], parts[3]
            if action == "prepare":
                return self._json(g.prepare_approval(cart_id, body.get("key_id")))
            if action == "approve":
                return self._json(g.approve(cart_id, body.get("signature")))
            if action == "decline":
                return self._json(g.decline(cart_id))
        if len(parts) == 4 and parts[:2] == ["api", "purchases"] and parts[3] == "refund":
            return self._json(g.refund(parts[2]))
        if path == "/api/redteam/hijack":
            return self._json(g.hijack())
        if path == "/api/reset":
            return self._json(g.reset())
        return self._error("Not found", 404)


def sim_approve_page(token: str) -> str:
    t = html.escape(token)
    q = urllib.parse.quote(token)
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulated wallet approval (demo mode)</title>
<link rel="stylesheet" href="/static/styles.css"></head>
<body class="sim-page">
<main class="sim-card">
  <div class="sim-tag">DEMO MODE · NO REAL WALLET</div>
  <h1>Simulated wallet approval (demo mode)</h1>
  <p>This page stands in for the PayPal approval step when the app runs without sandbox keys.
  Nothing here is PayPal, and no account or money is involved.</p>
  <dl>
    <dt>Merchant</dt><dd>Tabi Travel (demo)</dd>
    <dt>Request</dt><dd>Save a wallet so the AI travel agent can pay within the limits you sign.</dd>
    <dt>Setup token</dt><dd class="mono">%s</dd>
  </dl>
  <div class="sim-actions">
    <a class="btn primary" href="/paypal/return?approval_token_id=%s&amp;simulated=1">Approve</a>
    <a class="btn ghost" href="/paypal/cancel">Cancel</a>
  </div>
</main></body></html>""" % (t, q)


def build(args, env: dict):
    if args.demo:
        paypal = PayPalSimulated()
    elif env.get("PAYPAL_CLIENT_ID") and env.get("PAYPAL_CLIENT_SECRET"):
        paypal = PayPalSandbox(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"], "sandbox")
    else:
        paypal = PayPalSimulated()
    ai = llm.from_env(env, demo=args.demo)
    state_path = os.path.join(ROOT, "data", "state.json")
    return Gateway(paypal, ai, state_path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agent Gateway for PayPal")
    ap.add_argument("--demo", action="store_true", help="simulated PayPal and scripted agent (no keys needed)")
    ap.add_argument("--reset-all", action="store_true", help="wipe saved state, including the PayPal connection")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    env = load_env(os.path.join(ROOT, ".env"))
    if args.reset_all:
        path = os.path.join(ROOT, "data", "state.json")
        if os.path.exists(path):
            os.remove(path)
        print("Saved state wiped (data/state.json).")
        return

    host = args.host or env.get("HOST") or "127.0.0.1"
    port = args.port or int(env.get("PORT") or 8000)
    gateway = build(args, env)
    Handler.gateway = gateway
    base = env.get("BASE_URL") or env.get("RENDER_EXTERNAL_URL") or ""
    Handler.base_url = base.rstrip("/")

    if gateway.paypal.live:
        try:
            gateway.paypal.check_credentials()
            pp_line = "PayPal Sandbox (credentials OK)"
        except PayPalError as e:
            pp_line = "PayPal Sandbox (credential check FAILED: %s)" % e.short()
    else:
        pp_line = "PayPal simulated" + (" (--demo)" if args.demo else " (no PAYPAL_CLIENT_ID / SECRET in .env)")
    shown = Handler.base_url or "http://%s:%d" % ("localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    print("")
    print("  Agent Gateway for PayPal  ·  by NexBridge")
    print("  ----------------------------------------")
    print("  PayPal : %s" % pp_line)
    print("  AI     : %s" % gateway.ai.label)
    print("  Open   : %s" % shown)
    print("  State  : data/state.json   (Ctrl+C to stop)")
    print("")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
