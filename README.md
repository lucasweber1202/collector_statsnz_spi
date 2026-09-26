# collector_statsnz_spi

Official Stats NZ monthly Selected Price Indexes used as predictors of New Zealand inflation: food indexes, rent, energy, fuel and selected transport and hospitality components.

Source: https://www.stats.govt.nz/information-releases/selected-price-indexes-august-2026/

## Quickstart

With Python 3.11, run `pip install -e .`, copy `.env.example` to `.env`, and configure `COLLECTOR_DB_URL` for PostgreSQL or `PROD=true` with Databricks settings. Run `python main.py` for the default incremental window, or `python main.py --start-date 1960-01-01` for a full backfill. This repository uses its own `collector_statsnz_spi` schema.
