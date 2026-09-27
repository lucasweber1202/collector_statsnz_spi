"""Exercise every write path against a real PostgreSQL server.

SQLite is not evidence for production SQL: it accepts untyped NULLs in a MERGE
source and has no MERGE at all. These tests run only when
``COLLECTOR_TEST_PG_URL`` points at a disposable PostgreSQL database (they drop
and recreate this collector's schema there), e.g.::

    COLLECTOR_TEST_PG_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/kinea_test pytest
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from scripts import init_db, metadata, run_logs, time_series
from scripts.config import COUNTRY_CURRENCY, SCHEMA_NAME
from scripts.extract import parse_csv
from scripts.releases import (
    FIRST_RELEASE,
    NEW_RELEASE,
    REVISED_SOURCE,
    SAME_RELEASE,
    classify_release,
    stored_release,
)
from scripts.time_series import Observation

PG_URL = os.getenv("COLLECTOR_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set COLLECTOR_TEST_PG_URL to a disposable PostgreSQL database")

DAY1 = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


CSV = (
    b"Series_reference,Period,Data_value,STATUS,UNITS,Subject,Group,Series_title_1,Series_title_2,Series_title_3\n"
    b"CPIM.SE901,2026.07,1380,FINAL,Index,CPI,Food Price Index for New Zealand,Food,NA,NA\n"
    b"CPIM.SE901,2026.08,1386,FINAL,Index,CPI,Food Price Index for New Zealand,Food,NA,NA\n"
    b"CPIM.SE9072020000,2026.07,1612,FINAL,Index,CPI,CPI Selected Components for New Zealand,Petrol,NA,NA\n"
    b"CPIM.SE9072020000,2026.08,1629,FINAL,Index,CPI,CPI Selected Components for New Zealand,Petrol,NA,NA\n"
    b"CPIM.SE904501,2026.08,1407,FINAL,Index,CPI,CPI Selected Components for New Zealand,Electricity,NA,NA\n"
)


def _sample() -> tuple[list[Observation], dict[str, dict[str, Any]]]:
    parsed = parse_csv(CSV, "https://www.stats.govt.nz/test.csv", date(2026, 9, 18), min_rows=1)
    return parsed.observations, parsed.catalog


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(PG_URL)
    assert eng.dialect.name == "postgresql"
    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
    init_db.init_db(eng)
    yield eng
    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE"))
    eng.dispose()


def _write(eng: Engine, observations: list[Observation], catalog: dict[str, dict[str, Any]], at: datetime) -> Any:
    with eng.begin() as conn:
        result = time_series.upsert_time_series(conn, observations, at)
        counts = metadata.upsert_metadata(conn, catalog, at)
    return result, counts


def _rows(eng: Engine, table: str) -> list[dict[str, Any]]:
    with eng.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {SCHEMA_NAME}.{table}")).mappings()]


def _replace(observations: list[Observation], series_id: str, when: date, value: float) -> list[Observation]:
    return [
        Observation(o.series_id, o.reference_date, value, o.snapshot_id)
        if (o.series_id, o.reference_date) == (series_id, when)
        else o
        for o in observations
    ]


def test_init_db_creates_the_canonical_tables_idempotently(engine: Engine) -> None:
    init_db.init_db(engine)
    with engine.connect() as conn:
        tables: set[str] = set(
            conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": SCHEMA_NAME}
            ).scalars()
        )
    assert tables == {"metadata", "time_series", "logs"}


def test_parser_output_passes_the_metadata_vocabulary() -> None:
    _, catalog = _sample()
    metadata.validate_catalog(catalog)
    assert {entry["country"] for entry in catalog.values()} == {COUNTRY_CURRENCY}


def test_first_run_then_unchanged_rerun_is_a_no_op(engine: Engine) -> None:
    observations, catalog = _sample()
    result, counts = _write(engine, observations, catalog, DAY1)
    assert result.new_observations == len(observations)
    assert counts == (len(catalog), 0)
    before_ts, before_md = _rows(engine, "time_series"), _rows(engine, "metadata")
    result, counts = _write(engine, observations, catalog, DAY1 + timedelta(days=3))
    assert (result.new_observations, result.new_vintages, result.same_day_updates, counts) == (0, 0, 0, (0, 0))
    assert _rows(engine, "time_series") == before_ts
    assert _rows(engine, "metadata") == before_md
    assert {r["vintage_date"] for r in before_ts} == {DAY1.date()}
    for row in before_md:
        dates = sorted(o.reference_date for o in observations if o.series_id == row["series_id"])
        assert row["country"] == COUNTRY_CURRENCY
        assert (row["first_observation"], row["last_observation"], row["observation_count"]) == (dates[0], dates[-1], len(dates))
        assert row["collected_at"] == DAY1.replace(tzinfo=None)


def test_later_revision_adds_a_vintage_and_keeps_the_old_one(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    target = observations[-1]
    later = DAY1 + timedelta(days=30)
    result, _ = _write(engine, _replace(observations, target.series_id, target.reference_date, target.value + 1), catalog, later)
    assert (result.new_observations, result.new_vintages, result.same_day_updates) == (0, 1, 0)
    rows = sorted(
        (r["vintage_date"], r["value"])
        for r in _rows(engine, "time_series")
        if (r["series_id"], r["reference_date"]) == (target.series_id, target.reference_date)
    )
    assert rows == [(DAY1.date(), target.value), (later.date(), target.value + 1)]
    md = next(r for r in _rows(engine, "metadata") if r["series_id"] == target.series_id)
    assert md["observation_count"] == sum(o.series_id == target.series_id for o in observations)


def test_same_day_revision_updates_that_days_vintage(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    target = observations[0]
    result, _ = _write(engine, _replace(observations, target.series_id, target.reference_date, target.value + 2), catalog, DAY1 + timedelta(hours=2))
    assert (result.new_observations, result.new_vintages, result.same_day_updates) == (0, 0, 1)
    rows = [r for r in _rows(engine, "time_series") if (r["series_id"], r["reference_date"]) == (target.series_id, target.reference_date)]
    assert len(rows) == 1 and rows[0]["value"] == target.value + 2
    assert rows[0]["collected_at"] == (DAY1 + timedelta(hours=2)).replace(tzinfo=None)


def test_metadata_merge_accepts_null_in_every_nullable_column(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    series_id = min(catalog)
    row: dict[str, Any] = {column: None for column in metadata._COLUMNS}
    row.update(series_id=series_id, name="n", country=COUNTRY_CURRENCY, observation_count=1, source_url="https://example.invalid/x", collected_at=DAY1)
    with engine.begin() as conn:
        conn.execute(metadata._merge_statement(1), metadata._batch_parameters([row]))
    stored = next(r for r in _rows(engine, "metadata") if r["series_id"] == series_id)
    for column in ("description", "frequency", "unit", "first_observation", "last_observation", "eco_group", "last_publish_date"):
        assert stored[column] is None, column
    _, counts = _write(engine, observations, catalog, DAY1 + timedelta(days=1))
    assert counts == (0, 1)


def test_time_series_merge_statement_runs_on_postgres(engine: Engine) -> None:
    observations, catalog = _sample()
    _write(engine, observations, catalog, DAY1)
    target = observations[0]
    row = {"series_id": target.series_id, "reference_date": target.reference_date, "vintage_date": DAY1.date(), "value": 123.25, "collected_at": DAY1 + timedelta(hours=1)}
    with engine.begin() as conn:
        conn.execute(time_series._merge_statement(1), time_series._batch_parameters([row]))
    rows = [r for r in _rows(engine, "time_series") if (r["series_id"], r["reference_date"]) == (target.series_id, target.reference_date)]
    assert [r["value"] for r in rows] == [123.25]


def test_run_log_insert_with_null_traceback_and_truncation(engine: Engine) -> None:
    run_logs.insert_run_log(engine, DAY1, DAY1, "success", "ok", None)
    run_logs.insert_run_log(engine, DAY1, DAY1, "error", "x" * 70000, "trace")
    rows = sorted(_rows(engine, "logs"), key=lambda r: r["id"])
    assert rows[0]["traceback"] is None and rows[0]["status"] == "success"
    assert len(rows[1]["log_text"]) == 65535 and rows[1]["log_text"].endswith("[..., truncated ...]")


def test_release_status_from_stored_state(engine: Engine) -> None:
    observations, catalog = _sample()
    ids = sorted(catalog)
    latest = max(o.reference_date for o in observations)
    published = max((entry["last_publish_date"] for entry in catalog.values() if entry["last_publish_date"]), default=None)
    with engine.connect() as conn:
        assert stored_release(conn, ids) is None
    assert classify_release(None, published, latest, 1) == FIRST_RELEASE
    _write(engine, observations, catalog, DAY1)
    with engine.connect() as conn:
        previous = stored_release(conn, ids)
    assert previous is not None and previous.published == published and previous.latest_reference == latest
    assert classify_release(previous, published, latest, 0) == SAME_RELEASE
    assert classify_release(previous, published, latest, 1) == REVISED_SOURCE
    later = None if published is None else published + timedelta(days=7)
    assert classify_release(previous, later, latest + timedelta(days=7), 1) == NEW_RELEASE
