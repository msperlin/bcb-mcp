"""bcb-mcp: MCP server for Brazilian Central Bank (BCB-SGS) time series.

Python counterpart of the R package GetBCBData: same ids, long/wide output,
chunked downloads for long histories, plus a parquet/DuckDB cache.
"""

from __future__ import annotations

from .config import __version__
from .dictionary import SeriesDictionary, get_dictionary
from .service import SeriesService

__all__ = ["SeriesDictionary", "SeriesService", "__version__", "get_dictionary"]
