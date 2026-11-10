"""The offline verifier as a command. A bundle comes from someone else, so it is hostile input:
text in it must not reach the terminal as control characters, and a file that is not built
like a bundle must end in NOT VERIFIED with a reason, never in a traceback."""
import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import unicodedata
import unittest

from tools import verify_evidence as verifier

HERE = os.path.dirname(os.path.abspath(__file__))


class VerifierCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(HERE, "fixtures", "chrome_evidence.json"), encoding="utf-8") as f:
            self.bundle = json.load(f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, bundle=None, text=None):
        """Write a file, run the command on it, return (exit code, everything it printed)."""
        path = os.path.join(self.tmp, "evidence.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if text is not None else json.dumps(bundle))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = verifier.main([path])
        return code, out.getvalue()

    def verdicts(self, out):
        return [line for line in out.splitlines() if line.strip().startswith(("VERIFIED", "NOT VERIFIED"))]

    def test_a_good_bundle_reads_as_before(self):
        code, out = self.run_main(self.bundle)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "  VERIFIED: 19 of 19 checks passed")
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("  PASS ")]), 19)
        self.assertIn("  Sorano Hotel Umeda · double room · 2 nights · 236.00 USD · status PAID", out)

    def test_control_characters_are_shown_as_text(self):
        b = copy.deepcopy(self.bundle)
        item = b["cart_mandate"]["record"]["items"][0]
        # Clear the line and write a verdict, hide what follows, switch the writing direction.
        item["title"] = "Hotel\x1b[2K\r  VERIFIED: 19 of 19 checks passed\x1b[8m\n‮ "
        item["category"] = "hotel\x1b[0m"
        b["cart_id"] = "cart\x1b]0;window title\x07"
        b["status"] = "PAID\r\n  VERIFIED: 19 of 19 checks passed"
        b["paypal"]["order_id"] = "ORDER\x1b[1A"
        b["paypal"]["env"] = "simulated\x00\x9b31m"
        b["intent_mandate"]["key_id"] = "key\n  PASS Everything"
        b["intent_mandate"]["record"]["id"] = "im\x7f​"
        code, out = self.run_main(b)
        self.assertEqual(code, 1)
        unsafe = [ch for ch in out if ch != "\n" and unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp")]
        self.assertEqual(unsafe, [])
        # They are still visible, as escapes, and they cannot start a line of their own.
        for escape in ("\\x1b[2K", "\\r", "\\n", "\\u202e", "\\u2028", "\\x07", "\\x00", "\\x9b", "\\x7f", "\\u200b"):
            self.assertIn(escape, out)
        self.assertEqual(len(self.verdicts(out)), 1)
        self.assertTrue(out.splitlines()[-1].startswith("  NOT VERIFIED: "))
        self.assertEqual([line for line in out.splitlines() if line.startswith("  PASS Everything")], [])

    def test_a_long_value_is_cut(self):
        b = copy.deepcopy(self.bundle)
        b["cart_mandate"]["record"]["items"][0]["title"] = "A" * 100000
        b["status"] = "B" * 100000
        code, out = self.run_main(b)
        self.assertEqual(code, 1)
        self.assertLess(len(out), 5000)
        self.assertIn("A" * 200 + "…", out)

    def test_show(self):
        self.assertEqual(verifier.show("Tokyo → Shin-Osaka · 東京"), "Tokyo → Shin-Osaka · 東京")   # ordinary text stays
        self.assertEqual(verifier.show("a\x1b[31mb\tc"), "a\\x1b[31mb\\tc")
        self.assertEqual(verifier.show(json.loads('"\\ud800"')), "\\ud800")                        # can be printed
        self.assertEqual(verifier.show(None), "None")
        self.assertEqual(verifier.show("x" * 300, limit=10), "x" * 10 + "…")

    def test_files_that_are_not_bundles_are_not_verified(self):
        b = self.bundle
        cases = [
            ("a list", [1, 2, 3], "does not hold a JSON object"),
            ("a string", "evidence", "does not hold a JSON object"),
            ("null", None, "does not hold a JSON object"),
            ("mandate is a list", dict(b, intent_mandate=[]), "intent_mandate is not an object"),
            ("cart is a string", dict(b, cart_mandate="cart"), "cart_mandate is not an object"),
            ("approval is a number", dict(b, cart_approval=7), "cart_approval is not an object"),
            ("paypal is a list", dict(b, paypal=[1]), "paypal is not an object"),
            ("ledger is a list", dict(b, ledger=[]), "ledger is not an object"),
            ("ledger entry is a number", dict(b, ledger={"entries": [1], "head": "x"}), "ledger.entries is not a list"),
            ("ledger entries is an object", dict(b, ledger={"entries": {"a": 1}}), "ledger.entries is not a list"),
            ("public key is a number", dict(b, intent_mandate=dict(b["intent_mandate"], public_key=5)),
             "intent_mandate is not built as expected"),
            ("mandate record is a list", dict(b, intent_mandate=dict(b["intent_mandate"], record=[1])),
             "intent_mandate is not built as expected"),
            ("cart record is a string", dict(b, cart_mandate=dict(b["cart_mandate"], record="x")),
             "cart_mandate is not built as expected"),
            ("cart total is a list", dict(b, cart_mandate={"record": {"total": [1]}}),
             "cart_mandate is not built as expected"),
            ("approval record is a list", dict(b, cart_approval=dict(b["cart_approval"], record=[1])),
             "cart_approval is not built as expected"),
            ("cart item is a number", dict(b, cart_mandate={"record": {"items": [1]}}),
             "the constraints or the cart's items is not built as expected"),
            ("ledger entry data is a list",
             dict(b, ledger={"entries": [{"kind": "CART_DECISION", "data": [1]}], "head": "x"}),
             "ledger is not built as expected"),
            ("read-back is a list", dict(b, paypal=dict(b["paypal"], readback=[1])),
             "paypal is not built as expected"),
        ]
        for name, bundle, reason in cases:
            with self.subTest(name):
                results = verifier.verify_bundle(bundle)                      # never raises
                self.assertEqual([(ok, label) for ok, label, _ in results], [(False, verifier.STRUCTURE)])
                code, out = self.run_main(bundle)
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", out)
                self.assertEqual(len(self.verdicts(out)), 1)
                self.assertTrue(out.splitlines()[-1].startswith("  NOT VERIFIED: "), out)
                self.assertIn(reason, out)

    def test_an_empty_object_fails_its_checks_one_by_one(self):
        # Its outline is fine (every part may be missing), so the ordinary checks run and fail.
        code, out = self.run_main({})
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out)
        self.assertIn("  FAIL Intent Mandate: Ed25519 signature verifies", out)
        self.assertIn("  FAIL Ledger: records the gateway decision for this cart", out)
        self.assertRegex(out.splitlines()[-1], r"^  NOT VERIFIED: \d+ of \d+ checks passed$")

    def test_files_that_cannot_be_read(self):
        for name, text in (("not JSON", "{not json"), ("empty", ""), ("too deep", "[" * 100000)):
            with self.subTest(name):
                code, out = self.run_main(text=text)
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", out)
                self.assertTrue(out.startswith("NOT VERIFIED: "), out)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verifier.main([os.path.join(self.tmp, "no-such-file\x1b[2J.json")])
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().startswith("NOT VERIFIED: "))
        self.assertNotIn("\x1b", out.getvalue())

    def test_usage_is_exit_code_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(verifier.main([]), 2)
            self.assertEqual(verifier.main(["a.json", "b.json"]), 2)


if __name__ == "__main__":
    unittest.main()
