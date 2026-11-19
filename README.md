<div align="center">

# 🏔️ State Elevation & Hydrology Mapper

**Automated ArcGIS Pro pipeline that turns USGS elevation and hydrography data into styled, labeled elevation maps for each of the lower 48 U.S. states.**

![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![ArcGIS Pro](https://img.shields.io/badge/ArcGIS%20Pro-arcpy-2C7AC3?logo=arcgis&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)

<img src="maps/New_Jersey_elevation.png" alt="New Jersey elevation map" width="600">

</div>

---

## 📖 Overview

For each state, `elevation.py` downloads USGS 3DEP elevation tiles and mosaics them into one DEM cut to the state outline, in an equal-area projection. It builds a hillshade and draws NHD rivers sized by how much stream network flows into them. It adds lakes, counties, cities, named mountain peaks and the state high point. Then it saves a styled ArcGIS Pro project (`.aprx`) and a 600 DPI PNG map.

No API key is needed. Every source is a public download.

## 📑 Contents

- [Pipeline](#%EF%B8%8F-pipeline)
- [Requirements](#-requirements)
- [Installation](#-installation)
- [Configuration](#%EF%B8%8F-configuration)
- [Usage](#%EF%B8%8F-usage)
- [Output](#-output)
- [Data Sources](#%EF%B8%8F-data-sources)
- [Known Limitations](#%EF%B8%8F-known-limitations)

## ⚙️ Pipeline

| Step | Stage | Details |
|:---:|---|---|
| 1 | **Download DEM** | USGS 3DEP 1 arc-second (~30 m) 1° tiles, downloaded several at once, mosaicked with GDAL and cut to the state outline. Large states get a coarser cell size to stay under `MAX_DEM_CELLS`. |
| 2 | **Get boundaries** | 2023 Census state and county boundaries via `pygris`. |
| 3 | **Get hydrography** | NHD state shapefile: flowlines (streams and rivers) and waterbodies (lakes and reservoirs ≥ 0.3 km²). |
| 4 | **Convert units** | Elevation from meters to feet; cells below sea level are set to 0. |
| 5 | **Hillshade** | Azimuth 315°, altitude 35°. Vertical exaggeration scales with relief, so flat states still show terrain. |
| 6 | **Peaks & high point** | The highest named GNIS summits, spaced apart and kept at least 10 miles from the state high point. Heights come from published sources, not the DEM. |
| 7 | **Cities** | The state capital (star) plus the largest places by 2024 Census population estimate, spaced apart. |
| 8 | **Rivers** | Each NHD flowline gets an upstream length: itself plus everything that drains into it. Lines above a state-scaled threshold are drawn in 4 width classes. The longest named rivers get curved labels. |
| 9 | **Build project** | ArcGIS Pro project with styled layers, legend, north arrow, scale bar and credits. |
| 10 | **Export map** | 600 DPI PNG; portrait or landscape chosen from the state's shape. |
| 11 | **Clean up** | Removes downloads and intermediate files, but keeps a cache for fast redraws. |

## 📋 Requirements

- **Windows** with **ArcGIS Pro** installed
  > `arcpy` only ships with ArcGIS Pro and cannot be installed with pip.
- **Spatial Analyst** extension enabled (*Settings → Licensing* in ArcGIS Pro)
- Python packages: `geopandas`, `pygris` (plus `numpy`, `pandas`, `requests`, `shapely` and GDAL, which ship with ArcGIS Pro)
- **Memory:** each state being built can use 4–8 GB of RAM; see `PARALLEL_STATES`.
- **Plenty of disk space.** DEM tiles for a large state take several GB while it builds. NHD downloads range from hundreds of MB to several GB.

## 🔧 Installation

ArcGIS Pro's default Python environment is read-only, so clone it first. Open **Python Command Prompt** from the ArcGIS folder in the Start menu:

```bat
conda create --clone arcgispro-py3 --name dem-env
activate dem-env
conda install geopandas
python -m pip install pygris
```

> [!TIP]
> Install geopandas through **conda**, not pip, so it uses ArcGIS Pro's GDAL instead of bringing in a conflicting copy.

## 🛠️ Configuration

All settings are near the top of `elevation.py`.

### Run settings

| Setting | Default | Purpose |
|---|---|---|
| `STATES` | All 48 states | States to build, e.g. `["New Jersey", "Delaware"]`. Spell names out in full. |
| `SKIP_EXISTING` | `True` | Skip states whose PNG already exists, so a stopped run picks up where it left off |
| `PARALLEL_STATES` | `3` | States built at the same time. Use 2 with 16 GB of RAM, or 1 to watch progress in the window. |
| `REUSE_DATA` | `True` | Redraws reuse each state's downloaded DEM, NHD and GNIS data |
| `DOWNLOAD_THREADS` | `8` | DEM tiles downloaded at once |
| `LOG_FILE` | `C:/DEM/run_log.txt` | One line per state: done, failed (with the error) and minutes taken |

### Map settings

| Setting | Default | Purpose |
|---|---|---|
| `CRS_WKID` | `5070` | Output projection (NAD83 / Conus Albers) |
| `DEM_RES_M` | `30` | DEM cell size in meters |
| `MAX_DEM_CELLS` | `250,000,000` | Larger states get a coarser cell size |
| `STREAM_THRESHOLD_DIVISOR` | `450` | Raise for more rivers, lower for fewer |
| `STATE_STREAM_THRESHOLD_KM` | `{}` | Per-state override, e.g. `{"Delaware": 5}` |
| `RIVER_LABEL_COUNT` | `8` | Named rivers labeled per state |
| `LAKE_LABEL_COUNT` | `6` | Named lakes and reservoirs labeled per state |
| `MAX_CITIES` | `5` | Cities labeled besides the capital |
| `MAX_PEAKS` | `6` | Mountain peaks labeled besides the high point |
| `HIGH_POINT_GAP_MI` | `10` | No peak labels this close to the high point |
| `HILLSHADE_ALTITUDE` | `35` | Sun height; lower gives longer shadows |
| `LABEL_PT` / `LABEL_HALO_PT` | `6` / `0.3` | Label size and white halo width (labels are black) |
| `EXPORT_DPI` | `600` | PNG resolution; 300 exports about 4× faster |

`STATE_INFO` holds each state's capital, high point name and published high point elevation (ft). Edit it to correct a value.

## ▶️ Usage

From ArcGIS Pro's Python Command Prompt, with your environment activated:

```bat
cd C:\DEM
python elevation.py
```

Or call ArcGIS Pro's Python directly from any prompt:

```bat
"C:\Program Files\ArcGIS\Pro\bin\Python\scripts\propy.bat" elevation.py
```

When several states build at once, the window shows one line as each state finishes. Each state's full printout goes to `C:\DEM\<State>\build_log.txt`. A failed state doesn't stop the run; rerun the script to retry it, and finished states are skipped.

**To redraw maps** after changing a style setting, delete the PNGs you want redrawn and run again. With `REUSE_DATA` on, the downloads are skipped. If you change the DEM resolution or the NHD settings, or want fresh data, also delete that state's `cache\` folder.

**To collect all maps in one folder**, run `collect_maps.py` afterwards. It copies every state's PNG into `C:\DEM\All Maps`, copying only new or redrawn maps:

```bat
python C:\DEM\collect_maps.py
```

## 📂 Output

Each state gets its own folder under `C:\DEM\`:

```text
C:\DEM\<State>\
├── MyNewAutomatedProject.aprx   # ArcGIS Pro project with styled map and layout
├── <State>_elevation.png        # 600 DPI map image
├── elevation_ft.tif             # Elevation in feet (EPSG:5070)
├── hillshade.tif                # Hillshade raster
├── rivers.shp                   # NHD rivers and streams with size class
├── river_names.shp              # Longest named rivers (label layer)
├── lakes.shp                    # NHD lakes and reservoirs clipped to the state
├── counties.shp                 # County boundaries
├── cities.shp                   # Capital and largest cities
├── peaks.shp                    # Named mountain peaks with elevation
├── highpoint.shp                # State high point
├── cache\                       # Downloaded data reused by redraws
└── build_log.txt                # Printout when states run in parallel
```

`C:\DEM\run_log.txt` records every state's result across runs.

## 🗺️ Data Sources

| Data | Source |
|---|---|
| **Elevation** | USGS 3D Elevation Program (3DEP) 1 arc-second (~30 m) tiles from the [USGS National Map](https://www.usgs.gov/3d-elevation-program) |
| **Boundaries** | U.S. Census Bureau cartographic boundary files, via [pygris](https://github.com/walkerke/pygris) |
| **Rivers & lakes** | USGS [National Hydrography Dataset](https://www.usgs.gov/national-hydrography/national-hydrography-dataset) state shapefiles |
| **Cities** | Census Bureau 2024 city and town population estimates; place locations via pygris |
| **Peak names** | USGS [Geographic Names Information System](https://www.usgs.gov/tools/geographic-names-information-system-gnis) (GNIS) |
| **Peak elevations** | [Wikidata](https://www.wikidata.org/), matched to GNIS by feature ID |
| **High point elevations** | [Highpointers Foundation](https://archive.highpointersfoundation.org/US_State_Summits.shtml) state summit list |

> [!NOTE]
> The NHD was retired in October 2023 and is no longer updated. Current hydrography is maintained through the 3D Hydrography Program (3DHP).

## ⚠️ Known Limitations

- **Lower 48 only.** Alaska and Hawaii fall outside the Conus Albers projection and aren't in `STATE_INFO`. They would need their own projections (EPSG:3338 and ESRI:102007).
- **DEM heights run low on summits.** The 30 m grid averages the ground around each summit. That's why peak and high point labels use published elevations, and why the legend's top value can be lower than the high point label.
- **Peaks need a published elevation.** Summits without a Wikidata elevation are left off, so some small or flat states show fewer than `MAX_PEAKS`. If Wikidata can't be reached, the script falls back to DEM heights and says so in the log.
- **A few high point values are older surveys** (e.g. Mount Rainier at 14,410 ft); edit `STATE_INFO` to update them.
- **Cached data isn't refreshed automatically.** Delete a state's `cache\` folder after changing data settings.
- **Memory.** Too many parallel states can run out of RAM; lower `PARALLEL_STATES` if states fail with memory errors.
- **Run time** ranges from minutes for small states to an hour or more for large ones, mostly spent downloading.