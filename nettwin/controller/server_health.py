"""Small, dependency-free server health probe and fault classifier."""

from collections import namedtuple
from urllib.error import URLError
from urllib.request import urlopen


HealthResult = namedtuple("HealthResult", ["healthy", "latency_ms"])


class ServerHealthProbe:
    """Probe the application layer without coupling it to Ryu or Flask."""

    def __init__(self, url, timeout=1.0):
        self.url = url
        self.timeout = timeout

    def check(self):
        """Return health and elapsed time; failed requests return no latency."""
        started = _monotonic()
        try:
            with urlopen(self.url, timeout=self.timeout) as response:
                healthy = response.status == 200
        except (OSError, URLError, TimeoutError):
            healthy = False

        latency_ms = (_monotonic() - started) * 1000 if healthy else None
        return HealthResult(healthy, latency_ms)


def classify_fault(server_healthy, network_healthy):
    """Classify the observed state for clear telemetry and demonstrations."""
    if server_healthy and network_healthy:
        return "HEALTHY"
    if not server_healthy and network_healthy:
        return "SERVER_UNRESPONSIVE"
    if server_healthy and not network_healthy:
        return "NETWORK_PATH_UNAVAILABLE"
    return "NETWORK_AND_SERVER_UNAVAILABLE"


def _monotonic():
    import time

    return time.monotonic()
