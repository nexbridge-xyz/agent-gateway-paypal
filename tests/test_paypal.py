"""PayPal client details that do not need the network."""
import io
import socket
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from app.paypal import PayPalError, PayPalSandbox, PayPalSimulated


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


class SentTest(unittest.TestCase):
    """Whether PayPal can have received a request decides if a failed payment frees its budget."""

    def call(self, exc):
        pp = PayPalSandbox("id", "secret", timeout=7)
        req = urllib.request.Request("https://api-m.sandbox.paypal.com/v2/checkout/orders")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(PayPalError) as ctx:
                pp._open(req)
        return ctx.exception

    def test_errors_while_connecting_were_never_sent(self):
        for reason in (socket.timeout("timed out"), ConnectionRefusedError("refused"), socket.gaierror("no such host")):
            self.assertIs(self.call(urllib.error.URLError(reason)).sent, False, reason)

    def test_a_lost_answer_may_have_been_sent(self):
        self.assertIs(self.call(socket.timeout("timed out")).sent, True)

    def test_an_http_error_keeps_its_status(self):
        body = io.BytesIO(b'{"name": "INTERNAL_SERVER_ERROR", "message": "An internal server error has occurred."}')
        e = self.call(urllib.error.HTTPError("https://api-m.sandbox.paypal.com/x", 500, "Server Error", {}, body))
        self.assertEqual((e.status, e.name, e.sent), (500, "INTERNAL_SERVER_ERROR", True))

    def test_no_access_token_means_the_call_was_not_made(self):
        calls = []

        def no_token(req):
            calls.append(req.full_url)
            raise PayPalError(0, "TIMEOUT", "PayPal did not answer within 40 seconds.")

        pp = PayPalSandbox("id", "secret")
        with mock.patch.object(pp, "_open", side_effect=no_token):
            with self.assertRaises(PayPalError) as ctx:
                pp.charge_vault("vault", 9800, "USD", "Train", "R-101", "Train", "cart_1", "im_1", "cart_1")
        self.assertIs(ctx.exception.sent, False)
        self.assertEqual(len(calls), 1)  # only the token request; the order was never posted
        self.assertTrue(calls[0].endswith("/v1/oauth2/token"))

    def test_an_unreadable_token_answer_means_the_call_was_not_made(self):
        pp = PayPalSandbox("id", "secret")
        with mock.patch.object(pp, "_open", return_value={}):  # no access_token in the answer
            with self.assertRaises(PayPalError) as ctx:
                pp.get_order("O1")
        self.assertEqual((ctx.exception.name, ctx.exception.sent), ("TOKEN_ERROR", False))


class SimulatorTest(unittest.TestCase):
    def test_unknown_order_is_a_404_like_paypal(self):
        with self.assertRaises(PayPalError) as ctx:
            PayPalSimulated().get_order("NO-SUCH-ORDER")
        self.assertEqual((ctx.exception.status, ctx.exception.name), (404, "RESOURCE_NOT_FOUND"))


if __name__ == "__main__":
    unittest.main()
