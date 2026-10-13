"""Build exoplanets.db from the NASA Exoplanet Archive Planetary Systems (PS) CSV.

Two layers are written to a single SQLite file:

  planet_params  Raw layer. Every PS row and every column (error columns included),
                 plus a surrogate key `param_set_id` (the row's position in the CSV).
  planets        Curated layer. Exactly one row per planet, chosen by the selection
                 rule below, carrying the same columns plus a few selection fields.

Selection rule (also documented in the app's Notes tab):
  1. Score every row by completeness over SCORE_WEIGHTS: one point per populated
     field, with radius and mass counting double.
  2. Highest score wins. Ties go to the most recent study (pl_pubdate), then
     rowupdate, releasedate, NASA's default_flag, and finally file order.
  3. No planet is ever dropped: a sparse planet keeps its best available row.

Usage:
    python build_database.py [--csv data/PS.csv] [--db exoplanets.db]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "PS.csv"
DB_PATH = ROOT / "exoplanets.db"

# Fields that vary between studies and define a row's completeness. Radius and mass
# are the headline numbers, so they count double: a row carrying them can outrank a
# slightly more complete row that lacks them. sy_* photometry is deliberately excluded
# because it is catalog-level and nearly identical across a planet's rows.
SCORE_WEIGHTS = {
    "pl_orbper": 1,
    "pl_orbsmax": 1,
    "pl_rade": 2,
    "pl_bmasse": 2,
    "pl_insol": 1,
    "pl_eqt": 1,
    "st_teff": 1,
    "st_rad": 1,
    "st_mass": 1,
}
MAX_COMPLETENESS = len(SCORE_WEIGHTS)
MAX_SCORE = sum(SCORE_WEIGHTS.values())

# Without these the selection rule or the core views cannot work, so the build fails.
REQUIRED_COLUMNS = [
    "pl_name", "hostname", "default_flag", "discoverymethod", "disc_year",
    "soltype", "pl_controv_flag", "pl_refname", "pl_pubdate", "rowupdate",
    "releasedate", "sy_dist", *SCORE_WEIGHTS,
]
# Shown in the app when present; reported (not fatal) when missing.
EXPECTED_COLUMNS = [
    "disc_facility", "disc_telescope", "sy_snum", "sy_pnum", "st_spectype",
    "pl_bmassprov", "sy_vmag", "sy_bmag", "sy_gaiamag", "sy_jmag", "sy_hmag",
    "sy_kmag", "sy_tmag", "sy_kepmag", "ra", "dec",
]

# Free-text and date columns. Every other column is coerced to a number.
TEXT_COLUMNS = {
    "pl_name", "hostname", "discoverymethod", "disc_refname", "disc_facility",
    "disc_telescope", "disc_instrument", "soltype", "pl_refname", "pl_bmassprov",
    "st_refname", "st_spectype", "st_metratio", "sy_refname", "rastr", "decstr",
    "rowupdate", "pl_pubdate", "releasedate",
}
# Whole-number columns (flags, counts, years) stored as INTEGER rather than REAL.
INTEGER_COLUMNS = {
    "default_flag", "sy_snum", "sy_pnum", "cb_flag", "disc_year",
    "pl_controv_flag", "ttv_flag",
}


class MissingColumnsError(RuntimeError):
    """Raised when the source CSV lacks columns the build depends on."""


def read_ps_csv(csv_path: Path) -> tuple[pd.DataFrame, str | None]:
    """Read the PS CSV, skipping only the leading '#' metadata block.

    pandas' comment='#' is not safe here: it truncates any line containing '#'
    anywhere, and some reference fields contain HTML entities such as '&#x131;'.
    Returns the frame and the archive's export timestamp from the header, if found.
    """
    header_lines = []
    with open(csv_path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#"):
                break
            header_lines.append(line)
    df = pd.read_csv(csv_path, skiprows=len(header_lines), low_memory=False)

    exported_at = None
    for line in header_lines[:5]:
        text = line.lstrip("#").strip()
        try:
            exported_at = datetime.strptime(text, "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%d %H:%M")
            break
        except ValueError:
            continue
    return df, exported_at


def check_columns(columns) -> tuple[list[str], list[str]]:
    """Return (missing required columns, missing expected columns)."""
    present = set(columns)
    return (
        [c for c in REQUIRED_COLUMNS if c not in present],
        [c for c in EXPECTED_COLUMNS if c not in present],
    )


def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Coerce types. Returns the cleaned frame and {column: values nulled by coercion}."""
    df = df.copy()
    coerced = {}
    for col in df.columns:
        if col in TEXT_COLUMNS:
            df[col] = df[col].astype("string").str.strip()
            continue
        numeric = pd.to_numeric(df[col], errors="coerce")
        lost = int(numeric.isna().sum() - df[col].isna().sum())
        if lost > df[col].notna().sum() / 2:
            # Mostly non-numeric: an unfamiliar text column, so keep it untouched.
            df[col] = df[col].astype("string")
            continue
        if lost:
            coerced[col] = lost
        is_integer_col = col in INTEGER_COLUMNS or col.endswith("lim")
        if is_integer_col and (numeric.dropna() % 1 == 0).all():
            numeric = numeric.astype("Int64")
        df[col] = numeric
    df.insert(0, "param_set_id", pd.array(range(1, len(df) + 1), dtype="Int64"))
    return df, coerced


