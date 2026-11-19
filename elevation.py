import contextlib
import io
import math
import os
import re
import shutil
import time
import traceback
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely
from pygris import counties, places, states
from shapely.geometry import box
from osgeo import gdal  # import before arcpy (avoids DLL conflicts on Windows)
import arcpy
from arcpy.sa import *

# ---- Settings -----------------------------------------------------------------
# The 48 contiguous states: capital (starred on the map), highest point and its
# published elevation in ft (Highpointers Foundation list), used for the label
# because the 30 m DEM smooths summits.
STATE_INFO = {
    "Alabama": ("Montgomery", "Cheaha Mountain", 2407),
    "Arizona": ("Phoenix", "Humphreys Peak", 12633),
    "Arkansas": ("Little Rock", "Mount Magazine", 2753),
    "California": ("Sacramento", "Mount Whitney", 14494),
    "Colorado": ("Denver", "Mount Elbert", 14433),
    "Connecticut": ("Hartford", "Mount Frissell", 2380),
    "Delaware": ("Dover", "Ebright Azimuth", 451),
    "Florida": ("Tallahassee", "Britton Hill", 345),
    "Georgia": ("Atlanta", "Brasstown Bald", 4783),
    "Idaho": ("Boise", "Borah Peak", 12662),
    "Illinois": ("Springfield", "Charles Mound", 1235),
    "Indiana": ("Indianapolis", "Hoosier Hill", 1257),
    "Iowa": ("Des Moines", "Hawkeye Point", 1670),
    "Kansas": ("Topeka", "Mount Sunflower", 4039),
    "Kentucky": ("Frankfort", "Black Mountain", 4139),
    "Louisiana": ("Baton Rouge", "Driskill Mountain", 535),
    "Maine": ("Augusta", "Mount Katahdin", 5268),
    "Maryland": ("Annapolis", "Hoye-Crest", 3360),
    "Massachusetts": ("Boston", "Mount Greylock", 3487),
    "Michigan": ("Lansing", "Mount Arvon", 1979),
    "Minnesota": ("Saint Paul", "Eagle Mountain", 2301),
    "Mississippi": ("Jackson", "Woodall Mountain", 806),
    "Missouri": ("Jefferson City", "Taum Sauk Mountain", 1772),
    "Montana": ("Helena", "Granite Peak", 12799),
    "Nebraska": ("Lincoln", "Panorama Point", 5424),
    "Nevada": ("Carson City", "Boundary Peak", 13143),
    "New Hampshire": ("Concord", "Mount Washington", 6288),
    "New Jersey": ("Trenton", "High Point", 1803),
    "New Mexico": ("Santa Fe", "Wheeler Peak", 13161),
    "New York": ("Albany", "Mount Marcy", 5344),
    "North Carolina": ("Raleigh", "Mount Mitchell", 6684),
    "North Dakota": ("Bismarck", "White Butte", 3506),
    "Ohio": ("Columbus", "Campbell Hill", 1549),
    "Oklahoma": ("Oklahoma City", "Black Mesa", 4973),
    "Oregon": ("Salem", "Mount Hood", 11239),
    "Pennsylvania": ("Harrisburg", "Mount Davis", 3213),
    "Rhode Island": ("Providence", "Jerimoth Hill", 812),
    "South Carolina": ("Columbia", "Sassafras Mountain", 3554),
    "South Dakota": ("Pierre", "Black Elk Peak", 7242),
    "Tennessee": ("Nashville", "Clingmans Dome", 6643),
    "Texas": ("Austin", "Guadalupe Peak", 8749),
    "Utah": ("Salt Lake City", "Kings Peak", 13528),
    "Vermont": ("Montpelier", "Mount Mansfield", 4393),
    "Virginia": ("Richmond", "Mount Rogers", 5729),
    "Washington": ("Olympia", "Mount Rainier", 14410),
    "West Virginia": ("Charleston", "Spruce Knob", 4861),
    "Wisconsin": ("Madison", "Timms Hill", 1951),
    "Wyoming": ("Cheyenne", "Gannett Peak", 13804),
}
STATES = list(STATE_INFO)       # or a few, e.g. ["New Jersey", "Delaware"]
SKIP_EXISTING = True            # skip states whose map PNG already exists (resume)
LOG_FILE = "C:/DEM/run_log.txt"
PARALLEL_STATES = 3             # states built at the same time (each can use 4-8 GB of RAM);
                                # 1 = one at a time with full progress in this window
REUSE_DATA = True               # redraws reuse each state's DEM, NHD and GNIS data
                                # (in its folder); delete a state's folder for fresh data
DOWNLOAD_THREADS = 8            # DEM tiles downloaded at once

CRS_WKID = 5070                 # NAD83 / Conus Albers (equal-area, meters)
STATE_CRS = f"EPSG:{CRS_WKID}"

DEM_RES_M = 30                  # USGS 3DEP 1 arc-second tiles on AWS S3
DEM_CREDIT = "USGS 3DEP DEM"
MAX_DEM_CELLS = 250_000_000     # big states get a coarser cell size to fit

# Rivers (NHD flowlines). A line is drawn when its upstream length (itself plus
# everything upstream) is at least total stream length / STREAM_THRESHOLD_DIVISOR.
# Raise the divisor for more rivers, lower it for fewer.
STREAM_THRESHOLD_DIVISOR = 450
MIN_UPSTREAM_KM = 2
STATE_STREAM_THRESHOLD_KM = {}  # per-state override, e.g. {"Delaware": 5}

# Line width: upstream length >= threshold x multiple -> class 1-4. Named
# rivers also get a class from their length vs. the state's size (sqrt of
# land area), so border rivers like the Delaware aren't drawn too thin.
RIVER_CLASS_MULTIPLES = [1, 6, 30, 120]
RIVER_LENGTH_CLASS_FRACTIONS = [0.15, 0.4, 0.8]    # -> classes 2, 3, 4
RIVER_CLASS_NAMES = ["Minor streams", "Streams", "Rivers", "Major rivers"]
RIVER_CLASS_WIDTHS = [0.3, 0.6, 1.1, 1.8]          # points
RIVER_CLIP_MARGIN_M = 300       # keeps border rivers in one piece
NHD_RIVER_FTYPES = {460, 558}   # StreamRiver, ArtificialPath
NHD_LAKE_FTYPES = {390, 436}    # LakePond, Reservoir (not bays, estuaries, marshes)
LAKE_LABEL_COUNT = 6            # largest named lakes/reservoirs that get a label

