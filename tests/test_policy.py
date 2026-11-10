import datetime as dt
import unittest

from app import policy
from app.catalog import Catalog, trip_dates
from app.util import today

CUR = "USD"


def mandate(**over):
    c = {"currency": CUR, "total_budget": "600.00", "auto_approve_limit": "150.00",
         "allowed_categories": ["rail", "hotel"], "max_purchases": 6,
         "valid_until": (today() + dt.timedelta(days=12)).isoformat(), "verified_merchants_only": True}
    c.update(over)
    return {"constraints": c}


def usage(committed=0, reserved=0, count=0, skus=()):
    return {"committed": committed, "reserved": reserved, "count": count, "skus": list(skus)}


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.cat = Catalog(CUR)

    def run_check(self, sku, m=None, status="active", u=None):
        return policy.evaluate(m or mandate(), status, self.cat.get(sku), u or usage(), CUR)

    def failed(self, res):
        return sorted(c["key"] for c in res["checks"] if not c["ok"])

    def test_approved(self):
        res = self.run_check("R-101")
        self.assertEqual(res["result"], "APPROVED")
        self.assertEqual(self.failed(res), [])

    def test_needs_approval_above_limit(self):
        res = self.run_check("H-301")
        self.assertEqual(res["result"], "NEEDS_APPROVAL")
        self.assertEqual(self.failed(res), ["auto_limit"])

    def test_needs_approval_repeat(self):
        res = self.run_check("R-101", u=usage(9800, 0, 1, ["R-101"]))
        self.assertEqual(res["result"], "NEEDS_APPROVAL")
        self.assertEqual(self.failed(res), ["repeat"])

    def test_denied_not_active(self):
        for status in ("revoked", "none", "replaced"):
            res = self.run_check("R-101", status=status)
            self.assertEqual(res["result"], "DENIED")
            self.assertIn("mandate_active", self.failed(res))

    def test_denied_expired(self):
        res = self.run_check("R-101", m=mandate(valid_until=(today() - dt.timedelta(days=1)).isoformat()))
        self.assertEqual(res["result"], "DENIED")
        self.assertEqual(self.failed(res), ["time_window"])

    def test_denied_category(self):
        res = self.run_check("A-501")
        self.assertEqual(res["result"], "DENIED")
        self.assertEqual(self.failed(res), ["category"])

    def test_denied_unverified_merchant(self):
        m = mandate(total_budget="5000.00", auto_approve_limit="5000.00")
        res = self.run_check("H-399", m=m)
        self.assertEqual(res["result"], "DENIED")
        self.assertEqual(self.failed(res), ["merchant"])
        m["constraints"]["verified_merchants_only"] = False
        self.assertEqual(self.run_check("H-399", m=m)["result"], "APPROVED")

    def test_denied_budget_counts_reserved(self):
        res = self.run_check("R-102", u=usage(committed=19600, reserved=41200, count=3))
        self.assertEqual(res["result"], "DENIED")
        self.assertEqual(self.failed(res), ["budget"])

    def test_denied_count(self):
        res = self.run_check("R-101", m=mandate(max_purchases=2), u=usage(0, 0, 2))
        self.assertEqual(res["result"], "DENIED")
        self.assertEqual(self.failed(res), ["count"])

    def test_hijack_suite_denied(self):
        res = self.run_check("H-399", u=usage(committed=43200, count=3))
        self.assertEqual(res["result"], "DENIED")
        self.assertEqual(self.failed(res), ["auto_limit", "budget", "merchant"])

    def test_normalize(self):
        start, end = trip_dates()
        f = policy.normalize_fields({"total_budget": 600, "auto_approve_limit": 900,
                                     "allowed_categories": ["Rail", "hotel", "spaceship", "rail"],
                                     "valid_until": "2001-01-01", "max_purchases": 99}, CUR, end)
        self.assertEqual(f["total_budget"], "600.00")
        self.assertEqual(f["auto_approve_limit"], "600.00")  # clamped to the budget
        self.assertEqual(f["allowed_categories"], ["rail", "hotel"])
        self.assertEqual(f["valid_until"], end.isoformat())  # past date pushed forward
        self.assertEqual(f["max_purchases"], 20)
        far = policy.normalize_fields({"valid_until": (today() + dt.timedelta(days=400)).isoformat()}, CUR, end)
        self.assertEqual(far["valid_until"], (today() + dt.timedelta(days=90)).isoformat())

    def test_trip_dates(self):
        for offset in range(14):
            base = dt.date(2026, 12, 1) + dt.timedelta(days=offset)
            s, e = trip_dates(base)
            self.assertEqual(s.weekday(), 4)
            self.assertGreaterEqual((s - base).days, 10)
            self.assertLess((s - base).days, 17)
            self.assertEqual((e - s).days, 2)


if __name__ == "__main__":
    unittest.main()