def _not_null(col: str) -> str:
    return f'("{col}" IS NOT NULL)'


COMPLETENESS_SQL = " + ".join(_not_null(c) for c in SCORE_WEIGHTS)
SCORE_SQL = " + ".join(f"{w} * {_not_null(c)}" for c, w in SCORE_WEIGHTS.items())
# Most recent study first. substr() normalizes '2017-08', '2017-08-31' and
# '2017-08-31 00:00' to comparable ISO prefixes.
RECENCY_SQL = "COALESCE(substr(pl_pubdate, 1, 10), substr(rowupdate, 1, 10), substr(releasedate, 1, 10))"
SELECTION_ORDER_SQL = f"""
    selection_score DESC,
    {RECENCY_SQL} DESC,
    rowupdate DESC,
    releasedate DESC,
    default_flag DESC,
    param_set_id"""


def _create_planets_sql(raw_columns: list[tuple[str, str]]) -> str:
    col_defs = ",\n    ".join(f'"{name}" {sql_type}' for name, sql_type in raw_columns)
    return f"""
CREATE TABLE planets (
    {col_defs},
    disc_decade TEXT,
    completeness INTEGER,
    selection_score INTEGER,
    n_param_sets INTEGER,
    UNIQUE (pl_name),
    FOREIGN KEY (param_set_id) REFERENCES planet_params (param_set_id)
)"""


def _select_planets_sql(cols: str) -> str:
    """INSERT the top-ranked parameter set per planet. `cols` is the raw column list."""
    return f"""
WITH scored AS (
    SELECT *,
           {COMPLETENESS_SQL} AS completeness,
           {SCORE_SQL} AS selection_score
    FROM planet_params
    WHERE pl_name IS NOT NULL
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY pl_name ORDER BY {SELECTION_ORDER_SQL}) AS selection_rank,
           COUNT(*) OVER (PARTITION BY pl_name) AS n_param_sets
    FROM scored
)
INSERT INTO planets ({cols}, disc_decade, completeness, selection_score, n_param_sets)
SELECT {cols},
       CAST(disc_year / 10 * 10 AS TEXT) || 's',
       completeness,
       selection_score,
       n_param_sets
FROM ranked
WHERE selection_rank = 1"""