# River names: the RIVER_LABEL_COUNT longest named rivers (at least
# MIN_RIVER_LABEL_KM in the state), one curved label each. Canals, ditches
# and side branches ("North Branch ...") are skipped.
RIVER_LABEL_COUNT = 8
MIN_RIVER_LABEL_KM = 15
RIVER_LABEL_SKIP = re.compile(
    r"\b(?:Canal|Ditch)\b|^(?:North|South|East|West|Middle)\s+(?:Branch|Fork)\b",
    re.IGNORECASE)

# Hillshade: exaggeration = MIN x RELIEF_REFERENCE_FT / highest point, capped,
# so flat states get more relief.
MIN_VERTICAL_EXAGGERATION = 3
MAX_VERTICAL_EXAGGERATION = 10
RELIEF_REFERENCE_FT = 4500
HILLSHADE_ALTITUDE = 35         # sun height; lower = longer shadows
HILLSHADE_CLIP_PERCENT = 1.0    # contrast boost

LEGEND_TEXT_PT = 9
LABEL_PT = 6                    # map labels: cities, high point, peaks, rivers (all black)
LABEL_HALO_PT = 0.3             # thin white outline around map labels

# Cities: the state capital (star) plus the MAX_CITIES largest incorporated
# places by Census population estimate, at least CITY_SPACING x the state's
# size apart (size = square root of land area), so a cluster like
# Dallas-Fort Worth gets one label.
MAX_CITIES = 5
CITY_SPACING = 0.12
# Census population estimates, one CSV per state (no API key needed).
POP_CSV_URL = ("https://www2.census.gov/programs-surveys/popest/datasets/"
               "2020-2024/cities/totals/sub-est2024_{fips}.csv")

# Mountain names (USGS GNIS summits): the MAX_PEAKS highest named summits, at
# least PEAK_SPACING x the state's size apart. The state high point is
# labeled separately, so summits within HIGH_POINT_GAP_MI of it are skipped.
MAX_PEAKS = 6
HIGH_POINT_GAP_MI = 10          # no peak labels this close to the high point
PEAK_SPACING = 0.1
WIKIDATA_URL = "https://query.wikidata.org/sparql"   # published peak elevations
PEAK_CANDIDATES = 400           # highest GNIS summits (by DEM) looked up in Wikidata
GNIS_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/"
            "DomesticNames/DomesticNames_{abbr}_Text.zip")   # abbr "National" = all

EXPORT_DPI = 600
REQUEST_TIMEOUT = 600           # seconds

DEM_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/"
           "current/n{n:02d}w{w:03d}/USGS_1_n{n:02d}w{w:03d}.tif")
gdal.UseExceptions()


# ---- Downloads and cache ---------------------------------------------------------
def download_file(url, path, max_retries=3):
    """Save url to path, retrying on errors. False if the file doesn't exist (404)."""
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                if r.status_code == 404:
                    return False
                r.raise_for_status()
                with open(path + ".part", "wb") as f:
                    f.writelines(r.iter_content(1 << 20))
            os.replace(path + ".part", path)
            return True
        except requests.RequestException:
            if attempt == max_retries:
                raise
            time.sleep(10 * attempt)


def cached(path, make, keep=len):
    """make()'s result, saved to path and reused by later runs (REUSE_DATA).
    Results that fail keep() (e.g. empty after a failed download) aren't saved."""
    if REUSE_DATA and os.path.exists(path):
        return pd.read_pickle(path)
    result = make()
    if keep(result):
        pd.to_pickle(result, path + ".part")
        os.replace(path + ".part", path)
    return result


# ---- DEM ------------------------------------------------------------------------
def warp_state_dem(geom, cell_size, out_path, work_dir):
    """Download the USGS 1-degree tiles covering the state (lon/lat outline),
    several at once, and write one DEM in STATE_CRS, cut to the outline.
    Missing tiles (open water) are skipped. False if there are none."""
    x0, y0, x1, y1 = geom.bounds
    urls = [DEM_URL.format(n=lat + 1, w=-lon)            # named by NW corner
            for lat in range(math.floor(y0), math.ceil(y1))
            for lon in range(math.floor(x0), math.ceil(x1))
            if box(lon, lat, lon + 1, lat + 1).intersection(geom).area > 0]
    paths = [f"{work_dir}/{os.path.basename(u)}" for u in urls]
    print(f"Downloading {len(urls)} DEM tiles...")
    with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
        found = list(pool.map(download_file, urls, paths))
    tiles = [p for p, ok in zip(paths, found) if ok]
    if not tiles:
        return False
    cutline = os.path.splitext(out_path)[0] + "_outline.geojson"
    gpd.GeoSeries([geom], crs="EPSG:4269").to_file(cutline)
    print(f"Mosaicking {len(tiles)} DEM tiles ({cell_size} m)...")
    mosaic = gdal.BuildVRT("/vsimem/dem.vrt", tiles)
    gdal.Warp(out_path, mosaic, dstSRS=STATE_CRS, xRes=cell_size, yRes=cell_size,
              targetAlignedPixels=True, cutlineDSName=cutline, cropToCutline=True,
              resampleAlg="bilinear", outputType=gdal.GDT_Float32, dstNodata=-9999,
              multithread=True, warpOptions=["NUM_THREADS=ALL_CPUS"],
              creationOptions=["TILED=YES", "BIGTIFF=IF_SAFER"],
              callback=gdal.TermProgress_nocb if PARALLEL_STATES == 1 else None)
    mosaic = None
    gdal.Unlink("/vsimem/dem.vrt")
    for t in tiles:             # free the disk space now
        os.remove(t)
    return True


