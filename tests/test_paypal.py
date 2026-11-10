"""PayPal client details that do not need the network."""
import http.client
import io
import json
import socket
import ssl
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
        self.assertEqual((ctx.exception.name, ctx.exception.sent), ("BAD_RESPONSE", False))


class Answer:
    """Stands in for the response object urlopen returns."""

    def __init__(self, body=b"", error=None):
        self.body, self.error = body, error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self.error:
            raise self.error
        return self.body


class CatchAllTest(unittest.TestCase):
    """Whatever goes wrong in a request leaves _open as a PayPalError. An error after the
    request was sent must say sent=True: the gateway keeps the budget of such a payment."""

    def call(self, **urlopen):
        pp = PayPalSandbox("id", "secret", timeout=7)
        req = urllib.request.Request("https://api-m.sandbox.paypal.com/v2/checkout/orders")
        with mock.patch("urllib.request.urlopen", **urlopen):
            try:
                return pp._open(req)
            except PayPalError as e:
                return e

    def test_a_connection_that_breaks_after_sending_is_sent(self):
        # urlopen raises these itself only after the request is out (while it waits for the answer).
        for error in (http.client.RemoteDisconnected("Remote end closed connection without response"),
                      ConnectionResetError(54, "Connection reset by peer"),
                      http.client.BadStatusLine("garbage"),
                      ssl.SSLError("record layer failure")):
            e = self.call(side_effect=error)
            self.assertEqual((e.name, e.status, e.sent), ("CONNECTION_LOST", 0, True), repr(error))
        # ... or while the answer is being read.
        e = self.call(return_value=Answer(error=http.client.IncompleteRead(b'{"id": "5A1')))
        self.assertEqual((e.name, e.sent), ("CONNECTION_LOST", True))

    def test_an_answer_that_is_not_a_json_object_is_sent(self):
        for body in (b"<html>502 Bad Gateway</html>", b"[1, 2]", b'"text"', b"\xff\xfe\x00"):
            e = self.call(return_value=Answer(body))
            self.assertEqual((e.name, e.status, e.sent), ("BAD_RESPONSE", 0, True), body)

    def test_any_other_error_is_wrapped_and_counts_as_sent(self):
        # For example http.client refusing a header: its message quotes the header, token included.
        e = self.call(side_effect=ValueError("Invalid header value b'Bearer A21AAK-the-access-token'"))
        self.assertEqual((type(e), e.name, e.sent), (PayPalError, "BAD_RESPONSE", True))
        self.assertIn("ValueError", e.short())
        self.assertNotIn("the-access-token", e.short())      # only the kind of error is shown

    def test_error_details_in_an_unexpected_shape_do_not_break_the_message(self):
        for details in ("text", {"issue": "X"}, [1, "two"], 7):
            body = io.BytesIO(json.dumps({"name": "UNPROCESSABLE_ENTITY", "message": "m", "details": details}).encode())
            e = self.call(side_effect=urllib.error.HTTPError("https://api-m.sandbox.paypal.com/x", 422, "E", {}, body))
            self.assertEqual(e.short(), "UNPROCESSABLE_ENTITY: m", details)
        body = io.BytesIO(b'{"name": "UNPROCESSABLE_ENTITY", "message": "m", "debug_id": "d1",'
                          b' "details": [{"issue": "INSTRUMENT_DECLINED", "description": "Declined."}]}')
        e = self.call(side_effect=urllib.error.HTTPError("https://api-m.sandbox.paypal.com/x", 422, "E", {}, body))
        self.assertEqual(e.short(), "UNPROCESSABLE_ENTITY: m · INSTRUMENT_DECLINED Declined. (debug id d1)")

    def test_good_answers_come_through(self):
        self.assertEqual(self.call(return_value=Answer(b'{"id": "5A1", "status": "COMPLETED"}')),
                         {"id": "5A1", "status": "COMPLETED"})
        self.assertEqual(self.call(return_value=Answer(b"")), {})   # 204 No Content

    def test_connecting_errors_are_still_not_sent(self):
        e = self.call(side_effect=urllib.error.URLError(ConnectionRefusedError("refused")))
        self.assertEqual((e.name, e.sent), ("NETWORK_ERROR", False))
        e = self.call(side_effect=urllib.error.URLError(ssl.SSLError("handshake failure")))
        self.assertEqual((e.name, e.sent), ("NETWORK_ERROR", False))

    def http_error(self, code, body, read_error=None):
        fp = io.BytesIO(body)
        if read_error:
            fp.read = mock.Mock(side_effect=read_error)
        return urllib.error.HTTPError("https://api-m.sandbox.paypal.com/x", code, "Error", {}, fp)

    def test_an_error_status_is_kept_whatever_the_body_is(self):
        e = self.call(side_effect=self.http_error(422, b'["not", "an", "object"]'))
        self.assertEqual((e.status, e.name, e.sent), (422, "HTTP_422", True))
        e = self.call(side_effect=self.http_error(502, b"<html>Bad Gateway</html>"))
        self.assertEqual((e.status, e.name), (502, "HTTP_502"))
        self.assertIn("Bad Gateway", e.short())
        e = self.call(side_effect=self.http_error(400, b"", read_error=ConnectionResetError("reset")))
        self.assertEqual((e.status, e.name), (400, "HTTP_400"))

    def test_a_token_answer_without_a_token_is_a_paypal_error(self):
        for answer in ({}, {"token_type": "Bearer"}, {"access_token": "t", "expires_in": "soon"}):
            pp = PayPalSandbox("id", "secret")
            with mock.patch.object(pp, "_open", return_value=answer):
                with self.assertRaises(PayPalError) as ctx:
                    pp.check_credentials()
            self.assertEqual(ctx.exception.name, "BAD_RESPONSE", answer)
            self.assertIsNone(pp._token)


class SimulatorTest(unittest.TestCase):
    def test_unknown_order_is_a_404_like_paypal(self):
        with self.assertRaises(PayPalError) as ctx:
            PayPalSimulated().get_order("NO-SUCH-ORDER")
        self.assertEqual((ctx.exception.status, ctx.exception.name), (404, "RESOURCE_NOT_FOUND"))


if __name__ == "__main__":
    unittest.main()
