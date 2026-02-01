from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import geopandas as gpd
import rasterio
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
import fiona

LOGGER = logging.getLogger("zca.api")

BASE_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE_DIR / "output"
RUNS_DIR = OUTPUT_DIR / "web_runs"
RASTER_DIR = BASE_DIR / "climate_environment_CDC_grids_germany_annual"
GERMANY_BOUNDARY_PATH = BASE_DIR / "germany_boundary" / "german_boundary.shp"

ALLOWED_EXT = {".zip", ".shp", ".gpkg", ".geojson"}
ALLOWED_ARCHIVE_EXT = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
MAX_UPLOAD_MB = int(os.environ.get("ZCA_MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_ZIP_FILES = int(os.environ.get("ZCA_MAX_ZIP_FILES", "2000"))
MAX_ZIP_UNCOMPRESSED_BYTES = int(os.environ.get("ZCA_MAX_ZIP_UNCOMPRESSED_MB", "1600")) * 1024 * 1024
MAX_FEATURES = int(os.environ.get("ZCA_MAX_FEATURES", "2000"))
MAX_VERTICES = int(os.environ.get("ZCA_MAX_VERTICES", "200000"))
RUN_RETENTION_HOURS = int(os.environ.get("ZCA_RUN_RETENTION_HOURS", "48"))
MIN_FREE_DISK_GB = int(os.environ.get("ZCA_MIN_FREE_DISK_GB", "2"))
RATE_LIMIT_PER_MIN = int(os.environ.get("ZCA_RATE_LIMIT_PER_MIN", "120"))
REQUIRE_CLAMSCAN = os.environ.get("ZCA_REQUIRE_CLAMSCAN", "0") == "1"

LOCK_PATH = OUTPUT_DIR / ".analysis.lock"
LOCK_TTL_SECONDS = int(os.environ.get("ZCA_LOCK_TTL_SECONDS", str(60 * 60 * 4)))
DATA_COVERAGE_PATH = OUTPUT_DIR / "data_coverage.geojson"

DATA_COVERAGE_GEOJSON = None
DATA_COVERAGE_GEOM = None

app = FastAPI(title="Zonal Climate Analyzer API")

def _allowed_origins() -> list[str]:
    raw = os.environ.get("ZCA_ALLOWED_ORIGINS")
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return ["http://localhost:5173", "http://127.0.0.1:5173"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=RUNS_DIR), name="runs")

class GeoJSONPayload(BaseModel):
    geojson: dict
    lang: str | None = None


def _normalize_lang(lang: str | None) -> str:
    if not lang:
        return "de"
    return "en" if lang.lower().startswith("en") else "de"


def _done_message(lang: str | None) -> str:
    return "Analysis completed." if _normalize_lang(lang) == "en" else "Analyse abgeschlossen."


def _client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_RATE_LIMIT = {}


@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    if RATE_LIMIT_PER_MIN <= 0:
        return await call_next(request)
    now = int(time.time())
    key = (now // 60, _client_ip(request))
    count = _RATE_LIMIT.get(key, 0) + 1
    _RATE_LIMIT[key] = count
    if count > RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    return await call_next(request)


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


def _read_lock() -> dict | None:
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _pid_is_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _cleanup_lock() -> None:
    if not LOCK_PATH.exists():
        return
    data = _read_lock()
    if not data:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
        return
    pid = data.get("pid")
    ts = data.get("ts")
    if isinstance(pid, int) and _pid_is_alive(pid):
        return
    if isinstance(ts, (int, float)) and time.time() - ts < LOCK_TTL_SECONDS:
        return
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


@app.on_event("startup")
def _cleanup_lock_on_startup() -> None:
    _cleanup_lock()


def _cleanup_old_runs() -> None:
    if RUN_RETENTION_HOURS <= 0 or not RUNS_DIR.exists():
        return
    cutoff = time.time() - RUN_RETENTION_HOURS * 3600
    for run_dir in RUNS_DIR.iterdir():
        try:
            if not run_dir.is_dir():
                continue
            if run_dir.stat().st_mtime < cutoff:
                shutil.rmtree(run_dir, ignore_errors=True)
        except OSError:
            continue


@app.on_event("startup")
def _cleanup_runs_on_startup() -> None:
    _cleanup_old_runs()


def _load_data_coverage() -> None:
    global DATA_COVERAGE_GEOJSON, DATA_COVERAGE_GEOM
    if DATA_COVERAGE_GEOJSON is not None:
        return

    if DATA_COVERAGE_PATH.exists():
        try:
            stored = json.loads(DATA_COVERAGE_PATH.read_text(encoding="utf-8"))
            DATA_COVERAGE_GEOJSON = stored
            DATA_COVERAGE_GEOM = shape(stored)
            return
        except Exception:
            try:
                DATA_COVERAGE_PATH.unlink()
            except OSError:
                pass

    if GERMANY_BOUNDARY_PATH.exists():
        gdf = gpd.read_file(GERMANY_BOUNDARY_PATH).to_crs("EPSG:4326")
        geom = gdf.unary_union.buffer(0)
        DATA_COVERAGE_GEOM = geom
        DATA_COVERAGE_GEOJSON = mapping(geom)
        DATA_COVERAGE_PATH.write_text(json.dumps(DATA_COVERAGE_GEOJSON), encoding="utf-8")
        return

    if not RASTER_DIR.exists():
        raise RuntimeError("Raster data directory not found.")

    tif_files = list(RASTER_DIR.rglob("*.tif"))
    if not tif_files:
        raise RuntimeError("No .tif files found for coverage.")

    boxes = []
    crs = None
    for tif in tif_files:
        with rasterio.open(tif) as src:
            crs = src.crs
            bounds = src.bounds
            boxes.append(box(bounds.left, bounds.bottom, bounds.right, bounds.top))

    coverage = unary_union(boxes).buffer(0)
    gdf = gpd.GeoDataFrame(geometry=[coverage], crs=crs)
    gdf = gdf.to_crs("EPSG:4326")

    geom = gdf.geometry.iloc[0]
    DATA_COVERAGE_GEOM = geom
    DATA_COVERAGE_GEOJSON = mapping(geom)
    DATA_COVERAGE_PATH.write_text(json.dumps(DATA_COVERAGE_GEOJSON), encoding="utf-8")


def _ensure_within_coverage(gdf: gpd.GeoDataFrame) -> None:
    _load_data_coverage()
    if gdf.crs is None:
        raise HTTPException(status_code=400, detail="CRS is missing. Please include a .prj file.")
    gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.notnull()]
    if gdf.empty:
        raise HTTPException(status_code=400, detail="No valid geometries found.")
    if not gdf.geom_type.str.contains("polygon", case=False, na=False).any():
        raise HTTPException(status_code=400, detail="Only polygon geometries are supported.")
    outside = ~gdf.geometry.apply(lambda geom: DATA_COVERAGE_GEOM.covers(geom))
    if outside.any():
        raise HTTPException(
            status_code=400,
            detail="All polygons must lie within the raster coverage area."
        )


@contextmanager
def _analysis_lock():
    now = time.time()
    if LOCK_PATH.exists():
        _cleanup_lock()
    if LOCK_PATH.exists():
        data = _read_lock()
        ts = data.get("ts") if data else None
        if isinstance(ts, (int, float)) and now - ts < LOCK_TTL_SECONDS:
            raise HTTPException(status_code=429, detail="Analyzer is busy. Try again soon.")
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass

    try:
        LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "ts": now}), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Unable to acquire analysis lock.") from exc

    try:
        yield
    finally:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass


