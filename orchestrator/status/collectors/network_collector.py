"""Traffic per network interface, classified and mapped to Docker networks."""

from __future__ import annotations

import re
import time
from typing import Callable, Dict, List, Mapping, Tuple

from orchestrator.status.host_paths import HostPaths

#: Interface names the API accepts.
_NAME = re.compile(r"^[A-Za-z0-9_.@:-]{1,32}$")
_VLAN_NAME = re.compile(r"^(?P<parent>.+)\.(?P<vid>\d{1,4})$")

Counters = Tuple[int, int, int, int, int, int]  # rx bytes, tx bytes, rx errs, tx errs, rx drop, tx drop


class NetworkCollector:
    """Rates from /proc/net/dev, plus what each interface *is*.

    Every macvlan Docker network on this host sits on its own VLAN
    sub-interface (``eth0.130`` for ``privateStatus``), so the sub-interface's
    counters are that network's traffic, and the physical interface's
    counters are all of its VLANs combined. Classifying interfaces is what
    lets the API sum "all physical interfaces" without counting VLAN traffic
    twice.
    """

    def __init__(self, paths: HostPaths, clock: Callable[[], float] = time.monotonic) -> None:
        self._paths = paths
        self._clock = clock
        self._previous: Tuple[float, Dict[str, Counters]] | None = None

    def collect(self, docker_networks: Mapping[str, List[str]] | None = None) -> List[dict]:
        now = self._clock()
        current = self._read()
        previous, self._previous = self._previous, (now, current)
        elapsed = now - previous[0] if previous else 0.0
        vlans = self._vlan_table()

        result = []
        for name, counters in sorted(current.items()):
            if not _NAME.match(name):
                continue
            kind, parent, vlan_id = self._classify(name, vlans)
            rx_rate = tx_rate = None
            if previous and elapsed > 0 and name in previous[1]:
                before = previous[1][name]
                rx_delta, tx_delta = counters[0] - before[0], counters[1] - before[1]
                if rx_delta >= 0 and tx_delta >= 0:  # negative = counters reset
                    rx_rate = round(rx_delta / elapsed, 1)
                    tx_rate = round(tx_delta / elapsed, 1)

            speed = self._paths.read_int(self._paths.sys / "class/net" / name / "speed")
            result.append(
                {
                    "name": name,
                    "kind": kind,
                    "parent": parent,
                    "vlanId": vlan_id,
                    "dockerNetworks": list((docker_networks or {}).get(name, []))[:8],
                    "operState": (self._paths.read_text(self._paths.sys / "class/net" / name / "operstate") or "unknown").strip()[:16],
                    "speedMbps": speed if speed is not None and speed > 0 else None,
                    "rxBytesTotal": counters[0],
                    "txBytesTotal": counters[1],
                    "rxBytesPerSecond": rx_rate,
                    "txBytesPerSecond": tx_rate,
                    "rxErrors": counters[2],
                    "txErrors": counters[3],
                    "rxDropped": counters[4],
                    "txDropped": counters[5],
                }
            )
        return result

    def _read(self) -> Dict[str, Counters]:
        text = self._paths.read_text(self._paths.proc / "net/dev") or ""
        result: Dict[str, Counters] = {}
        for line in text.splitlines()[2:]:  # two header lines
            name, _, rest = line.partition(":")
            fields = rest.split()
            if len(fields) < 16:
                continue
            try:
                result[name.strip()] = (
                    int(fields[0]), int(fields[8]),   # bytes
                    int(fields[2]), int(fields[10]),  # errors
                    int(fields[3]), int(fields[11]),  # drops
                )
            except ValueError:
                continue
        return result

    def _vlan_table(self) -> Dict[str, Tuple[str, int]]:
        """/proc/net/vlan/config (root only): ``eth0.130 | 130 | eth0``."""
        text = self._paths.read_text(self._paths.proc / "net/vlan/config") or ""
        table: Dict[str, Tuple[str, int]] = {}
        for line in text.splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) == 3 and parts[1].isdigit():
                table[parts[0]] = (parts[2], int(parts[1]))
        return table

    def _classify(self, name: str, vlans: Mapping[str, Tuple[str, int]]) -> Tuple[str, str | None, int | None]:
        if name == "lo":
            return "loopback", None, None

        node = self._paths.sys / "class/net" / name
        uevent = self._paths.read_text(node / "uevent") or ""
        devtype = ""
        for line in uevent.splitlines():
            if line.startswith("DEVTYPE="):
                devtype = line.split("=", 1)[1].strip()

        if devtype == "vlan" or name in vlans:
            if name in vlans:
                parent, vid = vlans[name]
                return "vlan", parent, vid
            match = _VLAN_NAME.match(name)
            if match:
                return "vlan", match.group("parent"), int(match.group("vid"))
            return "vlan", None, None
        if devtype == "bridge" or (node / "bridge").exists():
            return "bridge", None, None
        if devtype == "macvlan":
            return "macvlan", None, None
        if (node / "device").exists():
            return "physical", None, None
        return "virtual", None, None
