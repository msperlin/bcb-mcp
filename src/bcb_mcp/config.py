"""Runtime configuration for bcb-mcp."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "bcb-mcp"
PACKAGE_NAME = "bcb_mcp"
__version__ = "0.1.1"

# --- BCB SGS endpoints -------------------------------------------------------
SGS_DATA_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series_id}/dados"
SGS_SEARCH_URL = "https://www3.bcb.gov.br/sgspub/localizarseries/localizarSeries.do"
SGS_SEARCH_HOME = "https://www3.bcb.gov.br/sgspub/"
USER_AGENT = f"{APP_NAME}/{__version__} (+https://github.com/msperlin/bcb-mcp)"

# --- HTTP behaviour ----------------------------------------------------------
HTTP_TIMEOUT_SECONDS = float(os.getenv("BCB_MCP_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("BCB_MCP_MAX_RETRIES", "3"))
# Minimum seconds between two requests to the BCB servers (be a good citizen).
MIN_REQUEST_INTERVAL = float(os.getenv("BCB_MCP_MIN_INTERVAL", "0.2"))
# Concurrent series downloads when several ids are requested at once.
MAX_CONCURRENCY = int(os.getenv("BCB_MCP_MAX_CONCURRENCY", "4"))

# The BCB data API rejects a single request spanning more than ~10 years.
# 3652 days = 10 years minus the extra leap day, keeping us safely inside.
MAX_DAYS_PER_REQUEST = 3652

# --- Output limits -----------------------------------------------------------
# 1,000 observations is already ~50k tokens of JSON per series; callers that
# want full history should raise max_rows explicitly or query the cache with SQL.
DEFAULT_MAX_ROWS = int(os.getenv("BCB_MCP_MAX_ROWS", "1000"))
MAX_ROWS_LIMIT = 100_000
DEFAULT_SEARCH_RESULTS = 25
MAX_SEARCH_RESULTS = 500

# --- Cache freshness ---------------------------------------------------------
# When a request touches the present, re-download the last few days once the
# cached copy is older than a few minutes, so provisional BCB revisions and new
# observations are picked up without refetching on every call.
TAIL_REFRESH_DAYS = int(os.getenv("BCB_MCP_TAIL_REFRESH_DAYS", "3"))
TAIL_REFRESH_MINUTES = int(os.getenv("BCB_MCP_TAIL_REFRESH_MINUTES", "60"))

# --- Cache -------------------------------------------------------------------
ENV_CACHE_DIR = "BCB_MCP_CACHE_DIR"


def get_cache_dir(override: str | Path | None = None) -> Path:
    """Resolve the cache directory, creating it if needed.

    Order of precedence: explicit argument > $BCB_MCP_CACHE_DIR > XDG cache home.
    """
    if override:
        path = Path(override).expanduser()
    elif os.getenv(ENV_CACHE_DIR):
        path = Path(os.environ[ENV_CACHE_DIR]).expanduser()
    else:
        base = os.getenv("XDG_CACHE_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".cache"
        path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
