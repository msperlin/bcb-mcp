"""Tests for the parquet cache and the DuckDB layer."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from bcb_mcp.cache import ParquetCache, merge_ranges


def _row(ref_date: dt.date, value: float, series_id: int = 11, name: str = "Selic") -> dict:
    return {
        "ref_date": ref_date,
        "value": value,
        "series_id": series_id,
        "series_name": name,
        "unit": "% p.d.",
        "frequency": "Daily",
        "source": "TEST",
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }


def test_merge_ranges_unions_overlaps() -> None:
    a, b, c = dt.date(2024, 1, 1), dt.date(2024, 1, 10), dt.date(2024, 2, 1)
    merged = merge_ranges([(a, b), (b, c)])
    assert merged == [(a, c)]
    assert merge_ranges([]) == []


def test_merge_series_creates_parquet_and_metadata(cache: ParquetCache) -> None:
    rows = [_row(dt.date(2024, 1, day), day / 100) for day in range(1, 6)]
    total = cache.merge_series(
        11,
        rows,
        series_name="Selic",
        unit="% p.d.",
        frequency="Daily",
        source="TEST",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 5))],
    )
    assert total == 5
    assert cache.series_path(11).exists()
    meta = cache.read_meta(11)
    assert meta["rows"] == 5
    assert meta["coverage"] == [["2024-01-01", "2024-01-05"]]
    assert meta["first_date"] == "2024-01-01"
    assert meta["last_date"] == "2024-01-05"


def test_merge_series_deduplicates_and_keeps_latest(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, 1), 1.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 1))],
    )
    newer = _row(dt.date(2024, 1, 1), 2.0)
    newer["fetched_at"] = "2026-06-01T00:00:00+00:00"
    cache.merge_series(
        11,
        [newer, _row(dt.date(2024, 1, 2), 3.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 2))],
    )
    rows = cache.read_series(11)
    assert [row["value"] for row in rows] == [2.0, 3.0]


def test_read_series_filters_dates(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, day), float(day)) for day in range(1, 11)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 10))],
    )
    rows = cache.read_series(11, dt.date(2024, 1, 3), dt.date(2024, 1, 5))
    assert [row["ref_date"] for row in rows] == [
        dt.date(2024, 1, 3),
        dt.date(2024, 1, 4),
        dt.date(2024, 1, 5),
    ]


def test_missing_ranges_detects_gaps(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, 15), 1.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 15), dt.date(2024, 2, 15))],
    )
    gaps = cache.missing_ranges(11, dt.date(2024, 1, 1), dt.date(2024, 3, 1))
    assert gaps == [
        (dt.date(2024, 1, 1), dt.date(2024, 1, 14)),
        (dt.date(2024, 2, 16), dt.date(2024, 3, 1)),
    ]


def test_missing_ranges_empty_when_fully_covered(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, 15), 1.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 12, 31))],
    )
    assert cache.missing_ranges(11, dt.date(2024, 2, 1), dt.date(2024, 3, 1)) == []


def test_missing_ranges_for_unknown_series(cache: ParquetCache) -> None:
    assert cache.missing_ranges(11, dt.date(2024, 1, 1), dt.date(2024, 1, 31)) == [
        (dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    ]


def test_save_query_writes_one_file_per_range(cache: ParquetCache) -> None:
    rows = [_row(dt.date(2024, 1, day), float(day)) for day in range(1, 4)]
    path = cache.save_query(11, dt.date(2024, 1, 1), dt.date(2024, 1, 3), rows)
    assert path is not None and path.exists()
    assert path.name == "sgs_11__2024-01-01__2024-01-03.parquet"


def test_query_runs_sql_over_cached_series(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, day), float(day)) for day in range(1, 4)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 3))],
    )
    result = cache.query("SELECT count(*) AS n, sum(value) AS total FROM cached_series")
    assert result["rows"][0]["n"] == 3
    assert result["rows"][0]["total"] == 6.0


def test_query_exposes_metadata_view(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, 1), 1.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 1))],
    )
    result = cache.query("SELECT series_id, series_name FROM series_metadata")
    assert result["rows"] == [{"series_id": 11, "series_name": "Selic"}]


def test_query_works_with_empty_cache(cache: ParquetCache) -> None:
    result = cache.query("SELECT * FROM cached_series")
    assert result["rows"] == []
    assert result["columns"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE cached_series",
        "DELETE FROM cached_series",
        "COPY (SELECT 1) TO '/tmp/x.csv'",
        "INSTALL httpfs",
        "SET memory_limit='1GB'",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT 1; SELECT 2",
        "  ",
    ],
)
def test_query_rejects_unsafe_sql(cache: ParquetCache, sql: str) -> None:
    with pytest.raises(ValueError):
        cache.query(sql)


def test_query_ignores_keywords_inside_literals(cache: ParquetCache) -> None:
    assert cache.query("SELECT 1 AS \"set time\", 'drop table x' AS note")["rows"] == [
        {"set time": 1, "note": "drop table x"}
    ]


def test_query_enforces_limit(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, day), float(day)) for day in range(1, 11)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 10))],
    )
    result = cache.query("SELECT * FROM cached_series", limit=3)
    assert result["row_count"] == 3


def test_catalog_search_roundtrip(cache: ParquetCache) -> None:
    rows = [
        {
            "id": 11,
            "name": "Interest rate - Selic",
            "unit": "% p.d.",
            "frequency": "Daily",
            "first_date": "04/06/1986",
            "last_date": "09/11/2026",
            "source": "BCB-Demab",
        }
    ]
    assert cache.save_catalog_search("selic", rows) is not None
    cached = cache.read_catalog_search("selic")
    assert cached and cached[0]["id"] == 11
    assert cache.read_catalog_search("unknown") is None


def test_stats_and_clear(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, 1), 1.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 1))],
    )
    cache.save_query(11, dt.date(2024, 1, 1), dt.date(2024, 1, 1), [_row(dt.date(2024, 1, 1), 1.0)])
    stats = cache.stats()
    assert stats["series_count"] == 1
    assert stats["query_files"] == 1

    cleared = cache.clear([11])
    assert cleared["cleared"] is True
    assert cache.stats()["series_count"] == 0


def test_meta_is_valid_json(cache: ParquetCache) -> None:
    cache.merge_series(
        11,
        [_row(dt.date(2024, 1, 1), 1.0)],
        series_name="Selic",
        covered=[(dt.date(2024, 1, 1), dt.date(2024, 1, 1))],
    )
    payload = json.loads(cache.meta_path(11).read_text())
    assert payload["series_id"] == 11
    assert payload["coverage"] == [["2024-01-01", "2024-01-01"]]
