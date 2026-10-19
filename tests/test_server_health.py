import unittest
from unittest.mock import patch

from nettwin.controller.server_health import (
    HealthResult,
    ServerHealthProbe,
    classify_fault,
)


class FaultClassificationTest(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(classify_fault(True, True), "HEALTHY")

    def test_server_failure_is_distinct_from_network_failure(self):
        self.assertEqual(classify_fault(False, True), "SERVER_UNRESPONSIVE")

    def test_network_failure(self):
        self.assertEqual(classify_fault(True, False), "NETWORK_PATH_UNAVAILABLE")

    def test_both_failures(self):
        self.assertEqual(
            classify_fault(False, False), "NETWORK_AND_SERVER_UNAVAILABLE")


class ServerHealthProbeTest(unittest.TestCase):
    @patch("nettwin.controller.server_health.urlopen")
    def test_success_records_latency(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        result = ServerHealthProbe("http://example.test/health").check()
        self.assertEqual(result.healthy, True)
        self.assertIsNotNone(result.latency_ms)

    @patch("nettwin.controller.server_health.urlopen", side_effect=OSError)
    def test_failure_has_no_latency(self, urlopen):
        self.assertEqual(
            ServerHealthProbe("http://example.test/health").check(),
            HealthResult(False, None),
        )


if __name__ == "__main__":
    unittest.main()
