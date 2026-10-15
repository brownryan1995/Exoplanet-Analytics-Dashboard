"""Exoplanet Analytics Dashboard (Phase 1).

Main views read the curated `planets` table (one row per planet). The per-planet
drill-down reads every source parameter set from the raw `planet_params` table.

Run:  streamlit run app.py
"""

from __future__ import annotations

import html
import math
import sqlite3
from contextlib import closing

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import build_database as bdb

st.set_page_config(page_title="Exoplanet Analytics Dashboard", page_icon="🪐", layout="wide")

DB_PATH = bdb.DB_PATH
INF = math.inf

# --- Display metadata ---------------------------------------------------------

LABELS = {
    "pl_name": "Planet",
    "hostname": "Host star",
    "discoverymethod": "Discovery method",
    "disc_year": "Discovery year",
    "disc_facility": "Discovery facility",
    "disc_telescope": "Discovery telescope",
    "pl_rade": "Radius (R⊕)",
    "pl_bmasse": "Mass (M⊕)",
    "pl_bmassprov": "Mass type",
    "pl_orbper": "Orbital period (days)",
    "pl_orbsmax": "Semi-major axis (AU)",
    "pl_insol": "Insolation (S⊕)",
    "pl_eqt": "Equilibrium temp (K)",
    "st_spectype": "Spectral type",
    "st_teff": "Star temp (K)",
    "st_rad": "Star radius (R☉)",
    "st_mass": "Star mass (M☉)",
    "sy_dist": "Distance (pc)",
    "sy_snum": "Stars in system",
    "sy_pnum": "Planets in system",
    "sy_vmag": "V mag",
    "sy_bmag": "B mag",
    "sy_gaiamag": "Gaia G mag",
    "sy_jmag": "J mag",
    "sy_hmag": "H mag",
    "sy_kmag": "Ks mag",
    "sy_tmag": "TESS mag",
    "sy_kepmag": "Kepler mag",
    "ra": "RA (deg)",
    "dec": "Dec (deg)",
    "soltype": "Solution type",
    "pl_controv_flag": "Controversial",
    "default_flag": "NASA default",
    "pl_pubdate": "Published",
    "reference": "Reference",
    "ref_url": "Link",
    "completeness": f"Key fields (of {bdb.MAX_COMPLETENESS})",
    "selection_score": f"Score (of {bdb.MAX_SCORE})",
    "n_param_sets": "Parameter sets",
    "selection_rank": "Rank",
    "param_set_id": "Param set ID",
    "disc_decade": "Discovery decade",
}
FORMATS = {
    "pl_rade": "%.2f", "pl_bmasse": "%.2f", "pl_orbper": "%.2f", "pl_orbsmax": "%.3f",
    "pl_insol": "%.1f", "pl_eqt": "%.0f", "st_teff": "%.0f", "st_rad": "%.2f",
    "st_mass": "%.2f", "sy_dist": "%.1f", "disc_year": "%d", "sy_snum": "%d", "sy_pnum": "%d",
    "sy_vmag": "%.2f", "sy_bmag": "%.2f", "sy_gaiamag": "%.2f", "sy_jmag": "%.2f",
    "sy_hmag": "%.2f", "sy_kmag": "%.2f", "sy_tmag": "%.2f", "sy_kepmag": "%.2f",
    "ra": "%.4f", "dec": "%.4f", "completeness": "%d", "selection_score": "%d",
    "n_param_sets": "%d", "selection_rank": "%d", "param_set_id": "%d",
}
EXPLORE_COLUMNS = [
    "pl_name", "hostname", "discoverymethod", "disc_year", "pl_rade", "pl_bmasse",
    "pl_bmassprov", "pl_orbper", "pl_orbsmax", "pl_eqt", "sy_dist", "st_spectype",
    "st_teff", "sy_pnum", "completeness", "pl_refname",
]
DETAIL_GROUPS = {
    "Planet": ["pl_rade", "pl_bmasse", "pl_bmassprov", "pl_orbper", "pl_orbsmax", "pl_insol", "pl_eqt"],
    "Host star": ["hostname", "st_spectype", "st_teff", "st_rad", "st_mass"],
    "System & photometry": ["sy_dist", "sy_snum", "sy_pnum", "ra", "dec", "sy_vmag", "sy_bmag",
                            "sy_gaiamag", "sy_jmag", "sy_hmag", "sy_kmag", "sy_tmag", "sy_kepmag"],
}
SOURCE_ROW_COLUMNS = [
    "pl_pubdate", "soltype", "default_flag", "selection_score", "completeness",
    *bdb.SCORE_WEIGHTS, "param_set_id", "pl_refname",
]

