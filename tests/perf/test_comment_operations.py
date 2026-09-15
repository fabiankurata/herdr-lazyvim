import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "perf_compare", Path(__file__).with_name("compare.py")
)
COMPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPARE)


class CommentOperationMetricsTest(unittest.TestCase):
    def test_namespaces_public_operation_metrics(self):
        observed = {
            "status": "PASS", "seeded_count": 10, "listed_count": 11,
            "fake_delivery_delay_ms": 0,
            "add_bookkeeping_ms": 1.0, "add_total_ms": 1.0,
            "list_bookkeeping_ms": 2.0, "list_total_ms": 2.0,
            "send_ack_bookkeeping_ms": 3.0, "send_ack_total_ms": 3.0,
        }
        metrics = COMPARE.comment_operation_metrics(observed, 10)
        self.assertEqual(metrics["send_ack_total_10_ms"], 3.0)

    def test_rejects_missing_metric(self):
        observed = {"status": "PASS", "seeded_count": 10, "listed_count": 11,
                    "fake_delivery_delay_ms": 0}
        with self.assertRaises(ValueError):
            COMPARE.comment_operation_metrics(observed, 10)


if __name__ == "__main__":
    unittest.main()
