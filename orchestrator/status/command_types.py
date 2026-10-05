"""The command types the dashboard API can hand out."""

from __future__ import annotations

LOGS = "logs"

#: ``docker start|stop|restart <container>``.
CONTAINER_ACTIONS = ("start", "stop", "restart")

#: ``systemctl reboot|poweroff`` -- the whole host.
POWER_ACTIONS = ("reboot", "poweroff")

ALL = (LOGS, *CONTAINER_ACTIONS, *POWER_ACTIONS)

#: What runs unless the config says otherwise: reading logs, changing nothing.
DEFAULT_ALLOWED = (LOGS,)
