<div align="center">

# 🏔️ State Elevation & Hydrology Mapper

**Automated ArcGIS Pro pipeline that turns raw elevation data into styled elevation, hillshade, stream, and lake maps for every U.S. state.**

![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![ArcGIS Pro](https://img.shields.io/badge/ArcGIS%20Pro-arcpy-2C7AC3?logo=arcgis&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)

<img src="maps/New_Jersey_elevation.png" alt="New Jersey elevation map" width="600">

</div>

---

## 📖 Overview

For each state, the script downloads a 30 m DEM, clips it to the state boundary, reprojects it to an equal-area projection, runs a full D8 hydrology workflow, overlays NHD waterbodies, and saves a styled ArcGIS Pro project (`.aprx`) plus a 300 DPI PNG map.

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
| 1 | **Download DEM** | USGS 3DEP 30 m from OpenTopography. States over the 225,000 km² request cap are tiled and mosaicked. |
| 2 | **Get boundaries** | 2023 Census county boundaries via `pygris`, used as the clipping boundary. |
| 3 | **Get lakes** | NHD state shapefile; keeps waterbodies ≥ 0.3 km², repairs invalid geometry, clips to the state. |
| 4 | **Clip DEM** | Clips elevation to the state boundary. |
| 5 | **Reproject** | NAD83 / Conus Albers (EPSG:5070) at 30 m, bilinear resampling. |
| 6 | **Convert units** | Elevation from meters to feet. |
| 7 | **Hillshade** | Azimuth 315°, altitude 45°. |
| 8 | **Hydrology** | Fill → Flow Direction (D8) → Flow Accumulation → Stream Definition → Stream to Feature. |
| 9 | **Build project** | ArcGIS Pro project with styled elevation, hillshade, stream, and lake layers. |
| 10 | **Export map** | 300 DPI PNG; portrait or landscape chosen from the state's shape. |
| 11 | **Clean up** | Removes intermediate rasters and downloads. |

## 📋 Requirements

- **Windows** with **ArcGIS Pro** installed
  > `arcpy` only ships with ArcGIS Pro and cannot be installed with pip.
- **Spatial Analyst** extension enabled (*Settings → Licensing* in ArcGIS Pro)
- A free **OpenTopography API key** from [opentopography.org](https://opentopography.org/)
- Python packages: `geopandas`, `pandas`, `pygris`, `requests`
- **Plenty of disk space.** Large states produce multi-GB intermediate rasters, and NHD downloads range from hundreds of MB to several GB.

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

### API key

Copy `config.example.json` to `config.json` and add your key:

```json
{
    "opentopography_api_key": "YOUR_API_KEY_HERE"
}
```

> [!IMPORTANT]
> `config.json` is excluded by `.gitignore`. Never commit your real API key.

### Script settings

These are set near the top of `elevation.py`:

| Setting | Default | Purpose |
|---|---|---|
| `PROJECTED_CRS_EPSG` | `5070` | Output projection (NAD83 / Conus Albers) |
| `PROJECTED_CELL_SIZE` | `30` | Output cell size in meters |
| `STATE_BOUNDING_BOXES` | All 50 states | States to process: `(min_lat, min_lon, max_lat, max_lon, FIPS)` |
| `REQUEST_TIMEOUT` | `600` | HTTP timeout in seconds |
| `MAX_DEM_AREA_KM2` | `200000` | Tile size limit for DEM downloads |
| `min_area_km2` | `0.3` | Smallest lake kept (in `download_state_waterbodies`) |
| `threshold` | `50000` | Cells needed to form a stream (≈ 17.4 sq mi drainage area at 30 m) |

To process only some states, trim `STATE_BOUNDING_BOXES` to the ones you want. Spell state names out in full (for example, `"New Hampshire"`), since they're used to build the NHD download URL.

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

## 📂 Output

Each state gets its own folder under `C:\DEM\`:

```text
C:\DEM\<State>\
├── MyNewAutomatedProject.aprx   # ArcGIS Pro project with styled map and layout
├── <State>_elevation.png        # 300 DPI map image
├── stateclipmeter\              # Elevation raster in feet (EPSG:5070)
├── hillshade\                   # Hillshade raster
├── hydro\                       # Derived stream network (shapefile)
└── lakesclip\                   # NHD lakes clipped to the state (shapefile)
```

> [!NOTE]
> Despite its folder name, `stateclipmeter\state_clipped_meters.tif` holds elevation in **feet**.

Exported map images for all states are collected in the [`maps/`](maps/) folder.

## 🗺️ Data Sources

| Data | Source |
|---|---|
| **Elevation** | USGS 3D Elevation Program (3DEP) 1 arc-second (~30 m), via [OpenTopography](https://opentopography.org/) |
| **Boundaries** | U.S. Census Bureau cartographic boundary files, via [pygris](https://github.com/walkerke/pygris) |
| **Lakes** | USGS [National Hydrography Dataset](https://www.usgs.gov/national-hydrography/national-hydrography-dataset) state shapefiles |

> [!NOTE]
> The NHD was retired in October 2023 and is no longer updated. Current hydrography is maintained through the 3D Hydrography Program (3DHP).

## ⚠️ Known Limitations

- **Alaska and Hawaii** fall outside the Conus Albers projection and will be distorted. Use Alaska Albers (EPSG:3338) and Hawaii Albers (ESRI:102007) instead. Alaska at 30 m is also very large (about 2 billion cells), so hydrology steps can take many hours or run out of memory.
- **Stream threshold** is a fixed drainage area for every state. This keeps networks comparable, but large states produce dense maps. Scale the threshold with state area, or symbolize by stream order, for cleaner maps.
- **Derived streams** come from the DEM alone, so they can cut straight across flat areas, lakes, and marshes where real channels differ.
- **Run time** ranges from minutes for small states to hours for large ones.
