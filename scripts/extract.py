"""Download the official Stats NZ Selected Price Indexes release CSV."""

from __future__ import annotations

import calendar
import csv
import hashlib
import html
import io
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from scripts.config import BACKOFF_FACTOR, DOWNLOAD_DELAY, MAX_RETRIES, REQUEST_TIMEOUT, USER_AGENT
from scripts.releases import ReleaseEvidence
from scripts.time_series import Observation

# Canonical metadata vocabulary produced by this source.
FREQUENCIES: frozenset[str] = frozenset({"monthly"})
UNITS: frozenset[str] = frozenset({"index"})
ECO_GROUPS: frozenset[str] = frozenset({"consumer_prices"})


logger = logging.getLogger(__name__)
COUNTRY_CURRENCY = "NZD"
SOURCE_ROOT = "https://www.stats.govt.nz"
MAX_STALE_MONTHS = 2
MIN_HISTORY_YEARS = 3
MIN_PAYLOAD_BYTES = 100_000
MIN_SOURCE_ROWS = 20_000
FIELDS = {
    "Series_reference",
    "Period",
    "Data_value",
    "STATUS",
    "UNITS",
    "Group",
    "Series_title_1",
    "Series_title_2",
    "Series_title_3",
}


class SourceLayoutError(ValueError):
    """The official release page or CSV no longer has the audited layout."""


class SourceAccessError(RuntimeError):
    """The source answered with something other than the requested file."""


@dataclass(frozen=True)
class SourceData:
    observations: list[Observation]
    catalog: dict[str, dict[str, Any]]
    releases: tuple[ReleaseEvidence, ...] = ()


def build_series_id(native: str) -> str:
    """Preserve the native dotted code in a parseable ID."""
    if not re.fullmatch(r"[A-Z0-9]+(?:\.[A-Z0-9]+)+", native):
        raise ValueError(f"Invalid native code: {native}")
    return "STATSNZ_SPI_" + native.replace(".", "_")


def parse_series_id(series_id: str) -> str:
    """Recover the precise Stats NZ code."""
    if not series_id.startswith("STATSNZ_SPI_"):
        raise ValueError(f"Invalid series_id: {series_id}")
    native = series_id.removeprefix("STATSNZ_SPI_").replace("_", ".")
    if build_series_id(native) != series_id:
        raise ValueError(f"Invalid series_id: {series_id}")
    return native


def discover_csv(client: httpx.Client, today: date) -> tuple[str, date, str]:
    """Find the latest released monthly CSV, not a hardcoded month."""
    for offset in range(4):
        index = today.year * 12 + today.month - 1 - offset
        year, month0 = divmod(index, 12)
        page = f"{SOURCE_ROOT}/information-releases/selected-price-indexes-{calendar.month_name[month0 + 1].lower()}-{year}/"
        response = _http_get(client, page)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        markup = html.unescape(response.text)
        match = re.search(r'"DocumentLink":"([^"]+\.csv)"', markup, re.IGNORECASE)
        published = re.search(r'"PublicationDate":"(\d{4}-\d{2}-\d{2})', markup)
        if not match or not published:
            raise SourceLayoutError(f"Stats NZ release layout changed: {page}")
        return (
            urljoin(SOURCE_ROOT, match.group(1).replace("\\/", "/")),
            date.fromisoformat(published.group(1)),
            page,
        )
    raise SourceAccessError("No recent official SPI release found")


def check_payload(response: httpx.Response) -> bytes:
    """Refuse an HTML challenge or error page before it can be parsed as CSV."""
    response.raise_for_status()
    blob = response.content
    content_type = response.headers.get("content-type", "").lower()
    head = blob[:512].lstrip().lower()
    if "text/html" in content_type or head.startswith((b"<!doctype", b"<html")):
        raise SourceAccessError(f"Stats NZ returned HTML instead of CSV: {response.url}")
    if not blob.removeprefix(b"\xef\xbb\xbf").lstrip(b'"').startswith(b"Series_reference"):
        raise SourceLayoutError(
            f"Stats NZ CSV does not start with the audited header: {response.url}"
        )
    if len(blob) < MIN_PAYLOAD_BYTES:
        raise SourceAccessError(f"Stats NZ CSV is implausibly small ({len(blob)} bytes)")
    return blob


def _selected(group: str, unit: str) -> bool:
    """Retain original monthly CPI components, food indexes and rents."""
    return (
        unit == "Index"
        and group.startswith(("CPI ", "Food Price Index "))
        and "Weighted Average Prices" not in group
        and "Seasonally adjusted" not in group
    )


