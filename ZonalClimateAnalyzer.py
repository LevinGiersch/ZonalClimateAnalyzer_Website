#!/usr/bin/env python
from __future__ import annotations

from pathlib import Path
from itertools import chain
from urllib.parse import urlparse
from typing import Iterable
import argparse
import json
import logging
import os
import shutil
import zipfile
import gzip
import filetype
from tqdm import tqdm

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

import geopandas as gpd
import rasterio
from rasterio.crs import CRS
from rasterstats import zonal_stats

import matplotlib
matplotlib.use("Agg")  # Use a non-GUI backend. Prevents QSocketNotifier.
import matplotlib.pyplot as plt

LOGGER = logging.getLogger("zca")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "climate_environment_CDC_grids_germany_annual"
DATA_INFO_DIR = BASE_DIR / "data_info"
OUTPUT_DIR = BASE_DIR / "output"
SHP_DIR = BASE_DIR / "shp"
PRJ_FILE = BASE_DIR / "gk3.prj"

DWD_BASE_URL = "https://opendata.dwd.de/climate_environment/CDC/grids_germany/annual/"
DEFAULT_TIMEOUT = 30
USER_AGENT = "ZonalClimateAnalyzer/1.0"

# Plot styling to match the web UI theme
PLOT_COLORS = {
    "mint": "#64f2c8",
    "coral": "#ff7a5c",
    "ice": "#6bc6ff",
    "deep": "#3b6cff",
    "sun": "#ffc857",
    "amber": "#f4a261",
    "leaf": "#7ad36f",
    "violet": "#9b6bff",
    "slate": "#9aa7b4",
    "ink": "#1d2328",
    "sand": "#f7f3ec",
    "panel": "#fbf8f2",
    "grid": "#d6cdc1"
}

matplotlib.rcParams.update({
    "figure.facecolor": "#f7f3ec",
    "axes.facecolor": "#fbf8f2",
    "savefig.facecolor": "#f7f3ec",
    "axes.edgecolor": "#d6cdc1",
    "axes.labelcolor": "#1d2328",
    "text.color": "#1d2328",
    "xtick.color": "#4d585f",
    "ytick.color": "#4d585f",
    "grid.color": "#d6cdc1",
    "grid.linestyle": "-",
    "grid.linewidth": 0.8,
    "grid.alpha": 0.7,
    "font.size": 11.5,
    "legend.frameon": False,
    "legend.labelcolor": "#1d2328"
})


# In[2]:


# Create subfolders
DATA_DIR.mkdir(exist_ok=True)
DATA_INFO_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
SHP_DIR.mkdir(exist_ok=True)


# # Get Shapefile

# In[3]:


def check_crs(shapefile: str) -> bool:
    '''
    Takes a path to a Shapefile (as String) as Input.
    Reads shapefile.
    Returns True if it has a valid CRS,
    Returns False if it die NOT have a valid CRS.
    '''

    # Load the shapefile
    gdf = gpd.read_file(shapefile)

    # Check if CRS is defined
    valid_crs = gdf.crs
    if valid_crs:
        print("\nCRS is defined:", gdf.crs)
        return True
    else:
        print("\nCRS is NOT defined.")
        return False


# In[4]:


def get_shp() -> Path:
    '''
    Lets the user input a path to the shapefile in the terminal.
    Checks if the path leads to a shapefile.
    Checks if the shapefile has a valid CRS.
    Gives feedback depending on the user input.
    Returns path to the shapefile if Valid shp and CRS are found.
    '''

    print('\n'+'#'*64)
    print('\nThis Program lets you analyze the climate history of any area within Germany.')
    print('You only need a shapefile defining the area you want to analyze.')

    while True:  # This function runs until input is valid
        shp_input = input('\nEnter the path to the shapefile here: ').strip()
        shp_path = Path(shp_input).expanduser().resolve()

        # If the input is a file path
        if shp_path.is_file() and shp_path.suffix.lower() == '.shp':
            try:
                gdf = gpd.read_file(shp_path)
                if gdf.crs:
                    print('Valid shapefile with valid CRS found.')
                    return shp_path
                else:
                    print('Shapefile found, but CRS is not defined.')
                    continue
            except Exception as e:
                print(f'Error reading shapefile: {e}')
                continue

        # If the input is a folder path, search for any .shp file inside
        elif shp_path.is_dir():
            shp_files = list(shp_path.glob("*.shp"))
            if shp_files:
                print(f'Found shapefiles: {[f.name for f in shp_files]}. \nPlease append the filename to the path and try again.\n')
                continue
            else:
                print('No shapefile found in the folder.')
                continue

        else:
            print('Invalid path or not a shapefile.')
            continue


# # Download Data

# In[5]:


