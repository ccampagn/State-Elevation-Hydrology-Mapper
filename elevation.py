import io
import zipfile
import math
import time
import requests
import pandas as pd
import geopandas as gpd
from pygris import counties, states
import arcpy
import os
from arcpy.sa import *
import shutil
import json

# Read the API key from config.json next to this script (kept out of Git).
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
try:
    with open(CONFIG_PATH) as f:
        API_KEY = json.load(f)["opentopography_api_key"]
except (FileNotFoundError, KeyError):
    raise SystemExit(f"Missing API key. Create {CONFIG_PATH} with an "
                     f'"opentopography_api_key" entry (see config.example.json).')


# All states share a single projected CRS: NAD83 / Conus Albers (EPSG:5070),
# meters. This is the standard USGS/EPA equal-area projection for CONUS-scale
# terrain and hydrology work -- it keeps cell area (and therefore flow
# accumulation and the stream threshold below) accurate and directly
# comparable across every state, without the per-zone edge distortion you'd
# get from stretching a single UTM or State Plane zone across a multi-state
# extent. No more per-state EPSG/cell-size bookkeeping needed.
PROJECTED_CRS_EPSG = 5070
PROJECTED_CELL_SIZE = 30  # meters, matches source USGS30m DEM resolution

# Format: (min_lat, min_lon, max_lat, max_lon, state_fips)
# Extents approximate the Census TIGER state boundaries (land + coastal water).
STATE_BOUNDING_BOXES = {
    "Alabama":        (30.2233, -88.4732, 35.0080, -84.8891,  1),
    # Alaska's Aleutians cross the 180° meridian. This box stops at -129.98 (SE panhandle)
    # and starts at -179.15, so it omits the few islands in the eastern hemisphere
    # (e.g. Attu, ~172.9°E). Handle those separately if you need them.
    "Alaska":         (51.2142, -179.1489, 71.3652, -129.9795, 2),
    "Arizona":        (31.3322, -114.8165, 37.0043, -109.0452, 4),
    "Arkansas":       (33.0041, -94.6179, 36.4996, -89.6444,  5),
    "California":     (32.5342, -124.4096, 42.0095, -114.1312, 6),
    "Colorado":       (36.9924, -109.0603, 41.0034, -102.0415, 8),
    "Connecticut":    (40.9801, -73.7278, 42.0506, -71.7870,  9),
    "Delaware":       (38.4510, -75.7887, 39.8390, -75.0489, 10),
    "Florida":        (24.5231, -87.6349, 31.0009, -80.0314, 12),
    "Georgia":        (30.3579, -85.6052, 35.0007, -80.8397, 13),
    # Includes the Northwestern Hawaiian Islands. Main islands only:
    # (18.9104, -160.2471, 22.2356, -154.8068)
    "Hawaii":         (18.9104, -178.3347, 28.4021, -154.8068, 15),
    "Idaho":          (41.9881, -117.2430, 49.0011, -111.0436, 16),
    "Illinois":       (36.9703, -91.5131, 42.5085, -87.4948, 17),
    "Indiana":        (37.7717, -88.0978, 41.7606, -84.7846, 18),
    "Iowa":           (40.3755, -96.6397, 43.5012, -90.1401, 19),
    "Kansas":         (36.9930, -102.0517, 40.0032, -94.5884, 20),
    "Kentucky":       (36.4971, -89.5715, 39.1475, -81.9650, 21),
    "Louisiana":      (28.9286, -94.0431, 33.0195, -88.8170, 22),
    "Maine":          (42.9767, -71.0843, 47.4598, -66.9498, 23),
    "Maryland":       (37.8856, -79.4877, 39.7230, -75.0489, 24),
    "Massachusetts":  (41.2371, -73.5081, 42.8868, -69.9284, 25),
    "Michigan":       (41.6961, -90.4181, 48.2388, -82.4135, 26),
    "Minnesota":      (43.4994, -97.2392, 49.3844, -89.4917, 27),
    "Mississippi":    (30.1739, -91.6550, 34.9961, -88.0979, 28),
    "Missouri":       (35.9957, -95.7747, 40.6136, -89.0988, 29),
    "Montana":        (44.3582, -116.0500, 49.0014, -104.0391, 30),
    "Nebraska":       (40.0000, -104.0535, 43.0017, -95.3083, 31),
    "Nevada":         (35.0019, -120.0057, 42.0022, -114.0396, 32),
    "New Hampshire":  (42.6970, -72.5570, 45.3053, -70.6104, 33),
    "New Jersey":     (38.9284, -75.5634, 41.3574, -73.8938, 34),
    "New Mexico":     (31.3323, -109.0502, 37.0002, -103.0020, 35),
    "New York":       (40.4961, -79.7624, 45.0159, -71.8562, 36),
    "North Carolina": (33.8423, -84.3219, 36.5881, -75.4606, 37),
    "North Dakota":   (45.9351, -104.0489, 49.0006, -96.5545, 38),
    "Ohio":           (38.4032, -84.8202, 41.9775, -80.5187, 39),
    "Oklahoma":       (33.6158, -103.0026, 37.0022, -94.4307, 40),
    "Oregon":         (41.9918, -124.5662, 46.2920, -116.4635, 41),
    "Pennsylvania":   (39.7198, -80.5199, 42.2698, -74.6895, 42),
    "Rhode Island":   (41.1460, -71.9081, 42.0190, -71.1201, 44),
    "South Carolina": (32.0346, -83.3539, 35.2154, -78.5420, 45),
    "South Dakota":   (42.4796, -104.0577, 45.9455, -96.4366, 46),
    "Tennessee":      (34.9830, -90.3103, 36.6781, -81.6469, 47),
    "Texas":          (25.8374, -106.6456, 36.5007, -93.5083, 48),
    "Utah":           (36.9980, -114.0530, 42.0016, -109.0411, 49),
    "Vermont":        (42.7269, -73.4380, 45.0166, -71.4653, 50),
    "Virginia":       (36.5408, -83.6754, 39.4660, -75.2422, 51),
    "Washington":     (45.5435, -124.7631, 49.0025, -116.9160, 53),
    "West Virginia":  (37.2015, -82.6447, 40.6388, -77.7195, 54),
    "Wisconsin":      (42.4920, -92.8881, 47.0806, -86.8054, 55),
    "Wyoming":        (40.9947, -111.0569, 45.0059, -104.0522, 56),
}

