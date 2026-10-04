"""One collector per kind of host data.

Every collector reads one source (a /proc file, a /sys tree, a CLI tool) and
returns plain dictionaries shaped like the API's contracts (camelCase keys).
Collectors that report rates keep the previous raw counters themselves, so the
first call returns no rate and every later call returns the rate since the
previous one.
"""
