# Changelog

## 0.1.0 (2026-11-10)

- First release.
- MCP server for the BCB-SGS time series API (STDIO, Streamable HTTP and SSE).
- Curated dictionary of ~150 Brazilian macro series with Portuguese and English
  aliases, categories, units, frequencies and descriptions.
- `search_series` searches the dictionary offline and, optionally, the live BCB
  catalog (paginated, session-aware).
- `get_series` mirrors `GetBCBData::gbcbd_get_series`: multiple ids, long/wide
  output, chunked downloads for spans longer than the 10-year API limit,
  retries with exponential backoff, and per-series failure reporting.
- Parquet cache with coverage tracking (only missing ranges are downloaded),
  one parquet file per executed query, and cached catalog searches.
- `query_cache` runs read-only DuckDB SQL over `cached_series`,
  `cached_queries` and `series_metadata`.
- `get_series_stats` returns compact summaries for LLM-friendly answers.
- MCP resources for the dictionary and cache status, plus `macro_briefing` and
  `compare_series` prompts.
