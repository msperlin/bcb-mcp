#!/usr/bin/env python3
"""Top-level script entrypoint so the server can run without installing.

python server.py --stdio
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from bcb_mcp.server import cli_main  # noqa: E402

if __name__ == "__main__":
    cli_main()