def build(csv_path: Path = CSV_PATH, db_path: Path = DB_PATH, verbose: bool = True) -> dict:
    """Rebuild both tables from scratch and return a summary dict."""
    started = time.perf_counter()
    if not Path(csv_path).exists():
        raise FileNotFoundError(
            f"Source CSV not found at {csv_path}. Download the Planetary Systems (PS) table "
            "as CSV from https://exoplanetarchive.ipac.caltech.edu and save it there."
        )
    raw, exported_at = read_ps_csv(csv_path)

    missing_required, missing_expected = check_columns(raw.columns)
    if missing_required:
        raise MissingColumnsError(
            f"{Path(csv_path).name} is missing required column(s): {', '.join(missing_required)}. "
            "The archive may have renamed them; update REQUIRED_COLUMNS in build_database.py."
        )

    df, coerced = clean(raw)

    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("DROP TABLE IF EXISTS planets")
            conn.execute("DROP TABLE IF EXISTS planet_params")
            conn.execute("DROP TABLE IF EXISTS build_info")

            conn.execute(pd.io.sql.get_schema(df, "planet_params", keys="param_set_id", con=conn))
            df.to_sql("planet_params", conn, if_exists="append", index=False, chunksize=2000)
            conn.execute("CREATE INDEX idx_planet_params_pl_name ON planet_params (pl_name)")

            raw_columns = [(row[1], row[2]) for row in conn.execute("PRAGMA table_info(planet_params)")]
            conn.execute(_create_planets_sql(raw_columns))
            conn.execute(_select_planets_sql(", ".join(f'"{name}"' for name, _ in raw_columns)))
            conn.execute("CREATE INDEX idx_planets_method_year ON planets (discoverymethod, disc_year)")

            counts = conn.execute("""
                SELECT (SELECT COUNT(*) FROM planet_params),
                       (SELECT COUNT(*) FROM planets),
                       (SELECT COUNT(DISTINCT pl_name) FROM planet_params WHERE pl_name IS NOT NULL),
                       (SELECT COUNT(*) FROM planet_params WHERE pl_name IS NULL),
                       (SELECT COUNT(DISTINCT hostname) FROM planets),
                       (SELECT COUNT(*) FROM planets WHERE default_flag = 0)
            """).fetchone()
            raw_rows, planet_rows, distinct_planets, unnamed_rows, hosts, not_default = counts
            if planet_rows != distinct_planets:
                raise RuntimeError(
                    f"Selection produced {planet_rows} rows for {distinct_planets} distinct planets."
                )

            built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            conn.execute("CREATE TABLE build_info (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO build_info VALUES (?, ?)", [
                ("source_file", Path(csv_path).name),
                ("archive_exported_at", exported_at or "unknown"),
                ("built_at", built_at),
                ("missing_expected_columns", ",".join(missing_expected)),
            ])
        conn.execute("VACUUM")  # reclaim pages freed by the DROPs; must run outside a transaction

    summary = {
        "db_path": str(db_path),
        "raw_rows": raw_rows,
        "raw_columns": len(raw_columns),
        "planet_rows": planet_rows,
        "distinct_planets": distinct_planets,
        "host_stars": hosts,
        "selected_not_default": not_default,
        "unnamed_rows_skipped": unnamed_rows,
        "missing_expected_columns": missing_expected,
        "values_coerced_to_null": coerced,
        "archive_exported_at": exported_at,
        "seconds": round(time.perf_counter() - started, 1),
    }
    if verbose:
        print_summary(summary)
    return summary


def print_summary(s: dict) -> None:
    pct = 100 * s["selected_not_default"] / s["planet_rows"] if s["planet_rows"] else 0
    print(f"Built {s['db_path']} in {s['seconds']} s (archive export: {s['archive_exported_at'] or 'unknown'})")
    print(f"  planet_params (raw):   {s['raw_rows']:>7,} rows x {s['raw_columns']} columns")
    print(f"  planets (curated):     {s['planet_rows']:>7,} rows, one per planet")
    print(f"  distinct planets:      {s['distinct_planets']:>7,}   host stars: {s['host_stars']:,}")
    print(f"  selected row is not NASA's default_flag row: {s['selected_not_default']:,} planets ({pct:.0f}%)")
    if s["unnamed_rows_skipped"]:
        print(f"  rows without pl_name (kept raw, not curated): {s['unnamed_rows_skipped']:,}")
    if s["values_coerced_to_null"]:
        detail = ", ".join(f"{c}={n}" for c, n in s["values_coerced_to_null"].items())
        print(f"  non-numeric values set to NULL: {detail}")
    missing = ", ".join(s["missing_expected_columns"]) or "none"
    print(f"  key columns missing:   {missing}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help=f"PS CSV (default: {CSV_PATH.relative_to(ROOT)})")
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"SQLite output (default: {DB_PATH.name})")
    args = parser.parse_args()
    try:
        build(args.csv, args.db)
    except (FileNotFoundError, MissingColumnsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
