"""Shared fixtures for the bcb-mcp test suite."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from bcb_mcp.cache import ParquetCache
from bcb_mcp.service import SeriesService


class FakeClient:
    """Deterministic stand-in for BCBClient (no network)."""

    def __init__(self, data: dict[int, list[tuple[dt.date, float]]] | None = None) -> None:
        self.data = data or {}
        self.calls: list[tuple[int, dt.date, dt.date]] = []
        self.catalog_calls: list[str] = []
        self.closed = False

    async def fetch_series(
        self, series_id: int, first_date: dt.date, last_date: dt.date
    ) -> list[tuple[dt.date, float]]:
        self.calls.append((series_id, first_date, last_date))
        rows = self.data.get(series_id, [])
        return [(d, v) for d, v in rows if first_date <= d <= last_date]

    async def fetch_series_range(
        self, series_id: int, first_date: dt.date, last_date: dt.date
    ) -> list[tuple[dt.date, float]]:
        return await self.fetch_series(series_id, first_date, last_date)

    async def search_catalog(self, text: str, max_results: int = 50) -> list[dict[str, Any]]:
        self.catalog_calls.append(text)
        return [
            {
                "id": 99999,
                "name": f"Fake series for {text}",
                "unit": "%",
                "frequency": "Monthly",
                "first_date": "01/01/2000",
                "last_date": "01/01/2025",
                "source": "TEST",
            }
        ][:max_results]

    async def close(self) -> None:
        self.closed = True


def monthly_series(
    start: dt.date, months: int, base: float = 1.0, step: float = 1.0
) -> list[tuple[dt.date, float]]:
    """Build a simple monthly series of (date, value) pairs."""
    rows = []
    year, month = start.year, start.month
    for index in range(months):
        rows.append((dt.date(year, month, 1), round(base + index * step, 6)))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return rows


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def cache(cache_dir: Path) -> ParquetCache:
    return ParquetCache(cache_dir)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient(
        {
            11: monthly_series(dt.date(2020, 1, 1), 24, base=0.05, step=0.01),
            433: monthly_series(dt.date(2020, 1, 1), 24, base=0.3, step=0.01),
            24369: monthly_series(dt.date(2020, 1, 1), 24, base=12.0, step=0.1),
        }
    )


@pytest.fixture
def service(cache: ParquetCache, fake_client: FakeClient) -> SeriesService:
    return SeriesService(cache=cache, client=fake_client)
