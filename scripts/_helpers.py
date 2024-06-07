# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import urllib
import zipfile

import country_converter as coco
import fiona
import geopandas as gpd
import numpy as np
import pandas as pd
import pypsa
import requests
import snakemake as sm
import yaml
from pypsa.clustering.spatial import _make_consense
from pypsa.components import component_attrs, components
from pypsa.descriptors import Dict
from shapely.geometry import Point
from snakemake.script import Snakemake
from tqdm import tqdm

logger = logging.getLogger(__name__)

# list of recognised nan values (NA and na excluded as may be confused with Namibia 2-letter country code)
NA_VALUES = ["NULL", "", "N/A", "NAN", "NaN", "nan", "Nan", "n/a", "null"]

REGION_COLS = ["geometry", "name", "x", "y", "country"]

# filename of the regions definition config file
REGIONS_CONFIG = "regions_definition_config.yaml"


def handle_exception(exc_type, exc_value, exc_traceback):
    """
    Customise errors traceback.
    """
    tb = exc_traceback
    while tb.tb_next:
        tb = tb.tb_next
    fl_name = tb.tb_frame.f_globals.get("__file__")
    func_name = tb.tb_frame.f_code.co_name

    if issubclass(exc_type, KeyboardInterrupt):
        logger.error(
            "Manual interruption %r, function %r: %s",
            fl_name,
            func_name,
            exc_value,
        )
    else:
        logger.error(
            "An error happened in module %r, function %r: %s",
            fl_name,
            func_name,
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )


def create_logger(logger_name, level=logging.INFO):
    """
    Create a logger for a module and adds a handler needed to capture in logs
    traceback from exceptions emerging during the workflow.
    """
    logger_instance = logging.getLogger(logger_name)
    logger_instance.setLevel(level)
    handler = logging.StreamHandler(stream=sys.stdout)
    logger_instance.addHandler(handler)
    sys.excepthook = handle_exception
    return logger_instance


def read_osm_config(*args):
    """
    Read values from the regions config file based on provided key arguments.

    Parameters
    ----------
    *args : str
        One or more key arguments corresponding to the values to retrieve
        from the config file. Typical arguments include "world_iso",
        "continent_regions", "iso_to_geofk_dict", and "osm_clean_columns".

    Returns
    -------
    tuple or str or dict
        If a single key is provided, returns the corresponding value from the
        regions config file. If multiple keys are provided, returns a tuple
        containing values corresponding to the provided keys.

    Examples
    --------
    >>> values = read_osm_config("key1", "key2")
    >>> print(values)
    ('value1', 'value2')

    >>> world_iso = read_osm_config("world_iso")
    >>> print(world_iso)
    {"Africa": {"DZ": "algeria", ...}, ...}
    """
    if "__file__" in globals():
        base_folder = get_dirname_path(__file__)
        if not path_exists(get_path(base_folder, "configs")):
            base_folder = get_dirname_path(base_folder)
    else:
        base_folder = get_current_directory_path()
    osm_config_path = get_path(base_folder, "configs", REGIONS_CONFIG)
    with open(osm_config_path, "r") as f:
        osm_config = yaml.safe_load(f)
    if len(args) == 0:
        return osm_config
    elif len(args) == 1:
        return osm_config[args[0]]
    else:
        return tuple([osm_config[a] for a in args])


def sets_path_to_root(root_directory_name, n=8):
    """
    Search and sets path to the given root directory (root/path/file).

    Parameters
    ----------
    root_directory_name : str
        Name of the root directory.
    n : int
        Number of folders the function will check upwards/root directed.
    """

    repo_name = root_directory_name
    n0 = n

    while n >= 0:
        n -= 1
        # if repo_name is current folder name, stop and set path
        if repo_name == get_basename_abs_path("."):
            repo_path = get_current_directory_path()  # current_path
            os.chdir(repo_path)  # change dir_path to repo_path
            print("This is the repository path: ", repo_path)
            print("Had to go %d folder(s) up." % (n0 - 1 - n))
            break
        # if repo_name NOT current folder name for 5 levels then stop
        if n == 0:
            print("Can't find the repo path.")
        # if repo_name NOT current folder name, go one directory higher
        else:
            change_to_script_dir(".")  # change to the upper folder


