# Docker Orchestrator

This repository contains a small Python orchestrator that starts Docker containers on a configurable schedule.

## What it does

- Runs pipeline sync containers once per configured hourly tick.
- Runs hourly and daily snapshot containers for each pipeline.
- Supports additional `scheduled_containers` that can run on:
  - `hourly`
  - `daily`
  - `every_N_hours` (for example, `every_6_hours`)
- Ensures a container is not restarted while it is already running.

## Files

- `docker_orchestrator.py` — main orchestrator script
- `docker_schedule_config.example.json` — example JSON configuration file

## Requirements

- Python 3.11+ (or compatible Python with `zoneinfo` support)
- Docker CLI installed and Docker daemon running

## Usage

Run once:

```powershell
python docker_orchestrator.py --once
```

Run continuously as a daemon:

```powershell
python docker_orchestrator.py
```

Optional flags:

- `--config <path>` — custom config file path
- `--force-daily` — run daily snapshot actions even when not at the daily tick
- `--ignore-hourly-tick` — run regardless of the hourly minute match
- `--log-level <level>` — logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)

## Config structure

Example `docker_schedule_config.example.json`:

> Copy `docker_schedule_config.example.json` to `docker_schedule_config.json` and update values before running the orchestrator.


```json
{
  "schedule": {
    "timezone": "Europe/Prague",
    "hourly_minute": 0,
    "daily_hour": 0,
    "daily_minute": 0
  },
  "pipelines": {
    "example": {
      "sync": "exampleSync",
      "hourly_snapshot": "exampleSnapshotHourly",
      "daily_snapshot": "exampleSnapshotDaily"
    },
    "secondary": {
      "sync": "secondarySync",
      "hourly_snapshot": "secondarySnapshotHourly",
      "daily_snapshot": "secondarySnapshotDaily"
    }
  },
  "scheduled_containers": [
    {
      "name": "scheduled-maintenance",
      "period": "daily"
    },
    {
      "name": "wattwise-simulate-12h-1",
      "period": "every_6_hours"
    }
  ]
}
```

## Config details

- `schedule.timezone` — IANA timezone name used to evaluate schedule ticks
- `schedule.hourly_minute` — minute of every hour when the orchestrator should run
- `schedule.daily_hour` / `schedule.daily_minute` — time of day for daily snapshots
- `pipelines` — pipeline definitions with `sync`, `hourly_snapshot`, and `daily_snapshot` container names
- `scheduled_containers` — extra containers to run on a schedule
  - `name` — Docker container name
  - `period` — one of `hourly`, `daily`, or `every_N_hours`

## Notes

- The orchestrator uses `docker inspect` to decide whether a container is already running.
- If a scheduled container is already running, it will wait for completion instead of starting a duplicate.