# ---- NHD ------------------------------------------------------------------------
def download_state_nhd(state, work_dir, min_area_km2=0.3, max_retries=3):
    """State NHD shapefile zip -> (lakes, river lines), both with a NAME field.
    River lines are in STATE_CRS without Z. Both empty if the download fails."""
    url = (f"https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/"
           f"State/Shape/NHD_H_{state.replace(' ', '_')}_State_Shape.zip")
    nhd_zip, nhd_dir = f"{work_dir}/nhd_state.zip", f"{work_dir}/nhd"
    print(f"Downloading NHD for {state}...")
    try:
        ok = download_file(url, nhd_zip, max_retries)
    except requests.RequestException as e:
        ok = False
        print(f"NHD download failed: {e}")
    if not ok:
        return gpd.GeoDataFrame(), gpd.GeoDataFrame()

    # Large states split layers into NHDWaterbody.shp, NHDWaterbody2.shp, ...
    with zipfile.ZipFile(nhd_zip) as z:
        files = [m for m in z.namelist()
                 if os.path.basename(m).lower().startswith(("nhdwaterbody", "nhdflowline"))]
        z.extractall(nhd_dir, files)

    def read_file(path, fields):    # only the needed fields (faster), if supported
        try:
            names = {c.lower(): c for c in gpd.read_file(path, rows=1).columns}
            return gpd.read_file(path, columns=[names[f] for f in fields])
        except Exception:
            return gpd.read_file(path)

    def read_layer(prefix, fields):     # field names lower-cased (gnis_name, ftype, ...)
        parts = [read_file(f"{nhd_dir}/{m}", fields) for m in files
                 if m.lower().endswith(".shp") and os.path.basename(m).lower().startswith(prefix)]
        df = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs) \
            if parts else gpd.GeoDataFrame()
        return df.rename(columns=str.lower)

    # Keep only NAME, FTYPE + geometry (NHD's date field can't go in a shapefile).
    water = read_layer("nhdwaterbody", ["gnis_name", "ftype", "areasqkm"])
    if len(water):
        water = water[water["areasqkm"] >= min_area_km2]
        water = gpd.GeoDataFrame(
            {"NAME": water["gnis_name"].fillna("").values,
             "FTYPE": pd.to_numeric(water["ftype"], errors="coerce").fillna(0).astype(int).values},
            geometry=water.geometry.values, crs=water.crs)
    flow = read_layer("nhdflowline", ["gnis_name", "ftype"])
    if len(flow):
        flow = flow[pd.to_numeric(flow["ftype"], errors="coerce").isin(NHD_RIVER_FTYPES)]
        flow = flow.to_crs(STATE_CRS)
        flow = gpd.GeoDataFrame({"NAME": flow["gnis_name"].fillna("").str.strip().values},
                                geometry=shapely.force_2d(np.asarray(flow.geometry)),
                                crs=STATE_CRS)
    return water, flow


def label_largest_lakes(lakes):
    """LABEL = name for the LAKE_LABEL_COUNT largest named lakes/reservoirs
    (only the biggest piece of each), "" for the rest (not drawn)."""
    lakes = lakes.copy()
    lakes["AREA"] = lakes.to_crs(STATE_CRS).area
    named = lakes[(lakes["NAME"] != "") & lakes["FTYPE"].isin(NHD_LAKE_FTYPES)]
    top = (named.sort_values("AREA", ascending=False).drop_duplicates("NAME")
           .head(LAKE_LABEL_COUNT).index)
    lakes["LABEL"] = ""
    lakes.loc[top, "LABEL"] = lakes.loc[top, "NAME"]
    return lakes.drop(columns="AREA")


def upstream_length_km(geoms):
    """Each line's length plus everything upstream (km). NHD lines run
    downstream, so upstream lines are those ending where a line starts.
    Splits share their upstream length equally; loops are cut."""
    geoms = np.asarray(geoms)
    n = len(geoms)
    ends = np.concatenate([shapely.get_point(geoms, 0), shapely.get_point(geoms, -1)])
    xy = np.round(np.column_stack([shapely.get_x(ends), shapely.get_y(ends)]), 0)
    node = np.unique(xy, axis=0, return_inverse=True)[1].ravel()
    frm, to = node[:n], node[n:]
    n_nodes = int(node.max()) + 1
    length = shapely.length(geoms) / 1000

    n_out = np.bincount(frm, minlength=n_nodes)
    remaining = np.bincount(to, minlength=n_nodes)   # inflows not yet counted
    order = np.argsort(frm, kind="stable")
    first_out = np.searchsorted(frm[order], np.arange(n_nodes + 1))
    inflow = np.zeros(n_nodes)
    total = np.full(n, np.nan)
    done = np.zeros(n_nodes, dtype=bool)

    def run(stack):
        while stack:
            k = stack.pop()
            if done[k]:
                continue
            done[k] = True
            share = inflow[k] / n_out[k] if n_out[k] else 0.0
            for line in order[first_out[k]:first_out[k + 1]]:
                total[line] = length[line] + share
                d = to[line]
                inflow[d] += total[line]
                remaining[d] -= 1
                if remaining[d] <= 0 and not done[d]:
                    stack.append(d)

    run(list(np.flatnonzero(remaining == 0)))        # sources, then downstream
    pending = np.flatnonzero(~done & (n_out > 0))    # what's left is in loops
    for k in pending[np.argsort(remaining[pending], kind="stable")]:
        run([k])
    return total


def named_stretch_km(flow):
    """Length (km) of the connected same-name stretch each line is part of;
    0 if unnamed. Separate rivers with the same name stay separate."""
    out = np.zeros(len(flow))
    geoms = np.asarray(flow.geometry)
    for name, ii in flow.groupby("NAME").indices.items():
        if not name:
            continue
        merged = shapely.line_merge(shapely.union_all(geoms[ii]))
        parts = [g for g in getattr(merged, "geoms", [merged]) if not g.is_empty]
        if parts:
            lengths = np.array([g.length for g in parts]) / 1000
            mids = shapely.line_interpolate_point(geoms[ii], 0.5, normalized=True)
            out[ii] = lengths[shapely.STRtree(parts).nearest(mids)]
    return out


def longest_named_rivers(rivers):
    """One line per river name (its longest connected stretch), for labels:
    the RIVER_LABEL_COUNT longest, with NAME and LEN_KM."""
    named = rivers[(rivers["NAME"] != "") & ~rivers["NAME"].str.contains(RIVER_LABEL_SKIP)]
    rows = []
    for name, grp in named.groupby("NAME"):
        merged = shapely.line_merge(shapely.union_all(np.asarray(grp.geometry)))
        parts = [g for g in getattr(merged, "geoms", [merged]) if not g.is_empty]
        if parts:
            longest = max(parts, key=lambda g: g.length)
            rows.append({"NAME": name, "LEN_KM": round(longest.length / 1000, 1),
                         "geometry": longest})
    if not rows:
        return gpd.GeoDataFrame()
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=STATE_CRS)
    out = out[out["LEN_KM"] >= MIN_RIVER_LABEL_KM]
    return out.sort_values("LEN_KM", ascending=False).head(RIVER_LABEL_COUNT)


def river_size_classes(upstream_km, stretch_km, threshold, state_size_km):
    """Class 0-4 per line (0 = not drawn)."""
    by_flow = np.searchsorted(np.array(RIVER_CLASS_MULTIPLES) * threshold,
                              upstream_km, side="right")
    by_name = np.searchsorted(np.array(RIVER_LENGTH_CLASS_FRACTIONS) * state_size_km,
                              stretch_km, side="right")
    return np.maximum(by_flow, np.where(by_name > 0, by_name + 1, 0))


# ---- Cities -----------------------------------------------------------------------------
def city_label(name):
    """Short name for consolidated city-counties, e.g. 'Nashville-Davidson
    metropolitan government (balance)' -> 'Nashville'."""
    name = name.replace(" city (balance)", "").replace(" (balance)", "")
    if any(w in name.lower() for w in ("government", "county")):
        name = name.replace("/", "-").split("-")[0]
    return name.strip()