def configure_logging(snakemake, skip_handlers=False):
    """
    Configure the basic behaviour for the logging module.

    Note: Must only be called once from the __main__ section of a script.

    The setup includes printing log messages to STDERR and to a log file defined
    by either (in priority order): snakemake.log.python, snakemake.log[0] or "logs/{rulename}.log".
    Additional keywords from logging.basicConfig are accepted via the snakemake configuration
    file under snakemake.config.logging.

    Parameters
    ----------
    snakemake : snakemake object
        Your snakemake object containing a snakemake.config and snakemake.log.
    skip_handlers : True | False (default)
        Do (not) skip the default handlers created for redirecting output to STDERR and file.
    """

    kwargs = snakemake.config.get("logging", dict()).copy()
    kwargs.setdefault("level", "INFO")

    if skip_handlers is False:
        fallback_path = get_path(
            get_dirname_path(__file__), "..", "logs", f"{snakemake.rule}.log"
        )
        logfile = snakemake.log.get(
            "python", snakemake.log[0] if snakemake.log else fallback_path
        )
        kwargs.update(
            {
                "handlers": [
                    # Prefer the "python" log, otherwise take the first log for each
                    # Snakemake rule
                    logging.FileHandler(logfile),
                    logging.StreamHandler(),
                ]
            }
        )
    logging.basicConfig(**kwargs, force=True)


def load_network(import_name=None, custom_components=None):
    """
    Helper for importing a pypsa.Network with additional custom components.

    Parameters
    ----------
    import_name : str
        As in pypsa.Network(import_name)
    custom_components : dict
        Dictionary listing custom components.
        For using ``snakemake.params.override_components"]``
        in ``config.yaml`` define:

        .. code:: yaml

            override_components:
                ShadowPrice:
                    component: ["shadow_prices","Shadow price for a global constraint.",np.nan]
                    attributes:
                    name: ["string","n/a","n/a","Unique name","Input (required)"]
                    value: ["float","n/a",0.,"shadow value","Output"]

    Returns
    -------
    pypsa.Network
    """

    override_components = None
    override_component_attrs_dict = None

    if custom_components is not None:
        override_components = pypsa.components.components.copy()
        override_component_attrs_dict = Dict(
            {k: v.copy() for k, v in pypsa.components.component_attrs.items()}
        )
        for k, v in custom_components.items():
            override_components.loc[k] = v["component"]
            override_component_attrs_dict[k] = pd.DataFrame(
                columns=["type", "unit", "default", "description", "status"]
            )
            for attr, val in v["attributes"].items():
                override_component_attrs_dict[k].loc[attr] = val

    return pypsa.Network(
        import_name=import_name,
        override_components=override_components,
        override_component_attrs=override_component_attrs_dict,
    )


def update_p_nom_max(n):
    """
    If extendable carriers (solar/onwind/...) have capacity >= 0, e.g. existing
    assets from the OPSD project are included to the network, the installed
    capacity might exceed the expansion limit.

    Hence, we update the assumptions.
    """
    n.generators.p_nom_max = n.generators[["p_nom_min", "p_nom_max"]].max(1)


def aggregate_p_nom(n):
    return pd.concat(
        [
            n.generators.groupby("carrier").p_nom_opt.sum(),
            n.storage_units.groupby("carrier").p_nom_opt.sum(),
            n.links.groupby("carrier").p_nom_opt.sum(),
            n.loads_t.p.groupby(n.loads.carrier, axis=1).sum().mean(),
        ]
    )


def aggregate_p(n):
    return pd.concat(
        [
            n.generators_t.p.sum().groupby(n.generators.carrier).sum(),
            n.storage_units_t.p.sum().groupby(n.storage_units.carrier).sum(),
            n.stores_t.p.sum().groupby(n.stores.carrier).sum(),
            -n.loads_t.p.sum().groupby(n.loads.carrier).sum(),
        ]
    )


def aggregate_e_nom(n):
    return pd.concat(
        [
            (n.storage_units["p_nom_opt"] * n.storage_units["max_hours"])
            .groupby(n.storage_units["carrier"])
            .sum(),
            n.stores["e_nom_opt"].groupby(n.stores.carrier).sum(),
        ]
    )


def aggregate_p_curtailed(n):
    return pd.concat(
        [
            (
                (
                    n.generators_t.p_max_pu.sum().multiply(n.generators.p_nom_opt)
                    - n.generators_t.p.sum()
                )
                .groupby(n.generators.carrier)
                .sum()
            ),
            (
                (n.storage_units_t.inflow.sum() - n.storage_units_t.p.sum())
                .groupby(n.storage_units.carrier)
                .sum()
            ),
        ]
    )


