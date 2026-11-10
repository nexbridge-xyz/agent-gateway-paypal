import copy
import unittest

from app.ledger import GENESIS, Ledger, verify_entries


class LedgerTest(unittest.TestCase):
    def make(self, n=5):
        entries = []
        led = Ledger(entries)
        for i in range(n):
            led.append("CART_DECISION", "entry %d" % i, {"cart_id": "cart_%d" % i, "amount": "%d.00" % i})
        return entries

    def test_verify_ok(self):
        entries = self.make()
        res = verify_entries(entries)
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 5)
        self.assertEqual(entries[0]["prev"], GENESIS)
        self.assertEqual(res["head"], entries[-1]["hash"])

    def test_empty(self):
        self.assertEqual(verify_entries([]), {"ok": True, "count": 0, "head": GENESIS})

    def test_editing_any_entry_breaks_it_there(self):
        base = self.make()
        for i in range(len(base)):
            for field, value in (("summary", "edited"), ("data", {"cart_id": "x"}), ("kind", "PAYPAL_REFUND")):
                entries = copy.deepcopy(base)
                entries[i][field] = value
                res = verify_entries(entries)
                self.assertFalse(res["ok"])
                self.assertEqual(res["broken_at"], i + 1)

    def test_rehashing_an_edit_breaks_the_next_entry(self):
        from app.ledger import entry_hash
        entries = self.make()
        entries[1]["summary"] = "edited"
        entries[1]["hash"] = entry_hash(entries[1])
        res = verify_entries(entries)
        self.assertFalse(res["ok"])
        self.assertEqual(res["broken_at"], 3)

    def test_deleting_an_entry_breaks_it(self):
        entries = self.make()
        del entries[2]
        self.assertFalse(verify_entries(entries)["ok"])


if __name__ == "__main__":
    unittest.main()
