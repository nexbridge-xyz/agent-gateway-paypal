"""The full DEMO flow through the gateway: simulated PayPal, scripted agent,
real signatures, real ledger, and the offline evidence verifier."""
import copy
import json
import os
import shutil
import tempfile
import unittest

from app import ed25519, llm
from app.gateway import Gateway, GatewayError
from app.paypal import PayPalSimulated
from app.util import b64url
from tools.verify_evidence import verify_bundle

HERE = os.path.dirname(os.path.abspath(__file__))


def all_pass(results):
    return [label for ok, label, _ in results if not ok]


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.g = Gateway(PayPalSimulated(), llm.ScriptedAI(0), os.path.join(self.tmp, "state.json"))
        self.sk = os.urandom(32)
        self.kid = self.g.register_device(b64url(ed25519.public_key(self.sk)))["key_id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sign(self, text):
        return b64url(ed25519.sign(self.sk, text.encode("utf-8")))

    def connect(self):
        url = self.g.paypal_begin("http://localhost:8000")
        self.g.paypal_finish({"approval_token_id": url.split("token=")[1]})

    def sign_mandate(self):
        sc = self.g.status()["scenario"]
        d = self.g.draft_mandate(sc["mandate_prompt"])
        f = d["fields"]
        self.assertEqual((f["total_budget"], f["auto_approve_limit"]), ("600.00", "150.00"))
        self.assertEqual(f["allowed_categories"], ["rail", "hotel"])
        self.assertEqual(f["valid_until"], sc["end"])
        p = self.g.prepare_mandate(sc["mandate_prompt"], f, self.kid)
        self.g.activate_mandate(p["mandate_id"], self.sign(p["signing_string"]))
        return sc

    def run_agent(self, text):
        self.g.start_agent(text)
        self.g.agent_thread.join(10)
        self.assertFalse(self.g.agent_running)

    def carts(self):
        return {c["sku"]: c for c in self.g.status()["carts"]}

    def approve(self, cart_id):
        p = self.g.prepare_approval(cart_id, self.kid)
        return self.g.approve(cart_id, self.sign(p["signing_string"]))

    def test_full_demo(self):
        self.connect()
        sc = self.sign_mandate()
        self.run_agent(sc["agent_prompt"])
        c = self.carts()
        self.assertEqual(c["R-101"]["status"], "PAID")
        self.assertEqual(c["R-201"]["status"], "PAID")
        self.assertEqual(c["H-301"]["status"], "PENDING_APPROVAL")
        self.assertEqual(self.g.status()["mandate"]["budget"]["waiting"], "236.00")

        res = self.approve(c["H-301"]["id"])
        self.assertEqual(res["status"], "PAID")
        self.assertEqual(self.g.status()["mandate"]["budget"]["left"], "168.00")

        hijack = self.g.hijack()
        self.assertEqual(hijack["status"], "DENIED")
        self.assertIn("not verified", hijack["why"])
        self.assertIn("budget", hijack["why"])

        self.assertTrue(self.g.verify_ledger()["ok"])
        bundle = json.loads(json.dumps(self.g.evidence(c["H-301"]["id"])))
        self.assertEqual(all_pass(verify_bundle(bundle)), [])
        # Auto-approved trains verify too (no human signature needed under the limit).
        self.assertEqual(all_pass(verify_bundle(self.g.evidence(c["R-101"]["id"]))), [])

        # The vault token never reaches the agent, the events or the evidence.
        vault = self.g.state["paypal"]["simulated"]["vault_id"]
        exposed = json.dumps([self.g.events_after(0), bundle, self.g.ledger_entries(), self.g.tool_get_mandate(),
                              self.g.status()])
        self.assertNotIn(vault, exposed)

        refund = self.g.refund(c["H-301"]["id"])
        self.assertEqual(refund["status"], "REFUNDED")
        self.assertEqual(self.g.status()["mandate"]["budget"]["left"], "404.00")

        self.g.revoke_mandate()
        self.assertEqual(self.g.request_purchase("H-302", "cheap")["status"], "DENIED")
        self.assertTrue(self.g.verify_ledger()["ok"])

        # A restart keeps the PayPal connection and the ledger.
        g2 = Gateway(PayPalSimulated(), llm.ScriptedAI(0), os.path.join(self.tmp, "state.json"))
        self.assertTrue(g2.paypal_connected())
        self.assertEqual(len(g2.ledger_entries()), len(self.g.ledger_entries()))
        g2.reset()
        self.assertTrue(g2.paypal_connected())
        self.assertIsNone(g2.status()["mandate"])
        self.assertEqual(g2.ledger_entries(), [])

    def test_recheck_after_revoke_denies_approval(self):
        self.connect()
        sc = self.sign_mandate()
        self.run_agent(sc["agent_prompt"])
        hotel = self.carts()["H-301"]["id"]
        p = self.g.prepare_approval(hotel, self.kid)
        self.g.revoke_mandate()
        res = self.g.approve(hotel, self.sign(p["signing_string"]))
        self.assertEqual(res["status"], "DENIED")
        self.assertEqual(self.carts()["H-301"]["status"], "DENIED")

    def test_followup_cheaper_hotel(self):
        self.connect()
        sc = self.sign_mandate()
        self.run_agent(sc["agent_prompt"])
        self.run_agent(sc["followup_prompt"])
        self.assertEqual(self.carts()["H-302"]["status"], "PAID")

    def test_bad_signature_rejected(self):
        self.connect()
        sc = self.g.status()["scenario"]
        d = self.g.draft_mandate(sc["mandate_prompt"])
        p = self.g.prepare_mandate(sc["mandate_prompt"], d["fields"], self.kid)
        with self.assertRaises(GatewayError):
            self.g.activate_mandate(p["mandate_id"], self.sign(p["signing_string"] + "x"))
        self.assertIsNone(self.g.status()["mandate"])

    def test_no_paypal_means_not_executed(self):
        sc = self.sign_mandate()
        res = self.g.request_purchase("R-101", "train")
        self.assertEqual(res["status"], "NOT_EXECUTED")
        self.assertEqual(self.g.status()["mandate"]["budget"]["paid"], "0.00")

    def test_agent_cannot_set_price_or_payee(self):
        self.connect()
        self.sign_mandate()
        out = self.g.call_tool("request_purchase", {"sku": "R-101", "reason": "x", "price": "0.01",
                                                    "payee": "attacker@example.com"})
        self.assertEqual(out["status"], "PAID")
        self.assertEqual(out["amount"], "$98.00")

    def test_chrome_webcrypto_evidence_fixture(self):
        with open(os.path.join(HERE, "fixtures", "chrome_evidence.json"), encoding="utf-8") as f:
            bundle = json.load(f)
        self.assertEqual(all_pass(verify_bundle(bundle)), [])
        tampered = copy.deepcopy(bundle)
        tampered["intent_mandate"]["record"]["constraints"]["total_budget"] = "6000.00"
        self.assertIn("Intent Mandate: signed string matches the record", all_pass(verify_bundle(tampered)))
        tampered = copy.deepcopy(bundle)
        tampered["ledger"]["entries"][2]["summary"] = "edited"
        self.assertIn("Ledger: hash chain intact from genesis", all_pass(verify_bundle(tampered)))


if __name__ == "__main__":
    unittest.main()
