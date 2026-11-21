"""Business logic for bcb-mcp: fetch, cache, reshape and summarise SGS series."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import statistics
from collections.abc import Iterable
from typing import Any

from .bcb_client import FOCUS_ENDPOINTS, BCBClient, BCBError, SeriesNotFound
from .cache import ParquetCache, merge_ranges
from .config import (
    DEFAULT_MAX_ROWS,
    DEFAULT_SEARCH_RESULTS,
    MAX_ROWS_LIMIT,
    MAX_SEARCH_RESULTS,
    TAIL_REFRESH_DAYS,
    TAIL_REFRESH_MINUTES,
)
from .dictionary import SeriesDictionary, get_dictionary, normalize

logger = logging.getLogger("bcb_mcp.service")

DEFAULT_YEARS = 5

RESAMPLE_FREQUENCIES = ("monthly", "quarterly", "annual")
AGG_METHODS = ("last", "mean", "sum", "compound")
TRANSFORMS = ("diff", "pct_change", "yoy", "acc12m", "index100")

# Minimum tail-refresh lookback by frequency so newly released observations
# (e.g. monthly IPCA dated on the 1st of the prior month) are never missed.
_FREQUENCY_TAIL_DAYS: dict[str, int] = {
    "daily": 3,
    "weekly": 14,
    "monthly": 60,
    "quarterly": 120,
    "annual": 400,
    "yearly": 400,
}

# Curated macro bundles for single-call snapshots (all IDs in bundled dictionary).
MACRO_SNAPSHOT_PRESETS: dict[str, dict[str, Any]] = {
    "overview": {
        "description": "Core Brazilian macro dashboard: policy rate, inflation, FX, activity, labor and public debt.",
        "series_ids": [432, 13522, 433, 1, 24364, 24369, 13762],
    },
    "inflation": {
        "description": "Headline IPCA (monthly & 12m), core IPCA measures, and IGP-M.",
        "series_ids": [433, 13522, 11427, 4466, 189],
    },
    "rates_and_fx": {
        "description": "Selic target, daily Selic, monthly Selic, annualized CDI, and USD/BRL exchange rate.",
        "series_ids": [432, 11, 4390, 4389, 1],
    },
    "activity": {
        "description": "IBC-Br, quarterly GDP (observed & seasonally adjusted), industrial production, services and unemployment.",
        "series_ids": [24364, 22109, 22099, 21859, 23982, 24369],
    },
    "fiscal": {
        "description": "Gross and net public debt (% GDP) and consolidated primary result.",
        "series_ids": [13762, 4513, 5364, 5507],
    },
    "external": {
        "description": "USD/BRL, international reserves, trade balance, current account and FDI.",
        "series_ids": [1, 3546, 22704, 22701, 22886],
    },
}

# Friendly English/Portuguese aliases -> exact BCB Olinda Focus `Indicador` strings.
FOCUS_INDICATOR_ALIASES: dict[str, str] = {
    "ipca": "IPCA",
    "inflation": "IPCA",
    "inflacao": "IPCA",
    "ipca 15": "IPCA-15",
    "ipca15": "IPCA-15",
    "igp m": "IGP-M",
    "igpm": "IGP-M",
    "igp di": "IGP-DI",
    "igpdi": "IGP-DI",
    "inpc": "INPC",
    "selic": "Selic",
    "interest rate": "Selic",
    "juros": "Selic",
    "pib": "PIB Total",
    "pib total": "PIB Total",
    "gdp": "PIB Total",
    "cambio": "Câmbio",
    "dolar": "Câmbio",
    "dollar": "Câmbio",
    "usd": "Câmbio",
    "usd brl": "Câmbio",
    "fx": "Câmbio",
    "exchange rate": "Câmbio",
    "desemprego": "Taxa de desocupação",
    "taxa de desocupacao": "Taxa de desocupação",
    "unemployment": "Taxa de desocupação",
    "resultado primario": "Resultado primário",
    "primary result": "Resultado primário",
    "resultado nominal": "Resultado nominal",
    "nominal result": "Resultado nominal",
    "divida liquida": "Dívida líquida do setor público",
    "net debt": "Dívida líquida do setor público",
    "divida bruta": "Dívida bruta do governo geral",
    "gross debt": "Dívida bruta do governo geral",
    "conta corrente": "Conta corrente",
    "current account": "Conta corrente",
    "balanca comercial": "Balança comercial",
    "trade balance": "Balança comercial",
    "fdi": "Investimento direto no país",
    "investimento direto": "Investimento direto no país",
}


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
            numeric = int(info.id) if info else _maybe_int(item)
            cached = self.cache.read_meta(numeric) if numeric is not None else None
            if info is None:
                if numeric is None:
                    out.append({"input": item, "found": False, "reason": "unknown alias or id"})
                    continue
                out.append(
                    {
                        "input": item,
                        "found": False,
                        "reason": "series id is not in the curated dictionary; it may still exist in SGS",
                        "suggestion": self.dictionary.suggest(str(item)),
                        "cached": cached is not None,
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
        resample: str | None = None,
        agg: str = "last",
        transform: str | None = None,
    ) -> dict[str, Any]:
        ids = list(series_ids)
        if not ids:
            raise ValueError("series_ids must contain at least one id or alias.")

        today = dt.date.today()
        first_date = parse_date(first_date)
        last_date = parse_date(last_date)
        # BCB never has future observations. Clamping to today prevents a
        # request such as last_date="2026-12-31" from marking future dates as
        # covered and freezing the series until that date arrives.
        last = min(last_date or today, today)
        first = first_date or (last - dt.timedelta(days=365 * DEFAULT_YEARS))
        if last < first:
            raise ValueError("last_date must be on or after first_date.")
        if format_data not in ("long", "wide"):
            raise ValueError("format_data must be 'long' or 'wide'.")
        if resample is not None and resample not in RESAMPLE_FREQUENCIES:
            raise ValueError(f"resample must be one of {RESAMPLE_FREQUENCIES} or None.")
        if agg not in AGG_METHODS:
            raise ValueError(f"agg must be one of {AGG_METHODS}.")
        if transform is not None and transform not in TRANSFORMS:
            raise ValueError(f"transform must be one of {TRANSFORMS} or None.")

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
            self._prepare_series(series_id, first, last, use_cache=use_cache, today=today)
            for series_id, _ in resolved
        ]
        prepared = await asyncio.gather(*tasks, return_exceptions=True)

        series_meta: list[dict[str, Any]] = []
        long_rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for (series_id, name), outcome in zip(resolved, prepared, strict=False):
            if isinstance(outcome, Exception):
                # Network/API errors are reported per series; anything else is a
                # bug and should not be silently turned into a "failure".
                if not isinstance(outcome, BCBError):
                    raise outcome
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

            if resample is not None:
                observations = _resample_observations(observations, resample, agg)
            if transform is not None:
                observations = _transform_observations(observations, transform)
            if not observations:
                failures.append(
                    {
                        "series_id": series_id,
                        "series_name": display_name,
                        "error": (
                            f"no observations remain after resample={resample!r} "
                            f"transform={transform!r}"
                        ),
                    }
                )
                continue

            truncated = len(observations) > safe_max
            selected = observations[-safe_max:] if truncated else observations

            # Keep the per-row payload minimal: repeating name/unit/frequency/
            # source on every observation costs more tokens than the data itself.
            # That metadata lives once in the `series` array.
            rows = [
                {
                    "ref_date": obs["ref_date"].isoformat()
                    if isinstance(obs["ref_date"], dt.date)
                    else str(obs["ref_date"]),
                    "value": obs["value"],
                    "series_id": series_id,
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

        # Two different ids can share a display name; make the wide columns unique
        # instead of silently overwriting one series with another.
        name_by_id: dict[int, str] = {}
        used_names: set[str] = set()
        warnings: list[str] = []
        for meta in series_meta:
            name = meta["series_name"]
            if name in used_names:
                name = f"{name} [{meta['series_id']}]"
                meta["series_name"] = name
                warnings.append(
                    f"Duplicate series name; column renamed to {name!r} to keep ids distinct."
                )
            used_names.add(name)
            name_by_id[meta["series_id"]] = name

        payload: dict[str, Any] = {
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "format_data": format_data,
            "series": series_meta,
            "failures": failures,
        }
        if resample is not None:
            payload["resample"] = resample
            payload["agg"] = agg
        if transform is not None:
            payload["transform"] = transform

        if format_data == "wide":
            payload["columns"] = ["ref_date", *[meta["series_name"] for meta in series_meta]]
            payload["rows"] = _to_wide(long_rows, name_by_id)
        else:
            payload["columns"] = ["ref_date", "value", "series_id"]
            payload["rows"] = long_rows

        if warnings:
            payload["warnings"] = warnings

        payload["row_count"] = len(payload["rows"])
        if any(meta["truncated"] for meta in series_meta):
            payload["hint"] = (
                f"Each series was capped at {safe_max} observations (most recent kept). "
                "Narrow the date range or raise max_rows for the full history."
            )
        return payload

    def _tail_refresh_range(
        self, series_id: int, first: dt.date, last: dt.date, today: dt.date
    ) -> tuple[dt.date, dt.date] | None:
        """Return a recent sub-range to re-download when the data may have moved.

        Only applies to windows that reach the present, and at most once every
        ``TAIL_REFRESH_MINUTES`` per series. Uses a frequency-aware lookback so
        monthly/quarterly releases (dated on the 1st of the prior period) are
        not missed when the cache already covers up to today.
        """
        if TAIL_REFRESH_DAYS <= 0 or last < today - dt.timedelta(days=1):
            return None
        meta = self.cache.read_meta(series_id)
        if meta:
            age = _age_minutes(meta.get("updated_at"))
            if age is not None and age < TAIL_REFRESH_MINUTES:
                return None
        info = self.dictionary.get(series_id)
        freq = (info.frequency if info else "") or (meta.get("frequency", "") if meta else "")
        lookback = max(TAIL_REFRESH_DAYS, _FREQUENCY_TAIL_DAYS.get(freq.strip().lower(), 0))
        start = max(first, today - dt.timedelta(days=lookback))
        return (start, last)

    async def _prepare_series(
        self,
        series_id: int,
        first: dt.date,
        last: dt.date,
        *,
        use_cache: bool,
        today: dt.date,
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

        # Revalidate the recent tail once in a while so new observations and
        # BCB revisions show up even when the range is nominally covered.
        tail = self._tail_refresh_range(series_id, first, last, today)
        if tail:
            gaps = merge_ranges([*gaps, tail])

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
            rows = [row for row in grouped.get(meta["series_id"], []) if row["value"] is not None]
            if not rows:
                stats.append({**meta, "n_obs": 0})
                continue
            values = [row["value"] for row in rows]
            first_value = values[0]
            last_value = values[-1]
            change = last_value - first_value
            min_row = min(rows, key=lambda r: r["value"])
            max_row = max(rows, key=lambda r: r["value"])

            prev_row = rows[-2] if len(rows) >= 2 else None
            period_abs = round(last_value - prev_row["value"], 6) if prev_row else None
            period_pct = (
                round((last_value - prev_row["value"]) / abs(prev_row["value"]) * 100, 4)
                if prev_row and prev_row["value"]
                else None
            )

            yoy_row = _find_yoy_row(rows)
            yoy_abs = round(last_value - yoy_row["value"], 6) if yoy_row else None
            yoy_pct = (
                round((last_value - yoy_row["value"]) / abs(yoy_row["value"]) * 100, 4)
                if yoy_row and yoy_row["value"]
                else None
            )

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
                    "prev_obs": prev_row["ref_date"] if prev_row else None,
                    "prev_value": prev_row["value"] if prev_row else None,
                    "period_change_abs": period_abs,
                    "period_change_pct": period_pct,
                    "yoy_obs": yoy_row["ref_date"] if yoy_row else None,
                    "yoy_value": yoy_row["value"] if yoy_row else None,
                    "yoy_change_abs": yoy_abs,
                    "yoy_change_pct": yoy_pct,
                    "mean": round(sum(values) / len(values), 6),
                    "median": round(statistics.median(values), 6),
                    "std": round(statistics.stdev(values), 6) if len(values) >= 2 else None,
                    "min": min_row["value"],
                    "min_date": min_row["ref_date"],
                    "max": max_row["value"],
                    "max_date": max_row["ref_date"],
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

    async def get_macro_snapshot(
        self,
        preset: str = "overview",
        *,
        years: int = 2,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Fetch summary statistics for a curated preset of Brazilian macro series."""
        key = preset.strip().lower()
        if key not in MACRO_SNAPSHOT_PRESETS:
            raise ValueError(
                f"Unknown preset {preset!r}. Available presets: {sorted(MACRO_SNAPSHOT_PRESETS)}."
            )
        spec = MACRO_SNAPSHOT_PRESETS[key]
        today = dt.date.today()
        safe_years = max(1, min(int(years), 30))
        first = today - dt.timedelta(days=365 * safe_years)
        result = await self.get_series_stats(
            spec["series_ids"],
            first_date=first,
            last_date=today,
            use_cache=use_cache,
        )
        return {
            "preset": key,
            "description": spec["description"],
            "available_presets": sorted(MACRO_SNAPSHOT_PRESETS),
            **result,
        }

    # -- Boletim Focus (Olinda OData) ----------------------------------------
    async def get_focus_expectations(
        self,
        indicator: str = "IPCA",
        *,
        horizon: str = "annual",
        first_date: dt.date | None = None,
        last_date: dt.date | None = None,
        reference_period: str | None = None,
        base_calculo: int = 0,
        smoothed: bool = True,
        max_rows: int = 200,
    ) -> dict[str, Any]:
        """Fetch Boletim Focus market expectations from BCB's Olinda OData API."""
        if horizon not in FOCUS_ENDPOINTS:
            raise ValueError(
                f"Unknown horizon {horizon!r}. Expected one of {sorted(FOCUS_ENDPOINTS)}."
            )
        if base_calculo not in (0, 1):
            raise ValueError("base_calculo must be 0 (30-day window) or 1 (5-business-day window).")

        resolved_indicator = (
            "Selic"
            if horizon == "selic_meetings"
            else FOCUS_INDICATOR_ALIASES.get(normalize(indicator), indicator.strip())
        )
        today = dt.date.today()
        last = min(parse_date(last_date) or today, today)
        first = parse_date(first_date) or (last - dt.timedelta(days=90))
        if last < first:
            raise ValueError("last_date must be on or after first_date.")

        safe_max = max(1, min(int(max_rows), 5000))
        raw_rows = await self.client.fetch_focus_expectations(
            horizon=horizon,
            indicator=resolved_indicator,
            first_date=first,
            last_date=last,
            reference_period=reference_period,
            base_calculo=base_calculo,
            smoothed=("S" if smoothed else "N") if horizon == "inflation_12m" else None,
            top=safe_max,
        )

        rows: list[dict[str, Any]] = []
        for item in raw_rows:
            row: dict[str, Any] = {
                "ref_date": item.get("Data"),
                "indicator": item.get("Indicador"),
                "median": item.get("Mediana"),
                "mean": item.get("Media"),
                "std": item.get("DesvioPadrao"),
                "min": item.get("Minimo"),
                "max": item.get("Maximo"),
                "respondents": item.get("numeroRespondentes"),
                "base_calculo": item.get("baseCalculo"),
            }
            if item.get("IndicadorDetalhe") is not None:
                row["indicator_detail"] = item.get("IndicadorDetalhe")
            if horizon == "selic_meetings":
                row["meeting"] = item.get("Reuniao")
            elif horizon == "inflation_12m":
                row["smoothed"] = item.get("Suavizada") == "S"
            else:
                row["reference_period"] = item.get("DataReferencia")
            rows.append(row)

        # Sort chronologically (ascending by ref_date, then target period).
        ref_key = "meeting" if horizon == "selic_meetings" else "reference_period"
        rows.sort(key=lambda r: (str(r.get("ref_date") or ""), str(r.get(ref_key) or "")))

        latest_date = rows[-1]["ref_date"] if rows else None
        latest_summary = [r for r in rows if r["ref_date"] == latest_date] if latest_date else []

        return {
            "source": "BCB Olinda Expectativas (Boletim Focus)",
            "horizon": horizon,
            "indicator": resolved_indicator,
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "reference_period": reference_period,
            "base_calculo": base_calculo,
            "latest_date": latest_date,
            "latest_by_reference": latest_summary,
            "row_count": len(rows),
            "rows": rows,
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


def _to_wide(long_rows: list[dict[str, Any]], name_by_id: dict[int, str]) -> list[dict[str, Any]]:
    """Pivot long rows to one column per series (like format_data='wide')."""
    by_date: dict[str, dict[str, Any]] = {}
    for row in long_rows:
        key = row["ref_date"]
        by_date.setdefault(key, {"ref_date": key})
        by_date[key][name_by_id[row["series_id"]]] = row["value"]
    return [by_date[key] for key in sorted(by_date)]


def _bucket_date(ref_date: dt.date, resample: str) -> dt.date:
    if resample == "monthly":
        return dt.date(ref_date.year, ref_date.month, 1)
    if resample == "quarterly":
        quarter_month = ((ref_date.month - 1) // 3) * 3 + 1
        return dt.date(ref_date.year, quarter_month, 1)
    if resample == "annual":
        return dt.date(ref_date.year, 1, 1)
    raise ValueError(f"Unsupported resample frequency: {resample!r}")


def _resample_observations(
    observations: list[dict[str, Any]], resample: str, agg: str
) -> list[dict[str, Any]]:
    """Aggregate observations into monthly, quarterly or annual buckets."""
    buckets: dict[dt.date, list[float]] = {}
    for obs in observations:
        val = obs["value"]
        if val is None:
            continue
        raw_date = obs["ref_date"]
        ref_date = (
            raw_date if isinstance(raw_date, dt.date) else dt.date.fromisoformat(str(raw_date))
        )
        buckets.setdefault(_bucket_date(ref_date, resample), []).append(float(val))

    out: list[dict[str, Any]] = []
    for bucket_dt in sorted(buckets):
        vals = buckets[bucket_dt]
        if agg == "last":
            agg_val = vals[-1]
        elif agg == "mean":
            agg_val = sum(vals) / len(vals)
        elif agg == "sum":
            agg_val = sum(vals)
        elif agg == "compound":
            agg_val = (math.prod(1.0 + v / 100.0 for v in vals) - 1.0) * 100.0
        else:  # pragma: no cover - validated upstream
            raise ValueError(f"Unsupported agg: {agg!r}")
        out.append({"ref_date": bucket_dt, "value": round(agg_val, 6)})
    return out


def _to_date(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _find_prior_year_value(
    pairs: list[tuple[dt.date, float]], index: int, tolerance_days: int = 15
) -> tuple[dt.date, float] | None:
    target_date = pairs[index][0]
    try:
        one_year_ago = target_date.replace(year=target_date.year - 1)
    except ValueError:
        # Feb 29 -> Feb 28
        one_year_ago = target_date.replace(year=target_date.year - 1, day=28)

    best: tuple[int, dt.date, float] | None = None
    for prior_date, prior_val in pairs[:index]:
        delta = abs((prior_date - one_year_ago).days)
        if delta <= tolerance_days and (best is None or delta < best[0]):
            best = (delta, prior_date, prior_val)
    return (best[1], best[2]) if best is not None else None


def _find_yoy_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rows) < 2:
        return None
    pairs = [(_to_date(row["ref_date"]), float(row["value"])) for row in rows]
    found = _find_prior_year_value(pairs, len(pairs) - 1)
    if found is None:
        return None
    ref_date, value = found
    return {"ref_date": ref_date.isoformat(), "value": value}


def _transform_observations(
    observations: list[dict[str, Any]], transform: str
) -> list[dict[str, Any]]:
    """Apply a derived time-series transformation."""
    pairs = [
        (_to_date(obs["ref_date"]), float(obs["value"]))
        for obs in observations
        if obs["value"] is not None
    ]
    if not pairs:
        return []

    out: list[dict[str, Any]] = []
    if transform == "diff":
        for i in range(1, len(pairs)):
            out.append({"ref_date": pairs[i][0], "value": round(pairs[i][1] - pairs[i - 1][1], 6)})
    elif transform == "pct_change":
        for i in range(1, len(pairs)):
            prev = pairs[i - 1][1]
            if prev:
                out.append(
                    {"ref_date": pairs[i][0], "value": round((pairs[i][1] / prev - 1.0) * 100.0, 6)}
                )
    elif transform == "yoy":
        for i in range(1, len(pairs)):
            prior = _find_prior_year_value(pairs, i)
            if prior is not None and prior[1]:
                out.append(
                    {
                        "ref_date": pairs[i][0],
                        "value": round((pairs[i][1] / prior[1] - 1.0) * 100.0, 6),
                    }
                )
    elif transform == "acc12m":
        for i in range(11, len(pairs)):
            window = [v for _, v in pairs[i - 11 : i + 1]]
            compounded = (math.prod(1.0 + v / 100.0 for v in window) - 1.0) * 100.0
            out.append({"ref_date": pairs[i][0], "value": round(compounded, 6)})
    elif transform == "index100":
        base = pairs[0][1]
        if base:
            for ref_date, val in pairs:
                out.append({"ref_date": ref_date, "value": round(val / base * 100.0, 6)})
    else:  # pragma: no cover - validated upstream
        raise ValueError(f"Unsupported transform: {transform!r}")
    return out


def _age_minutes(timestamp: str | None) -> float | None:
    """Minutes elapsed since an ISO-8601 timestamp written by the cache."""
    if not timestamp:
        return None
    try:
        moment = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - moment).total_seconds() / 60.0


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