RADIUS_STOPS = [0, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8, 11, 15, 20, 30, INF]
DISTANCE_STOPS = [0, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, INF]

# Validated categorical slots (light/dark) for the four most common methods;
# the long tail folds into a neutral "Other methods" series.
SERIES_COLORS = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"],
}
OTHER_COLOR = "#898781"
SURFACE = {"light": "#ffffff", "dark": "#0e1117"}
OTHER_LABEL = "Other methods"


# --- Data access --------------------------------------------------------------

def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


@st.cache_resource(show_spinner=False)
def ensure_database() -> set[str]:
    """Build exoplanets.db if absent (e.g. a fresh deploy) and verify its columns.

    Returns the set of columns available in the curated `planets` table.
    """
    def tables_present() -> bool:
        if not DB_PATH.exists():
            return False
        with closing(sqlite3.connect(DB_PATH)) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {"planets", "planet_params", "build_info"} <= names

    if not tables_present():
        with st.spinner("Building the database from data/PS.csv (first run only, ~10 s)..."):
            bdb.build(verbose=False)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        columns = set(_table_columns(conn, "planets"))
    missing = [c for c in bdb.REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise bdb.MissingColumnsError(
            f"The planets table is missing required column(s): {', '.join(missing)}. "
            "Re-run `python build_database.py` against a current PS export."
        )
    return columns


@st.cache_data(show_spinner=False)
def _query(sql: str, params: tuple, db_mtime: float) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def query(sql: str, params=()) -> pd.DataFrame:
    """Run a parameterized query. Cached until exoplanets.db is rebuilt."""
    return _query(sql, tuple(params), DB_PATH.stat().st_mtime)


def show_sql(sql: str, params=()) -> None:
    """Render a query for readers, with parameters inlined (display only)."""
    parts = sql.strip().split("?")
    text = parts[0]
    for value, rest in zip(params, parts[1:]):
        if isinstance(value, str):
            literal = "'" + value.replace("'", "''") + "'"
        elif isinstance(value, float):
            literal = f"{value:g}"
        else:
            literal = str(value)
        text += literal + rest
    st.code(text, language="sql")


def ref_labels(raw: pd.Series) -> pd.Series:
    """'<a refstr=... href=URL target=ref>Author et al. 2020</a>' -> 'Author et al. 2020'."""
    label = raw.str.extract(r">\s*(.*?)\s*</a>", expand=False).fillna(raw)
    return label.map(lambda s: html.unescape(s) if isinstance(s, str) else s)


def ref_urls(raw: pd.Series) -> pd.Series:
    return raw.str.extract(r"href=(\S+)", expand=False)


def column_config(columns) -> dict:
    config = {}
    for col in columns:
        label = LABELS.get(col, col)
        if col in FORMATS:
            config[col] = st.column_config.NumberColumn(label, format=FORMATS[col])
        elif col == "ref_url":
            config[col] = st.column_config.LinkColumn(label, display_text="open ↗")
        elif col in ("default_flag", "pl_controv_flag"):
            config[col] = st.column_config.CheckboxColumn(label)
        else:
            config[col] = st.column_config.TextColumn(label)
    return config


def fmt(value, spec: str = ",.2f", unit: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return "—"
    if isinstance(value, str):
        return value
    return f"{value:{spec}}{unit}"


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


# --- Bootstrap ----------------------------------------------------------------

try:
    PLANET_COLUMNS = ensure_database()
except (FileNotFoundError, bdb.MissingColumnsError) as exc:
    st.error(f"**Could not load the data.** {exc}")
    st.stop()

BUILD_INFO = dict(query("SELECT key, value FROM build_info").values)
EXPORTED_AT = BUILD_INFO.get("archive_exported_at", "unknown")


def available(columns) -> list[str]:
    return [c for c in columns if c in PLANET_COLUMNS]


# --- Reference values (always over the full dataset) ----------------------------

TOTAL_PLANETS = int(query("SELECT COUNT(*) FROM planets").iloc[0, 0])
METHOD_COUNTS = query(
    "SELECT discoverymethod, COUNT(*) AS planets FROM planets "
    "GROUP BY discoverymethod ORDER BY planets DESC, discoverymethod"
)
ALL_METHODS = METHOD_COUNTS.discoverymethod.tolist()
METHOD_N = METHOD_COUNTS.set_index("discoverymethod").planets
# Color follows the method, ranked on the full dataset, so filtering never repaints a series.
TOP_METHODS = ALL_METHODS[: len(SERIES_COLORS["light"])]
YEAR_MIN, YEAR_MAX = (int(v) for v in query("SELECT MIN(disc_year), MAX(disc_year) FROM planets").iloc[0])
SOLTYPE_COUNTS = query(
    "SELECT soltype, COUNT(*) AS planets FROM planets WHERE soltype IS NOT NULL "
    "GROUP BY soltype ORDER BY planets DESC"
).set_index("soltype").planets
UNKNOWN_COUNTS = query(
    "SELECT SUM(pl_rade IS NULL) AS radius, SUM(sy_dist IS NULL) AS distance FROM planets"
).iloc[0]


# --- Filters (sidebar) -----------------------------------------------------------
# One set of filters drives every view: the KPI tiles, the chart and the table.

def stop_label(value: float) -> str:
    return "no limit" if value == INF else f"{value:g}"


# Clearing a widget's session-state value does not redraw the widget, so a reset
# would leave stale values on screen. Instead every filter key carries a version
# number, and a reset bumps it so Streamlit draws fresh widgets at their defaults.
st.session_state.setdefault("filters_version", 0)


def fkey(name: str) -> str:
    return f"f_{name}_{st.session_state.filters_version}"


def reset_filters() -> None:
    st.session_state.filters_version += 1


with st.sidebar:
    st.header("Filters")
    st.caption("Filters apply instantly to the whole dashboard: tiles, chart and table.")
    search = st.text_input("Planet or host name contains", key=fkey("search"),
                           placeholder="e.g. TRAPPIST, then press Enter").strip()
    methods = st.multiselect("Discovery method", ALL_METHODS, key=fkey("methods"), placeholder="All methods",
                             format_func=lambda m: f"{m} ({METHOD_N[m]:,})")
    years = st.slider("Discovery year", YEAR_MIN, YEAR_MAX, (YEAR_MIN, YEAR_MAX), key=fkey("years"))
    radius = st.select_slider("Planet radius (Earth radii)", RADIUS_STOPS, (0, INF),
                              format_func=stop_label, key=fkey("radius"))
    keep_no_radius = st.checkbox(
        "Keep planets with no radius value", key=fkey("keep_no_radius"), disabled=radius == (0, INF),
        help=f"Applies once the range is narrowed. {UNKNOWN_COUNTS.radius:,} planets have no radius "
             "in their selected parameter set (common for radial-velocity discoveries).",
    )
    distance = st.select_slider("Distance from Earth (parsecs)", DISTANCE_STOPS, (0, INF),
                                format_func=stop_label, key=fkey("distance"))
    keep_no_distance = st.checkbox(
        "Keep planets with no distance value", key=fkey("keep_no_distance"), disabled=distance == (0, INF),
        help=f"Applies once the range is narrowed. {UNKNOWN_COUNTS.distance:,} planets have no distance.",
    )
    st.subheader("Quality signals")
    exclude_controversial = st.toggle(
        "Exclude controversial planets", key=fkey("no_controversial"),
        help="Planets whose status is flagged as questioned in the literature (pl_controv_flag = 1).",
    )
    soltypes = st.multiselect(
        "Solution type of the selected parameter set", SOLTYPE_COUNTS.index.tolist(), key=fkey("soltypes"),
        placeholder="All solution types", format_func=lambda s: f"{s} ({SOLTYPE_COUNTS[s]:,})",
        help="Every planet here is confirmed; this describes the study row chosen for it, e.g. a "
             "published paper vs. a Kepler/TESS pipeline catalog.",
    )
    st.button("Reset filters", on_click=reset_filters, width="stretch")


def range_clause(col: str, bounds: tuple[float, float], keep_unknown: bool):
    lo, hi = bounds
    conds, params = [], []
    if lo > 0:
        conds.append(f"{col} >= ?")
        params.append(float(lo))
    if hi != INF:
        conds.append(f"{col} <= ?")
        params.append(float(hi))
    if not conds:
        return None, []
    clause = " AND ".join(conds)
    if keep_unknown:
        clause = f"(({clause}) OR {col} IS NULL)"
    return clause, params


where, params = [], []
if search:
    where.append("(pl_name LIKE ? OR hostname LIKE ?)")
    params += [f"%{search}%"] * 2
if methods:
    where.append(f"discoverymethod IN ({', '.join('?' * len(methods))})")
    params += methods
if years != (YEAR_MIN, YEAR_MAX):
    where.append("disc_year BETWEEN ? AND ?")
    params += list(years)
for col, bounds, keep in [("pl_rade", radius, keep_no_radius), ("sy_dist", distance, keep_no_distance)]:
    clause, clause_params = range_clause(col, bounds, keep)
    if clause:
        where.append(clause)
        params += clause_params
if exclude_controversial:
    where.append("COALESCE(pl_controv_flag, 0) = 0")
if soltypes:
    where.append(f"soltype IN ({', '.join('?' * len(soltypes))})")
    params += soltypes

# Shared WHERE clause, spliced into every view's query below.
FILTER_SQL = ("WHERE " + "\n  AND ".join(where) + "\n") if where else ""


# --- Header & KPIs --------------------------------------------------------------

st.title("🪐 Exoplanet Analytics Dashboard")
st.caption(
    f"Confirmed exoplanets from the NASA Exoplanet Archive *Planetary Systems* table "
    f"(exported {EXPORTED_AT}), resolved to one curated row per planet. "
    "Personal portfolio project; no employer data."
)

KPI_SQL = f"""
WITH filtered AS (
    SELECT * FROM planets
    {FILTER_SQL.replace(chr(10), chr(10) + "    ").rstrip()}
)
SELECT COUNT(*)                        AS planets,
       COUNT(DISTINCT hostname)        AS host_stars,
       COUNT(DISTINCT discoverymethod) AS methods,
       MAX(disc_year)                  AS latest_year,
       SUM(disc_year = (SELECT MAX(disc_year) FROM filtered)) AS latest_year_planets
FROM filtered
"""
kpi = query(KPI_SQL, params).iloc[0]
n_match = int(kpi.planets)

if where:
    note, clear = st.columns([5, 1], vertical_alignment="center")
    note.info(f"**Filters active:** every view shows **{n_match:,}** of {TOTAL_PLANETS:,} planets.",
              icon="🔎")
    clear.button("Clear filters", on_click=reset_filters, width="stretch", key="clear_filters_top")


def count(value) -> str:
    return "—" if value is None or pd.isna(value) else f"{int(value):,}"


latest = "—" if pd.isna(kpi.latest_year) else str(int(kpi.latest_year))
tiles = st.columns(5)
tiles[0].metric("Confirmed planets", count(kpi.planets), border=True)
tiles[1].metric("Host stars", count(kpi.host_stars), border=True)
tiles[2].metric("Discovery methods", count(kpi.methods), border=True)
tiles[3].metric("Latest discovery year", latest, border=True)
tiles[4].metric(
    f"Discovered in {latest}", count(kpi.latest_year_planets), border=True,
    help=(f"{YEAR_MAX} is year to date as of the archive export ({EXPORTED_AT})."
          if latest == str(YEAR_MAX) else None),
)
with st.expander("SQL behind these numbers"):
    show_sql(KPI_SQL, params)

tab_time, tab_explore, tab_notes = st.tabs(["Discoveries over time", "Explore planets", "Notes & methods"])

# --- Discoveries over time -----------------------------------------------------


def series_for(method: str) -> str:
    return method if method in TOP_METHODS else OTHER_LABEL


def discoveries_chart(counts: pd.DataFrame) -> go.Figure:
    mode = theme_mode()
    colors = dict(zip(TOP_METHODS, SERIES_COLORS[mode])) | {OTHER_LABEL: OTHER_COLOR}
    counts = counts.assign(series=counts.discoverymethod.map(series_for))
    year_totals = counts.groupby("disc_year").planets.sum()

    fig = go.Figure()
    for series in [*TOP_METHODS, OTHER_LABEL]:
        rows = counts[counts.series == series]
        if rows.empty:
            continue
        by_year = rows.groupby("disc_year").planets.sum()
        breakdown = rows.groupby("disc_year").apply(
            lambda g: "<br>".join(f"  {m}: {n:,}" for m, n in zip(g.discoverymethod, g.planets)),
            include_groups=False,
        )
        is_other = series == OTHER_LABEL
        detail = breakdown if is_other else pd.Series("", index=by_year.index)
        fig.add_bar(
            x=by_year.index, y=by_year.values, name=series,
            marker=dict(color=colors[series], line=dict(color=SURFACE[mode], width=1)),
            customdata=list(zip(year_totals.loc[by_year.index], detail.loc[by_year.index])),
            hovertemplate=(
                f"<b>%{{x}}</b> · {series}: %{{y:,}}"
                + ("<br>%{customdata[1]}" if is_other else "")
                + "<br>All shown methods that year: %{customdata[0]:,}<extra></extra>"
            ),
        )
    peak_year = int(year_totals.idxmax())
    fig.add_annotation(
        x=peak_year, y=year_totals.max(), text=f"{peak_year}: {year_totals.max():,}",
        showarrow=False, yshift=12, font=dict(size=12),
    )
    fig.update_layout(
        barmode="stack", bargap=0.15, barcornerradius=4, height=440,
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None,
                    traceorder="normal"),
        xaxis=dict(title="Discovery year", dtick=2, showgrid=False),
        yaxis=dict(title="Planets discovered", rangemode="tozero"),
        hoverlabel=dict(align="left"),
    )
    return fig


with tab_time:
    st.subheader("Planets discovered per year, by discovery method")
    trend_sql = f"""
SELECT disc_year, discoverymethod, COUNT(*) AS planets
FROM planets
{FILTER_SQL}GROUP BY disc_year, discoverymethod
ORDER BY disc_year, discoverymethod
"""
    trend = query(trend_sql, params)
    if trend.empty:
        st.info("No planets match the current filters. Loosen them in the sidebar to draw the chart.")
    else:
        st.plotly_chart(discoveries_chart(trend), width="stretch", config={"displaylogo": False})
        tail = [m for m in ALL_METHODS if m not in TOP_METHODS and m in set(trend.discoverymethod)]
        if tail:
            st.caption(
                f"*{OTHER_LABEL}* groups {len(tail)} rarer method(s) ({', '.join(tail)}) so the "
                "chart stays readable; hover a bar for the breakdown. Use the sidebar's *Discovery "
                "method* filter to choose which methods are shown."
            )
        with st.expander("Chart data and SQL"):
            table = trend.pivot_table(index="disc_year", columns="discoverymethod", values="planets",
                                      fill_value=0, aggfunc="sum")
            table = table[[m for m in ALL_METHODS if m in table.columns]]
            table.insert(0, "Total", table.sum(axis=1))
            st.dataframe(table.rename_axis("Year").sort_index(ascending=False), width="stretch")
            show_sql(trend_sql, params)

# --- Explore -------------------------------------------------------------------

explore_cols = available(EXPLORE_COLUMNS)
explore_sql = f"SELECT {', '.join(explore_cols)}\nFROM planets\n{FILTER_SQL}ORDER BY disc_year DESC, pl_name"


def on_table_select() -> None:
    rows = st.session_state["explore_table"]["selection"]["rows"]
    names = st.session_state.get("_explore_names", [])
    if rows and rows[0] < len(names):
        st.session_state["detail_planet"] = names[rows[0]]


def render_detail(name: str) -> None:
    p = query("SELECT * FROM planets WHERE pl_name = ?", [name]).iloc[0]
    ref = ref_labels(pd.Series([p.pl_refname])).iloc[0]
    url = ref_urls(pd.Series([p.pl_refname])).iloc[0]

    st.subheader(name)
    facility = f" by {p.disc_facility}" if "disc_facility" in p and pd.notna(p.disc_facility) else ""
    st.caption(f"Orbits **{p.hostname}** · {p.discoverymethod} · discovered {p.disc_year}{facility}")
    if p.pl_controv_flag == 1:
        st.warning("This planet is flagged as **controversial**: its existence or nature is questioned "
                   "in the literature.", icon="⚠️")

    mass_type = p.get("pl_bmassprov")
    is_min_mass = mass_type == "Msini"
    m = st.columns(5)
    m[0].metric("Radius (Earth radii)", fmt(p.pl_rade, ",.2f"), border=True)
    m[1].metric(
        "Min. mass (Earth masses)" if is_min_mass else "Mass (Earth masses)", fmt(p.pl_bmasse, ",.2f"),
        border=True, help="M sin i: a lower bound, because the orbit's inclination is unknown." if is_min_mass else None,
    )
    m[2].metric("Orbital period (days)", fmt(p.pl_orbper, ",.2f"), border=True)
    m[3].metric("Equilibrium temp (K)", fmt(p.pl_eqt, ",.0f"), border=True)
    m[4].metric("Distance (parsecs)", fmt(p.sy_dist, ",.1f"), border=True)

    ref_md = f"[{ref}]({url})" if isinstance(url, str) else ref
    if p.n_param_sets == 1:
        competition = "It is the only parameter set for this planet, and NASA's default."
    elif p.default_flag == 1:
        competition = (f"It was chosen from **{p.n_param_sets}** parameter sets and is also NASA's "
                       "default.")
    else:
        competition = (f"It was chosen from **{p.n_param_sets}** parameter sets; NASA's default is a "
                       "different row (see the table below).")
    st.markdown(
        f"**Source of these values:** {ref_md}, published {str(p.pl_pubdate)[:10]} · solution type "
        f"*{p.soltype}*.  \nThis row fills **{p.completeness} of {bdb.MAX_COMPLETENESS}** key fields "
        f"(selection score **{p.selection_score} of {bdb.MAX_SCORE}**). {competition}"
    )

    left, right = st.columns([1, 2])
    with left:
        st.markdown("**Curated values**")
        rows = []
        for group, cols in DETAIL_GROUPS.items():
            for col in available(cols):
                value = p[col]
                spec = FORMATS.get(col, "%s").replace("%", "").replace("d", ".0f")
                shown = fmt(value) if isinstance(value, str) or pd.isna(value) else format(value, f",{spec}")
                rows.append({"Group": group, "Parameter": LABELS.get(col, col), "Value": shown})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=560)
    with right:
        heading = "Source parameter set" if p.n_param_sets == 1 else f"All {p.n_param_sets} source parameter sets"
        st.markdown(f"**{heading}** (from `planet_params`, ranked by the selection rule)")
        source_cols = [c for c in SOURCE_ROW_COLUMNS if c in PLANET_COLUMNS or c in ("selection_score", "completeness")]
        inner_cols = ", ".join(c for c in source_cols if c not in ("selection_score", "completeness"))
        source_sql = f"""
WITH scored AS (
    SELECT {inner_cols},
           rowupdate, releasedate,
           {bdb.COMPLETENESS_SQL} AS completeness,
           {bdb.SCORE_SQL} AS selection_score
    FROM planet_params
    WHERE pl_name = ?
)
SELECT ROW_NUMBER() OVER (ORDER BY {bdb.SELECTION_ORDER_SQL.strip()}) AS selection_rank,
       {', '.join(source_cols)}
FROM scored
ORDER BY selection_rank
"""
        sources = query(source_sql, [name])
        sources.insert(1, "chosen", (sources.param_set_id == p.param_set_id).map({True: "✓ chosen", False: ""}))
        sources["reference"] = ref_labels(sources.pl_refname)
        sources["ref_url"] = ref_urls(sources.pl_refname)
        sources["default_flag"] = sources.default_flag == 1
        sources = sources.drop(columns="pl_refname")
        order = ["selection_rank", "chosen", "reference", "pl_pubdate", "selection_score", "completeness",
                 *[c for c in bdb.SCORE_WEIGHTS if c in sources], "soltype", "default_flag", "ref_url",
                 "param_set_id"]
        config = column_config(order) | {"chosen": st.column_config.TextColumn("Chosen")}
        st.dataframe(sources[order], hide_index=True, width="stretch", column_config=config,
                     placeholder="—", height=min(560, 35 * (len(sources) + 1) + 3))
        with st.expander("SQL for this table"):
            show_sql(source_sql, [name])


