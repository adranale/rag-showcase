"""Compatibility entry point for deployed Azure commands (python ui_start.py).

The code lives in ui/start.py; remove this file once the Azure apps/jobs run the new path.
"""

from ui.start import main

if __name__ == "__main__":
    main()
