from __future__ import annotations

import unittest

from core.nt_client import NinjaTraderClient


class NinjaTraderClientTests(unittest.TestCase):
    def test_stop_order_type_is_normalized_to_stop_market(self) -> None:
        self.assertEqual(NinjaTraderClient._normalize_order_type("Stop"), "StopMarket")
        self.assertEqual(NinjaTraderClient._normalize_order_type("Market"), "Market")
        self.assertEqual(NinjaTraderClient._normalize_order_type("Limit"), "Limit")


if __name__ == "__main__":
    unittest.main()
