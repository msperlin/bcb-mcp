# Changelog

## 0.1.1 (2026-11-10)

Fixes found while reviewing the first release.

- `BCB_MCP_TIMEOUT` and `BCB_MCP_MAX_CONCURRENCY` are now actually applied (the
  client was hard-coding 60s and 4 concurrent requests).
- A `last_date` in the future is clamped to today, so a request such as
  `last_date=2026-12-31` can no longer mark future dates as covered and freeze
  the series until that date.
- Requests that reach the present revalidate the last few days once the cached
  copy is old enough (`BCB_MCP_TAIL_REFRESH_DAYS` / `..._MINUTES`), picking up new
  observations and BCB revisions.
- Cache writes take a cross-process file lock and use unique temporary files, so
  several MCP clients can share `~/.cache/bcb-mcp` safely.
- `get_series` rows are compact (`ref_date`, `value`, `series_id`); metadata is
  returned once per series. The default `max_rows` drops from 5,000 to 1,000
  (a 5-year daily series was ~66k tokens of JSON).
- `format_data="wide"` disambiguates duplicate series names instead of silently
  overwriting a column, and reports a `warnings` entry.
- Unexpected exceptions are no longer converted into per-series "failures";
  only `BCBError` is isolated.
- `query_cache` strips string literals before checking keywords, so aliases such
  as `"set time"` work, and the blocklist now covers `read_text`, `read_csv`,
  `read_parquet`, `glob`, `SET`, and other file/system functions.
- Dictionary 1.1.0: 151 series, adding IPCA accumulated in 12 months (13522),
  core IPCA EX0 (11427) and smoothed trimmed-mean core (4466), plus Portuguese
  aliases such as `dívida pública` and `taxa de câmbio`. Multi-word queries now
  ignore function words.

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