def place_populations(fips):
    """Place code ('place') and latest population estimate ('POP') for a state."""
    r = requests.get(POP_CSV_URL.format(fips=int(fips)), timeout=120)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), dtype=str, encoding="latin-1")
    df = df[df["SUMLEV"] == "162"]          # incorporated places
    year_col = sorted(c for c in df.columns if c.startswith("POPESTIMATE"))[-1]
    return pd.DataFrame({"place": df["PLACE"].str.zfill(5), "POP": df[year_col].astype(int)})


def state_cities(state, fips, state_size_km):
    """Capital + MAX_CITIES largest places, spaced out, as points in STATE_CRS
    with NAME, POP and CAPITAL (1/0). Empty on failure."""
    try:
        pop = place_populations(fips)
        shapes = places(state=fips, cb=True, year=2020, cache=True)
    except Exception as e:
        print(f"City data download failed ({e}) -- no cities.")
        return gpd.GeoDataFrame()
    shapes = shapes.merge(pop[["place", "POP"]], left_on="PLACEFP", right_on="place")
    pts = gpd.GeoDataFrame({"NAME": shapes["NAME"].map(city_label), "POP": shapes["POP"]},
                           geometry=shapes.to_crs(STATE_CRS).representative_point(),
                           crs=STATE_CRS).sort_values("POP", ascending=False)
    capital = STATE_INFO[state][0]
    pts["CAPITAL"] = 0
    match = pts.index[pts["NAME"].str.startswith(capital)] if capital else []
    if len(match):
        pts.loc[match[0], "CAPITAL"] = 1          # most populous match
    order = pd.concat([pts[pts["CAPITAL"] == 1], pts[pts["CAPITAL"] == 0]])
    kept = []
    for i, g in zip(order.index, order.geometry):
        if all(g.distance(order.geometry[k]) >= CITY_SPACING * state_size_km * 1000
               for k in kept):
            kept.append(i)
        if len(kept) == MAX_CITIES + int(len(match) > 0):
            break
    return order.loc[kept]


# ---- Mountain names -----------------------------------------------------------------
def gnis_summits(state, abbr, work_dir):
    """Named summits in the state (lon/lat points with NAME), from the state
    GNIS file, or the national file if the state one isn't there."""
    zip_path = f"{work_dir}/gnis.zip"
    for code in (abbr, "National"):
        try:
            print(f"Downloading GNIS names ({code})...")
            if download_file(GNIS_URL.format(abbr=code), zip_path):
                break
            print("  GNIS file not found")
        except requests.RequestException as e:
            print(f"  GNIS download failed: {e}")
    else:
        return gpd.GeoDataFrame()
    with zipfile.ZipFile(zip_path) as z:
        txt = next(n for n in z.namelist() if n.lower().endswith(".txt"))
        names = pd.read_csv(z.open(txt), sep="|", dtype=str, encoding="utf-8",
                            usecols=["feature_id", "feature_name", "feature_class", "state_name",
                                     "prim_lat_dec", "prim_long_dec"])
    os.remove(zip_path)
    names = names[(names["state_name"] == state) & (names["feature_class"] == "Summit")]
    names = names.dropna()
    return gpd.GeoDataFrame({"GNIS_ID": names["feature_id"].values,
                             "NAME": names["feature_name"].values},
                            geometry=gpd.points_from_xy(names["prim_long_dec"].astype(float),
                                                        names["prim_lat_dec"].astype(float)),
                            crs="EPSG:4269")


def wikidata_elevations(gnis_ids, batch=200, max_retries=3):
    """Published elevations (ft) from Wikidata, keyed by GNIS feature ID."""
    elev = {}
    for i in range(0, len(gnis_ids), batch):
        ids = " ".join(f'"{g}"' for g in gnis_ids[i:i + batch])
        query = ("SELECT ?g (MAX(?m) AS ?top) WHERE { VALUES ?g { " + ids + " } "
                 "?item wdt:P590 ?g; p:P2044 ?st. ?st psn:P2044/wikibase:quantityAmount ?m; "
                 "wikibase:rank ?rank. FILTER(?rank != wikibase:DeprecatedRank) } GROUP BY ?g")
        for attempt in range(max_retries):
            try:
                r = requests.post(WIKIDATA_URL, data={"query": query, "format": "json"},
                                  headers={"User-Agent": "StateElevationMaps/1.0 (ArcGIS Pro script)",
                                           "Accept": "application/sparql-results+json"},
                                  timeout=120)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(10 * (attempt + 1))
        for row in r.json()["results"]["bindings"]:
            elev[row["g"]["value"]] = float(row["top"]["value"]) * 3.28084
    return elev


def state_peaks(state, abbr, dem_path, work_dir, state_size_km, high_point_xy):
    """The MAX_PEAKS highest named summits, spaced out, with NAME and ELEV_FT
    (published Wikidata elevation), in STATE_CRS. The DEM only picks the
    candidates to look up; if Wikidata can't be reached, DEM heights are used."""
    try:
        peaks = cached(f"{work_dir}/gnis_summits.pkl",
                       lambda: gnis_summits(state, abbr, work_dir))
    except Exception as e:
        print(f"Mountain names failed ({e}) -- none shown.")
        return gpd.GeoDataFrame()
    if not len(peaks):
        return peaks
    peaks = peaks.to_crs(STATE_CRS)
    ds = gdal.Open(dem_path)
    x0, cell, _, y0, _, _ = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    elev = []
    for p in peaks.geometry:
        col, row = int((p.x - x0) / cell) - 2, int((y0 - p.y) / cell) - 2
        win = (band.ReadAsArray(col, row, 5, 5)
               if 0 <= col < ds.RasterXSize - 5 and 0 <= row < ds.RasterYSize - 5 else None)
        win = win[win != nodata] if win is not None else np.array([])
        elev.append(float(win.max()) if win.size else np.nan)
    ds = None
    peaks["ELEV_FT"] = elev
    peaks = peaks.dropna(subset=["ELEV_FT"]).sort_values("ELEV_FT", ascending=False)
    peaks = peaks.head(PEAK_CANDIDATES)
    try:
        published = wikidata_elevations(peaks["GNIS_ID"].tolist())
        peaks["ELEV_FT"] = peaks["GNIS_ID"].map(published)
        print(f"  {len(published)} of {len(peaks)} candidate summits have a published elevation")
        peaks = peaks.dropna(subset=["ELEV_FT"]).sort_values("ELEV_FT", ascending=False)
    except Exception as e:
        print(f"  Wikidata elevations failed ({e}) -- using DEM heights.")
    if high_point_xy is not None:
        peaks = peaks[peaks.distance(shapely.Point(high_point_xy)) > HIGH_POINT_GAP_MI * 1609.344]
    kept = []
    for i, g in zip(peaks.index, peaks.geometry):
        if all(g.distance(peaks.geometry[k]) >= PEAK_SPACING * state_size_km * 1000
               for k in kept):
            kept.append(i)
            if len(kept) == MAX_PEAKS:
                break
    peaks = peaks.loc[kept]
    peaks["ELEV_FT"] = peaks["ELEV_FT"].round().astype(int)
    return peaks


