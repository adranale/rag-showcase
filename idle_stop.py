"""Compatibility entry point for deployed Azure commands (python idle_stop.py).

The code lives in ops/idle_stop.py; remove this file once the Azure apps/jobs run the new path.
"""

import sys

from ops.idle_stop import main

if __name__ == "__main__":
    sys.exit(main())
