"""The full DEMO flow through the gateway: simulated PayPal, scripted agent,
real signatures, real ledger, and the offline evidence verifier."""
import copy
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import ed25519, llm
from app.gateway import Gateway, GatewayError, payment_status
from app.paypal import PAYMENT_TIMEOUT_TEXT, PAYMENT_UNKNOWN_HINT, PayPalError, PayPalSimulated
from app.util import CART_CONTEXT, b64url, b64url_decode, canonical_json, mask_tail, sha256_hex
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

    def test_evidence_masks_payer_email(self):
        self.connect()
        self.sign_mandate()
        out = self.g.request_purchase("R-101", "train")
        self.assertEqual(out["status"], "PAID")
        cart_id = self.carts()["R-101"]["id"]
        email = self.g.state["carts"][cart_id]["paypal"]["payer_email"]
        self.assertIn("@", email)
        bundle = self.g.evidence(cart_id)
        self.assertNotIn(email, json.dumps(bundle, ensure_ascii=False))
        self.assertEqual(bundle["paypal"]["payer_email"], "tr…@example.com")
        self.assertEqual(bundle["paypal"]["readback"]["payer_email"], "tr…@example.com")
        # Masking does not touch what the verifier checks.
        self.assertEqual(all_pass(verify_bundle(bundle)), [])
        # The gateway keeps the full email for itself.
        self.assertEqual(self.g.state["carts"][cart_id]["paypal"]["payer_email"], email)

    def test_P1_timeout_must_keep_the_budget(self):
        self.connect()
        self.sign_mandate()
        timeout = PayPalError(0, "TIMEOUT", "PayPal did not answer within 40 seconds.")
        with mock.patch.object(self.g.paypal, "charge_vault", side_effect=timeout):
            self.g.request_purchase("R-101", "train")
        # The result is unknown: the budget stays held, and the same SKU is not paid again on its own.
        self.assertNotEqual(self.g.status()["mandate"]["budget"]["left"], "600.00")
        self.assertNotEqual(self.g.request_purchase("R-101", "retry")["status"], "PAID")

    def test_payment_timeout_is_unknown_and_keeps_the_budget(self):
        self.connect()
        self.sign_mandate()
        timeout = PayPalError(0, "TIMEOUT", "PayPal did not answer within 40 seconds.")
        with mock.patch.object(self.g.paypal, "charge_vault", side_effect=timeout):
            out = self.g.call_tool("request_purchase", {"sku": "R-101", "reason": "train"})
        self.assertEqual(out["status"], "PAYMENT_UNKNOWN")
        self.assertEqual(out["error"], PAYMENT_TIMEOUT_TEXT)
        self.assertIn("Do not retry this item", out["note"])
        cart = self.carts()["R-101"]
        self.assertEqual(cart["status"], "PAYMENT_UNKNOWN")
        self.assertEqual(cart["error"], PAYMENT_TIMEOUT_TEXT)
        last = self.g.ledger_entries()[-1]
        self.assertEqual(last["kind"], "PAYMENT_UNKNOWN")
        self.assertEqual(last["data"]["error"], PAYMENT_TIMEOUT_TEXT)
        self.assertEqual(last["data"]["amount"], "98.00")
        event = [e for e in self.g.events_after(0) if e["kind"] == "payment_failed"][-1]
        self.assertEqual(event["status"], "PAYMENT_UNKNOWN")
        # It holds budget and a purchase count, and it shows up as a purchase for the agent.
        budget = self.g.status()["mandate"]["budget"]
        self.assertEqual((budget["paid"], budget["left"], budget["count"]), ("98.00", "502.00", 1))
        self.assertEqual([p["status"] for p in self.g.tool_get_mandate()["purchases"]], ["PAYMENT_UNKNOWN"])
        # Asking for the same item again is a repeat, so it waits for the traveler.
        again = self.g.call_tool("request_purchase", {"sku": "R-101", "reason": "retry"})
        self.assertEqual(again["status"], "PENDING_APPROVAL")
        failed = [c["key"] for c in self.g.state["carts"][self.g.state["cart_order"][-1]]["decision"]["checks"]
                  if not c["ok"]]
        self.assertEqual(failed, ["repeat"])
        # There is no capture to refund.
        with self.assertRaises(GatewayError) as ctx:
            self.g.refund(cart["id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(self.g.verify_ledger()["ok"])

    def test_clear_refusal_or_unsent_request_is_not_executed(self):
        self.connect()
        self.sign_mandate()
        errors = [PayPalError(422, "UNPROCESSABLE_ENTITY", "The instrument was declined."),
                  PayPalError(429, "RATE_LIMIT_REACHED", "Too many requests."),
                  PayPalError(0, "NETWORK_ERROR", "Connection refused", sent=False),
                  PayPalError(0, "TIMEOUT", "Could not reach PayPal within 40 seconds.", sent=False)]
        for error in errors:
            with self.subTest(error=error.name):
                with mock.patch.object(self.g.paypal, "charge_vault", side_effect=error):
                    out = self.g.call_tool("request_purchase", {"sku": "R-101", "reason": "train"})
                self.assertEqual(out["status"], "NOT_EXECUTED")
                self.assertEqual(out["error"], error.short())
                self.assertNotIn("note", out)
                self.assertEqual(self.g.ledger_entries()[-1]["kind"], "PAYMENT_FAILED")
                budget = self.g.status()["mandate"]["budget"]
                self.assertEqual((budget["paid"], budget["count"]), ("0.00", 0))

    def test_lost_or_unclear_answers_are_unknown(self):
        self.connect()
        self.sign_mandate()
        cases = [("R-101", PayPalError(500, "INTERNAL_SERVER_ERROR", "An internal server error has occurred.")),
                 ("R-201", PayPalError(503, "HTTP_503", "")),
                 ("H-302", ConnectionResetError("Remote end closed connection without response"))]
        for sku, error in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.g.paypal, "charge_vault", side_effect=error):
                    out = self.g.call_tool("request_purchase", {"sku": sku, "reason": "x"})
                self.assertEqual(out["status"], "PAYMENT_UNKNOWN")
                self.assertTrue(out["error"].endswith(PAYMENT_UNKNOWN_HINT))
                self.assertEqual(self.g.ledger_entries()[-1]["kind"], "PAYMENT_UNKNOWN")
        budget = self.g.status()["mandate"]["budget"]
        self.assertEqual((budget["paid"], budget["count"]), ("274.00", 3))

    def test_read_back_decides_when_the_first_answer_has_no_capture(self):
        self.connect()
        self.sign_mandate()
        first = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": None, "capture_status": None,
                 "payer_email": None}
        back = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": "C1", "capture_status": "COMPLETED"}
        with mock.patch.object(self.g.paypal, "charge_vault", return_value=first), \
                mock.patch.object(self.g.paypal, "get_order", return_value=back):
            out = self.g.request_purchase("R-101", "train")
        self.assertEqual(out["status"], "PAID")
        cart_id = self.carts()["R-101"]["id"]
        paypal = self.g.state["carts"][cart_id]["paypal"]
        self.assertEqual((paypal["capture_id"], paypal["capture_status"]), ("C1", "COMPLETED"))
        self.assertEqual(paypal["first_answer"], {"order_status": "COMPLETED", "capture_id": None,
                                                  "capture_status": None})
        self.assertEqual(all_pass(verify_bundle(json.loads(json.dumps(self.g.evidence(cart_id))))), [])
        self.assertEqual(self.g.refund(cart_id)["status"], "REFUNDED")  # the capture id came from the read-back

    def test_read_back_overrules_the_first_answer(self):
        self.connect()
        self.sign_mandate()
        first = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": "C1", "capture_status": "COMPLETED"}
        back = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": "C1", "capture_status": "PENDING"}
        with mock.patch.object(self.g.paypal, "charge_vault", return_value=first), \
                mock.patch.object(self.g.paypal, "get_order", return_value=back):
            out = self.g.request_purchase("R-101", "train")
        self.assertEqual(out["status"], "CAPTURE_PENDING")
        self.assertEqual(self.g.status()["mandate"]["budget"]["paid"], "98.00")

    def test_completed_order_without_capture_is_unknown_when_the_read_back_fails(self):
        self.connect()
        self.sign_mandate()
        first = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": None, "capture_status": None}
        # The simulator does not know order O1, so the read-back fails with 404.
        with mock.patch.object(self.g.paypal, "charge_vault", return_value=first):
            out = self.g.request_purchase("R-101", "train")
        self.assertEqual(out["status"], "PAYMENT_UNKNOWN")
        paypal = self.g.state["carts"][self.carts()["R-101"]["id"]]["paypal"]
        self.assertIn("RESOURCE_NOT_FOUND", paypal["readback"]["error"])
        last = self.g.ledger_entries()[-1]
        self.assertEqual((last["kind"], last["data"]["order_id"]), ("PAYMENT_UNKNOWN", "O1"))
        self.assertEqual(self.g.status()["mandate"]["budget"]["paid"], "98.00")

    def test_a_first_answer_that_is_clear_stands_when_the_read_back_fails(self):
        self.connect()
        self.sign_mandate()
        first = {"order_id": "O1", "order_status": "COMPLETED", "capture_id": "C1", "capture_status": "COMPLETED"}
        with mock.patch.object(self.g.paypal, "charge_vault", return_value=first), \
                mock.patch.object(self.g.paypal, "get_order", side_effect=ConnectionResetError("reset")):
            out = self.g.request_purchase("R-101", "train")
        self.assertEqual(out["status"], "PAID")
        self.assertNotIn("first_answer", self.g.state["carts"][self.carts()["R-101"]["id"]]["paypal"])

    def test_declined_or_uncaptured_orders_are_not_executed(self):
        self.connect()
        self.sign_mandate()
        answers = [{"order_id": "O1", "order_status": "COMPLETED", "capture_id": "C1", "capture_status": "DECLINED"},
                   {"order_id": "O2", "order_status": "PAYER_ACTION_REQUIRED", "capture_id": None,
                    "capture_status": None}]
        for answer in answers:
            with self.subTest(order=answer["order_status"], capture=answer["capture_status"]):
                with mock.patch.object(self.g.paypal, "charge_vault", return_value=answer):
                    out = self.g.request_purchase("R-101", "train")
                self.assertEqual(out["status"], "NOT_EXECUTED")
                self.assertEqual(self.g.ledger_entries()[-1]["kind"], "PAYMENT_FAILED")
                self.assertEqual(self.g.status()["mandate"]["budget"]["paid"], "0.00")

    def test_payment_status_table(self):
        self.assertEqual(payment_status("COMPLETED", "COMPLETED"), "PAID")
        self.assertEqual(payment_status("COMPLETED", "PENDING"), "CAPTURE_PENDING")
        for order, capture in (("COMPLETED", "DECLINED"), ("COMPLETED", "FAILED"), ("CREATED", None),
                               ("APPROVED", None), ("PAYER_ACTION_REQUIRED", None), ("VOIDED", None)):
            self.assertEqual(payment_status(order, capture), "NOT_EXECUTED", (order, capture))
        for order, capture in (("COMPLETED", None), (None, None), ("SOMETHING_NEW", None), ("APPROVED", "COMPLETED"),
                               ("COMPLETED", "REFUNDED")):
            self.assertEqual(payment_status(order, capture), "PAYMENT_UNKNOWN", (order, capture))

    def test_scripted_agent_reports_an_unknown_payment(self):
        self.connect()
        self.sign_mandate()
        timeout = PayPalError(0, "TIMEOUT", "PayPal did not answer within 40 seconds.")
        with mock.patch.object(self.g.paypal, "charge_vault", side_effect=timeout):
            self.run_agent("Book the bullet train from Tokyo to Osaka")
        final = [e["text"] for e in self.g.events_after(0) if e["kind"] == "agent_text" and not e.get("interim")][-1]
        self.assertIn("Payment result unknown", final)
        self.assertNotIn("Not booked", final)

    def test_refund_timeout_is_a_clear_error(self):
        self.connect()
        self.sign_mandate()
        self.g.request_purchase("R-101", "train")
        cart_id = self.carts()["R-101"]["id"]
        timeout = PayPalError(0, "TIMEOUT", "PayPal did not answer within 40 seconds.")
        with mock.patch.object(self.g.paypal, "refund", side_effect=timeout):
            with self.assertRaises(GatewayError) as ctx:
                self.g.refund(cart_id)
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("TIMEOUT", str(ctx.exception))
        self.assertEqual(self.carts()["R-101"]["status"], "PAID")

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

    def assert_state_can_be_saved(self):
        json.dumps(self.g.state, ensure_ascii=False).encode("utf-8")
        self.g.emit("system", text="still saving")  # raises if the state on disk can no longer be written
        self.assertFalse(os.path.exists(self.g.state_path + ".tmp"))

    def test_S4_lone_surrogate_does_not_wedge_the_agent(self):
        text = json.loads('"hello \\ud800"')  # half of a surrogate pair, as JSON delivers it
        try:
            self.g.start_agent(text)
        except Exception:
            pass
        if self.g.agent_thread:
            self.g.agent_thread.join(5)
        self.assertFalse(self.g.agent_running)
        self.assert_state_can_be_saved()

    def test_model_output_with_broken_unicode_is_made_safe(self):
        bad = json.loads('"\\ud800"')

        class BrokenTextAI(llm.ScriptedAI):
            def run_agent(self, history, user_text, call_tool, on_text):
                on_text("thinking " + bad)
                call_tool("request_purchase", {"sku": "R-101", "reason": "because " + bad})
                call_tool("request_purchase", {"sku": "X" + bad, "reason": "x"})
                call_tool("tool" + bad, {})
                return "done " + bad

            def draft_mandate(self, instruction, today, start, end):
                draft = super().draft_mandate(instruction, today, start, end)
                draft.update(summary="sum " + bad, notes=["note " + bad], trip={"destination": "Osaka" + bad})
                return draft

        self.g.ai = BrokenTextAI(0)
        self.connect()
        fields = self.g.draft_mandate(self.g.status()["scenario"]["mandate_prompt"] + bad)["fields"]
        p = self.g.prepare_mandate("book it " + bad, fields, self.kid)
        self.g.activate_mandate(p["mandate_id"], self.sign(p["signing_string"]))
        self.run_agent("Book my trip")
        self.assertEqual(self.carts()["R-101"]["status"], "PAID")
        texts = [e.get("text", "") for e in self.g.events_after(0)]
        self.assertIn("done ?", texts)
        self.assertEqual(self.g.state["carts"][self.carts()["R-101"]["id"]]["reason"], "because ?")
        self.assert_state_can_be_saved()
        # A restart reads the same state back.
        g2 = Gateway(PayPalSimulated(), llm.ScriptedAI(0), os.path.join(self.tmp, "state.json"))
        self.assertEqual(len(g2.ledger_entries()), len(self.g.ledger_entries()))

    def test_failed_save_leaves_no_temporary_file(self):
        self.connect()
        with mock.patch("json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.g.emit("system", text="x")
        self.assertFalse(os.path.exists(self.g.state_path + ".tmp"))
        self.assertTrue(os.path.exists(self.g.state_path))  # the last good copy is still there

    def test_agent_is_not_left_running_when_it_cannot_start(self):
        with mock.patch.object(self.g, "emit", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.g.start_agent("Book my trip")
        self.assertFalse(self.g.agent_running)
        self.assertTrue(self.g.reset()["ok"])

    def test_vault_fingerprint_needs_a_key_that_stays_in_the_state_file(self):
        self.connect()
        vault = self.g.state["paypal"]["simulated"]["vault_id"]
        key = self.g.state["secrets"]["fingerprint_key"]
        entry = self.g.ledger_entries()[0]
        fp = entry["data"]["vault_token_fingerprint"]
        self.assertEqual(entry["kind"], "PAYPAL_CONNECTED")
        # Not a plain hash of the token: without the key, guesses cannot be tested against it.
        self.assertNotEqual(fp, hashlib.sha256(vault.encode()).hexdigest()[:16])
        self.assertEqual(fp, hmac.new(b64url_decode(key), vault.encode(), hashlib.sha256).hexdigest()[:16])
        self.assertEqual(len(b64url_decode(key)), 32)
        # The page shows only the last two characters of the token.
        self.assertEqual(self.g.status()["paypal"]["vault_token"], "…" + vault[-2:])
        # The key is in state.json and nowhere else.
        self.sign_mandate()
        self.g.request_purchase("R-101", "train")
        exposed = json.dumps([self.g.status(), self.g.ledger_entries(), self.g.events_after(0),
                              self.g.evidence(self.carts()["R-101"]["id"]), self.g.tool_get_mandate()])
        self.assertNotIn(key, exposed)
        self.assertNotIn(vault, exposed)
        with open(self.g.state_path, encoding="utf-8") as f:
            self.assertIn(key, f.read())
        # It survives a restart and "Reset demo", so one token keeps one fingerprint.
        g2 = Gateway(PayPalSimulated(), llm.ScriptedAI(0), self.g.state_path)
        g2.reset()
        self.assertEqual(g2.state["secrets"]["fingerprint_key"], key)
        self.assertEqual(g2._vault_fingerprint(vault), fp)

    def test_mask_tail_shows_only_the_end(self):
        self.assertEqual(mask_tail("0712345678930"), "…30")
        self.assertEqual(mask_tail("sim1a2b3c4d"), "…4d")
        self.assertEqual(mask_tail("abcdefg"), "…")   # too short to show any of it
        self.assertEqual(mask_tail(None), "…")

    def test_E1_edited_cart_does_not_verify(self):
        self.connect()
        self.sign_mandate()
        self.g.request_purchase("R-101", "train")
        b = json.loads(json.dumps(self.g.evidence(self.carts()["R-101"]["id"])))
        rec = b["cart_mandate"]["record"]
        rec["total"]["value"] = "1.00"
        b["cart_mandate"]["signed_string"] = CART_CONTEXT + canonical_json(rec)
        self.assertNotEqual(all_pass(verify_bundle(b)), [])

    def test_verifier_ties_cart_and_capture_to_the_ledger(self):
        decision, capture, paypal = ("Ledger: records the gateway decision for this cart",
                                     "Ledger: records the PayPal capture", "PayPal: order and capture COMPLETED")
        self.connect()
        sc = self.sign_mandate()
        self.run_agent(sc["agent_prompt"])
        c = self.carts()
        self.approve(c["H-301"]["id"])
        hotel = json.loads(json.dumps(self.g.evidence(c["H-301"]["id"])))
        train = json.loads(json.dumps(self.g.evidence(c["R-101"]["id"])))
        # The number of checks is quoted in the docs and the video: 19 with an approval, 14 without.
        self.assertEqual((len(verify_bundle(hotel)), all_pass(verify_bundle(hotel))), (19, []))
        self.assertEqual((len(verify_bundle(train)), all_pass(verify_bundle(train))), (14, []))
        with open(os.path.join(HERE, "fixtures", "chrome_evidence.json"), encoding="utf-8") as f:
            self.assertEqual(len(verify_bundle(json.load(f))), 19)

        def edit(change):
            b = copy.deepcopy(train)
            change(b)
            b["cart_mandate"]["signed_string"] = CART_CONTEXT + canonical_json(b["cart_mandate"]["record"])
            return b

        # Nobody signs an auto-approved cart, so only the ledger can show that it was changed.
        other_item = edit(lambda b: b["cart_mandate"]["record"]["items"][0].update(title="Something else", sku="X-000"))
        self.assertEqual(all_pass(verify_bundle(other_item)), [decision])
        cheaper = edit(lambda b: b["cart_mandate"]["record"]["total"].update(value="1.00"))
        self.assertEqual(all_pass(verify_bundle(cheaper)), [decision, capture])
        # Changing the ledger to agree with the edited cart breaks the hash chain instead.
        for e in cheaper["ledger"]["entries"]:
            if e["kind"] == "CART_DECISION" and e["data"]["cart_id"] == cheaper["cart_id"]:
                e["data"].update(amount="1.00", cart_hash=sha256_hex(cheaper["cart_mandate"]["signed_string"].encode()))
        self.assertIn("Ledger: hash chain intact from genesis", all_pass(verify_bundle(cheaper)))
        # The PayPal order in the bundle must be the one the ledger recorded, and the one read back.
        self.assertEqual(all_pass(verify_bundle(edit(lambda b: b["paypal"].update(order_id="ANOTHER-ORDER")))),
                         [capture, paypal])
        self.assertEqual(all_pass(verify_bundle(edit(lambda b: b["paypal"]["readback"].update(order_id="ANOTHER")))),
                         [paypal])
        # The traveler's signature already covers an approved cart; the ledger check agrees with it.
        signed = copy.deepcopy(hotel)
        signed["cart_mandate"]["record"]["total"]["value"] = "1.00"
        signed["cart_mandate"]["signed_string"] = CART_CONTEXT + canonical_json(signed["cart_mandate"]["record"])
        failed = all_pass(verify_bundle(signed))
        self.assertIn("Approval: the traveler signed this exact cart, HUMAN_PRESENT", failed)
        self.assertIn(decision, failed)

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
