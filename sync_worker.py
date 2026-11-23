"""Compatibility entry point for deployed Azure commands (python sync_worker.py).

The code lives in ingestion/sync_worker.py; remove this file once the Azure apps/jobs run the new path.
"""

import sys

from ingestion.sync_worker import main

if __name__ == "__main__":
    sys.exit(main())