REQUEST_TIMEOUT = 600  # seconds, applied to all outbound HTTP calls
MAX_DEM_AREA_KM2 = 200000  # safety margin below OpenTopography's 225,000 km2 cap for USGS30m


def compute_bbox_area_km2(south, west, north, east):
    """Rough km^2 area of a lat/lon bounding box, using a flat approximation
    at the box's mid-latitude. This is only precise enough to decide how many
    tiles are needed to stay under the API's area cap -- not for measurement."""
    mid_lat = math.radians((south + north) / 2)
    width_km = (east - west) * 111.320 * math.cos(mid_lat)
    height_km = (north - south) * 110.574
    return abs(width_km * height_km)


def split_bbox_into_tiles(south, west, north, east, max_area_km2=MAX_DEM_AREA_KM2):
    """Split a bounding box into a grid of sub-boxes, each under max_area_km2,
    so a large state (e.g. New York) can be downloaded as several requests
    that stay under OpenTopography's 225,000 km2 per-request cap for
    USGS30m. Returns a list of (south, west, north, east) tuples -- a single-
    item list if the box is already small enough."""
    area = compute_bbox_area_km2(south, west, north, east)
    if area <= max_area_km2:
        return [(south, west, north, east)]

    grid_n = math.ceil(math.sqrt(area / max_area_km2))
    lat_step = (north - south) / grid_n
    lon_step = (east - west) / grid_n

    tiles = []
    for row in range(grid_n):
        for col in range(grid_n):
            tile_south = south + row * lat_step
            tile_north = south + (row + 1) * lat_step
            tile_west = west + col * lon_step
            tile_east = west + (col + 1) * lon_step
            tiles.append((tile_south, tile_west, tile_north, tile_east))
    return tiles


