# Stats NZ Selected Price Indexes

Authority: `guimasuko/collector_template` main `723f8633bbd367ad9cca0a199e84b10fd355da36`; `NZD` is in its `metadata.country` vocabulary and is what this collector emits.

The collector discovers the latest monthly [Selected price indexes release](https://www.stats.govt.nz/information-releases/selected-price-indexes-august-2026/) by checking recent reference months. It reads the release page's official CSV link and publication date, then downloads the full CSV from Stats NZ. There is no third-party data source.

The CSV contains Food Price Index levels, monthly selected CPI components, regional/national rents, seasonally adjusted variants, percentage changes, and weighted average prices for individual food items. The collector retains original **index levels** from the first three families. It excludes the 155 food item prices, percentage-change derivatives and seasonal-adjustment duplicates because these are either a different price-unit panel or derivable/secondary transformations. The CPI target is the separate quarterly `collector_statsnz_cpi`; these monthly components are predictors and must not be substituted for that target. Stats NZ warns that the monthly indexes and quarterly CPI are not simply interchangeable aggregations.

Each `series_id` preserves the native `Series_reference` (`CPIM.SE...`) and can be reversed by `parse_series_id`. Metadata derives names and descriptions from the official CSV. Publication date comes from the release page's `PublicationDate`, independent of monthly reference dates and collection timestamps. Blank official values are absent observations. `FINAL`, `REVISED` and `PROVISIONAL` are accepted source states; a changed value on later ingestion becomes a collector vintage. The source CSV is a current backfill, not evidence of the value's historical availability in its current form.

The per-series filter uses non-null observations, requires at least three years of history and a latest period within two calendar months. It runs before storage. A full workbook is fetched each run so a discontinued series with blank recent rows cannot masquerade as active. No official raw payload is committed.

Live check on 2026-09-26 UTC: the August 2026 CSV had 237 catalogue IDs and 59,983 source rows. The selected index families produced 60 retained IDs and 18,438 non-null observations dated 1960-01-31 through 2026-08-31, with no duplicate `(series_id, reference_date)` and no retained metadata lacking observations. Spot checks against CSV cells: petrol index `CPIM.SE9072020000` was 1102.0 on 2011-06-30, 1087.0 on 2019-01-31 and 1629.0 on 2026-08-31; electricity index `CPIM.SE904501` was 865.0, 1038.0 and 1407.0 on those dates; food index `CPIM.SE901` was 45.92346148 on 1960-01-31, 586.938102 on 1993-05-31 and 1386.0 on 2026-08-31. The opt-in live test rechecks source values on each run.

## Payload and layout checks

`check_payload` refuses an HTML/challenge response whatever its `Content-Type`,
a body that does not start with the audited `Series_reference` header, and an
implausibly small file; `parse_csv` also requires every audited column, at least
20,000 source rows and unique `(series_id, reference_date)` keys. Layout
failures raise `SourceLayoutError`; the run stops before any write and logs
`release_status=layout_changed`.

## Release monitoring

`scripts/releases.py` classifies the CSV on every run from the release page's
`PublicationDate` and the latest covered month, compared with what `metadata`
held before the run, and the rows the run changed: `first_release`,
`same_release`, `new_release`, `revised_source` (same publication, changed
values) or `layout_changed`. The run date is never evidence, so an unchanged
rerun on another day is `same_release`; a publication date that goes backwards
fails the run inside the write transaction.

## Verification (2026-09-27)

- PostgreSQL 16.13, live `main.py`: run 1 wrote 18,438 observations and 60
  metadata rows (`first_release`, August 2026 release published 2026-09-18);
  run 2 wrote nothing and left `collected_at` unchanged (`same_release`).
- `tests/test_postgres_integration.py`: canonical tables, idempotent rerun,
  later-day vintage, same-day overwrite, metadata MERGE with NULL in every
  nullable column, time-series MERGE, run-log NULL traceback/truncation,
  release classification.
- All emitted SQL parses with the Spark SQL grammar (pyspark 4.1.1).
  **Databricks corporate runtime: not verified.**