with tab_explore:
    total = TOTAL_PLANETS
    planets = query(explore_sql, params)
    planets["reference"] = ref_labels(planets.pop("pl_refname"))
    st.session_state["_explore_names"] = planets.pl_name.tolist()

    head_left, head_right = st.columns([3, 1], vertical_alignment="bottom")
    head_left.markdown(f"**{len(planets):,}** of {total:,} planets match the filters. "
                       "Select a row to see where its values come from.")
    head_right.download_button(
        "Download CSV", planets.to_csv(index=False).encode("utf-8"), "exoplanets_filtered.csv",
        mime="text/csv", width="stretch",
    )
    st.dataframe(
        planets, hide_index=True, width="stretch", height=380, placeholder="—",
        column_config=column_config(planets.columns) | {
            "pl_name": st.column_config.TextColumn(LABELS["pl_name"], pinned=True),
        },
        key="explore_table", on_select=on_table_select, selection_mode="single-row",
    )
    with st.expander("SQL behind this table"):
        show_sql(explore_sql, params)

    st.divider()
    all_names = query("SELECT pl_name FROM planets ORDER BY pl_name").pl_name.tolist()
    st.session_state.setdefault("detail_planet", None)
    st.selectbox(
        "Planet detail", all_names, key="detail_planet", index=None,
        placeholder=f"Select a table row, or type to search all {total:,} planets...",
    )
    if st.session_state.detail_planet:
        with st.container(border=True):
            render_detail(st.session_state.detail_planet)