def _safe_extract_zip(zip_ref: zipfile.ZipFile, dest_dir: Path) -> None:
    members = zip_ref.infolist()
    if len(members) > MAX_ZIP_FILES:
        raise HTTPException(status_code=400, detail="Zip file contains too many entries.")
    total_uncompressed = sum(member.file_size for member in members)
    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise HTTPException(status_code=400, detail="Zip file is too large to extract.")
    for member in members:
        target = dest_dir / member.filename
        if not str(target.resolve()).startswith(str(dest_dir.resolve())):
            raise HTTPException(status_code=400, detail="Invalid zip contents.")
        if member.is_dir():
            continue
        ext = Path(member.filename).suffix.lower()
        if ext and ext not in ALLOWED_ARCHIVE_EXT:
            raise HTTPException(status_code=400, detail="Zip contains unsupported file types.")
    zip_ref.extractall(dest_dir)


def _run_clamscan(path: Path) -> None:
    clamscan = shutil.which("clamscan")
    if not clamscan:
        if REQUIRE_CLAMSCAN:
            raise HTTPException(
                status_code=500,
                detail="clamscan is not installed on the server."
            )
        LOGGER.warning("clamscan not found; skipping malware scan.")
        return
    result = subprocess.run(
        [clamscan, "--no-summary", "-r", str(path)],
        text=True,
        capture_output=True,
        timeout=900
    )
    if result.returncode == 1:
        raise HTTPException(status_code=400, detail="Upload failed malware scan.")
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()[-400:]
        raise HTTPException(status_code=500, detail=f"clamscan failed. {message}")


