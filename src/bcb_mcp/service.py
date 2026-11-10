"""Business logic for bcb-mcp: fetch, cache, reshape and summarise SGS series."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from .bcb_client import BCBClient, BCBError, SeriesNotFound
from .cache import ParquetCache
from .config import (
    DEFAULT_MAX_ROWS,
    DEFAULT_SEARCH_RESULTS,
    MAX_ROWS_LIMIT,
    MAX_SEARCH_RESULTS,
)
from .dictionary import SeriesDictionary, get_dictionary, normalize

logger = logging.getLogger("bcb_mcp.service")

DEFAULT_YEARS = 5


class SeriesService:
    """High-level API shared by the MCP tools."""

    def __init__(
        self,
        cache: ParquetCache | None = None,
        dictionary: SeriesDictionary | None = None,
        client: BCBClient | None = None,
    ) -> None:
        self.cache = cache or ParquetCache()
        self.dictionary = dictionary or get_dictionary()
        self._client = client
        self._owns_client = client is None

    # -- client lifecycle ----------------------------------------------------
    @property
    def client(self) -> BCBClient:
        if self._client is None:
            self._client = BCBClient()
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
            self._client = None

    # -- dictionary ----------------------------------------------------------
    def list_categories(self) -> dict[str, Any]:
        return {
            "dictionary_version": self.dictionary.version,
            "generated_at": self.dictionary.generated_at,
            "total_series": len(self.dictionary),
            "categories": self.dictionary.list_categories(),
        }

    def list_series(self, category: str | None = None, limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        if category:
            series = self.dictionary.by_category(category)
            if not series:
                return {
                    "category": category,
                    "total": 0,
                    "series": [],
                    "available_categories": [
                        c["category"] for c in self.dictionary.list_categories()
                    ],
                }
        else:
            series = self.dictionary.all_series()
        return {
            "category": category,
            "total": len(series),
            "returned": min(len(series), safe_limit),
            "series": [s.to_dict() for s in series[:safe_limit]],
        }

    def describe_series(self, series_ids: list[int | str]) -> dict[str, Any]:
        out = []
        for item in series_ids:
            info = self.dictionary.resolve(item)
            cached = self.cache.read_meta(int(info.id)) if info else None
            if info is None:
                numeric = _maybe_int(item)
                if numeric is None:
                    out.append({"input": item, "found": False, "reason": "unknown alias or id"})
                    continue
                out.append(
                    {
                        "input": item,
                        "found": False,
                        "reason": "series id is not in the curated dictionary; it may still exist in SGS",
                        "suggestion": self.dictionary.suggest(str(item)),
                    }
                )
                continue
            payload = info.to_dict()
            payload["found"] = True
            payload["cached"] = cached is not None
            if cached:
                payload["cache"] = {
                    "coverage": cached.get("coverage", []),
                    "rows": cached.get("rows"),
                    "updated_at": cached.get("updated_at"),
                }
            out.append(payload)
        return {"count": len(out), "series": out}

    async def search_series(
        self,
        query: str,
        max_results: int = DEFAULT_SEARCH_RESULTS,
        *,
        include_online: bool = True,
        category: str | None = None,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(max_results, MAX_SEARCH_RESULTS))
        results: list[dict[str, Any]] = []
        seen: set[int] = set()

        for info, score in self.dictionary.search(query, limit=safe_limit, category=category):
            item = info.to_dict()
            item["match"] = "dictionary"
            item["score"] = score
            results.append(item)
            seen.add(info.id)

        online_error: str | None = None
        online_added = 0
        if include_online and len(results) < safe_limit and query.strip():
            try:
                online_rows = await self._online_search(query, safe_limit)
            except BCBError as exc:
                online_error = str(exc)
                online_rows = []
            for row in online_rows:
                if row["id"] in seen or len(results) >= safe_limit:
                    continue
                seen.add(row["id"])
                info = self.dictionary.get(row["id"])
                item = (
                    info.to_dict()
                    if info
                    else {
                        "id": row["id"],
                        "name": row["name"],
                        "name_pt": "",
                        "category": "uncategorised",
                        "unit": row.get("unit", ""),
                        "frequency": row.get("frequency", ""),
                        "source": row.get("source", ""),
                        "description": "",
                        "aliases": [],
                    }
                )
                item["match"] = "bcb_catalog"
                item["first_date"] = row.get("first_date")
                item["last_date"] = row.get("last_date")
                results.append(item)
                online_added += 1

        return {
            "query": query,
            "category": category,
            "total_returned": len(results),
            "dictionary_matches": len(results) - online_added,
            "online_matches": online_added,
            "online_error": online_error,
            "results": results[:safe_limit],
            "hint": (
                "Pass any 'id' to get_series(). The BCB catalog uses English names; "
                "the bundled dictionary also understands Portuguese aliases."
            ),
        }

    async def _online_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        cached = self.cache.read_catalog_search(query)
        if cached is not None:
            return cached
        rows = await self.client.search_catalog(query, max_results=max(limit, 20))
        self.cache.save_catalog_search(query, rows)
        return rows

    # -- series data ---------------------------------------------------------
    async def get_series(
        self,
        series_ids: Iterable[int | str],
        *,
        first_date: dt.date | None = None,
        last_date: dt.date | None = None,
        format_data: str = "long",
        use_cache: bool = True,
        max_rows: int = DEFAULT_MAX_ROWS,
        series_names: list[str] | None = None,
    ) -> dict[str, Any]:
        ids = list(series_ids)
        if not ids:
            raise ValueError("series_ids must contain at least one id or alias.")

        first_date = parse_date(first_date)
        last_date = parse_date(last_date)
        last = last_date or dt.date.today()
        first = first_date or (last - dt.timedelta(days=365 * DEFAULT_YEARS))
        if last < first:
            raise ValueError("last_date must be on or after first_date.")
        if format_data not in ("long", "wide"):
            raise ValueError("format_data must be 'long' or 'wide'.")

        safe_max = max(1, min(int(max_rows), MAX_ROWS_LIMIT))

        resolved: list[tuple[int, str]] = []
        for index, item in enumerate(ids):
            info = self.dictionary.resolve(item)
            if info is not None:
                name = (
                    series_names[index]
                    if series_names and index < len(series_names) and series_names[index]
                    else info.name
                )
                resolved.append((info.id, name))
                continue
            numeric = _maybe_int(item)
            if numeric is None:
                suggestions = self.dictionary.suggest(str(item), limit=3)
                raise ValueError(
                    f"Could not resolve series {item!r}. "
                    f"Try search_series(); similar ids: {suggestions or 'none'}."
                )
            name = (
                series_names[index]
                if series_names and index < len(series_names) and series_names[index]
                else f"SGS {numeric}"
            )
            resolved.append((numeric, name))

        tasks = [
            self._prepare_series(series_id, first, last, use_cache=use_cache)
            for series_id, _ in resolved
        ]
        prepared = await asyncio.gather(*tasks, return_exceptions=True)

        series_meta: list[dict[str, Any]] = []
        long_rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for (series_id, name), outcome in zip(resolved, prepared, strict=False):
            if isinstance(outcome, Exception):
                failures.append(
                    {"series_id": series_id, "series_name": name, "error": str(outcome)}
                )
                continue

            observations, from_cache, fetched_ranges = outcome
            info = self.dictionary.get(series_id)
            unit = info.unit if info else ""
            frequency = info.frequency if info else ""
            source = info.source if info else ""
            display_name = name if name != f"SGS {series_id}" else (info.name if info else name)

            if not observations:
                failures.append(
                    {
                        "series_id": series_id,
                        "series_name": display_name,
                        "error": "no observations in the requested period",
                    }
                )
                continue

            truncated = len(observations) > safe_max
            selected = observations[-safe_max:] if truncated else observations
            fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

            rows = [
                {
                    "ref_date": obs["ref_date"].isoformat()
                    if isinstance(obs["ref_date"], dt.date)
                    else str(obs["ref_date"]),
                    "value": obs["value"],
                    "series_id": series_id,
                    "series_name": display_name,
                    "unit": unit,
                    "frequency": frequency,
                    "source": source,
                    "fetched_at": fetched_at,
                }
                for obs in selected
            ]
            long_rows.extend(rows)

            first_obs = observations[0]["ref_date"]
            last_obs = observations[-1]["ref_date"]
            series_meta.append(
                {
                    "series_id": series_id,
                    "series_name": display_name,
                    "unit": unit,
                    "frequency": frequency,
                    "source": source,
                    "observations": len(observations),
                    "returned_observations": len(selected),
                    "truncated": truncated,
                    "first_obs": first_obs.isoformat()
                    if hasattr(first_obs, "isoformat")
                    else str(first_obs),
                    "last_obs": last_obs.isoformat()
                    if hasattr(last_obs, "isoformat")
                    else str(last_obs),
                    "last_value": observations[-1]["value"],
                    "from_cache": from_cache,
                    "downloaded_ranges": [
                        [start.isoformat(), end.isoformat()] for start, end in fetched_ranges
                    ],
                }
            )

        payload: dict[str, Any] = {
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "format_data": format_data,
            "series": series_meta,
            "failures": failures,
        }

        if format_data == "wide":
            payload["columns"] = ["ref_date", *[meta["series_name"] for meta in series_meta]]
            payload["rows"] = _to_wide(long_rows)
        else:
            payload["columns"] = ["ref_date", "value", "series_id", "series_name", "unit"]
            payload["rows"] = long_rows

        payload["row_count"] = len(payload["rows"])
        if any(meta["truncated"] for meta in series_meta):
            payload["hint"] = (
                f"Each series was capped at {safe_max} observations (most recent kept). "
                "Narrow the date range or raise max_rows for the full history."
            )
        return payload

    async def _prepare_series(
        self, series_id: int, first: dt.date, last: dt.date, *, use_cache: bool
    ) -> tuple[list[dict[str, Any]], bool, list[tuple[dt.date, dt.date]]]:
        """Return observations for a range, downloading only what is missing."""
        info = self.dictionary.get(series_id)
        series_name = info.name if info else f"SGS {series_id}"
        unit = info.unit if info else ""
        frequency = info.frequency if info else ""
        source = info.source if info else ""

        gaps: list[tuple[dt.date, dt.date]] = []
        if not use_cache:
            gaps = [(first, last)]
        else:
            gaps = self.cache.missing_ranges(series_id, first, last)

        downloaded: list[tuple[dt.date, dt.date]] = []
        if gaps:
            for start, end in gaps:
                try:
                    observations = await self.client.fetch_series(series_id, start, end)
                except SeriesNotFound as exc:
                    raise BCBError(f"SGS {series_id} not found: {exc}") from exc
                downloaded.append((start, end))
                if not observations and len(gaps) == 1:
                    raise BCBError(
                        f"SGS {series_id} returned no data between {start} and {end}. "
                        "Check the id and the date range."
                    )
                fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                rows = [
                    {
                        "ref_date": ref_date,
                        "value": value,
                        "series_id": series_id,
                        "series_name": series_name,
                        "unit": unit,
                        "frequency": frequency,
                        "source": source,
                        "fetched_at": fetched_at,
                    }
                    for ref_date, value in observations
                ]
                if rows:
                    await asyncio.to_thread(
                        self.cache.merge_series,
                        series_id,
                        rows,
                        series_name=series_name,
                        unit=unit,
                        frequency=frequency,
                        source=source,
                        covered=[(start, end)],
                    )
                    await asyncio.to_thread(self.cache.save_query, series_id, start, end, rows)
                elif use_cache:
                    # remember the empty range so we do not retry it every call
                    await asyncio.to_thread(
                        self.cache.merge_series,
                        series_id,
                        [],
                        series_name=series_name,
                        unit=unit,
                        frequency=frequency,
                        source=source,
                        covered=[(start, end)],
                    )

        observations = await asyncio.to_thread(self.cache.read_series, series_id, first, last)
        if not observations and not gaps:
            raise BCBError(f"SGS {series_id} has no cached observations for the requested period.")
        return observations, not downloaded, downloaded

    async def get_series_stats(
        self,
        series_ids: Iterable[int | str],
        *,
        first_date: dt.date | None = None,
        last_date: dt.date | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        data = await self.get_series(
            series_ids,
            first_date=first_date,
            last_date=last_date,
            format_data="long",
            use_cache=use_cache,
            max_rows=MAX_ROWS_LIMIT,
        )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in data["rows"]:
            grouped.setdefault(row["series_id"], []).append(row)

        stats = []
        for meta in data["series"]:
            rows = grouped.get(meta["series_id"], [])
            values = [row["value"] for row in rows if row["value"] is not None]
            if not values:
                stats.append({**meta, "n_obs": 0})
                continue
            first_value = values[0]
            last_value = values[-1]
            change = last_value - first_value
            stats.append(
                {
                    "series_id": meta["series_id"],
                    "series_name": meta["series_name"],
                    "unit": meta["unit"],
                    "frequency": meta["frequency"],
                    "n_obs": len(values),
                    "first_obs": rows[0]["ref_date"],
                    "last_obs": rows[-1]["ref_date"],
                    "first_value": first_value,
                    "last_value": last_value,
                    "mean": round(sum(values) / len(values), 6),
                    "min": min(values),
                    "max": max(values),
                    "change_abs": round(change, 6),
                    "change_pct": round(change / abs(first_value) * 100, 4)
                    if first_value
                    else None,
                }
            )
        return {
            "first_date": data["first_date"],
            "last_date": data["last_date"],
            "count": len(stats),
            "failures": data.get("failures", []),
            "stats": stats,
        }

    # -- cache utilities -----------------------------------------------------
    def cache_status(self) -> dict[str, Any]:
        status = self.cache.stats()
        status["dictionary_size"] = len(self.dictionary)
        return status

    def clear_cache(self, series_ids: list[int | str] | None = None) -> dict[str, Any]:
        resolved: list[int] | None = None
        if series_ids:
            resolved = []
            for item in series_ids:
                info = self.dictionary.resolve(item)
                numeric = info.id if info else _maybe_int(item)
                if numeric is None:
                    raise ValueError(f"Could not resolve series {item!r}.")
                resolved.append(int(numeric))
        return self.cache.clear(resolved)

    def query_cache(self, sql: str, limit: int = 1000) -> dict[str, Any]:
        result = self.cache.query(sql, limit=max(1, min(int(limit), MAX_ROWS_LIMIT)))
        result["available_views"] = {
            "cached_series": "consolidated observations (ref_date, value, series_id, series_name, unit, frequency, source, fetched_at)",
            "cached_queries": "one row per executed query (same columns)",
            "series_metadata": "per-series metadata (series_id, series_name, coverage, rows, updated_at)",
        }
        return result


def _to_wide(long_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pivot long rows to one column per series (like format_data='wide')."""
    by_date: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in long_rows:
        key = row["ref_date"]
        if key not in by_date:
            by_date[key] = {"ref_date": key}
            order.append(key)
        by_date[key][row["series_name"]] = row["value"]
    return [by_date[key] for key in sorted(order)]


def _maybe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_date(value: str | dt.date | None, *, default: dt.date | None = None) -> dt.date | None:
    """Accept ISO (YYYY-MM-DD) or Brazilian (DD/MM/YYYY) date strings."""
    if value is None:
        return default
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date {value!r}; use YYYY-MM-DD or DD/MM/YYYY.")


def normalize_query_text(text: str) -> str:
    return normalize(text)