def aggregate_costs(n, flatten=False, opts=None, existing_only=False):
    components_dict = dict(
        Link=("p_nom", "p0"),
        Generator=("p_nom", "p"),
        StorageUnit=("p_nom", "p"),
        Store=("e_nom", "p"),
        Line=("s_nom", None),
        Transformer=("s_nom", None),
    )

    costs = {}
    for c, (p_nom, p_attr) in zip(
        n.iterate_components(components_dict.keys(), skip_empty=False),
        components_dict.values(),
    ):
        if c.df.empty:
            continue
        if not existing_only:
            p_nom += "_opt"
        costs[(c.list_name, "capital")] = (
            (c.df[p_nom] * c.df.capital_cost).groupby(c.df.carrier).sum()
        )
        if p_attr is not None:
            p = c.pnl[p_attr].sum()
            if c.name == "StorageUnit":
                p = p.loc[p > 0]
            costs[(c.list_name, "marginal")] = (
                (p * c.df.marginal_cost).groupby(c.df.carrier).sum()
            )
    costs = pd.concat(costs)

    if flatten:
        assert opts is not None
        conv_techs = opts["conv_techs"]

        costs = costs.reset_index(level=0, drop=True)
        costs = costs["capital"].add(
            costs["marginal"].rename({t: t + " marginal" for t in conv_techs}),
            fill_value=0.0,
        )

    return costs


def progress_retrieve(
    url, file, data=None, headers=None, disable_progress=False, round_to_value=1.0
):
    """
    Function to download data from an url with a progress bar progress in
    retrieving data.

    Parameters
    ----------
    url : str
        Url to download data from
    file : str
        File where to save the output
    data : dict
        Data for the request (default None), when not none Post method is used
    disable_progress : bool
        When true, no progress bar is shown
    round_to_value : float
        (default 0) Precision used to report the progress
        e.g. 0.1 stands for 88.1, 10 stands for 90, 80
    """

    pbar = tqdm(total=100, disable=disable_progress)

    def dl_progress(count, block_size, total_size):
        pbar.n = (
            round(count * block_size * 100 / total_size / round_to_value)
            * round_to_value
        )
        pbar.refresh()

    if data is not None:
        data = urllib.parse.urlencode(data).encode()

    if headers:
        opener = urllib.request.build_opener()
        opener.addheaders = headers
        urllib.request.install_opener(opener)

    urllib.request.urlretrieve(url, file, reporthook=dl_progress, data=data)


def get_aggregation_strategies(aggregation_strategies):
    """
    Default aggregation strategies that cannot be defined in .yaml format must
    be specified within the function, otherwise (when defaults are passed in
    the function's definition) they get lost when custom values are specified
    in the config.
    """

    bus_strategies = dict(country=_make_consense("Bus", "country"))
    bus_strategies.update(aggregation_strategies.get("buses", {}))

    generator_strategies = {"build_year": lambda x: 0, "lifetime": lambda x: np.inf}
    generator_strategies.update(aggregation_strategies.get("generators", {}))

    return bus_strategies, generator_strategies


def mock_snakemake(rule_name, **wildcards):
    """
    This function is expected to be executed from the "scripts"-directory of "
    the snakemake project. It returns a snakemake.script.Snakemake object,
    based on the Snakefile.

    If a rule has wildcards, you have to specify them in **wildcards**.

    Parameters
    ----------
    rule_name: str
        name of the rule for which the snakemake object should be generated
    wildcards:
        keyword arguments fixing the wildcards. Only necessary if wildcards are
        needed.
    """

    script_dir = pathlib.Path(__file__).parent.resolve()
    assert (
        pathlib.Path.cwd().resolve() == script_dir
    ), f"mock_snakemake has to be run from the repository scripts directory {script_dir}"
    os.chdir(script_dir.parent)
    for p in sm.SNAKEFILE_CHOICES:
        if path_exists(p):
            snakefile = p
            break
    workflow = sm.Workflow(
        snakefile, overwrite_configfiles=[], rerun_triggers=[]
    )  # overwrite_config=config
    workflow.include(snakefile)
    workflow.global_resources = {}
    try:
        rule = workflow.get_rule(rule_name)
    except Exception as exception:
        print(
            exception,
            f"The {rule_name} might be a conditional rule in the Snakefile.\n"
            f"Did you enable {rule_name} in the config?",
        )
        raise
    dag = sm.dag.DAG(workflow, rules=[rule])
    wc = Dict(wildcards)
    job = sm.jobs.Job(rule, dag, wc)

    def make_accessable(*ios):
        for io in ios:
            for i in range(len(io)):
                io[i] = get_abs_path(io[i])

    make_accessable(job.input, job.output, job.log)
    snakemake = Snakemake(
        job.input,
        job.output,
        job.params,
        job.wildcards,
        job.threads,
        job.resources,
        job.log,
        job.dag.workflow.config,
        job.rule.name,
        None,
    )
    snakemake.benchmark = job.benchmark

    # create log and output dir if not existent
    for path in list(snakemake.log) + list(snakemake.output):
        build_directory(path)

    os.chdir(script_dir)
    return snakemake


