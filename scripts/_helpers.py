# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-

import io
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import country_converter as coco
import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import yaml
from fake_useragent import UserAgent
from pypsa.components import component_attrs, components
from shapely.geometry import Point

logger = logging.getLogger(__name__)

# list of recognised nan values (NA and na excluded as may be confused with Namibia 2-letter country code)
NA_VALUES = ["NULL", "", "N/A", "NAN", "NaN", "nan", "Nan", "n/a", "null"]

REGION_COLS = ["geometry", "name", "x", "y", "country"]

# filename of the regions definition config file
REGIONS_CONFIG = "regions_definition_config.yaml"

# prefix when running pypsa-earth rules in different directories (if running in pypsa-earth as subworkflow)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# absolute path to config.default.yaml
CONFIG_DEFAULT_PATH = os.path.join(BASE_DIR, "config.default.yaml")
CONFIGS_STORE_PATH = os.path.join(BASE_DIR, "config.default.yaml")


def write_config(config):
    # TODO Add licensing information
    with open("config_test.yaml", "w") as outfile:
        yaml.dump(config, outfile, default_flow_style=False)


def check_config_version(config, fp_config=CONFIG_DEFAULT_PATH):
    """
    Check that a version of the local config.yaml matches to the actual config
    version as defined in config.default.yaml.
    """

    # using snakemake capabilities to deal with yanl configs
    with open(fp_config, "r") as f:
        actual_config = yaml.safe_load(f)
    actual_config_version = actual_config.get("version")

    current_config_version = config.get("version")

    if actual_config_version != current_config_version:
        logger.error(
            f"The current version of 'config.yaml' doesn't match to the code version:\n\r"
            f" {current_config_version} provided, {actual_config_version} expected.\n\r"
            f"That can lead to weird errors during execution of the workflow.\n\r"
            f"Please update 'config.yaml' according to 'config.default.yaml' ."
        )


def handle_exception(exc_type, exc_value, exc_traceback):
    """
    Customise errors traceback.
    """
    tb = exc_traceback
    while tb.tb_next:
        tb = tb.tb_next
    flname = tb.tb_frame.f_globals.get("__file__")
    funcname = tb.tb_frame.f_code.co_name

    if issubclass(exc_type, KeyboardInterrupt):
        logger.error(
            "Manual interruption %r, function %r: %s",
            flname,
            funcname,
            exc_value,
        )
    else:
        logger.error(
            "An error happened in module %r, function %r: %s",
            flname,
            funcname,
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )


def copy_default_files():
    fn = Path(os.path.join(BASE_DIR, "config.yaml"))
    if not fn.exists():
        fn.write_text(
            "# Write down config entries differing from config.default.yaml\n\nrun: {}"
        )


