#!/usr/bin/env python3
"""Agent Gateway for PayPal: HTTP server.

    python3 server.py --demo        simulated PayPal + scripted agent, no keys needed
    python3 server.py               uses .env: PayPal sandbox and/or an AI key when set
    python3 server.py --reset-all   wipe that mode's saved state (PayPal connection, device keys,
                                    ledger); with --demo it wipes the demo's state instead

The sandbox and the demo keep separate state files, so nothing signed in one counts in the other.
Python 3.9+ standard library only.
"""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import socket
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
DATA_DIR = os.path.join(ROOT, "data")
MAX_BODY = 64 * 1024
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def allowed_hosts(port: int, base_url: str = "") -> set:
    """Host header values this server answers. Any other value gets 421.

    A page on another domain that is made to resolve to this machine (DNS rebinding)
    sends its own name as Host and as Origin, so the two matching proves nothing.
    Allowed: localhost, 127.0.0.1 and [::1] with the port the server listens on,
    and the host of BASE_URL.
    """
    names = ("localhost", "127.0.0.1", "[::1]")
    hosts = {"%s:%d" % (name, port) for name in names}
    if port == 80:
        hosts.update(names)  # browsers leave the default port out
    url = urllib.parse.urlsplit(base_url or "")
    if url.hostname:
        name = "[%s]" % url.hostname if ":" in url.hostname else url.hostname
        default = 443 if url.scheme == "https" else 80
        try:
            base_port = url.port or default
        except ValueError:
            base_port = default
        hosts.add("%s:%d" % (name, base_port))
        if base_port == default:
            hosts.add(name)
    return hosts


def env_value(raw: str) -> str:
    """The value of one KEY=VALUE line of a .env file.

    Inside quotes everything is the value, "#" included. Without quotes, " #" starts a
    comment ("PORT=8001  # my port"), while a "#" with no space before it stays in the
    value ("URL=https://example.com/#top").
    """
    def quoted(text):
        return len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'"

    val = raw.strip()
    if quoted(val):
        return val[1:-1]
    val = re.split(r"\s#", raw, maxsplit=1)[0].strip()
    return val[1:-1] if quoted(val) else val  # "value"  # comment


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
                values[key] = env_value(val)
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
    # Seconds a client may stay silent while it sends a request. Without it, a connection
    # that sends nothing would hold a thread for ever. Handling the request is not timed.
    timeout = 15

    # -- helpers -------------------------------------------------------------
    def log_message(self, fmt, *args):
        # Short log lines; query strings are dropped so tokens never reach the log.
        command = getattr(self, "command", None)
        if not command:
            return  # nothing was parsed: a silent connection that timed out, or an unreadable request line
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        if path.startswith("/api/events") or path.startswith("/api/status"):
            return
        sys.stderr.write("  %s %s %s\n" % (command, path, args[1] if len(args) > 1 else ""))

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
        # "replace": an error text from outside with broken Unicode must still reach the page.
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")
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

    def _hosts(self) -> set:
        return allowed_hosts(self.server.server_address[1], self.base_url)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").strip().lower() in self._hosts()

    def _unknown_host(self):
        here = self.base_url or "http://localhost:%d" % self.server.server_address[1]
        self._error("This server does not answer for that host name. Open it at %s, "
                    "or set BASE_URL to the address you use." % here, 421)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            # Browsers send Origin on every cross-site POST; a missing one is same-origin or a non-browser client.
            return True
        return urllib.parse.urlsplit(origin).netloc.lower() in self._hosts()

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            # read(-1) would read until the client closes the connection, with no limit at all.
            raise GatewayError("Content-Length must not be negative.", 400)
        if length > MAX_BODY:
            raise GatewayError("Request body too large.", 413)
        try:
            raw = self.rfile.read(length) if length else b""
        except (socket.timeout, TimeoutError):  # Python 3.9: socket.timeout is not TimeoutError
            raise GatewayError("The request body did not arrive in time.", 408)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            raise GatewayError("Body must be JSON.")
        if not isinstance(data, dict):
            raise GatewayError("Body must be a JSON object.")
        try:
            # "\ud800" in JSON is half of a surrogate pair. It cannot be written as UTF-8,
            # so once it is in the state every later save fails. Refuse it here.
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        except (ValueError, RecursionError):
            raise GatewayError("Body contains text that is not valid Unicode.")
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
            # /healthz carries no data and must answer a hosting platform's probe under any name.
            if not self._host_ok() and self.path.split("?", 1)[0] != "/healthz":
                return self._unknown_host()
            self._get()
        except GatewayError as e:
            self._error(str(e), e.status)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._error("Server error: %s" % e, 500)

    def do_POST(self):
        try:
            if not self._host_ok():
                return self._unknown_host()
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


