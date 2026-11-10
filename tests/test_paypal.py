"""PayPal client details that do not need the network."""
import socket
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from app.paypal import PayPalError, PayPalSandbox


class TokenTest(unittest.TestCase):
    def test_ignore_cache_only_on_first_token(self):
        pp = PayPalSandbox("id", "secret")
        sent = []

        def fake_open(req):
            if req.full_url.endswith("/v1/oauth2/token"):
                sent.append(dict(urllib.parse.parse_qsl(req.data.decode())))
                return {"access_token": "tok%d" % len(sent), "expires_in": 32400}
            return {}

        with mock.patch.object(pp, "_open", side_effect=fake_open):
            pp.check_credentials()
            pp.check_credentials()            # still valid: no new request
            self.assertEqual(len(sent), 1)
            pp._token_exp = time.time() - 1   # expired: ask again, from PayPal's cache
            pp.check_credentials()
        self.assertEqual(sent[0], {"grant_type": "client_credentials", "ignoreCache": "true"})
        self.assertEqual(sent[1], {"grant_type": "client_credentials"})


class TimeoutTest(unittest.TestCase):
    def call(self, exc):
        pp = PayPalSandbox("id", "secret", timeout=7)
        req = urllib.request.Request("https://api-m.sandbox.paypal.com/v2/checkout/orders")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(PayPalError) as ctx:
                pp._open(req)
        return ctx.exception

    def test_read_timeout_becomes_timeout_error(self):
        e = self.call(socket.timeout("timed out"))
        self.assertEqual(e.name, "TIMEOUT")
        self.assertIn("7 seconds", e.short())

    def test_connect_timeout_becomes_timeout_error(self):
        e = self.call(urllib.error.URLError(socket.timeout("timed out")))
        self.assertEqual(e.name, "TIMEOUT")

    def test_other_network_errors_stay_network_error(self):
        e = self.call(urllib.error.URLError(ConnectionRefusedError("refused")))
        self.assertEqual(e.name, "NETWORK_ERROR")


if __name__ == "__main__":
    unittest.main()