def check_if_already_downloaded(raster_links: list[str], raster_dir: Path) -> bool:
    """
    Returns True if all raster_links have already been downloaded as .tif files.
    """

    # Folder where files might be saved
    # Return False if folder is empty
    if not raster_dir.exists() or not any(raster_dir.iterdir()):
        return False
    tif_names = {f.stem for f in raster_dir.glob("*.tif")}
    if not tif_names:
        return False

    expected = set()
    for link in raster_links:
        name = Path(urlparse(link).path).name
        expected.add(Path(rename_dwd_file(name)).stem)

    return expected.issubset(tif_names)


def local_raster_ready(raster_dir: Path) -> bool:
    """
    Returns True when local raster data appears to be present.
    Avoids network checks for the web API path.
    """
    if not raster_dir.exists():
        return False
    if not any(raster_dir.iterdir()):
        return False
    for ext in (".tif", ".asc", ".asc.gz"):
        if any(raster_dir.rglob(f"*{ext}")):
            return True
    return False


# In[6]:


def _build_session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def list_of_dwd_data(
    session: requests.Session,
    file_types: Iterable[str] = (".asc.gz", ".pdf", ".zip"),
    timeout: int = DEFAULT_TIMEOUT,
) -> list[str]:
    '''
    Creates a list containing all dwd files to download.
    Parameters:
        file_types: string, ending that correspondes to the filetype that we want to download
    Returns:
        links: list containing links to all files to download
    '''

    # Get list of all download locations containing the data to download:
    base_download_location = DWD_BASE_URL
    folder_download_locations = [
        'air_temperature_max/',
        'air_temperature_mean/',
        'air_temperature_min/',
        'drought_index/',
        #'erosivity/',
        'frost_days/',
        'hot_days/',
        'ice_days/',
        'phenology/',
        'precipGE10mm_days/',
        'precipGE20mm_days/',
        'precipGE30mm_days/',
        'precipitation/',
        #'radiation_diffuse/',
        #'radiation_direct/',
        #'radiation_global/',
        'snowcover_days/',
        'summer_days/',
        'sunshine_duration/',
        'vegetation_begin/',
        'vegetation_end/'
    ]
    download_locations = [base_download_location+f for f in folder_download_locations]

    # Create a list containing all links
    links = []
    for location in download_locations:
        response = session.get(location, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"\nFailed to retrieve the webpage: {location}")

        soup = BeautifulSoup(response.text, 'html.parser')

        # Build absolute URLs
        found = [
            location + a['href']
            for a in soup.find_all('a', href=True)
            if a['href'].lower().endswith(tuple(file_types))
        ]
        links.append(found)

    links_flattend = list(chain.from_iterable(links))

    return links_flattend


# In[7]:


