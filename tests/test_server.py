"""HTTP layer: host and origin checks, static path containment, security headers. Also the state files."""
import argparse
import http.client
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
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

    def raw(self, data, wait=5):
        """Send bytes exactly as given; return (status code or None, everything the server sent)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        out = b""
        try:
            s.sendall(data)
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
        finally:
            s.close()
        parts = out.split()
        return (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None), out

    def post_head(self, path, length):
        return ("POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                "Content-Length: %s\r\n\r\n" % (path, self.port, length)).encode()

    def test_S5_negative_content_length(self):
        # Before the fix the server called read(-1): it read until the client closed, with no limit.
        # No body is sent here, so a server that waits for one shows up as a missing answer.
        status, _ = self.raw(self.post_head("/api/agent", -1), wait=3)
        self.assertEqual(status, 400)
        # With a body far over the limit: nothing of it is read or acted on.
        g = server.Handler.gateway
        seq = g.state["event_seq"]
        try:
            self.raw(self.post_head("/api/agent", -1) + b'{"text":"' + b"x" * 300000 + b'"}', wait=3)
        except OSError:
            pass  # the server hangs up while the body is still being sent
        time.sleep(0.3)
        self.assertEqual(g.state["event_seq"], seq)

    def test_S4_lone_surrogate_is_refused_and_the_server_stays_usable(self):
        g = server.Handler.gateway
        body = b'{"text":"hello \\ud800"}'            # JSON escape for half a surrogate pair
        status, out = self.raw(self.post_head("/api/agent", len(body)) + body)
        self.assertEqual(status, 400)
        self.assertIn(b"not valid Unicode", out)
        self.assertFalse(g.agent_running)
        for path in ("/api/mandate/draft", "/api/device/register"):
            body = b'{"instruction":"\\udfff","public_key":"\\ud800"}'
            self.assertEqual(self.raw(self.post_head(path, len(body)) + body)[0], 400, path)
        # Nothing unsaveable got in: polling works, and the agent can still be started and finishes.
        self.assertEqual(self.req("/api/events?after=0")[0], 200)
        self.assertEqual(self.req("/api/agent", {"text": "Book my trip"})[0], 200)
        g.agent_thread.join(10)
        self.assertFalse(g.agent_running)
        self.assertEqual(self.req("/api/reset", {})[0], 200)

    def test_deeply_nested_json_is_a_400(self):
        body = b"[" * 60000
        self.assertEqual(self.raw(self.post_head("/api/agent", len(body)) + body)[0], 400)

    def test_silent_clients_are_dropped(self):
        self.assertEqual(server.Handler.timeout, 15)
        server.Handler.timeout = 0.4
        try:
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                # Headers that promise a body which never comes.
                self.assertEqual(self.raw(self.post_head("/api/agent", 10), wait=5)[0], 408)
                # A connection that sends nothing at all is closed without an answer.
                self.assertEqual(self.raw(b"", wait=5), (None, b""))
                # An unreadable request line gets an error page, not a dropped connection.
                self.assertIn(b"Error code: 400", self.raw(b"GARBAGE\r\n\r\n", wait=5)[1])
            self.assertNotIn("Traceback", err.getvalue())
        finally:
            server.Handler.timeout = 15

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


class StateFileTest(unittest.TestCase):
    """One state file per PayPal mode. Nothing here touches the real data/ directory."""
    KEYS = {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "secret"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paypal_mode(self):
        demo, plain = argparse.Namespace(demo=True), argparse.Namespace(demo=False)
        self.assertEqual(server.paypal_mode(plain, self.KEYS), "sandbox")
        self.assertEqual(server.paypal_mode(demo, self.KEYS), "simulated")      # --demo wins over keys
        self.assertEqual(server.paypal_mode(plain, {}), "simulated")
        self.assertEqual(server.paypal_mode(plain, {"PAYPAL_CLIENT_ID": "id"}), "simulated")

    def test_state_path(self):
        self.assertEqual(server.state_path("sandbox", self.tmp), os.path.join(self.tmp, "state.json"))
        self.assertEqual(server.state_path("simulated", self.tmp), os.path.join(self.tmp, "state-simulated.json"))
        # The sandbox keeps the file it always had.
        self.assertEqual(server.state_path("sandbox"), os.path.join(server.ROOT, "data", "state.json"))
        self.assertEqual(os.path.dirname(server.state_path("simulated")), os.path.join(server.ROOT, "data"))

    def test_wipe_state_deletes_one_mode(self):
        for name in ("state.json", "state-simulated.json", "state-simulated.json.tmp"):
            with open(os.path.join(self.tmp, name), "w") as f:
                f.write("{}")
        self.assertTrue(server.wipe_state(server.state_path("simulated", self.tmp)))
        self.assertEqual(os.listdir(self.tmp), ["state.json"])
        self.assertFalse(server.wipe_state(server.state_path("simulated", self.tmp)))  # nothing left to wipe
        self.assertEqual(os.listdir(self.tmp), ["state.json"])

    def test_reset_all_wipes_only_the_mode_it_is_run_in(self):
        # main() runs with the .env reader and the deletion replaced, so no file is read or removed.
        for argv, env, mode in ((["--reset-all"], self.KEYS, "sandbox"),
                                (["--demo", "--reset-all"], self.KEYS, "simulated"),
                                (["--reset-all"], {}, "simulated")):
            with self.subTest(argv=argv, mode=mode):
                with mock.patch.object(server, "load_env", return_value=dict(env)), \
                        mock.patch.object(server, "wipe_state", return_value=True) as wipe, \
                        mock.patch.object(server, "build") as build, \
                        mock.patch("sys.stdout", new=io.StringIO()) as out:
                    server.main(argv)
                wipe.assert_called_once_with(server.state_path(mode))
                build.assert_not_called()                                              # the server is not started
                self.assertIn(os.path.join("data", os.path.basename(server.state_path(mode))), out.getvalue())


class EnvFileTest(unittest.TestCase):
    """The .env reader. Every file here is written to a temporary directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, text, environ=None):
        path = os.path.join(self.tmp, "sample.env")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        with mock.patch.dict(os.environ, environ or {}, clear=True):
            return server.load_env(path)

    def test_a_comment_after_a_value_is_dropped(self):
        env = self.load("PORT=8001   # my port\nHOST=127.0.0.1\t# after a tab\nEMPTY= # nothing set\nBASE_URL=\n")
        self.assertEqual((env["PORT"], env["HOST"], env["EMPTY"], env["BASE_URL"]), ("8001", "127.0.0.1", "", ""))
        self.assertEqual(int(env["PORT"]), 8001)    # this used to stop the server at startup

    def test_a_hash_with_no_space_before_it_stays(self):
        env = self.load("URL=https://example.com/#top\nCOLOR=#ff0000\nTOKEN=abc#def # a comment\n")
        self.assertEqual((env["URL"], env["COLOR"], env["TOKEN"]), ("https://example.com/#top", "#ff0000", "abc#def"))

    def test_quotes_keep_everything_inside(self):
        env = self.load('A="x # y"\nB=\'p # q\'\nC="quoted"   # then a comment\nD=  "spaces around"  \n')
        self.assertEqual((env["A"], env["B"], env["C"], env["D"]), ("x # y", "p # q", "quoted", "spaces around"))

    def test_other_lines_work_as_before(self):
        env = self.load("# a comment line\n\nexport NAME=value\nno equals sign here\n  INDENTED = padded  \n",
                        {"NAME": "from the real environment", "ONLY_REAL": "1"})
        self.assertEqual(env["NAME"], "from the real environment")     # real environment variables win
        self.assertEqual((env["INDENTED"], env["ONLY_REAL"]), ("padded", "1"))
        self.assertNotIn("no equals sign here", env)
        self.assertEqual(server.load_env(os.path.join(self.tmp, "missing.env")).get("NAME"), os.environ.get("NAME"))

    def test_every_example_line_works_when_its_hash_is_removed(self):
        # .env.example is the template people copy. A commented example such as
        # "# OPENAI_BASE_URL=..." must give a usable value once the leading "#" is taken away.
        with open(os.path.join(server.ROOT, ".env.example"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        examples = [m.groups() for m in (re.match(r"^#\s*([A-Z][A-Z0-9_]*)=(.+)$", line) for line in lines) if m]
        self.assertIn("OPENAI_BASE_URL", [key for key, _ in examples])
        for key, raw in examples:
            self.assertNotRegex(server.env_value(raw), r"\s", "%s would get a value with a space in it" % key)
        self.assertEqual(server.env_value(dict(examples)["OPENAI_BASE_URL"]), "https://api.openai.com/v1")
        self.assertTrue(any("DeepSeek" in line and "https://api.deepseek.com/v1" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
