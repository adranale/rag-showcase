"""Compatibility entry point for deployed Azure commands (python manage_users.py).

The code lives in ops/manage_users.py; remove this file once the Azure apps/jobs run the new path.
"""

from ops.manage_users import main

if __name__ == "__main__":
    main()
