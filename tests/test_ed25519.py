import unittest

from app import ed25519

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIG = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")

# Made by Node 18 WebCrypto (crypto.subtle.sign({name: "Ed25519"}, ...)) over a UTF-8 string
# with the Intent Mandate context and non-ASCII text.
WEBCRYPTO = {
    "pub": "1210205f3e972f40b4f10b74c011ecc5004fc431db246b85d0cae5150386c5e8",
    "sig": "72c90836ee139e06b17f06e03e18dda5e301dda9de03b99e520ad25f15f80d036036edeea0570a2f66230c8b9611969aea1f8e16a60715f3259eb3535818f108",
    "msg": "NexBridge Agent Gateway | Intent Mandate v1\n{\"a\":\"東京 → 大阪\"}",
}


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vector_1(self):
        self.assertEqual(ed25519.public_key(SECRET), PUBLIC)
        self.assertEqual(ed25519.sign(SECRET, b""), SIG)
        self.assertTrue(ed25519.verify(PUBLIC, b"", SIG))

    def test_wrong_message(self):
        self.assertFalse(ed25519.verify(PUBLIC, b"x", SIG))

    def test_wrong_key(self):
        other = ed25519.public_key(bytes(32))
        self.assertFalse(ed25519.verify(other, b"", SIG))

    def test_malformed_inputs(self):
        self.assertFalse(ed25519.verify(PUBLIC[:31], b"", SIG))
        self.assertFalse(ed25519.verify(PUBLIC, b"", SIG[:63]))
        bad_s = SIG[:32] + (2 ** 256 - 1).to_bytes(32, "little")
        self.assertFalse(ed25519.verify(PUBLIC, b"", bad_s))

    def test_webcrypto_signature_verifies(self):
        pub = bytes.fromhex(WEBCRYPTO["pub"])
        sig = bytes.fromhex(WEBCRYPTO["sig"])
        msg = WEBCRYPTO["msg"].encode("utf-8")
        self.assertTrue(ed25519.verify(pub, msg, sig))
        self.assertFalse(ed25519.verify(pub, msg + b" ", sig))


if __name__ == "__main__":
    unittest.main()