def two_2_three_digits_country(two_code_country):
    """
    Convert 2-digit to 3-digit country code:

    Parameters
    ----------
    two_code_country: str
        2-digit country name

    Returns
    ----------
    three_code_country: str
        3-digit country name
    """
    if two_code_country == "SN-GM":
        return f"{two_2_three_digits_country('SN')}-{two_2_three_digits_country('GM')}"

    three_code_country = coco.convert(two_code_country, to="ISO3")
    return three_code_country


def three_2_two_digits_country(three_code_country):
    """
    Convert 3-digit to 2-digit country code:

    Parameters
    ----------
    three_code_country: str
        3-digit country name

    Returns
    ----------
    two_code_country: str
        2-digit country name
    """
    if three_code_country == "SEN-GMB":
        return f"{three_2_two_digits_country('SN')}-{three_2_two_digits_country('GM')}"

    two_code_country = coco.convert(three_code_country, to="ISO2")
    return two_code_country


def two_digits_2_name_country(
    two_code_country, name_string="name_short", no_comma=False, remove_start_words=[]
):
    """
    Convert 2-digit country code to full name country:

    Parameters
    ----------
    two_code_country: str
        2-digit country name
    name_string: str (optional, default name_short)
        When name_short    CD -> DR Congo
        When name_official CD -> Democratic Republic of the Congo
    no_comma: bool (optional, default False)
        When true, country names with comma are extended to remove the comma.
        Example CD -> Congo, The Democratic Republic of -> The Democratic Republic of Congo
    remove_start_words: list (optional, default empty)
        When a sentence starts with any of the provided words, the beginning is removed.
        e.g. The Democratic Republic of Congo -> Democratic Republic of Congo (remove_start_words=["The"])

    Returns
    ----------
    full_name: str
        full country name
    """
    if remove_start_words is None:
        remove_start_words = list()
    if two_code_country == "SN-GM":
        return f"{two_digits_2_name_country('SN')}-{two_digits_2_name_country('GM')}"

    full_name = coco.convert(two_code_country, to=name_string)

    if no_comma:
        # separate list by delimiter
        splits = full_name.split(", ")

        # reverse the order
        splits.reverse()

        # return the merged string
        full_name = " ".join(splits)

    # when list is non-empty
    if remove_start_words:
        # loop over every provided word
        for word in remove_start_words:
            # when the full_name starts with the desired word, then remove it
            if full_name.startswith(word):
                full_name = full_name.replace(word, "", 1)

    return full_name


def country_name_2_two_digits(country_name):
    """
    Convert full country name to 2-digit country code.

    Parameters
    ----------
    country_name: str
        country name

    Returns
    ----------
    two_code_country: str
        2-digit country name
    """
    if (
        country_name
        == f"{two_digits_2_name_country('SN')}-{two_digits_2_name_country('GM')}"
    ):
        return "SN-GM"

    full_name = coco.convert(country_name, to="ISO2")
    return full_name


def read_csv_nafix(file, **kwargs):
    "Function to open a csv as pandas file and standardize the na value"
    if "keep_default_na" not in kwargs:
        kwargs["keep_default_na"] = False
    if "na_values" not in kwargs:
        kwargs["na_values"] = NA_VALUES

    if get_path_size(file) > 0:
        return pd.read_csv(file, **kwargs)
    else:
        return pd.DataFrame()


def to_csv_nafix(df, path, **kwargs):
    if "na_rep" in kwargs:
        del kwargs["na_rep"]
    # if len(df) > 0:
    if not df.empty or not df.columns.empty:
        return df.to_csv(path, **kwargs, na_rep=NA_VALUES[0])
    else:
        with open(path, "w") as fp:
            pass


def save_to_geojson(df, fn):
    pathlib.Path(fn).unlink(missing_ok=True)  # remove file if it exists

    # save file if the (Geo)DataFrame is non-empty
    if df.empty:
        # create empty file to avoid issues with snakemake
        with open(fn, "w") as fp:
            pass
    else:
        # save file
        df.to_file(fn, driver="GeoJSON")


def read_geojson(fn, cols=[], dtype=None, crs="EPSG:4326"):
    """
    Function to read a geojson file fn. When the file is empty, then an empty
    GeoDataFrame is returned having columns cols, the specified crs and the
    columns specified by the dtype dictionary it not none.

    Parameters:
    ------------
    fn : str
        Path to the file to read
    cols : list
        List of columns of the GeoDataFrame
    dtype : dict
        Dictionary of the type of the object by column
    crs : str
        CRS of the GeoDataFrame
    """
    # if the file is non-zero, read the geodataframe and return it
    if get_path_size(fn) > 0:
        return gpd.read_file(fn)
    else:
        # else return an empty GeoDataFrame
        df = gpd.GeoDataFrame(columns=cols, geometry=[], crs=crs)
        if isinstance(dtype, dict):
            for k, v in dtype.items():
                df[k] = df[k].astype(v)
        return df


