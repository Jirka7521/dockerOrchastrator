"""Can the server reach the internet? Ping and a DNS probe."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, List, Sequence

_TRANSMITTED = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")


def _run(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed executable, list args
        list(args), capture_output=True, text=True, timeout=timeout, check=False
    )


class ConnectivityCollector:
    """Pings every target and resolves one name, all in parallel.

    Several targets on different networks (Cloudflare, Google) separate "the
    internet is down" from "one provider is down"; the DNS probe separates
    "no route" from "no name resolution", which look identical in a browser.
    """

    def __init__(
        self,
        targets: Sequence[str],
        count: int,
        dns_host: str,
        runner: Callable[[Sequence[str], float], subprocess.CompletedProcess] = _run,
        resolver: Callable[..., list] = socket.getaddrinfo,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._targets = list(targets)
        self._count = count
        self._dns_host = dns_host
        self._runner = runner
        self._resolver = resolver
        self._clock = clock

    def collect(self) -> dict:
        with ThreadPoolExecutor(max_workers=len(self._targets) + 1, thread_name_prefix="status-ping") as pool:
            pings = [pool.submit(self._ping, target) for target in self._targets]
            dns = self._dns(pool)
            return {"targets": [future.result() for future in pings], "dns": dns}

    # ------------------------------------------------------------------- ping

    def _ping(self, target: str) -> dict:
        result = {"target": target, "ok": False, "sent": self._count, "received": 0, "lossPercent": 100.0}
        executable = shutil.which("ping")
        if executable is None:
            return {**result, "error": "ping is not installed"}

        # -n: no reverse DNS; -i 0.2: the shortest interval allowed without
        # root; -W 2: per-reply timeout. The whole run is bounded as well.
        args = [executable, "-n", "-c", str(self._count), "-i", "0.2", "-W", "2", target]
        try:
            completed = self._runner(args, self._count * 0.2 + 6)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**result, "error": f"ping failed: {exc}"[:500]}

        parsed = self.parse(completed.stdout)
        if parsed is None:
            error = (completed.stderr or completed.stdout).strip().splitlines()
            return {**result, "error": (error[-1] if error else "no ping summary")[:500]}
        return {"target": target, **parsed}

    @staticmethod
    def parse(output: str) -> dict | None:
        """The summary of iputils/busybox ping: counts, loss and round-trip times."""
        counts = _TRANSMITTED.search(output)
        if not counts:
            return None
        sent, received = int(counts.group(1)), int(counts.group(2))
        result = {
            "ok": received > 0,
            "sent": sent,
            "received": received,
            "lossPercent": round(100.0 * (sent - received) / sent, 1) if sent else 100.0,
        }
        rtt = _RTT.search(output)
        if rtt:
            result.update(
                rttMinMs=float(rtt.group(1)), rttAvgMs=float(rtt.group(2)), rttMaxMs=float(rtt.group(3))
            )
        return result

    # -------------------------------------------------------------------- dns

    def _dns(self, pool: ThreadPoolExecutor) -> dict:
        started = self._clock()
        future = pool.submit(self._resolver, self._dns_host, 443, 0, socket.SOCK_STREAM)
        try:
            infos = future.result(timeout=5)
        except FutureTimeout:
            return {"host": self._dns_host, "ok": False, "error": "resolution timed out after 5 s"}
        except OSError as exc:
            return {"host": self._dns_host, "ok": False, "error": str(exc)[:500]}

        addresses: List[str] = []
        for info in infos:
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
        return {
            "host": self._dns_host,
            "ok": bool(addresses),
            "resolveMs": round((self._clock() - started) * 1000, 1),
            "addresses": addresses[:16],
        }