def parse_csv(
    blob: bytes, url: str, published: date, min_rows: int = MIN_SOURCE_ROWS
) -> SourceData:
    """Parse finite index levels, native metadata, and monthly period ends."""
    reader = csv.DictReader(io.StringIO(blob.decode("utf-8-sig")))
    if not reader.fieldnames or not FIELDS.issubset(reader.fieldnames):
        raise SourceLayoutError("SPI CSV header changed")
    catalog: dict[str, dict[str, Any]] = {}
    observations: list[Observation] = []
    seen: set[tuple[str, date]] = set()
    snapshot = hashlib.sha256(blob).hexdigest()
    rows = 0
    for row in reader:
        rows += 1
        group = row["Group"]
        if not _selected(group, row["UNITS"]):
            continue
        sid = build_series_id(row["Series_reference"])
        titles = [
            v
            for key in ("Series_title_1", "Series_title_2", "Series_title_3")
            if (v := row[key]) not in ("", "NA")
        ]
        descriptor = {
            "name": " / ".join(titles) or row["Series_reference"],
            "description": f"Stats NZ SPI {group}; official code {row['Series_reference']}",
            "country": COUNTRY_CURRENCY,
            "frequency": "monthly",
            "unit": "index",
            "eco_group": "consumer_prices",
            "source_url": url,
            "last_publish_date": published,
        }
        if sid in catalog and catalog[sid] != descriptor:
            raise ValueError(f"Metadata conflict: {sid}")
        catalog[sid] = descriptor
        try:
            year, month = map(int, row["Period"].split("."))
            ref = date(year, month, calendar.monthrange(year, month)[1])
        except ValueError as exc:
            raise ValueError(f"Invalid SPI period: {row['Period']}") from exc
        raw = row["Data_value"]
        if raw in ("", "NA"):
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid SPI value {raw!r} for {sid}") from exc
        if not math.isfinite(value) or row["STATUS"] not in {"FINAL", "REVISED", "PROVISIONAL"}:
            raise ValueError(f"Invalid SPI value/status for {sid} on {ref}")
        if (sid, ref) in seen:
            raise ValueError(f"Duplicate SPI economic key: {sid} {ref}")
        seen.add((sid, ref))
        observations.append(Observation(sid, ref, value, snapshot))
    if rows < min_rows:
        raise SourceLayoutError(f"SPI CSV has {rows} rows; expected at least {min_rows}")
    if not observations:
        raise SourceLayoutError("SPI returned no selected observations")
    return SourceData(observations, catalog)


def filter_usable_series(data: SourceData, today: date) -> SourceData:
    """Remove dead or short histories using only non-null observations."""
    dates: dict[str, list[date]] = {}
    for obs in data.observations:
        dates.setdefault(obs.series_id, []).append(obs.reference_date)
    keep: set[str] = set()
    for sid, points in dates.items():
        first, last = min(points), max(points)
        stale = (today.year - last.year) * 12 + today.month - last.month
        history = (last.year - first.year) * 12 + last.month - first.month
        if stale <= MAX_STALE_MONTHS and history >= MIN_HISTORY_YEARS * 12:
            keep.add(sid)
        else:
            logger.info("Dropped %s: stale=%d months, history=%d months", sid, stale, history)
    if not keep:
        raise ValueError("No usable SPI series")
    return SourceData(
        [o for o in data.observations if o.series_id in keep],
        {s: v for s, v in data.catalog.items() if s in keep},
    )


def collect() -> SourceData:
    """Retrieve the current official CSV and filter series before storage."""
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        url, published, page = discover_csv(client, datetime.now(UTC).date())
        response = _http_get(client, url)
    parsed = parse_csv(check_payload(response), url, published)
    for fields in parsed.catalog.values():
        fields["source_url"] = page
    logger.info(
        "%s: %d candidate series and %d observations",
        page,
        len(parsed.catalog),
        len(parsed.observations),
    )
    usable = filter_usable_series(parsed, datetime.now(UTC).date())
    evidence = ReleaseEvidence(
        "selected_price_indexes_csv",
        url,
        published,
        max(o.reference_date for o in usable.observations),
        frozenset(usable.catalog),
    )
    return SourceData(usable.observations, usable.catalog, (evidence,))


def _http_get(client: httpx.Client, url: str) -> httpx.Response:
    """Retry transport failures, HTTP 429 and 5xx; preserve source-specific checks."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(DOWNLOAD_DELAY)
            response = client.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_FACTOR ** attempt
            logging.getLogger(__name__).warning(
                "GET failed, retry %d/%d in %.1fs", attempt, MAX_RETRIES, wait
            )
            time.sleep(wait)
    raise RuntimeError("COLLECTOR_MAX_RETRIES must be positive")


UPSTREAM_METADATA: dict[str, dict[str, Any]] = {}


def collect_raw_data(start_date: date | None = None) -> dict[date, dict[str, float | None]]:
    """Expose the canonical mapping and refresh upstream descriptors on every call."""
    UPSTREAM_METADATA.clear()
    data = collect()
    UPSTREAM_METADATA.update(data.catalog)
    parsed: dict[date, dict[str, float | None]] = {}
    for item in data.observations:
        if start_date is None or item.reference_date >= start_date:
            parsed.setdefault(item.reference_date, {})[item.series_id] = item.value
    logging.getLogger(__name__).info("Parsed %d dates", len(parsed))
    return parsed