# ---- Map helpers ------------------------------------------------------------------
def style_labels(layer, expression, size_pt=LABEL_PT, italic=False, follow_line=False):
    """Black Arcade labels with a thin white halo. italic uses Arial (the
    default, Tahoma, has no italic); follow_line: one label per line feature,
    curved along it."""
    layer.listLabelClasses()[0].expression = expression
    layer.showLabels = True
    cim = layer.getDefinition("V3")
    text_sym = cim.labelClasses[0].textSymbol.symbol
    black = arcpy.cim.CreateCIMObjectFromClassName("CIMRGBColor", "V3")
    black.values = [0, 0, 0, 100]
    text_sym.symbol.symbolLayers[0].color = black
    if italic:
        text_sym.fontFamilyName, text_sym.fontStyleName = "Arial", "Italic"
    if follow_line:
        mp = cim.labelClasses[0].maplexLabelPlacementProperties
        mp.featureType, mp.linePlacementMethod = "Line", "CenteredCurvedOnLine"
        mp.repeatLabel = False
    text_sym.height = size_pt
    white = arcpy.cim.CreateCIMObjectFromClassName("CIMRGBColor", "V3")
    white.values = [255, 255, 255, 100]
    fill = arcpy.cim.CreateCIMObjectFromClassName("CIMSolidFill", "V3")
    fill.color = white
    halo = arcpy.cim.CreateCIMObjectFromClassName("CIMPolygonSymbol", "V3")
    halo.symbolLayers = [fill]
    text_sym.haloSymbol, text_sym.haloSize = halo, LABEL_HALO_PT
    layer.setDefinition(cim)


def simple_symbol(layer, label, color=None, outline=None, size=None):
    """Single-symbol renderer with the given colors and size."""
    sym = layer.symbology
    if not hasattr(sym, "renderer"):
        return
    if sym.renderer.type != "SimpleRenderer":
        sym.updateRenderer("SimpleRenderer")
    s = sym.renderer.symbol
    if color is not None:
        s.color = color
    if outline is not None:
        s.outlineColor = outline
    if size is not None:
        s.size = size
    sym.renderer.label = label
    layer.symbology = sym


def set_text_height(text_sym, height):
    """Works on a CIM text symbol or a symbol reference wrapping one."""
    if text_sym is not None:
        getattr(text_sym, "symbol", text_sym).height = height


def place(elm, x, y, anchor="TOP_LEFT_CORNER"):
    try:
        elm.setAnchor(anchor)
    except Exception:
        pass
    elm.elementPositionX = x
    elm.elementPositionY = y


def rect(x_min, y_min, x_max, y_max):
    return arcpy.Polygon(arcpy.Array([
        arcpy.Point(x_min, y_min), arcpy.Point(x_min, y_max),
        arcpy.Point(x_max, y_max), arcpy.Point(x_max, y_min),
        arcpy.Point(x_min, y_min)]))


WATER_BLUE = {"RGB": [0, 112, 255, 100]}
LAKE_BLUE = {"RGB": [115, 178, 255, 100]}
LAKE_OUTLINE = {"RGB": [40, 110, 200, 100]}
COUNTY_LINE = {"RGB": [60, 60, 60, 100]}
NO_FILL = {"RGB": [0, 0, 0, 0]}
BLACK = {"RGB": [0, 0, 0, 100]}
WHITE = {"RGB": [255, 255, 255, 100]}
PEAK_GRAY = {"RGB": [70, 70, 70, 100]}

# ---- Main ---------------------------------------------------------------------
if __name__ == "__main__":
    print("Looking up state outlines from the Census...")
ALL_STATES = states(cb=True, year=2023, cache=True)
state_sr = arcpy.SpatialReference(CRS_WKID)