def create_country_list(input, iso_coding=True):
    """
    Create a country list for defined regions..

    Parameters
    ----------
    input : str
        Any two-letter country name, regional name, or continent given in the regions config file.
        Country name duplications won't distort the result.
        Examples are:
        ["NG","ZA"], downloading osm data for Nigeria and South Africa
        ["africa"], downloading data for Africa
        ["NAR"], downloading data for the North African Power Pool
        ["TEST"], downloading data for a customized test set.
        ["NG","ZA","NG"], won't distort result.

    Returns
    -------
    full_codes_list : list
        Example ["NG","ZA"]
    """

    _logger = logging.getLogger(__name__)
    _logger.setLevel(logging.INFO)

    def filter_codes(c_list, iso_coding=True):
        """
        Filter list according to the specified coding.

        When iso code are implemented (iso_coding=True), then remove the
        geofabrik-specific ones. When geofabrik codes are
        selected(iso_coding=False), ignore iso-specific names.
        """
        if (
            iso_coding
        ):  # if country lists are in iso coding, then check if they are 2-string
            # 2-code countries
            ret_list = [c for c in c_list if len(c) == 2]

            # check if elements have been removed and return a working if so
            if len(ret_list) < len(c_list):
                _logger.warning(
                    "Specified country list contains the following non-iso codes: "
                    + ", ".join(list(set(c_list) - set(ret_list)))
                )

            return ret_list
        else:
            return c_list  # [c for c in c_list if c not in iso_to_geofk_dict]

    full_codes_list = []

    world_iso, continent_regions = read_osm_config("world_iso", "continent_regions")

    for value1 in input:
        codes_list = []
        # extract countries in world
        if value1 == "Earth":
            for continent in world_iso.keys():
                codes_list.extend(list(world_iso[continent]))

        # extract countries in continent
        elif value1 in world_iso.keys():
            codes_list = list(world_iso[value1])

        # extract countries in regions
        elif value1 in continent_regions.keys():
            codes_list = continent_regions[value1]

        # extract countries
        else:
            codes_list.extend([value1])

        # create a list with all countries
        full_codes_list.extend(codes_list)

    # Removing duplicates and filter outputs by coding
    full_codes_list = filter_codes(list(set(full_codes_list)), iso_coding=iso_coding)

    return full_codes_list


def get_last_commit_message(path):
    """
    Function to get the last PyPSA-Earth Git commit message.

    Returns
    -------
    result : string
    """
    _logger = logging.getLogger(__name__)
    last_commit_message = None
    backup_cwd = get_current_directory_path()
    try:
        os.chdir(path)
        last_commit_message = (
            subprocess.check_output(
                ["git", "log", "-n", "1", "--pretty=format:%H %s"],
                stderr=subprocess.STDOUT,
            )
            .decode()
            .strip()
        )
    except subprocess.CalledProcessError as e:
        _logger.warning(f"Error executing Git: {e}")

    os.chdir(backup_cwd)
    return last_commit_message


def get_dirname_path(path):
    """
    It returns the directory name of the path.
    """
    return pathlib.Path(path).parent


def get_abs_path(path):
    """
    It returns the absolutized version of the path.
    """
    return pathlib.Path(path).absolute()


def get_basename_abs_path(path):
    """
    It returns the base name of a normalized and absolutized version of the
    path.
    """
    return pathlib.Path(path).absolute().name


def get_basename_path(path):
    """
    It returns the base name of the path.
    """
    return pathlib.Path(path).name


def get_path(*args):
    """
    It returns a new path string.
    """
    return pathlib.Path(*args)


def get_path_size(path):
    """
    It returns the size of a path (in bytes)
    """
    return pathlib.Path(path).stat().st_size


def build_directory(path, just_parent_directory=True):
    """
    It creates recursively the directory and its leaf directories.

    Parameters:
        path (str): The path to the file
        just_parent_directory (Boolean): given a path dir/subdir
            True: it creates just the parent directory dir
            False: it creates the full directory tree dir/subdir
    """

    # Check if the provided path points to a directory
    if just_parent_directory:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    else:
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def change_to_script_dir(path):
    """
    Change the current working directory to the directory containing the given
    script.

    Parameters:
        path (str): The path to the file.
    """

    # Get the absolutized and normalized path of directory containing the file
    directory_path = pathlib.Path(path).absolute().parent

    # Change the current working directory to the script directory
    os.chdir(directory_path)


