"""Server status reporting for the serverStatusPage dashboard.

The orchestrator already runs as root on the host with the Docker CLI at hand,
so it is the one component that reads the host's state -- /proc, /sys,
`docker`, `smartctl`, `ping` -- and pushes it to the dashboard API. Nothing
connects *to* the orchestrator: it only opens outbound, HMAC-signed requests,
and the only thing the API can ask of it is one container's log.

- :mod:`orchestrator.status.status_config` -- the ``status_reporter`` config section
- :mod:`orchestrator.status.request_signer` -- the signature scheme shared with the API
- :mod:`orchestrator.status.status_api_client` -- signed HTTP to the API
- :mod:`orchestrator.status.collectors` -- one collector per kind of host data
- :mod:`orchestrator.status.status_reporter` -- the threads that schedule it all

The wire format is documented in serverStatusPage's docs/AGENT_PROTOCOL.md.
"""