def process_state(state):
    """Build one state's map. True if a map was exported."""
    arcpy.env.extent = arcpy.env.cellSize = arcpy.env.snapRaster = arcpy.env.mask = None
    arcpy.env.overwriteOutput = True

    row = ALL_STATES[ALL_STATES["NAME"] == state]
    if row.empty:
        print(f"'{state}' not found in Census state boundaries -- skipped.")
        return False
    fips = row["STATEFP"].iloc[0]
    state_geom_ll = row.to_crs(epsg=4269).geometry.iloc[0]
    state_area_m2 = row.to_crs(STATE_CRS).area.iloc[0]
    cell_size = max(DEM_RES_M, 10 * math.ceil(math.sqrt(state_area_m2 / MAX_DEM_CELLS) / 10))
    print(f"\n=== {state} (FIPS {fips}) | {cell_size} m cells ===")

    # Outputs the project uses go in the state folder; data kept for redraws
    # in cache/; downloads and intermediate files in temp/ (deleted at the end).
    base, tmp, cache = f"C:/DEM/{state}", f"C:/DEM/{state}/temp", f"C:/DEM/{state}/cache"
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    in_dem, lakes_raw = f"{tmp}/dem.tif", f"{tmp}/lake_raw.shp"
    (counties_shp, dem_ft, hillshade_tif, rivers_shp, lakes_shp, highpoint_shp,
     cities_shp) = (f"{base}/{n}" for n in (
        "counties.shp", "elevation_ft.tif", "hillshade.tif", "rivers.shp", "lakes.shp",
        "highpoint.shp", "cities.shp"))
    peaks_shp = f"{base}/peaks.shp"
    river_names_shp = f"{base}/river_names.shp"

    # DEM in feet (below sea level -> 0), cut to the state outline
    dem_done = f"{cache}/dem_{cell_size}m.done"     # marks a complete elevation_ft.tif
    if REUSE_DATA and os.path.exists(dem_done) and arcpy.Exists(dem_ft):
        print("Reusing elevation_ft.tif")
    else:
        if not warp_state_dem(state_geom_ll, cell_size, in_dem, tmp):
            print(f"No DEM data found for {state} -- skipped.")
            return False
        dem_raster = Con(Raster(in_dem) < 0, 0, Raster(in_dem) * 3.28084)
        dem_raster.save(dem_ft)
        del dem_raster
        arcpy.management.CalculateStatistics(dem_ft)
        open(dem_done, "w").close()

    # Counties: clip boundary and map layer
    state_counties = counties(state=fips, cb=True, year=2023, cache=True).to_crs(STATE_CRS)
    state_counties.to_file(counties_shp)

    # NHD lakes and rivers
    lakes, flow = cached(f"{cache}/nhd.pkl", lambda: download_state_nhd(state, tmp),
                         keep=lambda r: len(r[0]) or len(r[1]))

    if len(lakes):
        lakes = label_largest_lakes(lakes)
        lakes.to_file(lakes_raw)
        arcpy.management.RepairGeometry(lakes_raw, "DELETE_NULL")
        arcpy.analysis.Clip(lakes_raw, counties_shp, lakes_shp)
        print(f"Lakes: {int(arcpy.management.GetCount(lakes_shp)[0])} "
              f"(labeled: {', '.join(n for n in lakes['LABEL'] if n) or 'none'})")
    else:
        lakes_shp = None

    dem_max = float(arcpy.management.GetRasterProperties(dem_ft, "MAXIMUM").getOutput(0))
    print(f"Highest DEM elevation: {dem_max:,.0f} ft")

    # Hillshade (z_factor converts feet to meters, then exaggerates)
    exaggeration = min(MAX_VERTICAL_EXAGGERATION, max(MIN_VERTICAL_EXAGGERATION,
                       MIN_VERTICAL_EXAGGERATION * RELIEF_REFERENCE_FT / max(dem_max, 1)))
    arcpy.ddd.HillShade(dem_ft, hillshade_tif, 315, HILLSHADE_ALTITUDE,
                        "NO_SHADOWS", 0.3048 * exaggeration)
    print(f"Hillshade done ({exaggeration:.1f}x exaggeration)")

    # Highest point: highest of the cells within 1 ft of the maximum
    top_cells = Con(Raster(dem_ft) >= dem_max - 1, Raster(dem_ft))
    arcpy.conversion.RasterToPoint(top_cells, "memory/hp", "VALUE")
    del top_cells
    with arcpy.da.SearchCursor("memory/hp", ["SHAPE@XY", "grid_code"]) as cur:
        best = max(cur, key=lambda r: r[1], default=None)
    arcpy.management.Delete("memory/hp")
    if best is not None:
        hp_name, hp_ft = STATE_INFO[state][1:]
        gpd.GeoDataFrame({"NAME": [hp_name], "ELEV_FT": [hp_ft]},
                         geometry=[shapely.Point(best[0])], crs=STATE_CRS).to_file(highpoint_shp)
        print(f"Highest point: {hp_name}, {hp_ft:,} ft (DEM {best[1]:,.0f} ft)")
    else:
        highpoint_shp = None

    state_size_km = math.sqrt(state_counties.geometry.area.sum()) / 1000

    # Mountain names (GNIS summits)
    peaks = state_peaks(state, row["STUSPS"].iloc[0], dem_ft, cache, state_size_km,
                        best[0] if best is not None else None)
    if len(peaks):
        peaks[["NAME", "ELEV_FT", "geometry"]].to_file(peaks_shp)
        print("Peaks: " + ", ".join(f"{n} ({e:,} ft)" for n, e in zip(peaks["NAME"], peaks["ELEV_FT"])))
    else:
        peaks_shp = None

    # Cities: capital + largest places
    cities = state_cities(state, fips, state_size_km)
    if len(cities):
        cities.to_file(cities_shp)
        print(f"Cities: {', '.join(cities['NAME'])}")
    else:
        cities_shp = None

    # Rivers: NHD lines sized by upstream length
    land_area_m2 = state_counties.geometry.area.sum()
    has_rivers, river_names = False, []
    if len(flow):
        clip_area = shapely.union_all(state_counties.geometry.values).buffer(RIVER_CLIP_MARGIN_M)
        rivers = gpd.clip(flow, clip_area).explode(index_parts=False)
        rivers = rivers[rivers.geom_type == "LineString"].reset_index(drop=True)
        river_names = longest_named_rivers(rivers)
        if len(river_names):
            river_names.to_file(river_names_shp)
            print(f"River names: {', '.join(river_names['NAME'])}")
        geoms = np.asarray(rivers.geometry)
        total_km = shapely.length(geoms).sum() / 1000
        threshold = STATE_STREAM_THRESHOLD_KM.get(
            state, max(MIN_UPSTREAM_KM, total_km / STREAM_THRESHOLD_DIVISOR))
        rivers["grid_code"] = river_size_classes(
            upstream_length_km(geoms), named_stretch_km(rivers), threshold,
            math.sqrt(land_area_m2) / 1000)
        rivers = rivers[rivers["grid_code"] >= 1]
        if len(rivers):
            rivers[["NAME", "grid_code", "geometry"]].to_file(rivers_shp)
            has_rivers = True
            counts = rivers["grid_code"].value_counts().sort_index()
            print(f"Rivers (threshold {threshold:,.1f} km): " + ", ".join(
                f"{RIVER_CLASS_NAMES[c - 1].lower()} {n:,}" for c, n in counts.items()))
    if not has_rivers:
        print(f"No rivers for {state}.")
    del flow

    # ---- Project and layers ----------------------------------------------------
    project_name = "MyNewAutomatedProject"
    aprx = arcpy.mp.CreateArcGISProject(base, project_name)
    new_map = aprx.createMap(name="Elevation Map", map_type="MAP")
    for lyr in reversed(new_map.listLayers()):      # drop the default basemap
        try:
            if lyr.isBasemapLayer:
                new_map.removeLayer(lyr)
        except Exception:
            pass

    # Hillshade under a semi-transparent DEM; vectors on top, counties last.
    hs_layer = new_map.addDataFromPath(hillshade_tif)
    hs_layer.name = "Hillshade"
    dem_layer = new_map.addDataFromPath(dem_ft)
    dem_layer.name = "Elevation (ft)"
    s_layer = new_map.addDataFromPath(rivers_shp) if has_rivers else None
    l_layer = new_map.addDataFromPath(lakes_shp) if lakes_shp else None
    c_layer = new_map.addDataFromPath(counties_shp)
    rn_layer = new_map.addDataFromPath(river_names_shp) if len(river_names) else None
    pk_layer = new_map.addDataFromPath(peaks_shp) if peaks_shp else None
    hp_layer = new_map.addDataFromPath(highpoint_shp) if highpoint_shp else None
    city_layer = new_map.addDataFromPath(cities_shp) if cities_shp else None

    sym = dem_layer.symbology
    if hasattr(sym, "colorizer"):
        if sym.colorizer.type != "RasterStretchColorizer":
            sym.updateColorizer("RasterStretchColorizer")
        sym.colorizer.colorRamp = aprx.listColorRamps("Elevation #1")[0]
        try:
            sym.colorizer.stretchType = "MinimumMaximum"
            sym.colorizer.minLabel = "0 ft"
            sym.colorizer.maxLabel = f"{dem_max:,.0f} ft"
        except Exception as e:
            print(f"Could not set elevation legend labels: {e}")
        dem_layer.symbology = sym
    dem_layer.transparency = 45

    try:    # hillshade contrast
        sym = hs_layer.symbology
        if hasattr(sym, "colorizer") and sym.colorizer.type == "RasterStretchColorizer":
            sym.colorizer.stretchType = "PercentClip"
            sym.colorizer.minPercent = sym.colorizer.maxPercent = HILLSHADE_CLIP_PERCENT
            hs_layer.symbology = sym
    except Exception as e:
        print(f"Could not boost hillshade contrast: {e}")

    if s_layer is not None:
        s_layer.name = "Rivers"
        try:    # width by size class
            sym = s_layer.symbology
            sym.updateRenderer("GraduatedSymbolsRenderer")
            sym.renderer.classificationField = "grid_code"
            sym.renderer.breakCount = len(RIVER_CLASS_NAMES)
            for upper, (brk, name, width) in enumerate(
                    zip(sym.renderer.classBreaks, RIVER_CLASS_NAMES, RIVER_CLASS_WIDTHS), 1):
                brk.upperBound = upper
                brk.label = name
                brk.symbol.color = WATER_BLUE
                brk.symbol.size = width
            s_layer.symbology = sym
        except Exception as e:
            print(f"Could not style rivers by size ({e}); using one width.")
            simple_symbol(s_layer, "Rivers", WATER_BLUE, size=0.5)

        # Legend stand-in: same file, draws nothing, one line in the legend.
        river_legend_layer = new_map.addDataFromPath(rivers_shp)
        river_legend_layer.name = "Rivers & streams"
        river_legend_layer.definitionQuery = "grid_code < 0"
        simple_symbol(river_legend_layer, "Rivers & streams", WATER_BLUE, size=1.0)
        try:
            new_map.moveLayer(s_layer, river_legend_layer, "BEFORE")
        except Exception:
            pass

    if l_layer is not None:
        l_layer.name = "Lakes"
        simple_symbol(l_layer, "Lakes", LAKE_BLUE, LAKE_OUTLINE, 0.4)
        try:
            style_labels(l_layer, "$feature.LABEL", italic=True)
        except Exception as e:
            print(f"Could not label lakes: {e}")

    c_layer.name = "County Boundaries"
    simple_symbol(c_layer, "County Boundaries", NO_FILL, COUNTY_LINE, 0.75)

    if rn_layer is not None:          # draws nothing; only carries the river names
        rn_layer.name = "River Names"
        simple_symbol(rn_layer, "River names", NO_FILL, size=0.5)
        try:
            style_labels(rn_layer, "$feature.NAME", italic=True, follow_line=True)
        except Exception as e:
            print(f"Could not label rivers: {e}")

    if pk_layer is not None:          # small gray triangles, name + elevation
        pk_layer.name = "Mountain Peaks"
        sym = pk_layer.symbology
        try:
            sym.renderer.symbol.applySymbolFromGallery("Triangle 1")
            pk_layer.symbology = sym
        except Exception:
            pass
        simple_symbol(pk_layer, "Mountain peak", PEAK_GRAY, WHITE, 5)
        try:
            style_labels(pk_layer, "$feature.NAME + TextFormatting.NewLine + "
                                   "Text($feature.ELEV_FT, '#,###') + ' ft'")
        except Exception as e:
            print(f"Could not label peaks: {e}")

    if hp_layer is not None:
        hp_layer.name = "Highest Point"
        sym = hp_layer.symbology
        if hasattr(sym, "renderer"):
            try:
                sym.renderer.symbol.applySymbolFromGallery("Triangle 1")
                hp_layer.symbology = sym
            except Exception:
                pass
        simple_symbol(hp_layer, "Highest point", BLACK, WHITE, 8)
        try:
            style_labels(hp_layer, "$feature.NAME + TextFormatting.NewLine + "
                                   "Text($feature.ELEV_FT, '#,###') + ' ft'")
        except Exception as e:
            print(f"Could not label highest point: {e}")

    # Cities: black dots and a star for the capital, with black labels
    if city_layer is not None:
        city_layer.name = "Cities"
        sym = city_layer.symbology
        try:
            sym.updateRenderer("UniqueValueRenderer")
            sym.renderer.fields = ["CAPITAL"]
            for grp in sym.renderer.groups:
                for itm in grp.items:
                    is_capital = str(itm.values[0][0]) == "1"
                    if is_capital:
                        try:
                            itm.symbol.applySymbolFromGallery("Star 1")
                        except Exception:
                            pass
                    itm.symbol.color = BLACK
                    itm.symbol.outlineColor = WHITE
                    itm.symbol.size = 8 if is_capital else 5
                    itm.label = "State capital" if is_capital else "City"
            city_layer.symbology = sym
        except Exception as e:
            print(f"Could not style cities ({e}); using plain dots.")
            simple_symbol(city_layer, "City", BLACK, WHITE, 5)
        try:
            style_labels(city_layer, "$feature.NAME")
        except Exception as e:
            print(f"Could not label cities: {e}")

    # ---- Layout ------------------------------------------------------------------
    # Title on top, map below; legend, north arrow, scale bar and credits
    # stacked in the lower right, with the state fitted to their left.
    ext = arcpy.Describe(dem_ft).extent
    page_w, page_h = (8.5, 11) if ext.height > ext.width else (11, 8.5)
    layout = aprx.createLayout(page_w, page_h, "INCH", "Elevation Layout")
    margin, title_band, box_w = 0.2, 0.5, 1.7
    box_x = page_w - margin - box_w
    map_top = page_h - margin - title_band

    map_frame = layout.createMapFrame(
        arcpy.Extent(margin, margin, page_w - margin, map_top), new_map, "Elevation Map Frame")
    dem_ext = map_frame.getLayerExtent(dem_layer, False, True)
    frame_w, frame_h = page_w - 2 * margin, map_top - margin
    pad_in = 0.15
    units_per_in = max(dem_ext.width / (frame_w - box_w - 0.2 - 2 * pad_in),
                       dem_ext.height / (frame_h - 2 * pad_in))
    x_min = dem_ext.XMin - pad_in * units_per_in
    y_mid = (dem_ext.YMin + dem_ext.YMax) / 2
    half_h = frame_h * units_per_in / 2
    map_frame.camera.setExtent(arcpy.Extent(x_min, y_mid - half_h,
                                            x_min + frame_w * units_per_in, y_mid + half_h))

    def style_item(category, preferred):
        items = aprx.listStyleItems("ArcGIS 2D", category, preferred)
        return items[0] if items else aprx.listStyleItems("ArcGIS 2D", category)[0]

    title = aprx.createTextElement(layout, arcpy.Point(margin, page_h - margin), "POINT",
                                   f"{state}: Elevation and Hydrology", 18, name="Title")
    place(title, margin, page_h - margin)

    # Legend: no title or border, one line per vector layer, one text size.
    legend = layout.createMapSurroundElement(
        rect(box_x, margin + 2.0, box_x + box_w, min(margin + 5.6, map_top - 0.1)),
        "LEGEND", map_frame, style_item("LEGEND", "Legend 1"), "Legend")
    for attr, value in (("showTitle", False), ("fittingStrategy", "AdjustFontSize")):
        try:
            setattr(legend, attr, value)
        except Exception:
            pass
    one_line_items = {"Rivers & streams", "Lakes", "County Boundaries", "Highest Point",
                      "Cities", "Mountain Peaks"}
    try:
        leg_cim = legend.getDefinition("V3")
        for item in leg_cim.items:
            item.showHeading = False
            if item.name in one_line_items:
                item.showLayerName = False
            for attr in ("layerNameSymbol", "labelSymbol"):
                try:
                    set_text_height(getattr(item, attr, None), LEGEND_TEXT_PT)
                except Exception:
                    pass
        leg_cim.graphicFrame.borderSymbol = None
        legend.setDefinition(leg_cim)
    except Exception as e:
        print(f"Could not restyle legend: {e}")
    # Hide the hillshade and the width-by-size rivers (after the CIM edits).
    to_hide = {"Hillshade", "Rivers", "River Names"}
    for item in legend.items:
        if item.name in to_hide:
            item.visible = False

    north_arrow = layout.createMapSurroundElement(
        arcpy.Point(box_x, margin + 1.4), "NORTH_ARROW", map_frame,
        style_item("NORTH_ARROW", "ArcGIS North 1"), "North Arrow")
    north_arrow.elementHeight = 0.35
    place(north_arrow, box_x + 0.1, margin + 1.75)

    # Scale bar: one thin segment, labels only at the ends.
    scale_bar = layout.createMapSurroundElement(
        rect(box_x, margin + 0.95, box_x + box_w, margin + 1.2), "SCALE_BAR",
        map_frame, style_item("SCALE_BAR", "Scale Line 1"), "Scale Bar")
    try:
        sb_cim = scale_bar.getDefinition("V3")
        sb_cim.divisions, sb_cim.subdivisions, sb_cim.divisionsBeforeZero = 1, 0, 0
        sb_cim.fittingStrategy = "AdjustDivision"
        sb_cim.labelFrequency = sb_cim.markFrequency = "Divisions"
        for attr in ("labelSymbol", "unitLabelSymbol"):
            try:
                set_text_height(getattr(sb_cim, attr, None), LEGEND_TEXT_PT)
            except Exception:
                pass
        scale_bar.setDefinition(sb_cim)
    except Exception as e:
        print(f"Could not simplify scale bar: {e}")

    credits_text = (
        f"Elevation: {DEM_CREDIT} ({cell_size} m)\n"
        "  via AWS Open Data\n"
        "Rivers & lakes: USGS NHD\n"
        "Boundaries & cities: U.S. Census Bureau\n"
        "Peak names: USGS GNIS\n"
        "Peak heights: Wikidata,\n  Highpointers Foundation\n"
        f"Projection: {state_sr.name.replace('_', ' ')}\n"
        f"Map created {time.strftime('%B %d, %Y')}")
    credits = aprx.createTextElement(layout, arcpy.Point(box_x, margin), "POINT",
                                     credits_text, 6, name="Credits")
    place(credits, box_x, margin, "BOTTOM_LEFT_CORNER")

    out_png = f"{base}/{state}_elevation.png"
    layout.exportToPNG(out_png, resolution=EXPORT_DPI)
    aprx.save()
    print(f"Map: {out_png}\nProject: {base}/{project_name}.aprx")

    # Clean up. Release raster objects first (Windows locks open files).
    del aprx
    arcpy.management.ClearWorkspaceCache()
    shutil.rmtree(tmp, ignore_errors=True)
    return True


