#!/usr/bin/env python3
"""Docker job orchestrator — command line entry point.

The implementation lives in the :mod:`orchestrator` package (one class per
module); this file stays so that the documented command keeps working:

    python docker_orchestrator.py --once

``python -m orchestrator`` does exactly the same thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running this file directly from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.cli import main  # noqa: E402 - import after sys.path setup

if __name__ == "__main__":
    sys.exit(main())
