# &#x20;State Elevation \& Hydrology Mapper

# 

# Automated ArcGIS Pro pipeline that builds an elevation map, hillshade, derived stream network, and lakes layer for every U.S. state, then packages each state into its own ArcGIS Pro project and exports a print-ready map image.

# 

# For each state, the script downloads a 30 m DEM, clips it to the state boundary, reprojects it to an equal-area projection, runs a full D8 hydrology workflow, overlays NHD waterbodies, and saves a styled `.aprx` project plus a 300 DPI PNG.

# 

# \## Pipeline

# 

# 1\. \*\*Download the DEM\*\* from OpenTopography (USGS 3DEP 30 m). States larger than the API's 225,000 km² request cap are split into tiles and mosaicked back together.

# 2\. \*\*Download county boundaries\*\* for the state from the U.S. Census Bureau (2023 cartographic boundaries, via `pygris`) and use them as the clipping boundary.

# 3\. \*\*Download lakes\*\* from the USGS National Hydrography Dataset (NHD) state shapefile, keep waterbodies of at least 0.3 km², repair invalid geometry, and clip to the state.

# 4\. \*\*Clip the DEM\*\* to the state boundary.

# 5\. \*\*Reproject\*\* to NAD83 / Conus Albers (EPSG:5070) at 30 m with bilinear resampling, so cell area is constant and hydrology results are comparable across states.

# 6\. \*\*Convert elevation\*\* from meters to feet.

# 7\. \*\*Generate a hillshade\*\* (azimuth 315°, altitude 45°).

# 8\. \*\*Run hydrology:\*\* Fill → Flow Direction (D8) → Flow Accumulation → Stream definition → Stream to Feature.

# 9\. \*\*Build an ArcGIS Pro project\*\* with the elevation, hillshade, stream, and lake layers styled.

# 10\. \*\*Export a map image\*\* (PNG, 300 DPI, portrait or landscape chosen automatically from the state's shape).

# 11\. \*\*Clean up\*\* intermediate rasters and downloads.

# 

# \## Requirements

# 

# \- \*\*Windows\*\* with \*\*ArcGIS Pro\*\* installed (`arcpy` is only available through ArcGIS Pro and cannot be installed with pip).

# \- \*\*Spatial Analyst\*\* extension enabled (Settings → Licensing in ArcGIS Pro). The hillshade step uses `arcpy.ddd`, which runs under either Spatial Analyst or 3D Analyst.

# \- An \*\*OpenTopography API key\*\* (free at \[opentopography.org](https://opentopography.org/)).

# \- Python packages: `geopandas`, `pandas`, `pygris`, `requests`.

# \- Plenty of disk space. Large states produce multi-GB intermediate rasters, and NHD downloads range from hundreds of MB to several GB.

# 

# \## Installation

# 

# ArcGIS Pro's default Python environment is read-only, so clone it first. Open \*\*Python Command Prompt\*\* from the ArcGIS folder in the Start menu:

# 

# ```

# conda create --clone arcgispro-py3 --name dem-env

# activate dem-env

# conda install geopandas

# python -m pip install pygris

# ```

# 

# Install geopandas through conda rather than pip, so it uses ArcGIS Pro's GDAL instead of bringing in a conflicting copy.

# 

# \## Configuration

# 

# Open `elevation.py` and set your OpenTopography API key:

# 

# ```python

# "API\_Key": "YOUR\_API\_KEY\_HERE"

# ```

# 

# Other settings near the top of the script:

# 

# | Setting | Default | Purpose |

# |---|---|---|

# | `PROJECTED\_CRS\_EPSG` | `5070` | Output projection (NAD83 / Conus Albers) |

# | `PROJECTED\_CELL\_SIZE` | `30` | Output cell size in meters |

# | `STATE\_BOUNDING\_BOXES` | All 50 states | States to process, as `(min\_lat, min\_lon, max\_lat, max\_lon, FIPS)` |

# | `REQUEST\_TIMEOUT` | `600` | HTTP timeout in seconds |

# | `MAX\_DEM\_AREA\_KM2` | `200000` | Tile size limit for DEM downloads |

# | `min\_area\_km2` | `0.3` | Smallest lake kept (in `download\_state\_waterbodies`) |

# | `threshold` | `50000` | Flow accumulation cells needed to form a stream (≈ 17.4 sq mi drainage area at 30 m) |

# 

# To process only some states, trim `STATE\_BOUNDING\_BOXES` down to the ones you want. State names must be spelled out in full (for example, `"New Hampshire"`), because they are used to build the NHD download URL.

# 

# \## Usage

# 

# From ArcGIS Pro's Python Command Prompt, with your environment activated:

# 

# ```

# cd C:\\DEM

# python elevation.py

# ```

# 

# Or call ArcGIS Pro's Python directly from any prompt:

# 

# ```

# "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" elevation.py

# ```

# 

# \## Output

# 

# Each state gets its own folder under `C:\\DEM\\`:

# 

# ```

# C:\\DEM\\<State>\\

# ├── MyNewAutomatedProject.aprx     ArcGIS Pro project with the styled map and layout

# ├── <State>\_elevation.png          300 DPI map image

# ├── stateclipmeter\\                Elevation raster in feet (EPSG:5070)

# ├── hillshade\\                     Hillshade raster

# ├── hydro\\                         Derived stream network (shapefile)

# └── lakesclip\\                     NHD lakes clipped to the state (shapefile)

# ```

# 

# Note: despite its folder name, `stateclipmeter\\state\_clipped\_meters.tif` holds elevation in \*\*feet\*\*.

# 

# \## Data Sources

# 

# \- \*\*Elevation:\*\* USGS 3D Elevation Program (3DEP) 1 arc-second (\~30 m), served by \[OpenTopography](https://opentopography.org/).

# \- \*\*Boundaries:\*\* U.S. Census Bureau cartographic boundary files, via \[pygris](https://github.com/walkerke/pygris).

# \- \*\*Lakes:\*\* USGS \[National Hydrography Dataset](https://www.usgs.gov/national-hydrography/national-hydrography-dataset) state shapefiles. The NHD was retired in October 2023 and is no longer updated; current hydrography is maintained through the 3D Hydrography Program (3DHP).

# 

# \## Known Limitations

# 

# \- \*\*Alaska and Hawaii\*\* fall outside the Conus Albers projection and will be distorted. Use Alaska Albers (EPSG:3338) and Hawaii Albers (ESRI:102007) for those states. Alaska at 30 m is also very large (about 2 billion cells), so hydrology steps can take many hours or run out of memory.

# \- \*\*Stream threshold\*\* is a fixed drainage area for every state. This keeps networks comparable, but large states produce dense maps. Scale the threshold with state area, or symbolize by stream order, for cleaner maps.

# \- \*\*Derived streams\*\* come from the DEM alone, so they can cut straight across flat areas, lakes, and marshes where real channels differ.

# \- \*\*Run time\*\* ranges from minutes for small states to hours for large ones.

