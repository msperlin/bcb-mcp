#!/usr/bin/env python3
"""Maintain the bundled SGS dictionary.

Usage::

    python scripts/build_dictionary.py --check     # verify every id returns data
    python scripts/build_dictionary.py --refresh   # refresh name/unit/frequency from BCB
    python scripts/build_dictionary.py --refresh --check --write

The script talks to the public BCB endpoints, so it needs network access. It is a
development helper: the packaged dictionary is the JSON file it edits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcb_mcp.bcb_client import BCBClient  # noqa: E402
from bcb_mcp.dictionary import DATA_FILE  # noqa: E402

DICTIONARY_PATH = ROOT / "src" / "bcb_mcp" / "data" / DATA_FILE


def load() -> dict:
    return json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))


def save(payload: dict) -> None:
    payload["generated_at"] = dt.date.today().isoformat()
    DICTIONARY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def check(payload: dict) -> int:
    """Verify that every id returns at least one observation."""
    problems = 0
    async with BCBClient(min_interval=0.3) as client:
        for entry in payload["series"]:
            series_id = int(entry["id"])
            try:
                rows = await client.fetch_series_range(
                    series_id, dt.date.today() - dt.timedelta(days=365 * 3), dt.date.today()
                )
            except Exception as exc:
                print(f"FAIL {series_id:>6} {entry['name'][:60]}: {exc}")
                problems += 1
                continue
            if not rows:
                print(f"EMPTY {series_id:>6} {entry['name'][:60]} (no data in the last 3 years)")
                problems += 1
            else:
                last_date, last_value = rows[-1]
                print(f"ok   {series_id:>6} {entry['name'][:55]:<55} {last_date} {last_value}")
    return problems


async def refresh(payload: dict) -> int:
    """Refresh name/unit/frequency/source from the official catalog (matched by id)."""
    updated = 0
    async with BCBClient(min_interval=0.5) as client:
        for entry in payload["series"]:
            series_id = int(entry["id"])
            try:
                rows = await client.search_catalog(entry["name"], max_results=25)
            except Exception as exc:
                print(f"skip {series_id:>6}: search failed ({exc})")
                continue
            match = next((row for row in rows if row["id"] == series_id), None)
            if not match:
                print(f"skip {series_id:>6}: not found by name in the catalog")
                continue
            changed = {}
            for key in ("name", "unit", "frequency", "source"):
                value = match.get(key)
                if value and value != entry.get(key):
                    changed[key] = (entry.get(key), value)
                    entry[key] = value
            if changed:
                updated += 1
                print(
                    f"upd  {series_id:>6}: "
                    + ", ".join(f"{key} {old!r} -> {new!r}" for key, (old, new) in changed.items())
                )
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify ids against the live API")
    parser.add_argument("--refresh", action="store_true", help="refresh metadata from the catalog")
    parser.add_argument("--write", action="store_true", help="write changes back to the JSON file")
    args = parser.parse_args()

    payload = load()
    print(f"{len(payload['series'])} series in {DICTIONARY_PATH}")

    problems = 0
    if args.refresh:
        import asyncio

        updated = asyncio.run(refresh(payload))
        print(f"metadata updated for {updated} series")
        if args.write:
            save(payload)
            print("dictionary written")

    if args.check:
        import asyncio

        problems = asyncio.run(check(payload))
        print(f"{problems} problem(s) found")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
