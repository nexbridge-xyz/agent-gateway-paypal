"""HTTP layer: host and origin checks, static path containment, security headers."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server
from app import llm
from app.gateway import Gateway
from app.paypal import PayPalError, PayPalSimulated


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        server.Handler.gateway = Gateway(PayPalSimulated(), llm.ScriptedAI(0), os.path.join(cls.tmp, "s.json"))
        server.Handler.base_url = ""
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path, body=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, headers=dict({"Content-Type": "application/json"}, **(headers or {})),
                                   method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_index_and_headers(self):
        status, headers, body = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Agent Gateway", body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, headers, _ = self.req("/api/status")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_static_cannot_escape(self):
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2f.env", "/static/vendor/../../app/gateway.py"):
            status, _, _ = self.req(path)
            self.assertEqual(status, 404, path)
        self.assertEqual(self.req("/static/vendor/ag-grid-community.min.js")[0], 200)

    def test_cross_origin_post_refused(self):
        status, _, _ = self.req("/api/reset", {}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.req("/api/reset", {}, {"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)

    def test_S1_foreign_host_is_refused(self):
        # What a DNS-rebinding page sends: its own name as Host and as Origin.
        evil = "evil.example:%d" % self.port
        status, _, _ = self.req("/api/reset", {}, {"Host": evil, "Origin": "http://" + evil})
        self.assertEqual(status, 421)
        for path in ("/", "/api/status", "/api/ledger", "/static/app.js", "/paypal/connect"):
            self.assertEqual(self.req(path, headers={"Host": evil})[0], 421, path)
        # Another port is another program on this machine; no port is not this server.
        self.assertEqual(self.req("/api/status", headers={"Host": "localhost:1"})[0], 421)
        self.assertEqual(self.req("/api/status", headers={"Host": "localhost"})[0], 421)
        # The health probe carries no data and answers under any name.
        self.assertEqual(self.req("/healthz", headers={"Host": evil})[0], 200)

    def test_local_names_are_served(self):
        for name in ("localhost", "127.0.0.1", "[::1]"):
            host = "%s:%d" % (name, self.port)
            for path in ("/", "/api/status", "/static/app.js"):
                self.assertEqual(self.req(path, headers={"Host": host})[0], 200, (host, path))
            # A page opened under one local name may post under another.
            status, _, _ = self.req("/api/reset", {}, {"Host": host, "Origin": "http://localhost:%d" % self.port})
            self.assertEqual(status, 200, host)

    def test_allowed_hosts(self):
        self.assertEqual(server.allowed_hosts(8000), {"localhost:8000", "127.0.0.1:8000", "[::1]:8000"})
        self.assertIn("localhost", server.allowed_hosts(80))
        self.assertNotIn("localhost", server.allowed_hosts(8000))
        hosted = server.allowed_hosts(8000, "https://Demo.Example.com")
        self.assertTrue({"demo.example.com", "demo.example.com:443", "localhost:8000"} <= hosted)
        self.assertNotIn("demo.example.com:8000", hosted)
        self.assertEqual(server.allowed_hosts(8000, "http://example.org:8080/") - server.allowed_hosts(8000),
                         {"example.org:8080"})

    def test_base_url_host_is_served(self):
        server.Handler.base_url = "https://demo.example.com"
        try:
            self.assertEqual(self.req("/api/status", headers={"Host": "demo.example.com"})[0], 200)
            # Behind a proxy the Host can be the local address while Origin is the public one.
            status, _, _ = self.req("/api/reset", {}, {"Origin": "https://demo.example.com"})
            self.assertEqual(status, 200)
            self.assertEqual(self.req("/api/status", headers={"Host": "other.example.com"})[0], 421)
        finally:
            server.Handler.base_url = ""

    def get(self, path, host):
        """One GET without following redirects: (status, Location header)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": host})
            resp = conn.getresponse()
            resp.read()
            return resp.status, resp.getheader("Location")
        finally:
            conn.close()

    def test_paypal_return_works_under_both_local_names(self):
        g = server.Handler.gateway
        for name in ("localhost", "127.0.0.1"):
            host = "%s:%d" % (name, self.port)
            g.state["paypal"].pop(g.paypal.env, None)  # start disconnected
            with mock.patch.object(g.paypal, "create_setup_token", wraps=g.paypal.create_setup_token) as create:
                status, approve = self.get("/paypal/connect", host)
            self.assertEqual(status, 302)
            # PayPal is told to send the traveler back to the name the page was opened under.
            self.assertEqual(create.call_args[0][0], "http://%s/paypal/return" % host)
            self.assertEqual(self.get(approve, host)[0], 200)
            token = approve.split("token=")[1]
            status, back = self.get("/paypal/return?approval_token_id=%s&simulated=1" % token, host)
            self.assertEqual((status, back), (302, "/?paypal=connected"), host)
            self.assertEqual(g.status()["paypal"]["status"], "connected")

    def test_body_limit(self):
        status, _, _ = self.req("/api/agent", {"text": "x" * 70000})
        self.assertEqual(status, 413)

    def test_sim_approve_page_is_labelled(self):
        status, _, body = self.req("/paypal/sim-approve?token=SIM-ST-1")
        self.assertEqual(status, 200)
        self.assertIn(b"Simulated wallet approval (demo mode)", body)
        self.assertNotIn(b"password", body.lower())

    def test_connect_timeout_shows_clear_error(self):
        g = server.Handler.gateway
        timeout = PayPalError(0, "TIMEOUT", "PayPal did not answer within 40 seconds.")
        with mock.patch.object(g.paypal, "create_setup_token", side_effect=timeout):
            status, _, body = self.req("/paypal/connect")
        self.assertEqual(status, 200)            # redirected back to the app, not a server error
        self.assertNotIn(b"Server error", body)
        self.assertTrue(g.status()["paypal"]["last_error"].startswith("TIMEOUT"))


if __name__ == "__main__":
    unittest.main()
