# ZonalClimateAnalyzer
*Analyze the climate history for any area, line or point inside Germany with a vector upload (shapefile .zip with CRS, GeoPackage, or GeoJSON).*

---

## 1 | Project Purpose
This script automates an end‑to‑end workflow to

1. **Download** annual gridded climate rasters (1951 → latest) from the **DWD Climate Data Center (CDC)**.
2. **Clip & analyse** those rasters for a user‑supplied area (polygon shapefile).
3. **Summarise** the results as zonal statistics (min / mean / max) in a tidy JSON file.
4. **Visualise** long‑term trends with ready‑made PNG plots. Also creates an interactive map of the analyzed area.

The goal is to give municipalities, researchers and students a quick way to quantify and visualise local climate change indicators without manual GIS work.

---

## 2 | Key Features
| Stage | What happens | Where in code |
|-------|--------------|---------------|
| **Input** | Interactive prompt asks for your *.shp* path | `get_shp()` |
| **Data download** | All relevant **.asc.gz / .zip** rasters fetched from the DWD open‑data mirror | `download_dwd_data()` |
| **Pre‑processing** | Decompress → rename → add CRS → re‑encode to GeoTIFF | `decompress_file()`, `asc_to_tif_add_crs()` |
| **Clean‑up** | Source archives removed to keep disk footprint small | `delete_raster_files()` |
| **Analysis** | Per‑polygon zonal stats with [`rasterstats`](https://pythonhosted.org/rasterstats/) | `calculate_zonal_stats()` |
| **Output** | - `area_rasterstats.json` containing the calculated data, <br> - 9 plots showing 17 different climate parameter long term trends, <br> - map of the analyzed area| visualiser section |


---

## 3 | Quick‑start
Linux:

```bash
# 1. Clone & enter
git clone https://github.com/LevinGiersch/ZonalClimateAnalyzer
cd ZonalClimateAnalyzer

# 2. Create & activate venv (recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install Python ≥3.9 deps
pip install -r requirements.txt

# 4. Run the script
python ZonalClimateAnalyzer.py /path/to/my_area.shp

# 5. (Optional) Interactive prompt:
# python ZonalClimateAnalyzer.py

# 6. Wait until the process is finished
```

Windows:

```cmd
# 1. Clone & enter
git clone https://github.com/LevinGiersch/ZonalClimateAnalyzer
cd ZonalClimateAnalyzer

# 2. Install Python ≥3.9 deps (recommended: venv/conda)
pip install -r requirements.txt

# 3. Run the script
python ZonalClimateAnalyzer.py C:\path\to\my_area.shp

# 4. (Optional) Interactive prompt:
# python ZonalClimateAnalyzer.py

# 5. Wait until the process is finished
```


- Runtime hint: the first execution downloads ≈ 1 GB of rasters and can take 10–20 min (depending on your connection).
- Use `--skip-download` (or set `ZCA_SKIP_DWD_DOWNLOAD=1`) if rasters are already available locally.

---

## 4 | Data Source & Coordinate System
| Aspect | Details |
|--------|---------|
| **Provider** | Deutscher Wetterdienst (DWD) – Climate Data Center |
| **URL root** | <https://opendata.dwd.de/climate_environment/CDC/grids_germany/annual/> |
| **Parameters pulled** | air_temperature\_\*, frost_days, hot_days, ice_days, drought_index, precipitation, snowcover_days, precipGE{10,20,30}mm\_days, sunshine_duration, vegetation\_{begin,end} |
| **Spatial grid** | 1 × 1 km **GK3 / DHDN Zone 3** (EPSG 31467) |
| **Temporal coverage** | 1951 – present (updated yearly) |

- A small `gk3.prj` file ships with the repo; shapefiles are re‑projected into this CRS so raster overlays line up exactly.
- Detailled informations on the data (as.pdf) can be found inside the 'ZonalClimateAnalyzer/climate_environment_CDC_grids_germany_annual' folder after executing the script.

---

## 5 | Outputs Explained

| File | What it shows |
|------|---------------|
| `map.html` | Interactive map of the analyzed area. Shows filename, area and perimeter on hover. |
| `min_mean_max_temp_plot.png` | Annual **maximum, mean and minimum air temperature**. Filled bands visualise the spread between the three series. |
| `ice_frost_days_plot.png` | Counts of **frost days** (T<sub>min</sub> < 0 °C) and **ice days** (T<sub>max</sub> < 0 °C). |
| `snowcover_days_plot.png` | **Days per year with snow depth > 1 cm**. |
| `summer_hot_days_plot.png` | Counts of **summer days** (T<sub>max</sub> ≥ 25 °C) and **hot days** (T<sub>max</sub> ≥ 30 °C). |
| `precipitation_drought_plot.png` | **Annual precipitation totals** (bars) with **drought index** overlay (line, scaled). |
| `precip_days_plot.png` | **Number of days with heavy precipitation** ≥ 10 mm, ≥ 20 mm, ≥ 30 mm. |
| `sunshine_duration_plot.png` | **Average daily sunshine hours** for each year. |
| `vegetativ_phase_plot.png` | **Start and end dates of the vegetative season** plus reference lines for astronomical seasons. |
| `vegetativ_phase_len_plot.png` | **Length of the vegetative season** (days between start and end) for each year. |

---

## 6 | Known Limitations
- The CLI script expects a **shapefile** path. The web upload supports shapefile `.zip` bundles (with `.prj`), `.gpkg`, and `.geojson`.
- Re‑downloads rasters each year; archive copies yourself for full reproducibility.

---

## 7 | Citation
Always credit the **DWD Climate Data Center** when publishing derived work.

> Deutscher Wetterdienst (2025): *Grids Germany – Annual*.
https://opendata.dwd.de/climate_environment/CDC/grids_germany/annual/
---

## 8 | Web App (React + API)

This repo now ships a minimal React UI and a small FastAPI server to upload a shapefile and return the generated plots.

### Start the API

```bash
pip install -r requirements.txt
python -m uvicorn api.server:app --reload --port 8000
```

The API expects `clamscan` to be available on the server for malware scanning.

Optional API configuration (env vars):
- `ZCA_ALLOWED_ORIGINS` (comma-separated list)
- `ZCA_MAX_UPLOAD_MB` (default 200)
- `ZCA_MAX_ZIP_FILES` (default 2000)
- `ZCA_MAX_ZIP_UNCOMPRESSED_MB` (default 1600)
- `ZCA_MAX_FEATURES` (default 2000)
- `ZCA_MAX_VERTICES` (default 200000)
- `ZCA_RUN_RETENTION_HOURS` (default 48)
- `ZCA_MIN_FREE_DISK_GB` (default 2)
- `ZCA_RATE_LIMIT_PER_MIN` (default 120, set 0 to disable)
- `ZCA_LOCK_TTL_SECONDS` (default 14400)

### Start the React app

```bash
cd web
npm install
npm run dev
```

Then open `http://localhost:5173` and upload one of:
- `.zip` with full shapefile set (`.shp`, `.shx`, `.dbf`, `.prj`) — `.prj` is required
- `.gpkg` (GeoPackage)
- `.geojson` (GeoJSON; assumed EPSG:4326 if CRS is missing)

You can also draw a polygon directly on the map in the UI and run the same analysis without uploading files. The valid area is limited to the raster grid coverage.
