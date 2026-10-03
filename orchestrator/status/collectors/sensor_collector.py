"""The temperature sensor's state, as its own stack reports it."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any


class SensorCollector:
    """Fetches the temperature API's current reading, raw.

    Deliberately no judgement here: whether the sensor is healthy depends on
    the device's online flag, the reading's age and plausibility, and the
    containers it flows through, and the dashboard API decides that where it
    can be tested -- it already has the container list. This only reports
    what the temperature API said, or why it said nothing.
    """

    MAX_BODY = 64 * 1024

    def __init__(self, url: str, timeout_seconds: float = 5.0, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._url = url
        self._timeout = timeout_seconds
        # The temperature API is on the LAN; never route this through a proxy.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def collect(self) -> dict:
        base = {"endpoint": self._url[:256]}
        request = urllib.request.Request(self._url, headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                body = response.read(self.MAX_BODY)
        except urllib.error.HTTPError as exc:
            # Reachable, but no reading (e.g. 404 before the first one).
            return {**base, "reachable": True, "httpStatus": exc.code, "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            return {**base, "reachable": False, "error": str(getattr(exc, "reason", exc))[:500]}

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {**base, "reachable": True, "httpStatus": status, "error": "the response is not JSON"}
        if not isinstance(data, dict):
            return {**base, "reachable": True, "httpStatus": status, "error": "unexpected response shape"}

        return {
            **base,
            "reachable": True,
            "httpStatus": status,
            "reading": {
                "deviceKey": self._text(data.get("deviceKey"), 64),
                "displayName": self._text(data.get("displayName"), 128),
                "measuredAt": self._text(data.get("measuredAt"), 40),
                "temperatureC": self._number(data.get("temperatureC")),
                "humidityPct": self._number(data.get("humidityPct")),
                "ageSeconds": self._integer(data.get("ageSeconds")),
                "isStale": data.get("isStale") if isinstance(data.get("isStale"), bool) else None,
                "isOnline": data.get("isOnline") if isinstance(data.get("isOnline"), bool) else None,
            },
        }

    @staticmethod
    def _text(value: Any, limit: int) -> str | None:
        return str(value)[:limit] if isinstance(value, str) and value else None

    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
