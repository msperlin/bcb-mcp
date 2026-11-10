"""Tests for the service layer (fetch + cache + reshape), with a fake HTTP client."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from bcb_mcp.config import DEFAULT_MAX_ROWS
from bcb_mcp.service import SeriesService, parse_date


async def test_get_series_long_format(service: SeriesService) -> None:
    out = await service.get_series(["selic"], first_date="2020-01-01", last_date="2021-12-31")
    assert out["format_data"] == "long"
    assert out["row_count"] == 24
    assert out["failures"] == []
    first = out["rows"][0]
    assert first["series_id"] == 11
    assert first["ref_date"] == "2020-01-01"
    # Per-row payload is minimal; metadata lives once in `series`.
    assert set(first) == {"ref_date", "value", "series_id"}
    meta = out["series"][0]
    assert meta["series_name"] == "Interest rate - Selic"
    assert meta["unit"] == "% p.d."


async def test_get_series_wide_format(service: SeriesService) -> None:
    out = await service.get_series(
        [11, 433], first_date="2020-01-01", last_date="2021-12-31", format_data="wide"
    )
    assert out["columns"][0] == "ref_date"
    assert len(out["columns"]) == 3
    assert out["row_count"] == 24
    assert out["rows"][0]["ref_date"] == "2020-01-01"


async def test_second_call_is_served_from_cache(service: SeriesService, fake_client) -> None:
    await service.get_series([11], first_date="2020-01-01", last_date="2021-12-31")
    calls_after_first = len(fake_client.calls)

    out = await service.get_series([11], first_date="2020-06-01", last_date="2020-12-31")
    assert len(fake_client.calls) == calls_after_first
    assert out["series"][0]["from_cache"] is True
    assert out["row_count"] == 7


async def test_only_missing_ranges_are_downloaded(service: SeriesService, fake_client) -> None:
    await service.get_series([11], first_date="2020-01-01", last_date="2020-12-31")
    await service.get_series([11], first_date="2020-01-01", last_date="2021-12-31")
    assert fake_client.calls[-1] == (11, dt.date(2021, 1, 1), dt.date(2021, 12, 31))


async def test_use_cache_false_forces_download(service: SeriesService, fake_client) -> None:
    await service.get_series([11], first_date="2020-01-01", last_date="2021-12-31")
    before = len(fake_client.calls)
    await service.get_series([11], first_date="2020-01-01", last_date="2021-12-31", use_cache=False)
    assert len(fake_client.calls) == before + 1


async def test_numeric_ids_without_dictionary_entry(service: SeriesService) -> None:
    out = await service.get_series([99999], first_date="2020-01-01", last_date="2021-12-31")
    # 99999 is not in the fake data, so the series fails but the call does not raise
    assert out["rows"] == []
    assert out["failures"][0]["series_id"] == 99999


async def test_unknown_alias_raises(service: SeriesService) -> None:
    with pytest.raises(ValueError, match="Could not resolve series"):
        await service.get_series(["this is not a series"], first_date="2020-01-01")


async def test_series_names_override(service: SeriesService) -> None:
    out = await service.get_series(
        [11], first_date="2020-01-01", last_date="2020-12-31", series_names=["Minha Selic"]
    )
    assert out["series"][0]["series_name"] == "Minha Selic"


async def test_invalid_dates(service: SeriesService) -> None:
    with pytest.raises(ValueError):
        await service.get_series([11], first_date="2021-01-01", last_date="2020-01-01")
    with pytest.raises(ValueError):
        parse_date("not a date")


async def test_invalid_format(service: SeriesService) -> None:
    with pytest.raises(ValueError, match="format_data"):
        await service.get_series([11], first_date="2020-01-01", format_data="tall")


async def test_max_rows_truncates_most_recent(service: SeriesService) -> None:
    out = await service.get_series(
        [11], first_date="2020-01-01", last_date="2021-12-31", max_rows=5
    )
    assert out["row_count"] == 5
    assert out["series"][0]["truncated"] is True
    assert out["rows"][-1]["ref_date"] == "2021-12-01"
    assert "hint" in out


async def test_stats(service: SeriesService) -> None:
    out = await service.get_series_stats([11], first_date="2020-01-01", last_date="2021-12-31")
    stat = out["stats"][0]
    assert stat["n_obs"] == 24
    assert stat["first_value"] == pytest.approx(0.05)
    assert stat["last_value"] == pytest.approx(0.28)
    assert stat["min"] == pytest.approx(0.05)
    assert stat["max"] == pytest.approx(0.28)
    assert stat["change_abs"] == pytest.approx(0.23)


async def test_query_cache(service: SeriesService) -> None:
    await service.get_series([11, 433], first_date="2020-01-01", last_date="2020-12-31")
    result = service.query_cache(
        "SELECT series_id, count(*) AS n FROM cached_series GROUP BY 1 ORDER BY 1"
    )
    assert result["rows"] == [{"series_id": 11, "n": 12}, {"series_id": 433, "n": 12}]
    assert "cached_series" in result["available_views"]


async def test_cache_status_and_clear(service: SeriesService) -> None:
    await service.get_series([11], first_date="2020-01-01", last_date="2020-12-31")
    status = service.cache_status()
    assert status["series_count"] == 1
    assert status["dictionary_size"] > 0

    cleared = service.clear_cache(["selic"])
    assert cleared["cleared"] is True
    assert service.cache_status()["series_count"] == 0


async def test_search_series_dictionary_only(service: SeriesService) -> None:
    out = await service.search_series("desemprego", include_online=False)
    assert out["results"][0]["id"] == 24369
    assert out["online_matches"] == 0
    assert out["dictionary_matches"] > 0


async def test_search_series_includes_online(service: SeriesService, fake_client) -> None:
    out = await service.search_series("something obscure", include_online=True)
    assert out["online_matches"] == 1
    assert out["results"][-1]["match"] == "bcb_catalog"
    assert fake_client.catalog_calls == ["something obscure"]


async def test_search_series_uses_cached_catalog(service: SeriesService, fake_client) -> None:
    await service.search_series("obscure thing", include_online=True)
    await service.search_series("obscure thing", include_online=True)
    assert fake_client.catalog_calls == ["obscure thing"]


async def test_list_and_describe(service: SeriesService) -> None:
    categories = service.list_categories()
    assert categories["total_series"] > 100

    listed = service.list_series(category="inflation")
    assert listed["total"] > 0

    described = service.describe_series(["selic", 999999])
    assert described["series"][0]["found"] is True
    assert described["series"][0]["unit"] == "% p.d."
    assert described["series"][1]["found"] is False


async def test_failed_series_does_not_break_others(service: SeriesService) -> None:
    out = await service.get_series([11, 999999], first_date="2020-01-01", last_date="2020-12-31")
    assert out["row_count"] == 12
    assert len(out["failures"]) == 1


def test_default_row_budget_is_llm_friendly() -> None:
    assert DEFAULT_MAX_ROWS <= 1000


async def test_future_last_date_is_clamped_to_today(service: SeriesService) -> None:
    today = dt.date.today()
    future = today + dt.timedelta(days=90)
    out = await service.get_series([11], first_date="2020-01-01", last_date=future.isoformat())
    assert out["last_date"] == today.isoformat()
    coverage = service.cache.read_meta(11)["coverage"]
    assert coverage[-1][1] == today.isoformat()


async def test_tail_refresh_picks_up_new_observations(service: SeriesService, fake_client) -> None:
    today = dt.date.today()
    start = today - dt.timedelta(days=40)
    fake_client.data[11] = [
        (start, 1.0),
        (today - dt.timedelta(days=1), 2.0),
    ]
    first = await service.get_series(
        [11], first_date=start.isoformat(), last_date=today.isoformat()
    )
    assert first["row_count"] == 2

    # A new observation appears and the cached copy is now considered old.
    fake_client.data[11].append((today, 3.0))
    meta = service.cache.read_meta(11)
    meta["updated_at"] = "2020-01-01T00:00:00+00:00"
    service.cache.meta_path(11).write_text(json.dumps(meta), encoding="utf-8")

    second = await service.get_series(
        [11], first_date=start.isoformat(), last_date=today.isoformat()
    )
    assert second["row_count"] == 3
    assert second["series"][0]["last_value"] == 3.0
    assert second["series"][0]["from_cache"] is False


async def test_wide_format_disambiguates_duplicate_names(service: SeriesService) -> None:
    out = await service.get_series(
        [11, 433],
        first_date="2020-01-01",
        last_date="2020-12-31",
        format_data="wide",
        series_names=["Same", "Same"],
    )
    assert out["columns"] == ["ref_date", "Same", "Same [433]"]
    assert out["rows"][0]["Same"] is not None
    assert out["warnings"]


async def test_unexpected_errors_are_not_swallowed(service: SeriesService) -> None:
    class BrokenClient:
        async def fetch_series(self, *_args, **_kwargs):
            raise RuntimeError("boom")

        async def fetch_series_range(self, *_args, **_kwargs):
            raise RuntimeError("boom")

        async def close(self) -> None:
            pass

    broken = SeriesService(cache=service.cache, client=BrokenClient())
    with pytest.raises(RuntimeError, match="boom"):
        await broken.get_series([11], first_date="2020-01-01", last_date="2020-12-31")