def get_current_directory_path():
    """
    It returns the current directory path.
    """
    return pathlib.Path.cwd()


def is_directory_path(path):
    """
    It returns True if the path points to a directory.

    False otherwise.
    """
    return pathlib.Path(path).is_dir()


def is_file_path(path):
    """
    It returns True if the path points to a file.

    False otherwise.
    """
    return pathlib.Path(path).is_file()


def get_relative_path(path, start_path="."):
    """
    It returns a relative path to path from start_path.

    Default for start_path is the current directory
    """
    return pathlib.Path(path).relative_to(start_path)


def path_exists(path):
    """
    It returns True if the path exists.

    False otherwise.
    """
    return pathlib.Path(path).exists()


def create_network_topology(n, prefix, connector=" <-> ", bidirectional=True):
    """
    Create a network topology like the power transmission network.

    Parameters
    ----------
    n : pypsa.Network
    prefix : str
    connector : str
    bidirectional : bool, default True
        True: one link for each connection
        False: one link for each connection and direction (back and forth)

    Returns
    -------
    pd.DataFrame with columns bus0, bus1 and length
    """

    ln_attrs = ["bus0", "bus1", "length"]
    lk_attrs = ["bus0", "bus1", "length", "underwater_fraction"]

    # TODO: temporary fix for when underwater_fraction is not found
    if "underwater_fraction" not in n.links.columns:
        if n.links.empty:
            n.links["underwater_fraction"] = None
        else:
            n.links["underwater_fraction"] = 0.0

    candidates = pd.concat(
        [n.lines[ln_attrs], n.links.loc[n.links.carrier == "DC", lk_attrs]]
    ).fillna(0)

    positive_order = candidates.bus0 < candidates.bus1
    candidates_p = candidates[positive_order]
    swap_buses = {"bus0": "bus1", "bus1": "bus0"}
    candidates_n = candidates[~positive_order].rename(columns=swap_buses)
    candidates = pd.concat([candidates_p, candidates_n])

    def make_index(c):
        return prefix + c.bus0 + connector + c.bus1

    topo = candidates.groupby(["bus0", "bus1"], as_index=False).mean()
    topo.index = topo.apply(make_index, axis=1)

    if not bidirectional:
        topo_reverse = topo.copy()
        topo_reverse.rename(columns=swap_buses, inplace=True)
        topo_reverse.index = topo_reverse.apply(make_index, axis=1)
        topo = pd.concat([topo, topo_reverse])

    return topo


def cycling_shift(df, steps=1):
    """
    Cyclic shift on index of pd.Series|pd.DataFrame by number of steps.
    """
    df = df.copy()
    new_index = np.roll(df.index, steps)
    df.values[:] = df.reindex(index=new_index).values
    return df


def download_gadm(country_code, update=False, out_logging=False):
    """
    Download gpkg file from GADM for a given country code.

    Parameters
    ----------
    country_code : str
        Two letter country codes of the downloaded files
    update : bool
        Update = true, forces re-download of files

    Returns
    -------
    gpkg file per country
    """

    gadm_filename = f"gadm36_{two_2_three_digits_country(country_code)}"
    gadm_url = f"https://biogeo.ucdavis.edu/data/gadm3.6/gpkg/{gadm_filename}_gpkg.zip"
    _logger = logging.getLogger(__name__)
    gadm_input_file_zip = get_path(
        get_current_directory_path(),
        "data",
        "raw",
        "gadm",
        gadm_filename,
        gadm_filename + ".zip",
    )  # Input filepath zip

    gadm_input_file_gpkg = get_path(
        get_current_directory_path(),
        "data",
        "raw",
        "gadm",
        gadm_filename,
        gadm_filename + ".gpkg",
    )  # Input filepath gpkg

    if not path_exists(gadm_input_file_gpkg) or update is True:
        if out_logging:
            _logger.warning(
                f"Stage 4/4: {gadm_filename} of country {two_digits_2_name_country(country_code)} does not exist, downloading to {gadm_input_file_zip}"
            )
        #  create data/osm directory
        build_directory(gadm_input_file_zip)

        with requests.get(gadm_url, stream=True) as r:
            with open(gadm_input_file_zip, "wb") as f:
                shutil.copyfileobj(r.raw, f)

        with zipfile.ZipFile(gadm_input_file_zip, "r") as zip_ref:
            zip_ref.extractall(get_dirname_path(gadm_input_file_zip))

    return gadm_input_file_gpkg, gadm_filename