def download_state_waterbodies(state, lakes_dir, min_area_km2=0.3, max_retries=3):
    """Download the state's NHD shapefile zip from USGS's static S3 staged
    products and return its waterbodies >= min_area_km2 as a GeoDataFrame.
    This replaces the NHDPlus_HR REST query, which kept failing with
    502/504 errors and dropped connections. A static file download has no
    pagination to fail partway through. Returns an empty GeoDataFrame if
    the download fails."""
    nhd_name = state.replace(" ", "_")  # e.g. "New Jersey" -> "New_Jersey"
    nhd_url = (f"https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/"
               f"NHD/State/Shape/NHD_H_{nhd_name}_State_Shape.zip")
    nhd_zip = os.path.join(lakes_dir, "nhd_state.zip")
    nhd_dir = os.path.join(lakes_dir, "nhd")

    downloaded = False
    for attempt in range(1, max_retries + 1):
        try:
            print(f"Downloading NHD shapefile for {state} (attempt {attempt}/{max_retries})...")
            with requests.get(nhd_url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                r.raise_for_status()
                with open(nhd_zip, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
            downloaded = True
            break
        except requests.exceptions.RequestException as e:
            print(f"NHD download failed: {e}")
            if attempt < max_retries:
                time.sleep(10 * attempt)

    if not downloaded:
        return gpd.GeoDataFrame()

    # Extract only the waterbody layer. Large states split it into
    # NHDWaterbody.shp, NHDWaterbody2.shp, etc., so grab every part.
    with zipfile.ZipFile(nhd_zip) as z:
        members = [m for m in z.namelist()
                   if os.path.basename(m).lower().startswith("nhdwaterbody")]
        z.extractall(nhd_dir, members=members)

    shp_paths = [os.path.join(nhd_dir, m) for m in members if m.lower().endswith(".shp")]
    parts = [gpd.read_file(p) for p in shp_paths]
    if not parts:
        print(f"No NHDWaterbody layer found in the {state} NHD download.")
        return gpd.GeoDataFrame()

    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
    # Shapefile field names may be lowercase (areasqkm), so match case-insensitively.
    area_col = next((c for c in gdf.columns if c.lower() == "areasqkm"), None)
    if area_col:
        gdf = gdf[gdf[area_col] >= min_area_km2]
    return gdf


for state, coor in STATE_BOUNDING_BOXES.items():
    arcpy.env.extent = None
    arcpy.env.cellSize = None
    arcpy.env.snapRaster = None
    arcpy.env.mask = None
    arcpy.env.overwriteOutput = True
    print(f"State: {state} | Coor: {coor}")

    os.makedirs(f"C:/DEM/{state}", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/stateDEM", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/stateclip", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/stateproj", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/stateclipmeter", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/hillshade", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/fill", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/flowdir", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/flowacc", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/stream", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/lakes", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/lakesclip", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/hydro", exist_ok=True)
    os.makedirs(f"C:/DEM/{state}/counties", exist_ok=True)

    # Define file paths
    in_dem = f"C:/DEM/{state}/stateDEM/dem.tif"
    clipping_shapefile = f"C:/DEM/{state}/counties/state_counties.shp"
    clipped_dem = f"C:/DEM/{state}/stateclip/state_clipped.tif"
    projected_dem = f"C:/DEM/{state}/stateproj/state_projected.tif"
    clipped_dem_meters = f"C:/DEM/{state}/stateclipmeter/state_clipped_meters.tif"
    output_hillshade = f"C:/DEM/{state}/hillshade/hillshade.tif"
    filled_dem = f"C:/DEM/{state}/fill/fill.tif"
    flow_dir_raster = f"C:/DEM/{state}/flowdir/flow_direction.tif"
    out_flow_acc_raster = f"C:/DEM/{state}/flowacc/flow_accumulation.tif"
    out_stream_raster = f"C:/DEM/{state}/stream/stream_network.tif"
    out_feature_class = f"C:/DEM/{state}/hydro/hydro_network.shp"
    lakes_raw = f"C:/DEM/{state}/lakes/lake_raw.shp"
    lakes_clipped = f"C:/DEM/{state}/lakesclip/lake.shp"

    url = 'https://portal.opentopography.org/API/usgsdem'
    dem_tiles_dir = f"C:/DEM/{state}/stateDEM/tiles"
    os.makedirs(dem_tiles_dir, exist_ok=True)

    # OpenTopography caps USGS30m requests at 225,000 km2. Large states
    # (e.g. New York) get split into a grid of smaller requests here and
    # mosaicked back into a single DEM, so the rest of the pipeline below
    # (clip, reproject, hydrology tools) doesn't need to know the download
    # was tiled at all.
    bbox_tiles = split_bbox_into_tiles(coor[0], coor[1], coor[2], coor[3])
    if len(bbox_tiles) > 1:
        print(f"{state} bounding box exceeds the API area limit -- splitting into {len(bbox_tiles)} tiles.")

    tile_paths = []
    download_failed = False
    for i, (t_south, t_west, t_north, t_east) in enumerate(bbox_tiles):
        tile_path = os.path.join(dem_tiles_dir, f"tile_{i}.tif")
        query_params = {
            "datasetName": "USGS30m",
            "south": t_south,
            "north": t_north,
            "west": t_west,
            "east": t_east,
            "outputFormat": "GTiff",
            "API_Key": "API_KEY"
        }
        response = requests.get(url, params=query_params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"Error downloading tile {i} for {state}: {response.status_code}: {response.text}")
            download_failed = True
            break

        with open(tile_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        tile_paths.append(tile_path)
        print(f"Downloaded tile {i + 1}/{len(bbox_tiles)} for {state}")

    if download_failed:
        shutil.rmtree(dem_tiles_dir, ignore_errors=True)
        continue

    if len(tile_paths) == 1:
        # No mosaic needed -- just move the single tile into place.
        shutil.move(tile_paths[0], in_dem)
    else:
        arcpy.management.MosaicToNewRaster(
            input_rasters=tile_paths,
            output_location=os.path.dirname(in_dem),
            raster_dataset_name_with_extension=os.path.basename(in_dem),
            coordinate_system_for_the_raster=arcpy.SpatialReference(4326),  # tiles are downloaded in geographic coords
            pixel_type="32_BIT_FLOAT",
            number_of_bands=1,
            mosaic_method="LAST"
        )
        print(f"Mosaicked {len(tile_paths)} tiles into a single DEM for {state}")

    shutil.rmtree(dem_tiles_dir, ignore_errors=True)
    print(f"DEM successfully downloaded and saved for {state}")

    # Download county boundaries for the state
    state_counties = counties(state=coor[4], cb=True, year=2023)
    state_counties.to_file(clipping_shapefile)

    # Waterbodies >= 1 km2 from the state's static NHD shapefile download
    gdf = download_state_waterbodies(state, f"C:/DEM/{state}/lakes")

    if len(gdf) == 0:
        print(f"No waterbody features retrieved for {state} -- skipping lakes layer for this state.")
        lakes_raw = None
        lakes_clipped = None
    else:
        gdf.to_file(lakes_raw)
        arcpy.management.RepairGeometry(lakes_raw, "DELETE_NULL")
        arcpy.analysis.Clip(
            in_features=lakes_raw,
            clip_features=clipping_shapefile,
            out_feature_class=lakes_clipped
        )

        count = int(arcpy.management.GetCount(lakes_clipped)[0])
        print(f"Saved {len(gdf)} raw waterbody features, {count} after clipping to {state} boundary")

    # Clip the DEM to the county boundary
    rectangle_extent = "#"
    arcpy.management.Clip(
        in_raster=in_dem,
        rectangle=rectangle_extent,
        out_raster=clipped_dem,
        in_template_dataset=clipping_shapefile,
        nodata_value="-9999",
        clipping_geometry="ClippingGeometry",
        maintain_clipping_extent="NO_MAINTAIN_EXTENT"
    )
    print("DEM successfully clipped to shapefile!")

    # Reproject to a projected CRS *before* any hydrology tools run.
    # Geographic coordinates (lat/lon degrees) make cell size vary with
    # latitude, which distorts D8 flow direction/accumulation and makes the
    # stream threshold non-comparable between states. Bilinear resampling
    # avoids terracing artifacts on continuous elevation data.
    out_sr = arcpy.SpatialReference(PROJECTED_CRS_EPSG)
    arcpy.management.ProjectRaster(
        in_raster=clipped_dem,
        out_raster=projected_dem,
        out_coor_system=out_sr,
        resampling_type="BILINEAR",
        cell_size=str(PROJECTED_CELL_SIZE)
    )
    print(f"DEM reprojected to EPSG:{PROJECTED_CRS_EPSG} at {PROJECTED_CELL_SIZE} m cell size")

    print("Performing raster calculation (meters to feet)...")
    input_raster = Raster(projected_dem)
    converted_raster = input_raster * 3.28084
    converted_raster.save(clipped_dem_meters)
    print(f"Converted DEM saved at:\n{clipped_dem_meters}")

    # Hillshade
    azimuth = 315
    altitude = 45
    model_shadows = "NO_SHADOWS"
    z_factor = 1

    arcpy.ddd.HillShade(
        in_raster=clipped_dem_meters,
        out_raster=output_hillshade,
        azimuth=azimuth,
        altitude=altitude,
        model_shadows=model_shadows,
        z_factor=z_factor
    )
    print("Hillshade successfully created!")

    # Fill sinks
    z_limit = None
    out_fill = Fill(clipped_dem_meters, z_limit)
    out_fill.save(filled_dem)
    print("DEM sink filling complete.")

    # Flow direction
    out_flow_direction = FlowDirection(in_surface_raster=filled_dem, flow_direction_type="D8")
    out_flow_direction.save(flow_dir_raster)
    print("Flow direction complete.")

    # Flow accumulation
    in_weight_raster = None
    data_type = "INTEGER"
    out_flow_accumulation = FlowAccumulation(
        in_flow_direction_raster=flow_dir_raster,
        in_weight_raster=in_weight_raster,
        data_type=data_type
    )
    out_flow_accumulation.save(out_flow_acc_raster)
    print("Flow accumulation raster created successfully.")

    # Stream definition -- cell_size is fixed at PROJECTED_CELL_SIZE (30 m)
    # under the shared EPSG:5070 CRS for every state, so this threshold means
    # the same real-world drainage area everywhere it's applied
    # (50000 cells x 30m x 30m ~= 17.4 sq mi).
    threshold = 50000
    out_con = Con(Raster(out_flow_acc_raster) > threshold, 1)
    out_con.save(out_stream_raster)
    print(f"Stream definition complete using a threshold of {threshold} cells.")

    # Stream to feature
    simplify_lines = "SIMPLIFY"
    StreamToFeature(out_stream_raster, flow_dir_raster, out_feature_class, simplify_lines)
    print(f"Vector stream network successfully created at: {out_feature_class}")

    # Build ArcGIS Pro project
    project_folder = f"C:/DEM/{state}"
    project_name = "MyNewAutomatedProject"
    print("Creating a brand new ArcGIS Pro project...")
    aprx = arcpy.mp.CreateArcGISProject(project_folder, project_name)

    print("Creating a new Map tab inside the project...")
    new_map = aprx.createMap(name="Elevation Map", map_type="MAP")

    print("Importing layers...")
    dem_layer = new_map.addDataFromPath(clipped_dem_meters)
    hs_layer = new_map.addDataFromPath(output_hillshade)
    s_layer = new_map.addDataFromPath(out_feature_class)
    l_layer = new_map.addDataFromPath(lakes_clipped) if lakes_clipped else None

    water_blue = {'RGB': [0, 112, 255, 100]}         # Solid bright blue
    no_outline = {'RGB': [0, 0, 0, 0]}               # 100% transparent outline
    dark_blue_outline = {'RGB': [0, 70, 170, 100]}   # Darker blue border

    if dem_layer.supports("SYMBOLOGY"):
        sym = dem_layer.symbology
        if hasattr(sym, "colorizer"):
            if sym.colorizer.type != "RasterStretchColorizer":
                sym.updateRenderer("RasterStretchColorizer")
            sym.colorizer.colorRamp = aprx.listColorRamps("Elevation #1")[0]
            dem_layer.symbology = sym

    if s_layer.isFeatureLayer:
        sym = s_layer.symbology
        if hasattr(sym, 'renderer'):
            sym.renderer.symbol.color = water_blue
            sym.renderer.symbol.outlineColor = dark_blue_outline
            sym.renderer.symbol.size = 0.5
            s_layer.symbology = sym
            print("Stream line network layer styled water blue.")

    if l_layer is not None and l_layer.isFeatureLayer:
        sym = l_layer.symbology
        if hasattr(sym, 'renderer'):
            sym.renderer.symbol.color = water_blue
            sym.renderer.symbol.outlineColor = dark_blue_outline
            sym.renderer.symbol.size = 0.5
            l_layer.symbology = sym
            print("Lakes layer styled water blue.")

    hs_layer.transparency = 50
    # Export the map as an image via a layout.
    # Pick portrait or landscape based on the state's shape.
    ext = arcpy.Describe(clipped_dem_meters).extent
    page_w, page_h = (8.5, 11) if ext.height > ext.width else (11, 8.5)

    layout = aprx.createLayout(page_w, page_h, "INCH", "Elevation Layout")
    margin = 0.25
    map_frame = layout.createMapFrame(
        arcpy.Extent(margin, margin, page_w - margin, page_h - margin),
        new_map,
        "Elevation Map Frame"
    )
    # Zoom the frame to the DEM's full extent
    map_frame.camera.setExtent(map_frame.getLayerExtent(dem_layer, False, True))

    out_png = f"C:/DEM/{state}/{state}_elevation.png"
    layout.exportToPNG(out_png, resolution=300)
    print(f"Map image exported to:\n{out_png}")
    aprx.save()

    # CreateArcGISProject writes <project_folder>/<project_name>.aprx
    # directly -- there is no extra <project_name>/ subfolder.
    final_aprx_path = os.path.join(project_folder, f"{project_name}.aprx")
    print(f"New project created and saved at:\n{final_aprx_path}")

    # Clean up memory / intermediate files
    del aprx
    arcpy.management.Delete(in_dem)
    arcpy.management.Delete(clipped_dem)
    arcpy.management.Delete(projected_dem)
    arcpy.management.Delete(filled_dem)
    arcpy.management.Delete(flow_dir_raster)
    arcpy.management.Delete(out_flow_acc_raster)
    arcpy.management.Delete(out_stream_raster)

    shutil.rmtree(f"C:/DEM/{state}/stateclip/")
    shutil.rmtree(f"C:/DEM/{state}/stateproj/")
    shutil.rmtree(f"C:/DEM/{state}/fill/")
    shutil.rmtree(f"C:/DEM/{state}/flowdir/")
    shutil.rmtree(f"C:/DEM/{state}/flowacc/")
    shutil.rmtree(f"C:/DEM/{state}/stream/")
    shutil.rmtree(f"C:/DEM/{state}/stateDEM/")
    shutil.rmtree(f"C:/DEM/{state}/counties/")
    shutil.rmtree(f"C:/DEM/{state}/lakes/", ignore_errors=True)  # also removes the NHD zip and extracted files