def create_logger(logger_name, level=logging.INFO):
    """
    Create a logger for a module and adds a handler needed to capture in logs
    traceback from exceptions emerging during the workflow.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    handler = logging.StreamHandler(stream=sys.stdout)
    logger.addHandler(handler)
    sys.excepthook = handle_exception
    return logger


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
        base_folder = os.path.dirname(__file__)
        if not os.path.exists(os.path.join(base_folder, "configs")):
            base_folder = os.path.dirname(base_folder)
    else:
        base_folder = os.getcwd()
    osm_config_path = os.path.join(base_folder, "configs", REGIONS_CONFIG)
    with open(osm_config_path, "r") as f:
        osm_config = yaml.safe_load(f)
    if len(args) == 0:
        return osm_config
    elif len(args) == 1:
        return osm_config[args[0]]
    else:
        return tuple([osm_config[a] for a in args])


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
    import logging

    kwargs = snakemake.config.get("logging", dict()).copy()
    kwargs.setdefault("level", "INFO")

    if skip_handlers is False:
        fallback_path = Path(__file__).parent.joinpath(
            "..", "logs", f"{snakemake.rule}.log"
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
    import pypsa
    from pypsa.descriptors import Dict

    override_components = None
    override_component_attrs = None

    if custom_components is not None:
        override_components = pypsa.components.components.copy()
        override_component_attrs = Dict(
            {k: v.copy() for k, v in pypsa.components.component_attrs.items()}
        )
        for k, v in custom_components.items():
            override_components.loc[k] = v["component"]
            override_component_attrs[k] = pd.DataFrame(
                columns=["type", "unit", "default", "description", "status"]
            )
            for attr, val in v["attributes"].items():
                override_component_attrs[k].loc[attr] = val

    return pypsa.Network(
        import_name=import_name,
        override_components=override_components,
        override_component_attrs=override_component_attrs,
    )


def pdbcast(v, h):
    return pd.DataFrame(
        v.values.reshape((-1, 1)) * h.values, index=v.index, columns=h.index
    )


def load_network_for_plots(
    fn, tech_costs, cost_config, elec_config, combine_hydro_ps=True
):
    import pypsa
    from add_electricity import load_costs, update_transmission_costs

    n = pypsa.Network(fn)

    n.loads["carrier"] = n.loads.bus.map(n.buses.carrier) + " load"
    n.stores["carrier"] = n.stores.bus.map(n.buses.carrier)

    n.links["carrier"] = (
        n.links.bus0.map(n.buses.carrier) + "-" + n.links.bus1.map(n.buses.carrier)
    )
    n.lines["carrier"] = "AC line"
    n.transformers["carrier"] = "AC transformer"

    n.lines["s_nom"] = n.lines["s_nom_min"]
    n.links["p_nom"] = n.links["p_nom_min"]

    if combine_hydro_ps:
        n.storage_units.loc[
            n.storage_units.carrier.isin({"PHS", "hydro"}), "carrier"
        ] = "hydro+PHS"

    # if the carrier was not set on the heat storage units
    # bus_carrier = n.storage_units.bus.map(n.buses.carrier)
    # n.storage_units.loc[bus_carrier == "heat","carrier"] = "water tanks"

    Nyears = n.snapshot_weightings.objective.sum() / 8760.0
    costs = load_costs(tech_costs, cost_config, elec_config, Nyears)
    update_transmission_costs(n, costs)

    return n


def update_p_nom_max(n):
    # if extendable carriers (solar/onwind/...) have capacity >= 0,
    # e.g. existing assets from the OPSD project are included to the network,
    # the installed capacity might exceed the expansion limit.
    # Hence, we update the assumptions.

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
    components = dict(
        Link=("p_nom", "p0"),
        Generator=("p_nom", "p"),
        StorageUnit=("p_nom", "p"),
        Store=("e_nom", "p"),
        Line=("s_nom", None),
        Transformer=("s_nom", None),
    )

    costs = {}
    for c, (p_nom, p_attr) in zip(
        n.iterate_components(components.keys(), skip_empty=False), components.values()
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
    url, file, data=None, headers=None, disable_progress=False, roundto=1.0
):
    """
    Function to download data from a url with a progress bar progress in
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
    roundto : float
        (default 0) Precision used to report the progress
        e.g. 0.1 stands for 88.1, 10 stands for 90, 80
    """
    import urllib

    from tqdm import tqdm

    pbar = tqdm(total=100, disable=disable_progress)

    def dlProgress(count, blockSize, totalSize, roundto=roundto):
        pbar.n = round(count * blockSize * 100 / totalSize / roundto) * roundto
        pbar.refresh()

    if data is not None:
        data = urllib.parse.urlencode(data).encode()

    if headers:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            with open(file, "wb") as f:
                f.write(response.read())

    else:
        urllib.request.urlretrieve(url, file, reporthook=dlProgress, data=data)


def content_retrieve(url, data=None, headers=None, max_retries=3, backoff_factor=0.3):
    """
    Retrieve the content of a url with improved robustness.

    This function uses a more robust approach to handle permission issues
    and avoid being blocked by the server. It implements exponential backoff
    for retries and rotates user agents.

    Parameters
    ----------
    url : str
        URL to retrieve the content from
    data : dict, optional
        Data for the request, by default None
    headers : dict, optional
        Headers for the request, defaults to a fake user agent
        If no headers are wanted at all, pass an empty dict.
    max_retries : int, optional
        Maximum number of retries, by default 3
    backoff_factor : float, optional
        Factor to apply between attempts, by default 0.3
    """
    if headers is None:
        ua = UserAgent()
        headers = {
            "User-Agent": ua.random,
            "Upgrade-Insecure-Requests": "1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Referer": "https://www.google.com/",
        }

    session = requests.Session()

    for i in range(max_retries):
        try:
            response = session.get(url, headers=headers, data=data)
            response.raise_for_status()
            return io.BytesIO(response.content)
        except (
            requests.exceptions.RequestException,
            requests.exceptions.HTTPError,
        ) as e:
            if i == max_retries - 1:  # last attempt
                raise
            else:
                # Exponential backoff
                wait_time = backoff_factor * (2**i) + np.random.uniform(0, 0.1)
                time.sleep(wait_time)

                # Rotate user agent for next attempt
                headers["User-Agent"] = UserAgent().random

    raise Exception("Max retries exceeded")


def get_aggregation_strategies(aggregation_strategies):
    """
    Default aggregation strategies that cannot be defined in .yaml format must
    be specified within the function, otherwise (when defaults are passed in
    the function's definition) they get lost when custom values are specified
    in the config.
    """
    import numpy as np

    # to handle the new version of PyPSA.
    try:
        from pypsa.clustering.spatial import _make_consense
    except Exception:
        # TODO: remove after new release and update minimum pypsa version
        from pypsa.clustering.spatial import _make_consense

    bus_strategies = dict(country=_make_consense("Bus", "country"))
    bus_strategies.update(aggregation_strategies.get("buses", {}))

    generator_strategies = {"build_year": lambda x: 0, "lifetime": lambda x: np.inf}
    generator_strategies.update(aggregation_strategies.get("generators", {}))

    return bus_strategies, generator_strategies


def mock_snakemake(
    rulename, root_dir=None, submodule_dir=None, configfile=None, **wildcards
):
    """
    This function is expected to be executed from the "scripts"-directory of "
    the snakemake project. It returns a snakemake.script.Snakemake object,
    based on the Snakefile.

    If a rule has wildcards, you have to specify them in **wildcards**.

    Parameters
    ----------
    rulename: str
        name of the rule for which the snakemake object should be generated
    configfile: str
        path to config file to be used in mock_snakemake
    wildcards:
        keyword arguments fixing the wildcards. Only necessary if wildcards are
        needed.
    """
    import os

    import snakemake as sm
    from pypsa.descriptors import Dict
    from snakemake.script import Snakemake

    script_dir = Path(__file__).parent.resolve()
    if root_dir is None:
        root_dir = script_dir.parent
    else:
        root_dir = Path(root_dir).resolve()

    user_in_script_dir = Path.cwd().resolve() == script_dir
    if str(submodule_dir) in __file__:
        # the submodule_dir path is only need to locate the project dir
        os.chdir(Path(__file__[: __file__.find(str(submodule_dir))]))
    elif user_in_script_dir:
        os.chdir(root_dir)
    elif Path.cwd().resolve() != root_dir:
        raise RuntimeError(
            "mock_snakemake has to be run from the repository root"
            f" {root_dir} or scripts directory {script_dir}"
        )
    try:
        for p in sm.SNAKEFILE_CHOICES:
            if os.path.exists(p):
                snakefile = p
                break

        if isinstance(configfile, str):
            with open(configfile, "r") as file:
                configfile = yaml.safe_load(file)

        workflow = sm.Workflow(
            snakefile,
            overwrite_configfiles=[],
            rerun_triggers=[],
            overwrite_config=configfile,
        )
        workflow.include(snakefile)
        workflow.global_resources = {}
        try:
            rule = workflow.get_rule(rulename)
        except Exception as exception:
            print(
                exception,
                f"The {rulename} might be a conditional rule in the Snakefile.\n"
                f"Did you enable {rulename} in the config?",
            )
            raise
        dag = sm.dag.DAG(workflow, rules=[rule])
        wc = Dict(wildcards)
        job = sm.jobs.Job(rule, dag, wc)

        def make_accessable(*ios):
            for io in ios:
                for i in range(len(io)):
                    io[i] = os.path.abspath(io[i])

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
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    finally:
        if user_in_script_dir:
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


def two_digits_2_name_country(two_code_country, nocomma=False, remove_start_words=[]):
    """
    Convert 2-digit country code to full name country:

    Parameters
    ----------
    two_code_country: str
        2-digit country name
    nocomma: bool (optional, default False)
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
    if two_code_country == "SN-GM":
        return f"{two_digits_2_name_country('SN')}-{two_digits_2_name_country('GM')}"

    full_name = coco.convert(two_code_country, to="name_short")

    if nocomma:
        # separate list by delim
        splits = full_name.split(", ")

        # reverse the order
        splits.reverse()

        # return the merged string
        full_name = " ".join(splits)

    # when list is non empty
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

    if os.stat(file).st_size > 0:
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
    if os.path.exists(fn):
        os.unlink(fn)  # remove file if it exists

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
    if os.path.getsize(fn) > 0:
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
    import logging

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
    backup_cwd = os.getcwd()
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


# PYPSA-EARTH-SEC
def annuity(n, r):
    """
    Calculate the annuity factor for an asset with lifetime n years and.

    discount rate of r, e.g. annuity(20, 0.05) * 20 = 1.6
    """

    if isinstance(r, pd.Series):
        return pd.Series(1 / n, index=r.index).where(
            r == 0, r / (1.0 - 1.0 / (1.0 + r) ** n)
        )
    elif r > 0:
        return r / (1.0 - 1.0 / (1.0 + r) ** n)
    else:
        return 1 / n


def prepare_costs(
    cost_file: str, USD_to_EUR: float, fill_values: dict, Nyears: float | int = 1
):
    # set all asset costs and other parameters
    costs = pd.read_csv(cost_file, index_col=[0, 1]).sort_index()

    # correct units to MW and EUR
    costs.loc[costs.unit.str.contains("/kW"), "value"] *= 1e3
    costs.loc[costs.unit.str.contains("USD"), "value"] *= USD_to_EUR

    # min_count=1 is important to generate NaNs which are then filled by fillna
    costs = (
        costs.loc[:, "value"].unstack(level=1).groupby("technology").sum(min_count=1)
    )
    costs = costs.fillna(fill_values)

    def annuity_factor(v):
        return annuity(v["lifetime"], v["discount rate"]) + v["FOM"] / 100

    costs["fixed"] = [
        annuity_factor(v) * v["investment"] * Nyears for i, v in costs.iterrows()
    ]

    return costs


def create_network_topology(
    n, prefix, like="ac", connector=" <-> ", bidirectional=True
):
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


def create_dummy_data(n, sector, carriers):
    ind = n.buses_t.p.index
    ind = n.buses.index[n.buses.carrier == "AC"]

    if sector == "industry":
        col = [
            "electricity",
            "coal",
            "coke",
            "solid biomass",
            "methane",
            "hydrogen",
            "low-temperature heat",
            "naphtha",
            "process emission",
            "process emission from feedstock",
            "current electricity",
        ]
    else:
        raise Exception("sector not found")
    data = (
        np.random.randint(10, 500, size=(len(ind), len(col))) * 1000 * 1
    )  # TODO change 1 with temp. resolution

    return pd.DataFrame(data, index=ind, columns=col)


# def create_transport_data_dummy(pop_layout,
#                                 transport_data,
#                                 cars=4000000,
#                                 average_fuel_efficiency=0.7):

#     for country in pop_layout.ct.unique():

#         country_data = pd.DataFrame(
#             data=[[cars, average_fuel_efficiency]],
#             columns=transport_data.columns,
#             index=[country],
#         )
#         transport_data = pd.concat([transport_data, country_data], axis=0)

#     transport_data_dummy = transport_data

#     return transport_data_dummy

# def create_temperature_dummy(pop_layout, temperature):

#     temperature_dummy = pd.DataFrame(index=temperature.index)

#     for index in pop_layout.index:
#         temperature_dummy[index] = temperature["ES0 0"]

#     return temperature_dummy

# def create_energy_totals_dummy(pop_layout, energy_totals):
#     """
#     Function to add additional countries specified in pop_layout.index to energy_totals, these countries take the same values as Spain
#     """
#     # All countries in pop_layout get the same values as Spain
#     for country in pop_layout.ct.unique():
#         energy_totals.loc[country] = energy_totals.loc["ES"]

#     return energy_totals


def cycling_shift(df, steps=1):
    """
    Cyclic shift on index of pd.Series|pd.DataFrame by number of steps.
    """
    df = df.copy()
    new_index = np.roll(df.index, steps)
    df.values[:] = df.reindex(index=new_index).values
    return df


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

    attrs = {k: v.copy() for k, v in component_attrs.items()}

    for component, list_name in components.list_name.items():
        fn = f"{directory}/{list_name}.csv"
        if os.path.isfile(fn):
            overrides = pd.read_csv(fn, index_col=0, na_values="n/a")
            attrs[component] = overrides.combine_first(attrs[component])

    return attrs


def get_country(target, **keys):
    """
    Function to convert country codes using pycountry.

    Parameters
    ----------
    target: str
        Desired type of country code.
        Examples:
        - 'alpha_3' for 3-digit
        - 'alpha_2' for 2-digit
        - 'name' for full country name
    keys: dict
        Specification of the country name and reference system.
        Examples:
        - alpha_3="ZAF" for 3-digit
        - alpha_2="ZA" for 2-digit
        - name="South Africa" for full country name

    Returns
    -------
    country code as requested in keys or np.nan, when country code is not recognized

    Example of usage
    -------
    - Convert 2-digit code to 3-digit codes: get_country('alpha_3', alpha_2="ZA")
    - Convert 3-digit code to 2-digit codes: get_country('alpha_2', alpha_3="ZAF")
    - Convert 2-digit code to full name: get_country('name', alpha_2="ZA")
    """
    import pycountry as pyc

    assert len(keys) == 1
    try:
        return getattr(pyc.countries.get(**keys), target)
    except (KeyError, AttributeError):
        return np.nan


def download_GADM(country_code, update=False, out_logging=False):
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

    GADM_filename = f"gadm36_{two_2_three_digits_country(country_code)}"
    GADM_url = f"https://biogeo.ucdavis.edu/data/gadm3.6/gpkg/{GADM_filename}_gpkg.zip"
    _logger = logging.getLogger(__name__)
    GADM_inputfile_zip = os.path.join(
        os.getcwd(),
        "data",
        "raw",
        "gadm",
        GADM_filename,
        GADM_filename + ".zip",
    )  # Input filepath zip

    GADM_inputfile_gpkg = os.path.join(
        os.getcwd(),
        "data",
        "raw",
        "gadm",
        GADM_filename,
        GADM_filename + ".gpkg",
    )  # Input filepath gpkg

    if not os.path.exists(GADM_inputfile_gpkg) or update is True:
        if out_logging:
            _logger.warning(
                f"Stage 4/4: {GADM_filename} of country {two_digits_2_name_country(country_code)} does not exist, downloading to {GADM_inputfile_zip}"
            )
        #  create data/osm directory
        os.makedirs(os.path.dirname(GADM_inputfile_zip), exist_ok=True)

        with requests.get(GADM_url, stream=True) as r:
            with open(GADM_inputfile_zip, "wb") as f:
                shutil.copyfileobj(r.raw, f)

        with zipfile.ZipFile(GADM_inputfile_zip, "r") as zip_ref:
            zip_ref.extractall(os.path.dirname(GADM_inputfile_zip))

    return GADM_inputfile_gpkg, GADM_filename


def get_GADM_layer(country_list, layer_id, update=False, outlogging=False):
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
        file_gpkg, name_file = download_GADM(country_code, update, outlogging)

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

    geodf_GADM = gpd.GeoDataFrame(pd.concat(geodf_list, ignore_index=True))
    geodf_GADM.set_crs(geodf_list[0].crs, inplace=True)

    return geodf_GADM


def locate_bus(
    coords,
    co,
    gadm_level,
    path_to_gadm=None,
    gadm_clustering=False,
    col="name",
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
            gdf = get_GADM_layer(co, gadm_level)
            col = "GID_{}".format(gadm_level)

        # gdf.set_index("GADM_ID", inplace=True)
    gdf_co = gdf[
        gdf[col].str.contains(co)
    ]  # geodataframe of entire continent - output of prev function {} are placeholders
    # in strings - conditional formatting
    # insert any variable into that place using .format - extract string and filter for those containing co (MA)
    point = Point(coords["x"], coords["y"])  # point object

    if gdf_co.empty:
        return None

    try:
        return gdf_co[gdf_co.contains(point)][
            col
        ].item()  # filter gdf_co which contains point and returns the bus

    except ValueError:
        return gdf_co[gdf_co.geometry == min(gdf_co.geometry, key=(point.distance))][
            col
        ].item()  # looks for closest one shape=node


def get_conv_factors(sector):
    # Create a dictionary with all the conversion factors from ktons or m3 to TWh based on https://unstats.un.org/unsd/energy/balance/2014/05.pdf
    if sector == "industry":
        fuels_conv_toTWh = {
            "Gas Oil/ Diesel Oil": 0.01194,
            "Motor Gasoline": 0.01230,
            "Kerosene-type Jet Fuel": 0.01225,
            "Aviation gasoline": 0.01230,
            "Biodiesel": 0.01022,
            "Natural gas liquids": 0.01228,
            "Biogasoline": 0.007444,
            "Bitumen": 0.01117,
            "Fuel oil": 0.01122,
            "Liquefied petroleum gas (LPG)": 0.01313,
            "Liquified Petroleum Gas (LPG)": 0.01313,
            "Lubricants": 0.01117,
            "Naphtha": 0.01236,
            "Fuelwood": 0.00254,
            "Charcoal": 0.00819,
            "Patent fuel": 0.00575,
            "Brown coal briquettes": 0.00575,
            "Hard coal": 0.007167,
            "Hrad coal": 0.007167,
            "Other bituminous coal": 0.005556,
            "Anthracite": 0.005,
            "Peat": 0.00271,
            "Peat products": 0.00271,
            "Lignite": 0.003889,
            "Brown coal": 0.003889,
            "Sub-bituminous coal": 0.005555,
            "Coke-oven coke": 0.0078334,
            "Coke oven coke": 0.0078334,
            "Coke Oven Coke": 0.0078334,
            "Gasoline-type jet fuel": 0.01230,
            "Conventional crude oil": 0.01175,
            "Brown Coal Briquettes": 0.00575,
            "Refinery Gas": 0.01375,
            "Petroleum coke": 0.009028,
            "Coking coal": 0.007833,
            "Peat Products": 0.00271,
            "Petroleum Coke": 0.009028,
            "Additives and Oxygenates": 0.008333,
            "Bagasse": 0.002144,
            "Bio jet kerosene": 0.011111,
            "Crude petroleum": 0.011750,
            "Gas coke": 0.007326,
            "Gas Coke": 0.007326,
            "Refinery gas": 0.01375,
            "Coal Tar": 0.007778,
            "Paraffin waxes": 0.01117,
            "Ethane": 0.01289,
            "Oil shale": 0.00247,
            "Other kerosene": 0.01216,
        }
    return fuels_conv_toTWh


def aggregate_fuels(sector):
    gas_fuels = [
        "Natural gas (including LNG)",  #
        "Natural Gas (including LNG)",  #
    ]

    oil_fuels = [
        "Motor Gasoline",  ##
        "Liquefied petroleum gas (LPG)",  ##
        "Liquified Petroleum Gas (LPG)",  ##
        "Fuel oil",  ##
        "Kerosene-type Jet Fuel",  ##
        "Conventional crude oil",  #
        "Crude petroleum",  ##
        "Lubricants",
        "Naphtha",  ##
        "Gas Oil/ Diesel Oil",  ##
        "Petroleum coke",  ##
        "Petroleum Coke",  ##
        "Ethane",  ##
        "Bitumen",  ##
        "Refinery gas",  ##
        "Additives and Oxygenates",  #
        "Refinery Gas",  ##
        "Aviation gasoline",  ##
        "Gasoline-type jet fuel",  ##
        "Paraffin waxes",  ##
        "Natural gas liquids",  #
        "Other kerosene",
    ]

    biomass_fuels = [
        "Bagasse",  #
        "Fuelwood",  #
        "Biogases",
        "Biogasoline",  #
        "Biodiesel",  #
        "Charcoal",  #
        "Black Liquor",  #
        "Bio jet kerosene",  #
        "Animal waste",  #
        "Industrial Waste",  #
        "Industrial waste",
        "Municipal Wastes",  #
        "Vegetal waste",
    ]

    coal_fuels = [
        "Anthracite",
        "Brown coal",  #
        "Brown coal briquettes",  #
        "Coke oven coke",
        "Coke-oven coke",
        "Coke Oven Coke",
        "Coking coal",
        "Hard coal",  #
        "Hrad coal",  #
        "Other bituminous coal",
        "Sub-bituminous coal",
        "Coking coal",
        "Coke Oven Gas",  ##
        "Gas Coke",
        "Gasworks Gas",  ##
        "Lignite",  #
        "Peat",  #
        "Peat products",
        "Coal Tar",  ##
        "Brown Coal Briquettes",  ##
        "Gas coke",
        "Peat Products",
        "Oil shale",  #
        "Oil Shale",  #
        "Coal coke",  ##
        "Patent fuel",  ##
        "Blast Furnace Gas",  ##
        "Recovered gases",  ##
    ]

    electricity = ["Electricity"]

    heat = ["Heat", "Direct use of geothermal heat", "Direct use of solar thermal heat"]

    return gas_fuels, oil_fuels, biomass_fuels, coal_fuels, heat, electricity


def safe_divide(numerator, denominator, default_value=np.nan):
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