def _find_shapefile(upload_dir: Path) -> Path:
    candidates = sorted(upload_dir.rglob("*.shp"))
    if not candidates:
        raise HTTPException(status_code=400, detail="No .shp file found in upload.")
    return candidates[0]


def _cleanup_run_dir(run_dir: Path) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)


def _write_upload(upload_file: UploadFile, destination: Path) -> int:
    size = 0
    with destination.open("wb") as buffer:
        while True:
            # Stream to disk to avoid buffering large uploads in memory.
            chunk = upload_file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload too large. Max {MAX_UPLOAD_MB}MB.",
                )
            buffer.write(chunk)
    return size


def _ensure_disk_space() -> None:
    usage = shutil.disk_usage(OUTPUT_DIR)
    free_gb = usage.free // (1024 * 1024 * 1024)
    if free_gb < MIN_FREE_DISK_GB:
        raise HTTPException(status_code=507, detail="Server disk space is too low.")


def _count_vertices(geom) -> int:
    if geom is None:
        return 0
    if geom.geom_type == "Polygon":
        rings = [geom.exterior] + list(geom.interiors)
        return sum(len(ring.coords) for ring in rings if ring is not None)
    if geom.geom_type == "MultiPolygon":
        return sum(_count_vertices(g) for g in geom.geoms)
    return 0


def _validate_geo_limits(gdf: gpd.GeoDataFrame) -> None:
    if len(gdf) > MAX_FEATURES:
        raise HTTPException(status_code=400, detail="Too many features in upload.")
    vertex_count = int(gdf.geometry.apply(_count_vertices).sum())
    if vertex_count > MAX_VERTICES:
        raise HTTPException(status_code=400, detail="Geometry is too complex.")

def _validate_shapefile(shp_path: Path) -> None:
    required = {".shx", ".dbf"}
    missing = [ext for ext in required if not shp_path.with_suffix(ext).exists()]
    if missing:
        detail = "Missing shapefile components: " + ", ".join(missing)
        detail += ". Please upload a .zip containing .shp, .shx, .dbf, and .prj."
        raise HTTPException(status_code=400, detail=detail)
    if not shp_path.with_suffix(".prj").exists():
        raise HTTPException(
            status_code=400,
            detail="Missing .prj CRS file. Please include the .prj file in your upload."
        )


def _load_geodataframe(path: Path, layer: str | None = None) -> gpd.GeoDataFrame:
    try:
        return gpd.read_file(path, layer=layer)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Unable to read the vector file.") from exc