def paypal_mode(args, env: dict) -> str:
    """Which PayPal the server talks to: "sandbox" when its keys are set and --demo is not given."""
    live = not args.demo and env.get("PAYPAL_CLIENT_ID") and env.get("PAYPAL_CLIENT_SECRET")
    return "sandbox" if live else "simulated"


def state_path(mode: str, data_dir: str | None = None) -> str:
    """The state file of one PayPal mode. Each mode has its own file, so a mandate signed in
    the demo is never there when the sandbox runs. The sandbox keeps the name data/state.json."""
    name = "state.json" if mode == "sandbox" else "state-%s.json" % mode
    return os.path.join(data_dir or DATA_DIR, name)


def wipe_state(path: str) -> bool:
    """Delete one state file, and a half-written copy beside it. True if the file was there."""
    found = os.path.exists(path)
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
    return found


STARTUP_ERROR = "The PayPal credential check failed when the server started: "


def paypal_line(gateway, demo: bool = False) -> str:
    """The PayPal line of the startup banner, after checking the sandbox credentials.

    A failed check is one line here and never a traceback. The reason also goes on the
    PayPal card, so the page says why PayPal will not work, and it is taken off again by
    the next start whose check succeeds. Only that line is taken off: a line that
    something else left on the card, such as a refund without a clear answer, stays.
    """
    if not gateway.paypal.live:
        return "PayPal simulated" + (" (--demo)" if demo else " (no PAYPAL_CLIENT_ID / SECRET in .env)")
    try:
        gateway.paypal.check_credentials()
    except PayPalError as e:
        gateway.paypal_error(STARTUP_ERROR + e.short())
        return "PayPal Sandbox (credential check FAILED: %s)" % e.short()
    gateway.paypal_clear_error(STARTUP_ERROR)
    return "PayPal Sandbox (credentials OK)"


def build(args, env: dict, data_dir: str | None = None):
    if paypal_mode(args, env) == "sandbox":
        paypal = PayPalSandbox(env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"], "sandbox")
    else:
        paypal = PayPalSimulated()
    ai = llm.from_env(env, demo=args.demo)
    return Gateway(paypal, ai, state_path(paypal.env, data_dir))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agent Gateway for PayPal")
    ap.add_argument("--demo", action="store_true", help="simulated PayPal and scripted agent (no keys needed)")
    ap.add_argument("--reset-all", action="store_true",
                    help="wipe this mode's saved state, including the PayPal connection (with --demo: the demo's)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    env = load_env(os.path.join(ROOT, ".env"))
    if args.reset_all:
        # Only the file of the mode this command line selects. The other mode's file is left alone.
        mode = paypal_mode(args, env)
        path = state_path(mode)
        shown = os.path.relpath(path, ROOT)
        print(("Saved state wiped (%s)." if wipe_state(path) else "No saved state to wipe (%s).") % shown)
        if mode == "sandbox":
            print("That is the PayPal sandbox state. To wipe the demo's state, add --demo.")
        else:
            print("That is the demo's state. The PayPal sandbox state (data/state.json) was not touched.")
        return

    host = args.host or env.get("HOST") or "127.0.0.1"
    port = args.port or int(env.get("PORT") or 8000)
    gateway = build(args, env)
    Handler.gateway = gateway
    base = env.get("BASE_URL") or env.get("RENDER_EXTERNAL_URL") or ""
    Handler.base_url = base.rstrip("/")

    pp_line = paypal_line(gateway, args.demo)
    shown = Handler.base_url or "http://%s:%d" % ("localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    print("")
    print("  Agent Gateway for PayPal  ·  by NexBridge")
    print("  ----------------------------------------")
    print("  PayPal : %s" % pp_line)
    print("  AI     : %s" % gateway.ai.label)
    print("  Open   : %s" % shown)
    if host not in ("127.0.0.1", "localhost", "::1") and not Handler.base_url:
        print("  Hosts  : only localhost requests are answered. Set BASE_URL to serve another address.")
    print("  State  : %s   (Ctrl+C to stop)" % os.path.relpath(gateway.state_path, ROOT))
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
