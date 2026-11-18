"""Allow running via `python -m wechat_mp_export`."""

import sys
from wechat_mp_export.cli import main

if __name__ == "__main__":
    sys.exit(main())
