"""Compatibility entry point for deployed Azure commands (python migrate_job.py).

The code lives in ops/migrate_job.py; remove this file once the Azure apps/jobs run the new path.
"""

from ops.migrate_job import main

if __name__ == "__main__":
    main()
