"""Check SPI native IDs, scope, non-null filters and live source cells."""

from __future__ import annotations

import csv
import io
import os
from datetime import UTC, date, datetime

import httpx
import pytest

from scripts.extract import (
    build_series_id,
    discover_csv,
    filter_usable_series,
    parse_csv,
    parse_series_id,
)


def test_id_roundtrip() -> None:
    assert parse_series_id(build_series_id("CPIM.SE9072020000")) == "CPIM.SE9072020000"
    with pytest.raises(ValueError):
        parse_series_id("STATSNZ_SPI_bad_CODE_7")


@pytest.mark.skipif(os.getenv("SPI_LIVE_SMOKE") != "1", reason="opt-in official source")
def test_live_source_cells_and_filters() -> None:
    with httpx.Client(timeout=30) as client:
        url, published, page = discover_csv(client, datetime.now(UTC).date())
        response = client.get(url)
        response.raise_for_status()
    assert page.startswith("https://www.stats.govt.nz/")
    parsed = parse_csv(response.content, url, published)
    retained = filter_usable_series(parsed, datetime.now(UTC).date())
    assert len(retained.catalog) <= len(parsed.catalog)
    assert {o.series_id for o in retained.observations} == set(retained.catalog)
    keys = [(o.series_id, o.reference_date) for o in retained.observations]
    assert len(keys) == len(set(keys))
    native_id = "CPIM.SE9072020000"
    rows = [r for r in csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))) if r["Series_reference"] == native_id and r["Data_value"]]
    selected = [rows[0], rows[len(rows) // 2], rows[-1]]
    observed = {(o.series_id, o.reference_date): o.value for o in retained.observations}
    for row in selected:
        year, month = (int(s) for s in row["Period"].split("."))
        import calendar

        ref = date(year, month, calendar.monthrange(year, month)[1])
        assert observed[(build_series_id(native_id), ref)] == float(row["Data_value"])