def get_gadm_layer(country_list, layer_id, update=False, outlogging=False):
    """
    Function to retrieve a specific layer id of a geopackage for a selection of
    countries.

    Parameters
    ----------
    country_list : str
        List of the countries
    layer_id : int
        Layer to consider in the format GID_{layer_id}.
        When the requested layer_id is greater than the last available layer, then the last layer is selected.
        When a negative value is requested, then, the last layer is requested
    """
    # initialization of the list of geodataframes
    geodf_list = []

    for country_code in country_list:
        # download file gpkg
        file_gpkg, name_file = download_gadm(country_code, update, outlogging)

        # get layers of a geopackage
        list_layers = fiona.listlayers(file_gpkg)

        # get layer name
        if layer_id < 0 | layer_id >= len(list_layers):
            # when layer id is negative or larger than the number of layers, select the last layer
            layer_id = len(list_layers) - 1
        code_layer = np.mod(layer_id, len(list_layers))
        layer_name = (
            f"gadm36_{two_2_three_digits_country(country_code).upper()}_{code_layer}"
        )

        # read gpkg file
        geodf_temp = gpd.read_file(file_gpkg, layer=layer_name)

        # convert country name representation of the main country (GID_0 column)
        geodf_temp["GID_0"] = [
            three_2_two_digits_country(twoD_c) for twoD_c in geodf_temp["GID_0"]
        ]

        # create a subindex column that is useful
        # in the GADM processing of sub-national zones
        geodf_temp["GADM_ID"] = geodf_temp[f"GID_{code_layer}"]

        # concatenate geodataframes
        geodf_list = pd.concat([geodf_list, geodf_temp])

    geodf_gadm = gpd.GeoDataFrame(pd.concat(geodf_list, ignore_index=True))
    geodf_gadm.set_crs(geodf_list[0].crs, inplace=True)

    return geodf_gadm


def locate_bus(
    coords,
    co,
    gadm_level,
    path_to_gadm=None,
    gadm_clustering=False,
):
    """
    Function to locate the right node for a coordinate set input coords of
    point.

    Parameters
    ----------
    coords: pandas dataseries
        dataseries with 2 rows x & y representing the longitude and latitude
    co: string (code for country where coords are MA Morocco)
        code of the countries where the coordinates are
    """
    col = "name"
    if not gadm_clustering:
        gdf = gpd.read_file(path_to_gadm)
    else:
        if path_to_gadm:
            gdf = gpd.read_file(path_to_gadm)
            if "GADM_ID" in gdf.columns:
                col = "GADM_ID"

                if gdf[col][0][
                    :3
                ].isalpha():  # TODO clean later by changing all codes to 2 letters
                    gdf[col] = gdf[col].apply(
                        lambda name: three_2_two_digits_country(name[:3]) + name[3:]
                    )
        else:
            gdf = get_gadm_layer(co, gadm_level)
            col = "GID_{}".format(gadm_level)

        # gdf.set_index("GADM_ID", inplace=True)
    gdf_co = gdf[
        gdf[col].str.contains(co)
    ]  # geodataframe of entire continent - output of prev function {} are placeholders
    # in strings - conditional formatting
    # insert any variable into that place using .format - extract string and filter for those containing co (MA)
    point = Point(coords["x"], coords["y"])  # point object

    try:
        return gdf_co[gdf_co.contains(point)][
            col
        ].item()  # filter gdf_co which contains point and returns the bus

    except ValueError:
        return gdf_co[gdf_co.geometry == min(gdf_co.geometry, key=(point.distance))][
            col
        ].item()  # looks for closest one shape=node


def override_component_attrs(directory):
    """Tell PyPSA that links can have multiple outputs by
    overriding the component_attrs. This can be done for
    as many buses as you need with format busi for i = 2,3,4,5,....
    See https://pypsa.org/doc/components.html#link-with-multiple-outputs-or-inputs

    Parameters
    ----------
    directory : string
        Folder where component attributes to override are stored
        analogous to ``pypsa/component_attrs``, e.g. `links.csv`.

    Returns
    -------
    Dictionary of overridden component attributes.
    """

    attrs = Dict({k: v.copy() for k, v in component_attrs.items()})

    for component, list_name in components.list_name.items():
        fn = f"{directory}/{list_name}.csv"
        if is_file_path(fn):
            overrides = pd.read_csv(fn, index_col=0, na_values="n/a")
            attrs[component] = overrides.combine_first(attrs[component])

    return attrs