def _load_geopackage(path: Path) -> gpd.GeoDataFrame:
    try:
        layers = fiona.listlayers(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid GeoPackage file.") from exc
    if not layers:
        raise HTTPException(status_code=400, detail="GeoPackage has no layers.")
    for layer in layers:
        gdf = _load_geodataframe(path, layer=layer)
        if not gdf.empty:
            return gdf
    raise HTTPException(status_code=400, detail="GeoPackage has no usable features.")


def _collect_outputs(shp_stem: str, run_dir: Path) -> list[dict]:
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    label_map = {
        "lufttemperatur_min_mittel_max": "Lufttemperatur (Min/Mittel/Max)",
        "frost_eistage": "Frost- und Eistage",
        "schneedeckentage": "Schneedeckentage",
        "sommer_heisse_tage": "Sommer- und Heiße Tage",
        "niederschlag_trockenheit": "Niederschlag + Trockenheitsindex",
        "starkniederschlag_tage": "Starkniederschlagstage",
        "sonnenscheindauer": "Sonnenscheindauer",
        "vegetationsperiode": "Vegetationsperiode",
        "vegetationsperiode_dauer": "Dauer der Vegetationsperiode",
        "map": "Interaktive Karte"
    }

    outputs = []
    for file in OUTPUT_DIR.iterdir():
        if not file.is_file():
            continue
        if not file.name.startswith(f"{shp_stem}_"):
            continue
        if file.suffix.lower() not in {".png", ".html"}:
            continue

        dest = results_dir / file.name
        shutil.copy2(file, dest)

        output_type = "map" if file.suffix.lower() == ".html" else "plot"
        stem = file.stem
        if stem.startswith(f"{shp_stem}_"):
            stem = stem[len(shp_stem) + 1:]
        label = label_map.get(stem)
        if not label:
            label = stem.replace("_", " ").strip().title() if stem else file.name
        outputs.append({
            "name": file.name,
            "type": output_type,
            "label": label,
            "url": f"/runs/{run_dir.name}/results/{file.name}"
        })

    if not outputs:
        raise HTTPException(status_code=500, detail="No outputs were generated.")

    outputs.sort(key=lambda item: item["name"])
    return outputs


def _build_run_zip(run_dir: Path) -> Path:
    results_dir = run_dir / "results"
    if not results_dir.exists():
        raise HTTPException(status_code=404, detail="Run results not found.")
    zip_path = run_dir / "outputs.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(results_dir.iterdir()):
            if not file.is_file():
                continue
            zf.write(file, arcname=file.name)
    return zip_path


def _purge_outputs_for_stem(stem: str) -> None:
    for file in OUTPUT_DIR.iterdir():
        if file.is_file() and file.name.startswith(f"{stem}_"):
            try:
                file.unlink()
            except OSError:
                pass


def _local_raster_ready() -> bool:
    if not RASTER_DIR.exists():
        return False
    if not any(RASTER_DIR.iterdir()):
        return False
    for ext in (".tif", ".asc", ".asc.gz"):
        if any(RASTER_DIR.rglob(f"*{ext}")):
            return True
    return False


def _run_analyzer(shp_path: Path, run_dir: Path, lang: str | None = None) -> list[dict]:
    _purge_outputs_for_stem(shp_path.stem)
    with _analysis_lock():
        env = os.environ.copy()
        env["ZCA_LANG"] = _normalize_lang(lang)
        if _local_raster_ready():
            env.setdefault("ZCA_SKIP_DWD_DOWNLOAD", "1")
        process = subprocess.run(
            [sys.executable, str(BASE_DIR / "ZonalClimateAnalyzer.py"), str(shp_path)],
            text=True,
            cwd=str(BASE_DIR),
            capture_output=True,
            env=env,
            timeout=60 * 60
        )

    if process.returncode != 0:
        error_snippet = (process.stderr or process.stdout or "").strip()
        error_snippet = error_snippet[-1200:]
        raise HTTPException(status_code=500, detail=f"Analyzer failed. {error_snippet}")

    return _collect_outputs(shp_path.stem, run_dir)


@app.get("/api/coverage")
def data_coverage():
    _load_data_coverage()
    return {
        "geojson": DATA_COVERAGE_GEOJSON
    }


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...), lang: str = Form("de")):
    _ensure_disk_space()
    _cleanup_old_runs()
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()

    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="Unsupported file type.")

    run_id = uuid4().hex
    run_dir = RUNS_DIR / run_id
    upload_dir = run_dir / "upload"
    upload_dir.mkdir(parents=True, exist_ok=True)

    upload_path = upload_dir / filename
    try:
        _write_upload(file, upload_path)
        _run_clamscan(upload_path)

        if ext == ".zip":
            try:
                with zipfile.ZipFile(upload_path, "r") as zip_ref:
                    _safe_extract_zip(zip_ref, upload_dir)
            except zipfile.BadZipFile as exc:
                raise HTTPException(status_code=400, detail="Invalid zip file.") from exc
            _run_clamscan(upload_dir)

        if ext in {".gpkg", ".geojson"}:
            if ext == ".gpkg":
                gdf = _load_geopackage(upload_path)
            else:
                gdf = _load_geodataframe(upload_path)
            _validate_geo_limits(gdf)
            if gdf.crs is None:
                gdf = gdf.set_crs("EPSG:4326", allow_override=True)
            _ensure_within_coverage(gdf)
            shp_path = upload_dir / "uploaded_vector.shp"
            gdf.to_file(shp_path, driver="ESRI Shapefile", index=False)
        else:
            shp_path = _find_shapefile(upload_dir)
            _validate_shapefile(shp_path)
            gdf = _load_geodataframe(shp_path)
            _validate_geo_limits(gdf)
            _ensure_within_coverage(gdf)

        outputs = _run_analyzer(shp_path, run_dir, lang)
        return {
            "runId": run_id,
            "message": _done_message(lang),
            "outputs": outputs,
            "zipUrl": f"/api/runs/{run_id}/download",
        }
    except HTTPException:
        _cleanup_run_dir(run_dir)
        raise
    except Exception as exc:
        LOGGER.exception("Unexpected error while analyzing upload.")
        _cleanup_run_dir(run_dir)
        raise HTTPException(status_code=500, detail="Unexpected server error.") from exc


