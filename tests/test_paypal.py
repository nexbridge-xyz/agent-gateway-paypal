"""PayPal client details that do not need the network."""
import time
import unittest
import urllib.parse
from unittest import mock

from app.paypal import PayPalSandbox


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


if __name__ == "__main__":
    unittest.main()
