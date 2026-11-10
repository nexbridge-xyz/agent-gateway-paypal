"""HTTP layer: origin check, static path containment, security headers."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server
from app import llm
from app.gateway import Gateway
from app.paypal import PayPalSimulated


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

    def test_body_limit(self):
        status, _, _ = self.req("/api/agent", {"text": "x" * 70000})
        self.assertEqual(status, 413)

    def test_sim_approve_page_is_labelled(self):
        status, _, body = self.req("/paypal/sim-approve?token=SIM-ST-1")
        self.assertEqual(status, 200)
        self.assertIn(b"Simulated wallet approval (demo mode)", body)
        self.assertNotIn(b"password", body.lower())


if __name__ == "__main__":
    unittest.main()
