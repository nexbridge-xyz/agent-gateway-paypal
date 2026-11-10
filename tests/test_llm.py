"""Request shapes for the AI providers, checked without network access."""
import datetime as dt
import json
import unittest
from unittest import mock

from app import llm
from app.gateway import Gateway
from app.paypal import PayPalSimulated

TODAY = dt.date(2026, 11, 10)
START, END = dt.date(2026, 11, 20), dt.date(2026, 11, 22)
DRAFT = {"summary": "Up to $600.00 on trains and hotels.", "total_budget": 600, "auto_approve_limit": 150,
         "allowed_categories": ["rail", "hotel"], "max_purchases": 6, "valid_until": "2026-11-22",
         "trip": {"destination": "Osaka"}, "notes": []}


class FakeHTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, body, timeout=120):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(json.dumps(body))})
        return self.replies.pop(0)


class AnthropicTest(unittest.TestCase):
    def setUp(self):
        self.ai = llm.AnthropicAI("test-key", "claude-sonnet-5-5")

    def test_label(self):
        self.assertEqual(self.ai.label, "Claude Sonnet 5.5")

    def test_draft_uses_structured_outputs(self):
        fake = FakeHTTP([{"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(DRAFT)}]}])
        with mock.patch.object(llm, "_http_json", fake):
            out = self.ai.draft_mandate("Book my Osaka trip", TODAY, START, END)
        self.assertEqual(out["total_budget"], 600)
        body = fake.calls[0]["body"]
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["output_config"]["effort"], "low")
        self.assertEqual(body["output_config"]["format"], {"type": "json_schema", "schema": llm.MANDATE_TOOL["schema"]})
        self.assertNotIn("thinking", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(fake.calls[0]["headers"]["anthropic-version"], "2026-10-21")

    def test_agent_tools_are_strict_auto(self):
        fake = FakeHTTP([
            {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t1", "name": "get_mandate", "input": {}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "All booked."}]},
        ])
        calls = []
        with mock.patch.object(llm, "_http_json", fake):
            final = self.ai.run_agent([], "Book it", lambda n, a: calls.append(n) or {"active": True}, lambda t: None)
        self.assertEqual(final, "All booked.")
        self.assertEqual(calls, ["get_mandate"])
        body = fake.calls[0]["body"]
        self.assertEqual(body["max_tokens"], 16000)
        self.assertEqual(body["tool_choice"], {"type": "auto"})
        self.assertTrue(all(t["strict"] is True for t in body["tools"]))
        self.assertEqual([t["name"] for t in body["tools"]], ["get_mandate", "search_inventory", "request_purchase"])
        # The assistant turn (with any thinking blocks) is sent back unchanged.
        self.assertEqual(fake.calls[1]["body"]["messages"][1]["content"][0]["type"], "tool_use")

    def test_max_tokens_is_an_error_not_done(self):
        fake = FakeHTTP([{"stop_reason": "max_tokens", "content": [
            {"type": "tool_use", "id": "t1", "name": "request_purchase", "input": {"sku": "R-1"}}]}])
        calls = []
        with mock.patch.object(llm, "_http_json", fake):
            with self.assertRaises(llm.AIError) as ctx:
                self.ai.run_agent([], "Book it", lambda n, a: calls.append(n), lambda t: None)
        self.assertIn("max_tokens", str(ctx.exception))
        self.assertEqual(calls, [])  # a truncated tool call is never run

    def test_gateway_shows_the_error(self):
        fake = FakeHTTP([{"stop_reason": "max_tokens", "content": []}])
        g = Gateway(PayPalSimulated(), self.ai, None)
        with mock.patch.object(llm, "_http_json", fake):
            g.start_agent("Book it")
            g.agent_thread.join(5)
        kinds = [(e["kind"], e.get("text", "")) for e in g.events_after(0)]
        self.assertTrue(any(k == "error" and "max_tokens" in t for k, t in kinds))
        self.assertFalse(any(k == "agent_text" for k, _ in kinds))

    def test_refusal(self):
        fake = FakeHTTP([{"stop_reason": "refusal", "content": []}])
        with mock.patch.object(llm, "_http_json", fake):
            with self.assertRaises(llm.AIError):
                self.ai.draft_mandate("x", TODAY, START, END)


class OpenAICompatTest(unittest.TestCase):
    def test_draft_named_tool_and_length(self):
        ai = llm.OpenAICompatAI("k", "some-model", "https://example.invalid/v1")
        fake = FakeHTTP([{"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "c1", "function": {"name": "propose_mandate", "arguments": json.dumps(DRAFT)}}]}}]}])
        with mock.patch.object(llm, "_http_json", fake):
            self.assertEqual(ai.draft_mandate("x", TODAY, START, END)["max_purchases"], 6)
        self.assertEqual(fake.calls[0]["body"]["tool_choice"]["function"]["name"], "propose_mandate")
        fake = FakeHTTP([{"choices": [{"finish_reason": "length", "message": {"content": "cut"}}]}])
        with mock.patch.object(llm, "_http_json", fake):
            with self.assertRaises(llm.AIError):
                ai.run_agent([], "x", lambda n, a: {}, lambda t: None)


class PromptTest(unittest.TestCase):
    def test_the_model_is_told_not_to_retry_an_unknown_payment(self):
        tool = [t for t in llm.AGENT_TOOLS if t["name"] == "request_purchase"][0]
        for text in (llm.AGENT_SYSTEM, tool["description"]):
            self.assertIn("PAYMENT_UNKNOWN", text)
            self.assertIn("do not retry the item", text.lower())


class ScriptedDraftTest(unittest.TestCase):
    def test_parses_demo_instruction(self):
        d = llm.ScriptedAI(0).draft_mandate(
            "Book my Osaka trip, Nov 20–22. Up to $600 total, trains and hotels only. Ask me before anything over $150.",
            TODAY, START, END)
        self.assertEqual((d["total_budget"], d["auto_approve_limit"]), (600.0, 150.0))
        self.assertEqual(d["allowed_categories"], ["rail", "hotel"])
        self.assertEqual(d["valid_until"], "2026-11-22")
        self.assertIn("$600.00", d["summary"])

    def test_yen_and_no_limit(self):
        d = llm.ScriptedAI(0).draft_mandate("Hotels in Kyoto up to 30,000 yen.", TODAY, START, END)
        self.assertEqual(d["total_budget"], 200.0)
        self.assertEqual(d["auto_approve_limit"], 0)
        self.assertTrue(any("yen" in n for n in d["notes"]))


if __name__ == "__main__":
    unittest.main()