def download_dwd_data(
    session: requests.Session,
    links: list[str],
    dest_dir: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """
    Download files from a list of full URLs into a target directory.
    Parameters:
        links (list[str]): List of full download URLs.
        dest_dir (str or Path): Local directory where the files will be saved.
        timeout (int, optional): Maximum number of seconds to wait for a server response. Defaults to 30.
    Returns: 
        None
    """

    dest_dir.mkdir(parents=True, exist_ok=True)

    for file_url in tqdm(links,
                     desc='',
                     bar_format='{l_bar}{bar:40}| ({n_fmt}/{total_fmt}) Downloading files.',
                     ncols=120):
        filename = Path(urlparse(file_url).path).name
        file_path = dest_dir / filename

        with session.get(file_url, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                LOGGER.warning("Download failed (%s): %s", r.status_code, file_url)
                continue
            with file_path.open('wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)


# # Process the Data

# In[8]:


def list_of_files(folder: Path, file_type: str = ".gz") -> list[str]:
    '''
    Returns list containing all filesnames in folder with the ending file_type.
    Args:
        folder: string, path in filesystem including target folder
        file_type: string, ending that correspondes to the filetype that we want to download
    Return:
        List of all files of file_type within folder
    '''

    if not folder.exists():
        return []

    files = sorted(
        [
            str(f)
            for f in folder.iterdir()
            if f.is_file() and "".join(f.suffixes).lower().endswith(file_type.lower())
        ]
    )
    return files


# In[9]:


def rename_dwd_file(file: str) -> str:
    """
    Takes filename as string as input.
    Removes everything except for the core name and the year from the dwd filename.
    Returns new filename as string.
    """

    name = Path(file).name
    name = name.replace("grids_germany_annual_", "")
    for suffix in (".asc.gz", ".zip", ".asc", ".tif"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.endswith("_1917") or name.endswith("_2017"):
        pass
    elif name.endswith("17"):
        name = name[:-2]
    name = name.rstrip("_")
    return f"{name}.asc"


# In[10]:


def decompress_file(file: str) -> str:
    """
    Decompress files and saves a copy in the same folder.
    Args:
        file (str): path to file (input file)
    Return:
        decompressed_file (str): Path to decompressed file (output file)
    """

    path = Path(file)
    ft = filetype.guess(file)
    ft_ext = ft.extension if ft else None

    if path.suffix.lower() in {".asc", ".tif"} or ft_ext in {"asc", "tif"}:
        LOGGER.info("Filetype is already %s, no decompression needed", path.suffix)
        return str(path)

    # Keep output alongside the source file instead of the current working directory.
    decompressed_file = path.with_name(rename_dwd_file(path.name))

    # Unpack the mis-labelled “…asc.gz” archive (which is really a ZIP)
    if zipfile.is_zipfile(path) or ft_ext == "zip" or path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            asc_members = [m for m in zf.namelist() if m.lower().endswith(".asc")]
            if not asc_members:
                raise ValueError("No .asc file found in archive.")
            with zf.open(asc_members[0]) as src, decompressed_file.open("wb") as dst:
                dst.write(src.read())
        return str(decompressed_file)

    # Default to gzip if .gz or unknown data that isn't a zip
    with gzip.open(path, mode="rb") as f_in:
        with decompressed_file.open(mode="wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    return str(decompressed_file)


# In[11]:


def asc_to_tif_add_crs(asc_input: str, prj_txt: str) -> str:
    """
    Takes asc_input, adds crs (from prj.txt), saves it as .tif in the same folder.
    Args:
        asc_input (str): path to asc file (input file)
        prj_txt (str): path to prj file (text file that contains projection information)
    Returns:
        tif_output (tif): path/to/output.tif file
    """

    # CRS from .prj_file
    with open(prj_txt, "r", encoding="utf-8") as f:
        wkt = f.read()
    crs = CRS.from_wkt(wkt)

    # Read the .asc file
    with rasterio.open(asc_input) as src:
        data = src.read(1)
        profile = src.profile

    # Update Profile with CRS
    profile.update({
        "driver": "GTiff",
        "crs": crs,
        "dtype": rasterio.float32,
        "compress": "lzw",
    })

    tif_output = asc_input.replace(".asc", "") + ".tif"

    # Write to a new GeoTIFF file with CRS assigned
    with rasterio.open(tif_output, 'w', **profile) as dst:
        dst.write(data.astype(rasterio.float32), 1)

    #print(f'asc_to_tif_add_crs: Successfully transformed \n"{asc_input}" to \n"{tif_output}" \nand added {crs}.\n')
    return tif_output


# In[12]:


def delete_raster_files(folder_path: str) -> None:
    """
    Deletes all .asc, .asc.gz and .zip files within the folder..
    Args:
        folder_path (str): Path to folder containing the files.
    """

    folder = Path(folder_path)
    patterns = ["*.asc", "*.asc.gz", "*.zip"]
    deleted_files = 0

    for pattern in patterns:
        for file in folder.glob(pattern):
            try:
                file.unlink()
            except OSError:
                continue
            deleted_files += 1


# In[13]:


def change_shp_crs(shp_input: str, prj_txt: str) -> str:
    """
    Takes shp_input, transforms to crs (from prj.txt), outputs as shp_output
    Args:
        shp_input (str): path to shp file (input file)
        prj_txt (str): path to prj file (text file that contains projection information)
    
    Returns:
        shp_output (str): path/to/output.tif file
    """

    # CRS from .prj_file
    with open(prj_txt, "r", encoding="utf-8") as f:
        wkt = f.read()
    target_crs = CRS.from_wkt(wkt)

    # Read Shapefile
    gdf = gpd.read_file(shp_input)

    # Check if CRS is defined
    if gdf.crs is None:
        raise ValueError("Input shapefile CRS is undefined. Set correct CRS (e.g. gdf.set_crs()).")

    # Transform shp_input to target_crs
    gdf_transformed = gdf.to_crs(target_crs)

    # Create Output Folder
    SHP_DIR.mkdir(parents=True, exist_ok=True)

    # Define output name
    shp_output = str(SHP_DIR / Path(str(shp_input).replace(".shp", "") + "_" + str(target_crs).replace(":", "") + ".shp").name)

    # Save transformed shp to shp_output
    gdf_transformed.to_file(shp_output, encoding='utf-8')

    #print(f'change_shp_crs: Successfully copied \n"{shp_input}" to \n"{shp_output}" \nand added {target_crs}.\n')
    return shp_output


# In[14]:


def dissolve_shp(shp_input: str) -> str:
    """
    Takes shp_input, dissolve all polygon features into one, outputs as dissolved_shp
    Args:
        shp_input (str): path to shp file (input file)
    Returns:
        shp_output (str): path/to/output.tif file
    """

    # Read Shapefile
    gdf = gpd.read_file(shp_input)

    # Dissolve features in gdf
    gdf_dissolved = gdf.dissolve()

    # Create Output Folder
    SHP_DIR.mkdir(parents=True, exist_ok=True)

    # Define output name
    shp_output = str(SHP_DIR / Path(str(shp_input).replace(".shp", "") + "_dissolved" + ".shp").name)

    # Save transformed shp to shp_output
    gdf_dissolved.to_file(shp_output, encoding='utf-8')

    return shp_output


# In[15]:


def calculate_zonal_stats(shp: str, tif: str) -> list[dict]:
    """
    Calculates zonal stats of the tif for each feature in the shp.
    Args:
        shp (str): path to shp file
        tif (str): path to tif file
    Returns:
        stats (list[dict]): list of dictionarys which contain min, max, mean and count of the raster data for each poly.
    """

    # Set all_touched to False if you want to include only raster-cells that are completely within the shapefile.
    stats = zonal_stats(shp, tif, all_touched=True)

    return stats


# In[16]:


def zonal_climate_analysis(shp_input: str, raster_folder: str, prj_file: str) -> tuple[str, str]:
    """
    Perform rasterstats calculation on shp_input with every raster file in the raster_folder.
    Args:
        shp_input (str): path to shp file (input file) to perform calculations on
        raster_folder (str): path to folder containing all raster files to perform the rasterstats calculations with. has to be in .asc.gz file format
        prj_txt (str): path to prj file (text file that contains projection information)
    Creates:
        rasterstats_dict (dict{str:[{}]}): dict containing the name of the raster file as key and the corresponding rasterstats as a list of dicts as values.
    Returns:
        json_output_path_name (str): path to the created json file conatining rasterstats calculations.
        shp_crs_dissolved (str): path to the dissolved shapefile with transformed crs the rasterstats where calculated on.
    """

    raster_folder = Path(raster_folder)

    # Prepare shapefile:
    shp_crs = change_shp_crs(shp_input, prj_file)
    shp_crs_dissolved = dissolve_shp(shp_crs)

    # Create list of compessed .asc.gz rasterfiles:
    files_asc_gz = list_of_files(raster_folder, file_type=".asc.gz")

    if len(files_asc_gz) != 0:
        # Decompress rasterfiles:
        for f in tqdm(files_asc_gz,
                      desc='',
                      bar_format='{l_bar}{bar:40}| ({n_fmt}/{total_fmt}) Decompressing files.',
                      ncols=120):
            decompress_file(f)
    else:
        print('Files are already decompressed.')

    # Create list of decompressed .asc rasterfiles:
    files_asc = list_of_files(raster_folder, file_type=".asc")

    if len(files_asc) != 0:
        # Transform decompressed files to tif and add crs:
        for f in tqdm(files_asc,
                      desc='',
                      bar_format='{l_bar}{bar:40}| ({n_fmt}/{total_fmt}) Transforming files to the right format.',
                      ncols=120):
            asc_to_tif_add_crs(f, prj_file)
    else:
        print('Files are already transformed to the right format.')

    # Create list .tif rasterfiles
    files_tif = list_of_files(raster_folder, file_type=".tif")
    if not files_tif:
        raise RuntimeError("No .tif files found after preprocessing.")

    # Create list containing rasterstats:
    rasterstats_list = []

    # Iterate over files_tif and perform rasterstats calculations on each rasterfile and the shapefile:
    for f in tqdm(files_tif,
                  desc='',
                  bar_format='{l_bar}{bar:40}| ({n_fmt}/{total_fmt}) Calculating rasterstats.',
                  ncols=120):
        rasterstats_list.append(calculate_zonal_stats(shp_crs_dissolved, f)) # Append rasterstats to rasterstats_list

    # Combine rasterstats and the name of the raster the stats are calculated with
    raster_path = str(raster_folder)
    filenames = [Path(fn).with_suffix('').name for fn in files_tif]

    # Delete deprecated files
    delete_raster_files(raster_path)

    # Create dict
    rasterstats_dict = dict(zip(filenames, rasterstats_list))

    # Convert rasterstats_dict to better json format:
    rasterstats_json = {}

    for key, value in rasterstats_dict.items():
        name = key[:-5]
        year = key[-4:]
        if name not in rasterstats_json:
            rasterstats_json[name] = {}
        rasterstats_json[name][year] = value

    # Export dict as json:
    path_to_shp = Path(shp_crs_dissolved)
    shp_name = path_to_shp.name
    json_output_path_name = str(OUTPUT_DIR / (shp_name.replace(".shp", "") + "_rasterstats.json"))

    with open(json_output_path_name, "w", encoding="utf-8") as rs_json:
        json.dump(rasterstats_json, rs_json)

    return json_output_path_name, shp_crs_dissolved


# # Visualize

# In[17]:


def years_values(rs_data: dict, parameter_name: str):
    """
    Arguments:
        parameter_name (str): key in rasterstats.json dictionary
    Returns:
        title (str): parameter_name
        years (list): list of years
        values_max (list): list of max values
        values_mean (list): list of mean values
        values_min (list): list of min values
    """

    # Title:
    title = parameter_name

    # Years:
    years = sorted(rs_data[title], key=lambda y: int(y))

    # Values max:
    values_max = []
    for year in (rs_data[parameter_name][y] for y in years):
        for entry in year:
            values_max.append(entry['max'])

    # Values mean:
    values_mean = []
    for year in (rs_data[parameter_name][y] for y in years):
        for entry in year:
            values_mean.append(entry['mean'])

    # Values mean:
    values_min = []
    for year in (rs_data[parameter_name][y] for y in years):
        for entry in year:
            values_min.append(entry['min'])

    return title, years, values_max, values_mean, values_min


# In[18]:


def create_map(shapefile: str, shp_name: str) -> None:
    '''
    Takes path to shapefile as string as input.
    Creates interactive map as html.
    Adds area and perimeter as tooltips on hover in the html map.
    '''

    shp_path = Path(shapefile)
    gdf = gpd.read_file(shapefile)
    if gdf.crs is None:
        raise ValueError("CRS is missing. Set a CRS before running.")

    # Compute in a local metric CRS
    gdf_m = gdf.to_crs(gdf.estimate_utm_crs())
    is_poly = gdf_m.geom_type.str.contains("polygon", case=False, na=False)
    gdf["area_km2"] = (gdf_m.area.where(is_poly)) / 1_000_000
    gdf["perim_km"] = (gdf_m.length.where(is_poly)) / 1_000
    gdf["shapefile"] = shp_path.stem

    # interactive map
    m = gdf.to_crs(4326).explore(
        tooltip=["area_km2", "perim_km"],
        popup=False
    )

    # Save Map
    mapname = shp_name + "_" + "map.html"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    map_path = str(OUTPUT_DIR / mapname)
    m.save(map_path)
    print(f'Successfully created and saved map: {mapname}')


# In[19]:

def apply_plot_style(fig, ax, years, y_label, y_max=None, y_min=0, title=None):
    fig.set_size_inches(12, 6.6)
    fig.patch.set_facecolor(PLOT_COLORS["sand"])
    ax.set_facecolor(PLOT_COLORS["panel"])
    ax.set_xlabel('Jahr', labelpad=10)
    ax.set_ylabel(y_label, labelpad=10)
    if title:
        ax.set_title(title, fontsize=14, pad=12, fontweight="semibold")
    if years:
        ax.set_xlim([min(years), max(years)])
        try:
            year_ints = [int(y) for y in years]
            tick_years = [str(y) for y in year_ints if y % 5 == 0]
            if tick_years:
                ax.set_xticks(tick_years)
        except ValueError:
            pass
        ax.tick_params(axis='x', rotation=35)
    if y_max is not None:
        ax.set_ylim([y_min, y_max])
    ax.grid(True, axis='y')
    ax.margins(x=0.01)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(PLOT_COLORS["grid"])
    ax.spines["bottom"].set_color(PLOT_COLORS["grid"])
    fig.subplots_adjust(top=0.86, right=0.98, left=0.08, bottom=0.22)


def style_legend(ax):
    handles, labels = ax.get_legend_handles_labels()
    if not handles:
        return
    cols = min(len(labels), 3)
    legend = ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.16),
        ncol=cols,
        frameon=True,
        handlelength=2.4,
        columnspacing=1.2,
        borderaxespad=0.6
    )
    legend.get_frame().set_facecolor(PLOT_COLORS["panel"])
    legend.get_frame().set_edgecolor(PLOT_COLORS["grid"])
    legend.get_frame().set_linewidth(0.8)
    legend.get_frame().set_alpha(1.0)


def save_plot(fig, plot_path):
    fig.savefig(plot_path, bbox_inches='tight', dpi=300, facecolor=fig.get_facecolor())


def plot_air_temp_min_mean_max(rs_data: dict, shp_name: str):
    # Air Temp min mean max
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # List oft startyears
    startyears = []

    # Max Temp
    title, years, values_max, t_max, values_min = years_values(rs_data, "air_temp_max")
    t_max = [t_max[i]/10 for i in range(len(t_max))]                            # 1/10 so it is in degrees noch in degrees/10
    startyears.append(years[0])

    # Mean Temp
    title, years, values_max, t_mean, values_min = years_values(rs_data, "air_temp_mean")
    t_mean = [t_mean[i]/10 for i in range(len(t_mean))]
    startyears.append(years[0])

    # Min Temp
    title, years, values_max, t_min, values_min = years_values(rs_data, "air_temp_min")
    t_min = [t_min[i]/10 for i in range(len(t_min))]
    startyears.append(years[0])

    # Crop to the same start-year
    common_startyear = int(max(startyears))
    years_filtered = [y for y in years if int(y) >= common_startyear]
    t_max  = [v for y, v in zip(map(int, years), t_max)  if y >= common_startyear]
    t_mean = [v for y, v in zip(map(int, years), t_mean) if y >= common_startyear]
    t_min  = [v for y, v in zip(map(int, years), t_min)  if y >= common_startyear]

    # Plot
    ax.plot(years_filtered, t_max, color=PLOT_COLORS["coral"], linewidth=2.2, label='Maximale Lufttemperatur')
    ax.plot(years_filtered, t_mean, color=PLOT_COLORS["mint"], linewidth=2.2, label='Mittlere Lufttemperatur')
    ax.plot(years_filtered, t_min, color=PLOT_COLORS["ice"], linewidth=2.2, label='Minimale Lufttemperatur')

    # Fill between lines
    ax.fill_between(years_filtered, t_max, t_mean, color=PLOT_COLORS["coral"], alpha=0.18)
    ax.fill_between(years_filtered, t_mean, t_min, color=PLOT_COLORS["ice"], alpha=0.18)

    apply_plot_style(
        fig,
        ax,
        years_filtered,
        'Temperatur in °C',
        max(t_max) * 1.2,
        title='Lufttemperatur (Min / Mittel / Max)'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "lufttemperatur_min_mittel_max.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[20]:


def plot_frost_ice_days(rs_data: dict, shp_name: str):
    # Frost and Ice Days
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Ice Days
    title, years, values_max, values_mean_id, values_min = years_values(rs_data, "ice_days")

    # Frost Days
    title, years, values_max, values_mean_fd, values_min = years_values(rs_data, "frost_days")

    # List containing 365 (days per year) as many times as there are years (upper limit)
    days_in_year_max = [365]*len(years)

    # List containing 365 (days per year) as many times as there are years (lower limit)
    days_in_year_min = [0]*len(years)

    # Plot
    ax.plot(years, values_mean_fd, color=PLOT_COLORS["ice"], linewidth=2.2, label='Frosttage (min 0°C)')
    ax.plot(years, values_mean_id, color=PLOT_COLORS["deep"], linewidth=2.2, label='Eistage (max 0°C)')

    # Fill between lines
    ax.fill_between(years, values_mean_fd, values_mean_id, color=PLOT_COLORS["ice"], alpha=0.18)
    ax.fill_between(years, values_mean_id, days_in_year_min, color=PLOT_COLORS["deep"], alpha=0.15)

    apply_plot_style(
        fig,
        ax,
        years,
        'Tage',
        max(values_mean_fd) * 1.2,
        title='Frost- und Eistage'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "frost_eistage.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[21]:


def plot_snowcover_days(rs_data: dict, shp_name: str):
    # Snowcover Days
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Snowcover Days
    title, years, values_max, values_mean_snd, values_min = years_values(rs_data, "snowcover_days")

    # List containing 365 (days per year) as many times as there are years (upper limit)
    days_in_year_max = [365]*len(years)

    # List containing 365 (days per year) as many times as there are years (lower limit)
    days_in_year_min = [0]*len(years)

    # Plot
    ax.plot(years, values_mean_snd, color=PLOT_COLORS["ice"], linewidth=2.2, label='Tage mit > 1cm Schneehöhe')

    # Fill between lines
    ax.fill_between(years, values_mean_snd, days_in_year_min, color=PLOT_COLORS["ice"], alpha=0.18)

    apply_plot_style(
        fig,
        ax,
        years,
        'Tage',
        max(values_mean_snd) * 1.2,
        title='Schneedeckentage'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "schneedeckentage.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[22]:


def plot_summer_hot_days(rs_data: dict, shp_name: str):
    # Summer and Hot Days
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Ice Days
    title, years, values_max, values_mean_sd, values_min = years_values(rs_data, "summer_days")

    # Frost Days
    title, years, values_max, values_mean_hd, values_min = years_values(rs_data, "hot_days")

    # List containing 365 (days per year) as many times as there are years (upper limit)
    days_in_year_max = [365]*len(years)

    # List containing 365 (days per year) as many times as there are years (lower limit)
    days_in_year_min = [0]*len(years)

    # Plot
    ax.plot(years, values_mean_sd, color=PLOT_COLORS["sun"], linewidth=2.2, label='Sommertage (max 25°C)')
    ax.plot(years, values_mean_hd, color=PLOT_COLORS["coral"], linewidth=2.2, label='Heiße Tage (max 30°C)')

    # Fill between lines
    ax.fill_between(years, values_mean_hd, values_mean_sd, color=PLOT_COLORS["sun"], alpha=0.2)
    ax.fill_between(years, values_mean_sd, days_in_year_min, color=PLOT_COLORS["coral"], alpha=0.15)

    apply_plot_style(
        fig,
        ax,
        years,
        'Tage',
        max(values_mean_sd) * 1.2,
        title='Sommer- und Heiße Tage'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "sommer_heisse_tage.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[23]:


def plot_precipitaion(rs_data: dict, shp_name: str):
    # Precipitation
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Precipitation
    title, years, values_max, values_mean_pp, values_min = years_values(rs_data, "precipitation")

    # Precipitation
    title, yearsdi, values_max, values_mean_di, values_min = years_values(rs_data, "drought_index")
    values_mean_di = [di*10 for di in values_mean_di]

    # Plot
    ax.plot(years, values_mean_pp, color=PLOT_COLORS["ice"], linewidth=2.2, label='Niederschlag in mm')
    ax.fill_between(years, values_mean_pp, color=PLOT_COLORS["ice"], alpha=0.2)
    ax.plot(yearsdi, values_mean_di, color=PLOT_COLORS["coral"], linewidth=2.2, label='Trockenheitsindex (mm/°C)')

    apply_plot_style(
        fig,
        ax,
        years,
        'Niederschlag (mm)',
        max(values_mean_pp) * 1.2,
        title='Niederschlag + Trockenheitsindex'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "niederschlag_trockenheit.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[24]:


def plot_precipitaion_days(rs_data: dict, shp_name: str):
    # Precipitation Days
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # 10mm
    title, years, values_max, p10, values_min = years_values(rs_data, "precipGE10mm_days")

    # 20mm
    title, years, values_max, p20, values_min = years_values(rs_data, "precipGE20mm_days")
    #p1020 = [p10[i]+p20[i] for i in range(len(p10))]

    # 30mm
    title, years, values_max, p30, values_min = years_values(rs_data, "precipGE30mm_days")
    #p102030 = [p10[i]+p20[i]+p30[i] for i in range(len(p10))]

    # List containing 365 (days per year) as many times as there are years (lower limit)
    days_in_year_min = [0]*len(years)

    # Plot
    ax.plot(years, p10, color=PLOT_COLORS["ice"], linewidth=2.2, label='Anzahl der Tage mit Niederschlagshöhe >= 10 mm')
    ax.plot(years, p20, color=PLOT_COLORS["deep"], linewidth=2.2, label='Anzahl der Tage mit Niederschlagshöhe >= 20 mm')
    ax.plot(years, p30, color=PLOT_COLORS["violet"], linewidth=2.2, label='Anzahl der Tage mit Niederschlagshöhe >= 30 mm')

    # Fill between lines
    ax.fill_between(years, p10, p20, color=PLOT_COLORS["ice"], alpha=0.18)
    ax.fill_between(years, p20, p30, color=PLOT_COLORS["deep"], alpha=0.16)
    ax.fill_between(years, p30, days_in_year_min, color=PLOT_COLORS["violet"], alpha=0.14)

    apply_plot_style(
        fig,
        ax,
        years,
        'Tage',
        max(p10) * 1.2,
        title='Starkniederschlagstage'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "starkniederschlag_tage.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[25]:


def plot_sunshine_duration(rs_data: dict, shp_name: str):
    # Sunshine Duration
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Sunshine Duration
    title, years, values_max, values_mean_sd, values_min = years_values(rs_data, "sunshine_duration")
    values_mean_sd = [sd/365 for sd in values_mean_sd]

    # Plot
    ax.plot(years, values_mean_sd, color=PLOT_COLORS["sun"], linewidth=2.2, label='Durchschnittliche Sonnenstunden pro Tag')
    ax.fill_between(years, values_mean_sd, color=PLOT_COLORS["sun"], alpha=0.18)

    apply_plot_style(
        fig,
        ax,
        years,
        'Sonnenstunden pro Tag',
        max(values_mean_sd) * 1.2,
        title='Sonnenscheindauer'
    )
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "sonnenscheindauer.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[26]:


def plot_vegetation_begin_end(rs_data: dict, shp_name: str):
    # Vegetation begin and vegetation end
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Vegetation begin line
    title, years, values_max, values_mean_b, values_min = years_values(rs_data, "vegetation_begin")

    # Vegetation end line
    title, years, values_max, values_mean_e, values_min = years_values(rs_data, "vegetation_end")

    # List containing 365 (days per year) as many times as there are years (upper limit)
    days_in_year_max = [365]*len(years)

    # List containing 365 (days per year) as many times as there are years (lower limit)
    days_in_year_min = [0]*len(years)

    # Plot
    ax.plot(years, values_mean_e, color=PLOT_COLORS["coral"], linewidth=2.2, label='Ende der vegetativen Phase')
    ax.plot(years, values_mean_b, color=PLOT_COLORS["leaf"], linewidth=2.2, label='Begin der vegetativen Phase')

    # Marking the beginning of the seasons
    #ax.axhline(y=335, color='lightblue', linestyle='--', label='Winterbeginn')
    #ax.axhline(y=244, color='orange', linestyle='--', label='Herbstbeginn')
    #ax.axhline(y=152, color='darkgreen', linestyle='--', label='Sommerbeginn')
    #ax.axhline(y=60, color='lightgreen', linestyle='--', label='Frühlingsbeginn')

    # Fill between lines
    ax.fill_between(years, values_mean_e, values_mean_b, color=PLOT_COLORS["leaf"], alpha=0.2, label='Vegetative Phase')

    apply_plot_style(fig, ax, years, 'Tage', 365, title='Vegetationsperiode')
    style_legend(ax)

    # Add month ticks on right side
    ax2 = ax.twinx()
    month_days = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    month_labels = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
                    "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
    ax2.set_ylim(ax.get_ylim())
    ax2.set_yticks(month_days)
    ax2.set_yticklabels(month_labels)
    ax2.set_ylabel("Monatsbeginn", color=PLOT_COLORS["slate"])
    ax2.tick_params(axis='y', colors=PLOT_COLORS["slate"])

    # Save Plot
    plotname = shp_name + "_" + "vegetationsperiode.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# In[27]:


def plot_vegetation_phase_length(rs_data: dict, shp_name: str):
    # Vegetation phase length
    plt.close()

    # Create a figure containing a single Axes.
    fig, ax = plt.subplots()

    # Vegetation begin line
    title, years, values_max, values_mean_b, values_min = years_values(rs_data, "vegetation_begin")

    # Vegetation end line
    title, years, values_max, values_mean_e, values_min = years_values(rs_data, "vegetation_end")

    # Vegetation phase length
    veg_len = [values_mean_e[i]-values_mean_b[i] for i in range(len(years))]

    # List containing 365 (days per year) as many times as there are years (upper limit)
    days_in_year_max = [365]*len(years)

    # List containing 365 (days per year) as many times as there are years (lower limit)
    days_in_year_min = [0]*len(years)

    # Plot
    ax.plot(years, veg_len, color=PLOT_COLORS["leaf"], linewidth=2.2, label='Vegetative Phase')

    # Fill between lines
    ax.fill_between(years, veg_len, days_in_year_min, color=PLOT_COLORS["leaf"], alpha=0.2)

    apply_plot_style(fig, ax, years, 'Tage', 365, title='Dauer der Vegetationsperiode')
    style_legend(ax)

    # Save Plot
    plotname = shp_name + "_" + "vegetationsperiode_dauer.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_path = str(OUTPUT_DIR / plotname)
    save_plot(fig, plot_path)
    print(f'Successfully created and saved plot: {plotname}')


# # Run the Program

# In[28]:


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zonal Climate Analyzer")
    parser.add_argument("shapefile", nargs="?", help="Path to a shapefile to analyze.")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading rasters (requires local data).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)

    if args.shapefile:
        shp = Path(args.shapefile).expanduser().resolve()
        if not shp.exists():
            raise FileNotFoundError(f"Shapefile not found: {shp}")
    else:
        shp = get_shp()

    print("\nDownload the data:")
    raster_root = DATA_DIR
    skip_download = args.skip_download or os.environ.get("ZCA_SKIP_DWD_DOWNLOAD", "").lower() in {
        "1",
        "true",
        "yes",
    }

    session = _build_session()
    if skip_download:
        if not local_raster_ready(raster_root):
            raise RuntimeError(
                "Raster data not found locally. "
                "Unset ZCA_SKIP_DWD_DOWNLOAD to allow downloads."
            )
        print("Skipping DWD download (ZCA_SKIP_DWD_DOWNLOAD=1).")
    else:
        pdf_links = list_of_dwd_data(session, file_types=[".pdf"])
        raster_links = list_of_dwd_data(session, file_types=[".asc.gz", ".zip"])

        if check_if_already_downloaded(raster_links, raster_root):
            print("All files are already downloaded.")
        else:
            print("Download the PDF files containing information about the DWD data:")
            download_dwd_data(session, pdf_links, DATA_INFO_DIR)

            print("Download the raster files:")
            download_dwd_data(session, raster_links, DATA_DIR)

    print("\nProcess the data:")
    rasterstats_json, shp_crs_dissolved = zonal_climate_analysis(shp, str(DATA_DIR), str(PRJ_FILE))

    input_file = rasterstats_json
    with open(input_file, "r", encoding="utf-8") as json_file:
        rs = json.load(json_file)

    print("\nCreating Map and Plots:")
    shp_name = shp.stem

    create_map(shp_crs_dissolved, shp_name)
    plot_air_temp_min_mean_max(rs, shp_name)
    plot_frost_ice_days(rs, shp_name)
    plot_snowcover_days(rs, shp_name)
    plot_summer_hot_days(rs, shp_name)
    plot_precipitaion(rs, shp_name)
    plot_precipitaion_days(rs, shp_name)
    plot_sunshine_duration(rs, shp_name)
    plot_vegetation_begin_end(rs, shp_name)
    plot_vegetation_phase_length(rs, shp_name)

    print("\nFinished!")
    print(f"\nMap and plots are saved here:\n{OUTPUT_DIR}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