def get_conv_factors(sector):
    """
    Create a dictionary with all the conversion factors for the standard net calorific value
    from Tera Joule per Kilo Metric-ton to Tera Watt-hour based on
    https://unstats.un.org/unsd/energy/balance/2014/05.pdf.

    Considering that 1 Watt-hour = 3600 Joule, one obtains the values below dividing
    the standard net calorific values from the pdf by 3600.

    For example, the value "hard coal": 0.007167 is given by 25.8 / 3600, where 25.8 is the standard
    net calorific value.
    """

    conversion_factors_dict = {
        "additives and oxygenates": 0.008333,
        "anthracite": 0.005,
        "aviation gasoline": 0.01230,
        "bagasse": 0.002144,
        "biodiesel": 0.01022,
        "biogasoline": 0.007444,
        "bio jet kerosene": 0.011111,
        "bitumen": 0.01117,
        "brown coal": 0.003889,
        "brown coal briquettes": 0.00575,
        "charcoal": 0.00819,
        "coal tar": 0.007778,
        "coke-oven coke": 0.0078334,
        "coke-oven gas": 0.000277,
        "coking coal": 0.007833,
        "conventional crude oil": 0.01175,
        "crude petroleum": 0.011750,
        "ethane": 0.01289,
        "fuel oil": 0.01122,
        "fuelwood": 0.00254,
        "gas coke": 0.007326,
        "gas oil/ diesel oil": 0.01194,
        "gasoline-type jet fuel": 0.01230,
        "hard coal": 0.007167,
        "kerosene-type jet fuel": 0.01225,
        "lignite": 0.003889,
        "liquefied petroleum gas (lpg)": 0.01313,
        "lubricants": 0.011166,
        "motor gasoline": 0.01230,
        "naphtha": 0.01236,
        "natural gas": 0.00025,
        "natural gas liquids": 0.01228,
        "oil shale": 0.00247,
        "other bituminous coal": 0.005556,
        "paraffin waxes": 0.01117,
        "patent fuel": 0.00575,
        "peat": 0.00271,
        "peat products": 0.00271,
        "petroleum coke": 0.009028,
        "refinery gas": 0.01375,
        "sub-bituminous coal": 0.005555,
    }

    if sector == "industry":
        return conversion_factors_dict
    else:
        logger.info(f"No conversion factors available for sector {sector}")
        return np.nan


def aggregate_fuels(sector):
    gas_fuels = [
        "blast furnace gas",
        "natural gas (including lng)",
        "natural gas liquids",
    ]

    oil_fuels = [
        "bitumen",
        "conventional crude oil",
        "crude petroleum",
        "ethane",
        "fuel oil",
        "gas oil/ diesel oil",
        "kerosene-type jet fuel",
        "liquefied petroleum gas (lpg)",
        "lubricants",
        "motor gasoline",
        "naphtha",
        "patent fuel",
        "petroleum coke",
        "refinery gas",
    ]

    coal_fuels = [
        "anthracite",
        "brown coal",
        "brown coal briquettes",
        "coke-oven coke",
        "coke-oven gas",
        "coking coal",
        "gas coke",
        "gasworks gas",
        "hard coal",
        "lignite",
        "other bituminous coal",
        "peat",
        "peat products",
        "sub-bituminous coal",
    ]

    biomass_fuels = [
        "bagasse",
        "fuelwood",
        "biogases",
        "biogasoline",
        "biodiesel",
        "charcoal",
        "black liquor",
    ]

    electricity = ["electricity"]

    heat = ["heat", "direct use of geothermal heat", "direct use of solar thermal heat"]

    if sector == "industry":
        return gas_fuels, oil_fuels, biomass_fuels, coal_fuels, heat, electricity
    else:
        logger.info(f"No fuels available for sector {sector}")
        return np.nan


def modify_commodity(commodity):
    if commodity.strip() == "Hrad coal":
        commodity = "Hard coal"
    elif commodity.strip().casefold() == "coke oven gas":
        commodity = "Coke-oven gas"
    elif commodity.strip().casefold() == "coke oven coke":
        commodity = "Coke-oven coke"
    elif commodity.strip() == "Liquified Petroleum Gas (LPG)":
        commodity = "Liquefied Petroleum Gas (LPG)"
    elif commodity.strip() == "Gas Oil/Diesel Oil":
        commodity = "Gas Oil/ Diesel Oil"
    elif commodity.strip() == "Lignite brown coal- recoverable resources":
        commodity = "Lignite brown coal - recoverable resources"
    return commodity.strip().casefold()


def safe_divide(numerator, denominator):
    """
    Safe division function that returns NaN when the denominator is zero.
    """
    if denominator != 0.0:
        return numerator / denominator
    else:
        logging.warning(
            f"Division by zero: {numerator} / {denominator}, returning NaN."
        )
        return np.nan
