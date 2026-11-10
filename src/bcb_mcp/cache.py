"""Parquet cache backed by DuckDB.

Layout of the cache directory (``$BCB_MCP_CACHE_DIR`` or ``~/.cache/bcb-mcp``)::

    parquet/series/sgs_<id>.parquet     consolidated, deduplicated series store
    parquet/queries/sgs_<id>__<a>__<b>.parquet  one file per executed query
    parquet/catalog/search_<hash>.parquet      cached catalog searches
    meta/sgs_<id>.json                  coverage, official metadata, timestamps

The consolidated store is the source of truth for reads: a request is only sent
to BCB for the date ranges that are not yet covered, which mirrors (and
improves on) the ``memoise``-based cache of the R package GetBCBData.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .config import get_cache_dir

logger = logging.getLogger("bcb_mcp.cache")

SERIES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ref_date", "DATE"),
    ("value", "DOUBLE"),
    ("series_id", "INTEGER"),
    ("series_name", "VARCHAR"),
    ("unit", "VARCHAR"),
    ("frequency", "VARCHAR"),
    ("source", "VARCHAR"),
    ("fetched_at", "VARCHAR"),
)
SERIES_SCHEMA = ", ".join(f"{name} {kind}" for name, kind in SERIES_COLUMNS)


def _empty_select() -> str:
    """A typed, empty relation matching the series schema."""
    cols = ", ".join(f"CAST(NULL AS {kind}) AS {name}" for name, kind in SERIES_COLUMNS)
    return f"SELECT {cols} LIMIT 0"


_FORBIDDEN_SQL = (
    "insert",
    "update",
    "delete",
    "drop",
    "create",
    "alter",
    "attach",
    "detach",
    "copy",
    "export",
    "import",
    "install",
    "load",
    "pragma",
    "set ",
    "call",
    "read_csv",
    "read_json",
    "read_parquet_metadata",
    "httpfs",
    "shell",
)

_ALLOWED_SQL_PREFIXES = ("select", "with", "describe", "show", "summarize", "explain")


@dataclass
class CacheEntry:
    series_id: int
    path: Path
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "file": self.path.name,
            "rows": self.meta.get("rows"),
            "coverage": self.meta.get("coverage", []),
            "series_name": self.meta.get("series_name"),
            "frequency": self.meta.get("frequency"),
            "updated_at": self.meta.get("updated_at"),
            "size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else None,
        }


class ParquetCache:
    """Read/write parquet cache with DuckDB as the query engine."""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self.root = get_cache_dir(cache_dir)
        self.series_dir = self.root / "parquet" / "series"
        self.query_dir = self.root / "parquet" / "queries"
        self.catalog_dir = self.root / "parquet" / "catalog"
        self.meta_dir = self.root / "meta"
        for path in (self.series_dir, self.query_dir, self.catalog_dir, self.meta_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    # -- paths ---------------------------------------------------------------
    def series_path(self, series_id: int) -> Path:
        return self.series_dir / f"sgs_{series_id}.parquet"

    def meta_path(self, series_id: int) -> Path:
        return self.meta_dir / f"sgs_{series_id}.json"

    def query_path(self, series_id: int, first: dt.date, last: dt.date) -> Path:
        return self.query_dir / f"sgs_{series_id}__{first.isoformat()}__{last.isoformat()}.parquet"

    # -- metadata / coverage -------------------------------------------------
    def read_meta(self, series_id: int) -> dict[str, Any] | None:
        path = self.meta_path(series_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt cache
            logger.warning("ignoring unreadable metadata file %s", path)
            return None

    def coverage(self, series_id: int) -> list[tuple[dt.date, dt.date]]:
        meta = self.read_meta(series_id)
        if not meta:
            return []
        ranges: list[tuple[dt.date, dt.date]] = []
        for item in meta.get("coverage", []):
            try:
                ranges.append((dt.date.fromisoformat(item[0]), dt.date.fromisoformat(item[1])))
            except (ValueError, IndexError, TypeError):
                continue
        return merge_ranges(ranges)

    def missing_ranges(
        self, series_id: int, first: dt.date, last: dt.date
    ) -> list[tuple[dt.date, dt.date]]:
        """Return the sub-ranges of [first, last] not present in the cache."""
        if not self.series_path(series_id).exists():
            return [(first, last)]
        covered = self.coverage(series_id)
        if not covered:
            return [(first, last)]

        gaps: list[tuple[dt.date, dt.date]] = []
        cursor = first
        for start, end in covered:
            if end < cursor:
                continue
            if start > last:
                break
            if start > cursor:
                gaps.append((cursor, min(start - dt.timedelta(days=1), last)))
            cursor = max(cursor, end + dt.timedelta(days=1))
            if cursor > last:
                break
        if cursor <= last:
            gaps.append((cursor, last))
        return [(start, end) for start, end in gaps if start <= end]

    # -- writing -------------------------------------------------------------
    def merge_series(
        self,
        series_id: int,
        rows: Iterable[dict[str, Any]],
        *,
        series_name: str,
        unit: str = "",
        frequency: str = "",
        source: str = "",
        covered: list[tuple[dt.date, dt.date]] | None = None,
    ) -> int:
        """Merge new observations into the consolidated series parquet."""
        new_rows = [row for row in rows if row.get("ref_date")]
        with self._write_lock:
            target = self.series_path(series_id)
            tmp_parquet = target.with_suffix(".parquet.tmp")
            if tmp_parquet.exists():
                tmp_parquet.unlink()

            if new_rows:
                with _ndjson_temp(new_rows) as ndjson_path:
                    existing = (
                        f"(SELECT * FROM read_parquet('{_esc(str(target))}'))"
                        if target.exists()
                        else f"({_empty_select()})"
                    )
                    sql = f"""
                        COPY (
                            WITH merged AS (
                                {existing}
                                UNION ALL
                                (SELECT ref_date::DATE, value::DOUBLE, series_id::INTEGER,
                                        series_name::VARCHAR, unit::VARCHAR,
                                        frequency::VARCHAR, source::VARCHAR,
                                        fetched_at::VARCHAR
                                 FROM read_json_auto('{_esc(str(ndjson_path))}'))
                            ),
                            ranked AS (
                                SELECT *, row_number() OVER (
                                    PARTITION BY ref_date ORDER BY fetched_at DESC
                                ) AS rn
                                FROM merged
                            )
                            SELECT ref_date, value, series_id, series_name, unit,
                                   frequency, source, fetched_at
                            FROM ranked WHERE rn = 1 ORDER BY ref_date
                        ) TO '{_esc(str(tmp_parquet))}' (FORMAT PARQUET)
                    """
                    _run(sql)
                    os.replace(tmp_parquet, target)

            total_rows = self._count_rows(target) if target.exists() else 0
            meta = self.read_meta(series_id) or {}
            ranges = list(meta.get("coverage", []))
            ranges = [
                [start.isoformat(), end.isoformat()]
                for start, end in merge_ranges([*_as_ranges(ranges), *(covered or [])])
            ]
            first_dt, last_dt = self._bounds(target) if target.exists() else (None, None)
            meta.update(
                {
                    "series_id": series_id,
                    "series_name": series_name,
                    "unit": unit or meta.get("unit", ""),
                    "frequency": frequency or meta.get("frequency", ""),
                    "source": source or meta.get("source", ""),
                    "rows": total_rows,
                    "coverage": ranges,
                    "first_date": first_dt.isoformat() if first_dt else None,
                    "last_date": last_dt.isoformat() if last_dt else None,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                }
            )
            _atomic_json(self.meta_path(series_id), meta)
            return total_rows

    def save_query(
        self,
        series_id: int,
        first: dt.date,
        last: dt.date,
        rows: Iterable[dict[str, Any]],
    ) -> Path | None:
        """Persist the exact result of one query as its own parquet file."""
        materialised = [row for row in rows if row.get("ref_date")]
        if not materialised:
            return None
        target = self.query_path(series_id, first, last)
        tmp = target.with_suffix(".parquet.tmp")
        with _ndjson_temp(materialised) as ndjson_path:
            sql = f"""
                COPY (
                    SELECT ref_date::DATE, value::DOUBLE, series_id::INTEGER,
                           series_name::VARCHAR, unit::VARCHAR, frequency::VARCHAR,
                           source::VARCHAR, fetched_at::VARCHAR
                    FROM read_json_auto('{_esc(str(ndjson_path))}')
                    ORDER BY ref_date
                ) TO '{_esc(str(tmp))}' (FORMAT PARQUET)
            """
            _run(sql)
        os.replace(tmp, target)
        return target

    def save_catalog_search(self, query: str, rows: list[dict[str, Any]]) -> Path | None:
        if not rows:
            return None
        digest = hashlib.sha1(query.lower().strip().encode("utf-8")).hexdigest()[:12]
        target = self.catalog_dir / f"search_{digest}.parquet"
        tmp = target.with_suffix(".parquet.tmp")
        with _ndjson_temp(rows) as ndjson_path:
            sql = f"""
                COPY (SELECT * FROM read_json_auto('{_esc(str(ndjson_path))}'))
                TO '{_esc(str(tmp))}' (FORMAT PARQUET)
            """
            _run(sql)
        os.replace(tmp, target)
        return target

    def read_catalog_search(
        self, query: str, max_age_hours: float = 24 * 7
    ) -> list[dict[str, Any]] | None:
        digest = hashlib.sha1(query.lower().strip().encode("utf-8")).hexdigest()[:12]
        path = self.catalog_dir / f"search_{digest}.parquet"
        if not path.exists():
            return None
        age_hours = (dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            return None
        return self._rows(f"SELECT * FROM read_parquet('{_esc(str(path))}')")

    # -- reading -------------------------------------------------------------
    def read_series(
        self, series_id: int, first: dt.date | None = None, last: dt.date | None = None
    ) -> list[dict[str, Any]]:
        path = self.series_path(series_id)
        if not path.exists():
            return []
        where = []
        if first:
            where.append(f"ref_date >= DATE '{first.isoformat()}'")
        if last:
            where.append(f"ref_date <= DATE '{last.isoformat()}'")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._rows(
            f"SELECT ref_date, value FROM read_parquet('{_esc(str(path))}') {clause} "
            f"ORDER BY ref_date"
        )

    def _rows(self, sql: str) -> list[dict[str, Any]]:
        con = duckdb.connect()
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
        finally:
            con.close()

    def _count_rows(self, path: Path) -> int:
        rows = self._rows(f"SELECT count(*) AS n FROM read_parquet('{_esc(str(path))}')")
        return int(rows[0]["n"]) if rows else 0

    def _bounds(self, path: Path) -> tuple[dt.date | None, dt.date | None]:
        rows = self._rows(
            f"SELECT min(ref_date) AS a, max(ref_date) AS b FROM read_parquet('{_esc(str(path))}')"
        )
        if not rows:
            return None, None
        return rows[0]["a"], rows[0]["b"]

    # -- SQL over the cache --------------------------------------------------
    def query(self, sql: str, limit: int = 1000) -> dict[str, Any]:
        """Run a read-only SQL query against the cached parquet files."""
        statement = sql.strip().rstrip(";")
        if not statement:
            raise ValueError("SQL statement is empty.")
        if ";" in statement:
            raise ValueError("Only a single SQL statement is allowed.")
        lowered = statement.lower()
        if not lowered.startswith(_ALLOWED_SQL_PREFIXES):
            raise ValueError("Only read-only statements (SELECT/WITH/DESCRIBE/SHOW) are allowed.")
        for token in _FORBIDDEN_SQL:
            if token in lowered:
                raise ValueError(f"Statement contains a forbidden keyword: {token!r}.")

        con = duckdb.connect()
        try:
            self._register_views(con)
            wrapped = f"SELECT * FROM ({statement}) AS _q LIMIT {int(limit)}"
            cursor = con.execute(wrapped)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            return {
                "columns": columns,
                "row_count": len(rows),
                "rows": [dict(zip(columns, row, strict=False)) for row in rows],
                "limit": limit,
            }
        finally:
            con.close()

    def _register_views(self, con: duckdb.DuckDBPyConnection) -> None:
        series_glob = self.series_dir / "sgs_*.parquet"
        if any(self.series_dir.glob("sgs_*.parquet")):
            con.execute(
                f"CREATE VIEW cached_series AS SELECT * FROM "
                f"read_parquet('{_esc(str(series_glob))}')"
            )
        else:
            con.execute(f"CREATE VIEW cached_series AS {_empty_select()}")

        query_glob = self.query_dir / "sgs_*.parquet"
        if any(self.query_dir.glob("sgs_*.parquet")):
            con.execute(
                f"CREATE VIEW cached_queries AS SELECT * FROM "
                f"read_parquet('{_esc(str(query_glob))}')"
            )
        else:
            con.execute(f"CREATE VIEW cached_queries AS {_empty_select()}")

        meta_glob = self.meta_dir / "sgs_*.json"
        if any(self.meta_dir.glob("sgs_*.json")):
            con.execute(
                f"CREATE VIEW series_metadata AS SELECT * FROM "
                f"read_json_auto('{_esc(str(meta_glob))}', union_by_name=true)"
            )
        else:
            con.execute(
                "CREATE VIEW series_metadata AS SELECT CAST(NULL AS INTEGER) AS series_id, "
                "CAST(NULL AS VARCHAR) AS series_name LIMIT 0"
            )

    # -- housekeeping --------------------------------------------------------
    def list_entries(self) -> list[CacheEntry]:
        entries = []
        for path in sorted(self.series_dir.glob("sgs_*.parquet")):
            try:
                series_id = int(path.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            entries.append(CacheEntry(series_id, path, self.read_meta(series_id) or {}))
        return entries

    def stats(self) -> dict[str, Any]:
        entries = self.list_entries()
        total_bytes = sum(e.path.stat().st_size for e in entries if e.path.exists())
        query_files = list(self.query_dir.glob("sgs_*.parquet"))
        catalog_files = list(self.catalog_dir.glob("search_*.parquet"))
        return {
            "cache_dir": str(self.root),
            "series_count": len(entries),
            "total_series_rows": sum(int(e.meta.get("rows") or 0) for e in entries),
            "total_size_mb": round(
                (
                    total_bytes
                    + sum(p.stat().st_size for p in query_files)
                    + sum(p.stat().st_size for p in catalog_files)
                )
                / 1e6,
                2,
            ),
            "query_files": len(query_files),
            "catalog_search_files": len(catalog_files),
            "series": [e.to_dict() for e in entries],
        }

    def clear(self, series_ids: list[int] | None = None) -> dict[str, Any]:
        removed: list[str] = []
        if series_ids:
            for series_id in series_ids:
                for path in (
                    self.series_path(int(series_id)),
                    self.meta_path(int(series_id)),
                    *self.query_dir.glob(f"sgs_{int(series_id)}__*.parquet"),
                ):
                    if path.exists():
                        path.unlink()
                        removed.append(str(path.relative_to(self.root)))
        else:
            for directory in (self.series_dir, self.query_dir, self.catalog_dir, self.meta_dir):
                for path in directory.glob("*"):
                    if path.is_file():
                        path.unlink()
                        removed.append(str(path.relative_to(self.root)))
        return {
            "cleared": bool(removed),
            "removed_count": len(removed),
            "removed": removed[:200],
            "cache_dir": str(self.root),
        }


# --- helpers ----------------------------------------------------------------


def merge_ranges(
    ranges: Iterable[tuple[dt.date, dt.date]],
) -> list[tuple[dt.date, dt.date]]:
    """Union overlapping/adjacent date intervals."""
    ordered = sorted((r for r in ranges if r[0] and r[1]), key=lambda r: r[0])
    merged: list[tuple[dt.date, dt.date]] = []
    for start, end in ordered:
        if end < start:
            continue
        if merged and start <= merged[-1][1] + dt.timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _as_ranges(items: Iterable[Any]) -> list[tuple[dt.date, dt.date]]:
    out = []
    for item in items:
        try:
            out.append((dt.date.fromisoformat(item[0]), dt.date.fromisoformat(item[1])))
        except (ValueError, IndexError, TypeError):
            continue
    return out


def _esc(value: str) -> str:
    return value.replace("'", "''")


def _run(sql: str) -> None:
    con = duckdb.connect()
    try:
        con.execute(sql)
    finally:
        con.close()


class _ndjson_temp:
    """Context manager writing rows to a temporary newline-delimited JSON file."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._path: Path | None = None

    def __enter__(self) -> Path:
        fd, name = tempfile.mkstemp(prefix="bcb_mcp_", suffix=".json")
        os.close(fd)
        path = Path(name)
        with path.open("w", encoding="utf-8") as handle:
            for row in self._rows:
                payload = {
                    key: (value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else value)
                    for key, value in row.items()
                }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._path = path
        return path

    def __exit__(self, *_exc: object) -> None:
        if self._path and self._path.exists():
            self._path.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(name)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