@app.post("/api/analyze-geojson")
async def analyze_geojson(payload: GeoJSONPayload):
    _ensure_disk_space()
    _cleanup_old_runs()
    run_id = uuid4().hex
    run_dir = RUNS_DIR / run_id
    upload_dir = run_dir / "upload"
    upload_dir.mkdir(parents=True, exist_ok=True)

    geojson_path = upload_dir / "drawn.geojson"
    try:
        geojson_path.write_text(json.dumps(payload.geojson), encoding="utf-8")
        _run_clamscan(geojson_path)

        try:
            gdf = gpd.read_file(geojson_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid GeoJSON.") from exc

        if gdf.empty:
            raise HTTPException(status_code=400, detail="GeoJSON has no features.")
        if not gdf.geom_type.str.contains("polygon", case=False, na=False).any():
            raise HTTPException(status_code=400, detail="Only polygon geometries are supported.")
        _validate_geo_limits(gdf)

        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326", allow_override=True)

        _ensure_within_coverage(gdf)

        shp_path = upload_dir / "drawn.shp"
        gdf.to_file(shp_path, driver="ESRI Shapefile", index=False)
        outputs = _run_analyzer(shp_path, run_dir, payload.lang)
        return {
            "runId": run_id,
            "message": _done_message(payload.lang),
            "outputs": outputs,
            "zipUrl": f"/api/runs/{run_id}/download",
        }
    except HTTPException:
        _cleanup_run_dir(run_dir)
        raise
    except Exception as exc:
        LOGGER.exception("Unexpected error while analyzing GeoJSON.")
        _cleanup_run_dir(run_dir)
        raise HTTPException(status_code=500, detail="Unexpected server error.") from exc


@app.get("/api/runs/{run_id}/download")
def download_run_outputs(run_id: str):
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found.")
    zip_path = _build_run_zip(run_dir)
    return FileResponse(zip_path, filename=f"{run_id}_outputs.zip", media_type="application/zip")