# ---- Run all states ---------------------------------------------------------------
# One failure doesn't stop the run: the error is printed and logged, and the
# next state starts. Rerun the script to retry -- finished states are skipped.
def run_state(state):
    """process_state, timed, with errors caught -> result text."""
    start = time.time()
    try:
        result = "done" if process_state(state) else "no map"
    except Exception as e:
        traceback.print_exc()
        result = f"FAILED: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        arcpy.management.ClearWorkspaceCache()
    return f"{result} ({(time.time() - start) / 60:.0f} min)"


def run_state_logged(state):
    """run_state with its printout going to the state's build_log.txt
    (used when several states run at once)."""
    os.makedirs(f"C:/DEM/{state}", exist_ok=True)
    with open(f"C:/DEM/{state}/build_log.txt", "w", buffering=1) as out, \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return run_state(state)


if __name__ == "__main__":
    todo = [s for s in STATES if not (SKIP_EXISTING and
                                      os.path.exists(f"C:/DEM/{s}/{s}_elevation.png"))]
    results = {s: "already done" for s in STATES if s not in todo}
    run_start = time.time()

    def record(state, result):
        results[state] = result
        print(f"  {state}: {result}")
        with open(LOG_FILE, "a") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M')}  {state}: {result}\n")

    if PARALLEL_STATES > 1 and len(todo) > 1:
        print(f"Building {len(todo)} states, {PARALLEL_STATES} at a time. Each state's "
              f"progress is in C:/DEM/<state>/build_log.txt")
        # A fresh process per state keeps memory from building up.
        try:
            pool = ProcessPoolExecutor(PARALLEL_STATES, max_tasks_per_child=1)
        except TypeError:                   # Python before 3.11
            pool = ProcessPoolExecutor(PARALLEL_STATES)
        with pool:
            jobs = {pool.submit(run_state_logged, s): s for s in todo}
            for job in as_completed(jobs):
                try:
                    result = job.result()
                except Exception as e:      # e.g. the process ran out of memory
                    result = f"FAILED: {type(e).__name__}: {e}"
                record(jobs[job], result)
    else:
        for i, state in enumerate(todo, 1):
            print(f"\n##### {i}/{len(todo)}: {state} #####")
            record(state, run_state(state))

    print(f"\n===== Finished in {(time.time() - run_start) / 3600:.1f} h =====")
    for state in STATES:
        print(f"  {state:15s} {results[state]}")
    failed = [s for s in STATES if results[s].startswith("FAILED")]
    if failed:
        print(f"\n{len(failed)} failed -- rerun the script to retry them: {', '.join(failed)}")