# --- Notes & methods -----------------------------------------------------------

with tab_notes:
    stats = query("""
SELECT (SELECT COUNT(*) FROM planet_params)                         AS raw_rows,
       (SELECT MAX(n_param_sets) FROM planets)                         AS max_sets,
       SUM(default_flag = 0)                                           AS not_default,
       SUM(soltype LIKE 'Kepler Project Candidate%' OR soltype = 'TESS Project Candidate') AS pipeline,
       SUM(pl_bmasselim = 1)                                           AS mass_limits,
       SUM(pl_rade IS NULL)                                            AS no_radius,
       SUM(pl_bmasse IS NULL)                                          AS no_mass,
       SUM(pl_rade > 30)                                               AS huge_radius,
       SUM(completeness <= 3)                                          AS sparse_planets
FROM planets
""").iloc[0]
    weights = ", ".join(
        f"`{c}`" + (" (x2)" if w == 2 else "") for c, w in bdb.SCORE_WEIGHTS.items()
    )

    st.markdown(f"""
### Data source
NASA Exoplanet Archive, **Planetary Systems (PS)** table, downloaded as CSV (archive export
{EXPORTED_AT}). PS stores **one row per published parameter set**: each planet appears once
for every study that characterized it. This export has **{stats.raw_rows:,} rows** describing
**{total:,} planets** (up to {stats.max_sets} rows for a single planet).

PS was chosen over the archive's one-row-per-planet *Composite* table (PSCompPars) because each
PS row is internally consistent: all of its values come from the same study. PSCompPars stitches
values from different papers into one row.

### Data model
| Table | Grain | Purpose |
|---|---|---|
| `planet_params` | one row per published parameter set | Raw layer: every PS row and column (error columns included), plus a surrogate key `param_set_id`. Powers the drill-down. |
| `planets` | one row per planet | Curated layer: the selected row for each planet with the same columns, plus `disc_decade`, `completeness`, `selection_score` and `n_param_sets`. Powers every main view. `param_set_id` links back to the source row. |

### How one row is chosen per planet
1. **Completeness first.** Each parameter set earns one point for every populated field among
   {weights}. Radius and mass are the headline numbers, so they **count double** (maximum score
   {bdb.MAX_SCORE}), so a row that carries them can outrank a slightly more complete row that lacks
   them. For example, a row with radius and mass plus three other fields (score 7) beats a row with
   six other fields (score 6).
   Host-star photometry (`sy_*` magnitudes) is left out because it is catalog-level and nearly
   identical across a planet's rows.
2. **Ties go to the newest study**: the latest reference publication date (`pl_pubdate`), then the
   archive's `rowupdate` and `releasedate`, then NASA's `default_flag`, then file order (so the
   result is deterministic).
3. **No planet is dropped.** A sparsely measured planet keeps its best available row, however
   sparse. {stats.sparse_planets:,} planets end up with 3 or fewer of the 9 key fields.
4. **NASA's default is not trusted blindly.** The default parameter set is often not the most
   complete one; this rule picks a different row for **{stats.not_default:,} planets
   ({stats.not_default / total:.0%})**. `default_flag` is kept as a column for reference.

Every planet's detail view (Explore tab) shows the chosen row, the reference it came from, and
every competing parameter set ranked by this rule.
""")
    with st.expander("The SQL that builds the curated table"):
        st.code(bdb._select_planets_sql("<every planet_params column>").strip() + ";", language="sql")

    st.markdown(f"""
### Known caveats
- **Self-consistent, not best-of-each-field.** A planet's values come from one study. A different
  study may have measured a single field (say, mass) that the chosen row lacks. A field-by-field
  composite with per-value lineage is planned for a later phase.
- **Pipeline catalogs often win.** {stats.pipeline:,} planets' chosen row is a Kepler (KOI) or TESS
  pipeline solution, because those uniform catalog fits fill many fields at once. They never carry a
  mass, and occasionally hold implausible fits: {stats.huge_radius} chosen rows report a radius above
  30 R⊕, shown as published. Use the *solution type* filter to focus on published solutions.
- **Mass may be a minimum.** For many radial-velocity planets the archive's mass is *M sin i*, a
  lower bound. The *Mass type* column says which. {stats.no_mass:,} planets have no mass and
  {stats.no_radius:,} have no radius in their chosen row.
- **Limits count as values.** Some values are upper or lower limits (`*lim` flags in the data).
  They count as populated when scoring completeness; {stats.mass_limits} chosen rows carry a mass
  upper limit.
- **Error columns are kept but hidden.** Every `*err1`/`*err2` uncertainty column is stored in both
  tables; the views do not display them.
- **The latest year is partial.** Discovery counts for {YEAR_MAX} run to the export date.
- **Publication dates vary in precision** (`2011-08`, `2017-08-31`, `2017-08-31 00:00`); they are
  compared on their ISO prefix.
- **Parsing note.** The CSV starts with `#` metadata lines, but some reference fields also contain
  `#` inside HTML entities (e.g. `&#x131;`). Reading with a blanket comment character silently
  truncates those rows, so only the leading metadata block is skipped.

### Derived fields
- `disc_decade`: discovery year floored to the decade, e.g. 2016 → `2010s`.
- `completeness`: how many of the 9 key fields the chosen row fills (0-9).
- `selection_score`: completeness with radius and mass counted twice (0-{bdb.MAX_SCORE}).
- `n_param_sets`: how many PS rows exist for the planet.

### About
Personal portfolio project built with Python, pandas, SQLite, Streamlit and Plotly. It is not
affiliated with, and uses no data from, any employer or client. Data: NASA Exoplanet Archive,
operated by Caltech under contract with NASA.
""")
