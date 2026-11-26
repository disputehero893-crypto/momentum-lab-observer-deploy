import unittest
from momentum.stream_events import decode_message

class StreamEventTests(unittest.TestCase):
    def test_stock_trade(self):
        events = decode_message('[{"T":"t","S":"AAPL","p":123.45,"t":"2026-10-20T14:00:00Z"}]', "stocks")
        self.assertEqual(len(events), 1)
        self.assertEqual(str(events[0].price), "123.45")

    def test_crypto_quote(self):
        events = decode_message('[{"T":"q","S":"BTC/USD","bp":62000,"ap":62001,"t":"2026-10-20T14:00:00Z"}]', "crypto")
        self.assertEqual(len(events), 1)
        self.assertEqual(str(events[0].ask), "62001")

    def test_reject_crossed_quote(self):
        with self.assertRaises(ValueError):
            decode_message('[{"T":"q","S":"AAPL","bp":101,"ap":100,"t":"2026-10-20T14:00:00Z"}]', "stocks")

    def test_ignore_control(self):
        self.assertEqual(decode_message('[{"T":"success","msg":"authenticated"}]', "stocks"), [])

    def test_missing_timestamp(self):
        with self.assertRaises(ValueError):
            decode_message('[{"T":"t","S":"AAPL","p":100}]', "stocks")

if __name__ == "__main__":
    unittest.main()
