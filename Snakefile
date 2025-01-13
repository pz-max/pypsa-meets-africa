# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys

sys.path.append("./scripts")

from os.path import normpath, exists, isdir
from shutil import copyfile, move

from snakemake.remote.HTTP import RemoteProvider as HTTPRemoteProvider

from _helpers import (
    create_country_list,
    get_last_commit_message,
    check_config_version,
    copy_default_files,
    write_config,
)
from build_demand_profiles import get_load_paths_gegis
from retrieve_databundle_light import datafiles_retrivedatabundle
from pathlib import Path


HTTP = HTTPRemoteProvider()

copy_default_files()


# configfile: "config.default.yaml"
configfile: "configs/bundle_config.yaml"
configfile: "configs/powerplantmatching_config.yaml"
configfile: "configs/config.main.yaml"
configfile: "configs/config.spatial.yaml"
configfile: "configs/config.weather.yaml"
configfile: "configs/config.elec.yaml"
configfile: "configs/config.sector.yaml"
configfile: "configs/config.costs.yaml"
configfile: "configs/config.solve.yaml"
configfile: "configs/config.plot.yaml"
configfile: "config.yaml"


write_config(config)
# check_config_version(config=config)

config.update({"git_commit": get_last_commit_message(".")})

# convert country list according to the desired region
config["countries"] = create_country_list(config["countries"])

# create a list of iteration steps, required to solve the experimental design
# each value is used as wildcard input e.g. solution_{unc}
config["scenario"]["unc"] = [
    f"m{i}" for i in range(config["monte_carlo"]["options"]["samples"])
]


run = config.get("run", {})
RDIR = run["name"] + "/" if run.get("name") else ""
CDIR = RDIR if not run.get("shared_cutouts") else ""
SECDIR = run["sector_name"] + "/" if run.get("sector_name") else ""
SDIR = config["summary_dir"].strip("/") + f"/{SECDIR}"
RESDIR = config["results_dir"].strip("/") + f"/{SECDIR}"

load_data_paths = get_load_paths_gegis("data", config)

if config["enable"].get("retrieve_cost_data", True):
    COSTS = "resources/" + RDIR + f"costs_{config['costs']['year']}.csv"
else:
    COSTS = "data/costs.csv"
ATLITE_NPROCESSES = config["atlite"].get("nprocesses", 4)


wildcard_constraints:
    simpl="[a-zA-Z0-9]*|all",
    clusters="[0-9]+(m|flex)?|all|min",
    ll="(v|c)([0-9\.]+|opt|all)|all",
    opts="[-+a-zA-Z0-9\.]*",
    unc="[-+a-zA-Z0-9\.]*",
    sopts="[-+a-zA-Z0-9\.\s]*",
    discountrate="[-+a-zA-Z0-9\.\s]*",
    demand="[-+a-zA-Z0-9\.\s]*",
    h2export="[0-9]+m?|all",
    planning_horizons="20[2-9][0-9]|2100",


if config["custom_rules"] is not []:
    for rule in config["custom_rules"]:

        include: rule


rule clean:
    run:
        try:
            shell("snakemake -j 1 solve_all_networks --delete-all-output")
        except:
            shell("snakemake -j 1 solve_all_networks_monte --delete-all-output")
            pass
        shell("snakemake -j 1 run_all_scenarios --delete-all-output")


rule solve_all_networks:
    input:
        expand(
            "results/" + RDIR + "networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
            **config["scenario"],
        ),


rule plot_all_p_nom:
    input:
        expand(
            "results/"
            + RDIR
            + "plots/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_p_nom.{ext}",
            **config["scenario"],
            ext=["png", "pdf"],
        ),


rule make_all_summaries:
    input:
        expand(
            "results/"
            + RDIR
            + "summaries/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}",
            **config["scenario"],
            country=["all"] + config["countries"],
        ),


rule plot_all_summaries:
    input:
        expand(
            "results/"
            + RDIR
            + "plots/summary_{summary}_elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}.{ext}",
            summary=["energy", "costs"],
            **config["scenario"],
            country=["all"] + config["countries"],
            ext=["png", "pdf"],
        ),


if config["enable"].get("retrieve_databundle", True):

    rule retrieve_databundle_light:
        params:
            countries=config["countries"],
            tutorial=config["tutorial"],
            hydrobasins_level=config["renewable"]["hydro"]["hydrobasins_level"],
        output:  #expand(directory('{file}') if isdir('{file}') else '{file}', file=datafiles)
            expand("{file}", file=datafiles_retrivedatabundle(config)),
            directory("data/landcover"),
        log:
            "logs/" + RDIR + "retrieve_databundle.log",
        benchmark:
            "benchmarks/" + RDIR + "retrieve_databundle_light"
        script:
            "scripts/retrieve_databundle_light.py"


if config["enable"].get("download_osm_data", True):

    rule download_osm_data:
        params:
            countries=config["countries"],
        output:
            cables="resources/" + RDIR + "osm/raw/all_raw_cables.geojson",
            generators="resources/" + RDIR + "osm/raw/all_raw_generators.geojson",
            generators_csv="resources/" + RDIR + "osm/raw/all_raw_generators.csv",
            lines="resources/" + RDIR + "osm/raw/all_raw_lines.geojson",
            substations="resources/" + RDIR + "osm/raw/all_raw_substations.geojson",
        log:
            "logs/" + RDIR + "download_osm_data.log",
        benchmark:
            "benchmarks/" + RDIR + "download_osm_data"
        script:
            "scripts/download_osm_data.py"


rule clean_osm_data:
    params:
        crs=config["crs"],
        clean_osm_data_options=config["clean_osm_data_options"],
    input:
        cables="resources/" + RDIR + "osm/raw/all_raw_cables.geojson",
        generators="resources/" + RDIR + "osm/raw/all_raw_generators.geojson",
        lines="resources/" + RDIR + "osm/raw/all_raw_lines.geojson",
        substations="resources/" + RDIR + "osm/raw/all_raw_substations.geojson",
        country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
        offshore_shapes="resources/" + RDIR + "shapes/offshore_shapes.geojson",
        africa_shape="resources/" + RDIR + "shapes/africa_shape.geojson",
    output:
        generators="resources/" + RDIR + "osm/clean/all_clean_generators.geojson",
        generators_csv="resources/" + RDIR + "osm/clean/all_clean_generators.csv",
        lines="resources/" + RDIR + "osm/clean/all_clean_lines.geojson",
        substations="resources/" + RDIR + "osm/clean/all_clean_substations.geojson",
    log:
        "logs/" + RDIR + "clean_osm_data.log",
    benchmark:
        "benchmarks/" + RDIR + "clean_osm_data"
    script:
        "scripts/clean_osm_data.py"


rule build_osm_network:
    params:
        build_osm_network=config.get("build_osm_network", {}),
        countries=config["countries"],
        crs=config["crs"],
    input:
        generators="resources/" + RDIR + "osm/clean/all_clean_generators.geojson",
        lines="resources/" + RDIR + "osm/clean/all_clean_lines.geojson",
        substations="resources/" + RDIR + "osm/clean/all_clean_substations.geojson",
        country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
    output:
        lines="resources/" + RDIR + "base_network/all_lines_build_network.csv",
        converters="resources/" + RDIR + "base_network/all_converters_build_network.csv",
        transformers="resources/"
        + RDIR
        + "base_network/all_transformers_build_network.csv",
        substations="resources/" + RDIR + "base_network/all_buses_build_network.csv",
    log:
        "logs/" + RDIR + "build_osm_network.log",
    benchmark:
        "benchmarks/" + RDIR + "build_osm_network"
    script:
        "scripts/build_osm_network.py"


rule build_shapes:
    params:
        build_shape_options=config["build_shape_options"],
        crs=config["crs"],
        countries=config["countries"],
    input:
        # naturalearth='data/bundle/naturalearth/ne_10m_admin_0_countries.shp',
        # eez='data/bundle/eez/World_EEZ_v8_2014.shp',
        # nuts3='data/bundle/NUTS_2013_60M_SH/data/NUTS_RG_60M_2013.shp',
        # nuts3pop='data/bundle/nama_10r_3popgdp.tsv.gz',
        # nuts3gdp='data/bundle/nama_10r_3gdp.tsv.gz',
        eez="data/eez/eez_v11.gpkg",
    output:
        country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
        offshore_shapes="resources/" + RDIR + "shapes/offshore_shapes.geojson",
        africa_shape="resources/" + RDIR + "shapes/africa_shape.geojson",
        gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
    log:
        "logs/" + RDIR + "build_shapes.log",
    benchmark:
        "benchmarks/" + RDIR + "build_shapes"
    threads: 1
    resources:
        mem_mb=3096,
    script:
        "scripts/build_shapes.py"


rule base_network:
    params:
        voltages=config["electricity"]["voltages"],
        transformers=config["transformers"],
        snapshots=config["snapshots"],
        links=config["links"],
        lines=config["lines"],
        hvdc_as_lines=config["electricity"]["hvdc_as_lines"],
        countries=config["countries"],
        base_network=config["base_network"],
    input:
        osm_buses="resources/" + RDIR + "base_network/all_buses_build_network.csv",
        osm_lines="resources/" + RDIR + "base_network/all_lines_build_network.csv",
        osm_converters="resources/"
        + RDIR
        + "base_network/all_converters_build_network.csv",
        osm_transformers="resources/"
        + RDIR
        + "base_network/all_transformers_build_network.csv",
        country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
        offshore_shapes="resources/" + RDIR + "shapes/offshore_shapes.geojson",
    output:
        "networks/" + RDIR + "base.nc",
    log:
        "logs/" + RDIR + "base_network.log",
    benchmark:
        "benchmarks/" + RDIR + "base_network"
    threads: 1
    resources:
        mem_mb=500,
    script:
        "scripts/base_network.py"


rule build_bus_regions:
    params:
        alternative_clustering=config["cluster_options"]["alternative_clustering"],
        crs=config["crs"],
        countries=config["countries"],
    input:
        country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
        offshore_shapes="resources/" + RDIR + "shapes/offshore_shapes.geojson",
        base_network="networks/" + RDIR + "base.nc",
        #gadm_shapes="resources/" + RDIR + "shapes/MAR2.geojson",
        #using this line instead of the following will test updated gadm shapes for MA.
        #To use: downlaod file from the google drive and place it in resources/" + RDIR + "shapes/
        #Link: https://drive.google.com/drive/u/1/folders/1dkW1wKBWvSY4i-XEuQFFBj242p0VdUlM
        gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
    output:
        regions_onshore="resources/" + RDIR + "bus_regions/regions_onshore.geojson",
        regions_offshore="resources/" + RDIR + "bus_regions/regions_offshore.geojson",
    log:
        "logs/" + RDIR + "build_bus_regions.log",
    benchmark:
        "benchmarks/" + RDIR + "build_bus_regions"
    threads: 1
    resources:
        mem_mb=1000,
    script:
        "scripts/build_bus_regions.py"


def terminate_if_cutout_exists(config=config):
    """
    Check if any of the requested cutout files exist.
    If that's the case, terminate execution to avoid data loss.
    """
    config_cutouts = [
        d_value["cutout"] for tc, d_value in config["renewable"].items()
    ] + list(config["atlite"]["cutouts"].keys())

    for ct in set(config_cutouts):
        cutout_fl = "cutouts/" + CDIR + ct + ".nc"
        if os.path.exists(cutout_fl):
            raise Exception(
                "An option `build_cutout` is enabled, while a cutout file '"
                + cutout_fl
                + "' still exists and risks to be overwritten. If this is an intended behavior, please move or delete this file and re-run the rule. Otherwise, just disable the `build_cutout` rule in the config file."
            )


if config["enable"].get("build_cutout", False):
    terminate_if_cutout_exists(config)

    rule build_cutout:
        params:
            snapshots=config["snapshots"],
            cutouts=config["atlite"]["cutouts"],
        input:
            onshore_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
            offshore_shapes="resources/" + RDIR + "shapes/offshore_shapes.geojson",
        output:
            "cutouts/" + CDIR + "{cutout}.nc",
        log:
            "logs/" + RDIR + "build_cutout/{cutout}.log",
        benchmark:
            "benchmarks/" + RDIR + "build_cutout_{cutout}"
        threads: ATLITE_NPROCESSES
        resources:
            mem_mb=ATLITE_NPROCESSES * 1000,
        script:
            "scripts/build_cutout.py"


if config["enable"].get("build_natura_raster", False):

    rule build_natura_raster:
        params:
            area_crs=config["crs"]["area_crs"],
        input:
            shapefiles_land="data/landcover",
            cutouts=expand(
                "cutouts/" + CDIR + "{cutout}.nc",
                cutout=[c["cutout"] for _, c in config["renewable"].items()],
            ),
        output:
            "resources/" + RDIR + "natura.tiff",
        log:
            "logs/" + RDIR + "build_natura_raster.log",
        benchmark:
            "benchmarks/" + RDIR + "build_natura_raster"
        script:
            "scripts/build_natura_raster.py"


if not config["enable"].get("build_natura_raster", False):

    rule copy_defaultnatura_tiff:
        input:
            "data/natura/natura.tiff",
        output:
            "resources/" + RDIR + "natura.tiff",
        run:
            import shutil

            shutil.copyfile(input[0], output[0])


if config["enable"].get("retrieve_cost_data", True):

    rule retrieve_cost_data:
        params:
            version=config["costs"]["version"],
        input:
            HTTP.remote(
                f"raw.githubusercontent.com/PyPSA/technology-data/{config['costs']['version']}/outputs/"
                + "costs_{year}.csv",
                keep_local=True,
            ),
        output:
            "resources/" + RDIR + "costs_{year}.csv",
        log:
            "logs/" + RDIR + "retrieve_cost_data_{year}.log",
        resources:
            mem_mb=5000,
        run:
            move(input[0], output[0])


rule build_demand_profiles:
    params:
        snapshots=config["snapshots"],
        load_options=config["load_options"],
        countries=config["countries"],
    input:
        base_network="networks/" + RDIR + "base.nc",
        regions="resources/" + RDIR + "bus_regions/regions_onshore.geojson",
        load=load_data_paths,
        #gadm_shapes="resources/" + RDIR + "shapes/MAR2.geojson",
        #using this line instead of the following will test updated gadm shapes for MA.
        #To use: downlaod file from the google drive and place it in resources/" + RDIR + "shapes/
        #Link: https://drive.google.com/drive/u/1/folders/1dkW1wKBWvSY4i-XEuQFFBj242p0VdUlM
        gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
    output:
        "resources/" + RDIR + "demand_profiles.csv",
    log:
        "logs/" + RDIR + "build_demand_profiles.log",
    benchmark:
        "benchmarks/" + RDIR + "build_demand_profiles"
    threads: 1
    resources:
        mem_mb=3000,
    script:
        "scripts/build_demand_profiles.py"


rule build_renewable_profiles:
    params:
        crs=config["crs"],
        renewable=config["renewable"],
        countries=config["countries"],
        alternative_clustering=config["cluster_options"]["alternative_clustering"],
    input:
        natura="resources/" + RDIR + "natura.tiff",
        copernicus="data/copernicus/PROBAV_LC100_global_v3.0.1_2019-nrt_Discrete-Classification-map_EPSG-4326.tif",
        gebco="data/gebco/GEBCO_2021_TID.nc",
        country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
        offshore_shapes="resources/" + RDIR + "shapes/offshore_shapes.geojson",
        hydro_capacities="data/hydro_capacities.csv",
        eia_hydro_generation="data/eia_hydro_annual_generation.csv",
        powerplants="resources/" + RDIR + "powerplants.csv",
        regions=lambda w: (
            "resources/" + RDIR + "bus_regions/regions_onshore.geojson"
            if w.technology in ("onwind", "solar", "hydro", "csp")
            else "resources/" + RDIR + "bus_regions/regions_offshore.geojson"
        ),
        cutout=lambda w: "cutouts/"
        + CDIR
        + config["renewable"][w.technology]["cutout"]
        + ".nc",
    output:
        profile="resources/" + RDIR + "renewable_profiles/profile_{technology}.nc",
    log:
        "logs/" + RDIR + "build_renewable_profile_{technology}.log",
    benchmark:
        "benchmarks/" + RDIR + "build_renewable_profiles_{technology}"
    threads: ATLITE_NPROCESSES
    resources:
        mem_mb=ATLITE_NPROCESSES * 5000,
    script:
        "scripts/build_renewable_profiles.py"


rule build_powerplants:
    params:
        geo_crs=config["crs"]["geo_crs"],
        countries=config["countries"],
        gadm_layer_id=config["build_shape_options"]["gadm_layer_id"],
        alternative_clustering=config["cluster_options"]["alternative_clustering"],
        powerplants_filter=config["electricity"]["powerplants_filter"],
    input:
        base_network="networks/" + RDIR + "base.nc",
        pm_config="configs/powerplantmatching_config.yaml",
        custom_powerplants="data/custom_powerplants.csv",
        osm_powerplants="resources/" + RDIR + "osm/clean/all_clean_generators.csv",
        #gadm_shapes="resources/" + RDIR + "shapes/MAR2.geojson",
        #using this line instead of the following will test updated gadm shapes for MA.
        #To use: downlaod file from the google drive and place it in resources/" + RDIR + "shapes/
        #Link: https://drive.google.com/drive/u/1/folders/1dkW1wKBWvSY4i-XEuQFFBj242p0VdUlM
        gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
    output:
        powerplants="resources/" + RDIR + "powerplants.csv",
        powerplants_osm2pm="resources/" + RDIR + "powerplants_osm2pm.csv",
    log:
        "logs/" + RDIR + "build_powerplants.log",
    benchmark:
        "benchmarks/" + RDIR + "build_powerplants"
    threads: 1
    resources:
        mem_mb=500,
    script:
        "scripts/build_powerplants.py"


rule add_electricity:
    params:
        countries=config["countries"],
        costs=config["costs"],
        conventional=config.get("conventional", {}),
        electricity=config["electricity"],
        alternative_clustering=config["cluster_options"]["alternative_clustering"],
        renewable=config["renewable"],
        length_factor=config["lines"]["length_factor"],
    input:
        **{
            f"profile_{tech}": "resources/"
            + RDIR
            + f"renewable_profiles/profile_{tech}.nc"
            for tech in config["renewable"]
            if tech in config["electricity"]["renewable_carriers"]
        },
        **{
            f"conventional_{carrier}_{attr}": fn
            for carrier, d in config.get("conventional", {None: {}}).items()
            for attr, fn in d.items()
            if str(fn).startswith("data/")
        },
        base_network="networks/" + RDIR + "base.nc",
        tech_costs=COSTS,
        powerplants="resources/" + RDIR + "powerplants.csv",
        #gadm_shapes="resources/" + RDIR + "shapes/MAR2.geojson",
        #using this line instead of the following will test updated gadm shapes for MA.
        #To use: downlaod file from the google drive and place it in resources/" + RDIR + "shapes/
        #Link: https://drive.google.com/drive/u/1/folders/1dkW1wKBWvSY4i-XEuQFFBj242p0VdUlM
        gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
        hydro_capacities="data/hydro_capacities.csv",
        demand_profiles="resources/" + RDIR + "demand_profiles.csv",
    output:
        "networks/" + RDIR + "elec.nc",
    log:
        "logs/" + RDIR + "add_electricity.log",
    benchmark:
        "benchmarks/" + RDIR + "add_electricity"
    threads: 1
    resources:
        mem_mb=3000,
    script:
        "scripts/add_electricity.py"


rule simplify_network:
    params:
        renewable=config["renewable"],
        geo_crs=config["crs"]["geo_crs"],
        cluster_options=config["cluster_options"],
        countries=config["countries"],
        build_shape_options=config["build_shape_options"],
        electricity=config["electricity"],
        costs=config["costs"],
        config_lines=config["lines"],
        config_links=config["links"],
        focus_weights=config.get("focus_weights", None),
    input:
        network="networks/" + RDIR + "elec.nc",
        tech_costs=COSTS,
        regions_onshore="resources/" + RDIR + "bus_regions/regions_onshore.geojson",
        regions_offshore="resources/" + RDIR + "bus_regions/regions_offshore.geojson",
    output:
        network="networks/" + RDIR + "elec_s{simpl}.nc",
        regions_onshore="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}.geojson",
        regions_offshore="resources/"
        + RDIR
        + "bus_regions/regions_offshore_elec_s{simpl}.geojson",
        busmap="resources/" + RDIR + "bus_regions/busmap_elec_s{simpl}.csv",
        connection_costs="resources/"
        + RDIR
        + "bus_regions/connection_costs_s{simpl}.csv",
    log:
        "logs/" + RDIR + "simplify_network/elec_s{simpl}.log",
    benchmark:
        "benchmarks/" + RDIR + "simplify_network/elec_s{simpl}"
    threads: 1
    resources:
        mem_mb=4000,
    script:
        "scripts/simplify_network.py"


if config["augmented_line_connection"].get("add_to_snakefile", False) == True:

    rule cluster_network:
        params:
            build_shape_options=config["build_shape_options"],
            electricity=config["electricity"],
            costs=config["costs"],
            length_factor=config["lines"]["length_factor"],
            renewable=config["renewable"],
            geo_crs=config["crs"]["geo_crs"],
            countries=config["countries"],
            cluster_options=config["cluster_options"],
            focus_weights=config.get("focus_weights", None),
            #custom_busmap=config["enable"].get("custom_busmap", False)
        input:
            network="networks/" + RDIR + "elec_s{simpl}.nc",
            country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
            regions_onshore="resources/"
            + RDIR
            + "bus_regions/regions_onshore_elec_s{simpl}.geojson",
            regions_offshore="resources/"
            + RDIR
            + "bus_regions/regions_offshore_elec_s{simpl}.geojson",
            #gadm_shapes="resources/" + RDIR + "shapes/MAR2.geojson",
            #using this line instead of the following will test updated gadm shapes for MA.
            #To use: downlaod file from the google drive and place it in resources/" + RDIR + "shapes/
            #Link: https://drive.google.com/drive/u/1/folders/1dkW1wKBWvSY4i-XEuQFFBj242p0VdUlM
            gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
            # busmap=ancient('resources/" + RDIR + "bus_regions/busmap_elec_s{simpl}.csv'),
            # custom_busmap=("data/custom_busmap_elec_s{simpl}_{clusters}.csv"
            #                if config["enable"].get("custom_busmap", False) else []),
            tech_costs=COSTS,
        output:
            network="networks/" + RDIR + "elec_s{simpl}_{clusters}_pre_augmentation.nc",
            regions_onshore="resources/"
            + RDIR
            + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
            regions_offshore="resources/"
            + RDIR
            + "bus_regions/regions_offshore_elec_s{simpl}_{clusters}.geojson",
            busmap="resources/"
            + RDIR
            + "bus_regions/busmap_elec_s{simpl}_{clusters}.csv",
            linemap="resources/"
            + RDIR
            + "bus_regions/linemap_elec_s{simpl}_{clusters}.csv",
        log:
            "logs/" + RDIR + "cluster_network/elec_s{simpl}_{clusters}.log",
        benchmark:
            "benchmarks/" + RDIR + "cluster_network/elec_s{simpl}_{clusters}"
        threads: 1
        resources:
            mem_mb=3000,
        script:
            "scripts/cluster_network.py"

    rule augmented_line_connections:
        params:
            lines=config["lines"],
            augmented_line_connection=config["augmented_line_connection"],
            hvdc_as_lines=config["electricity"]["hvdc_as_lines"],
            electricity=config["electricity"],
            costs=config["costs"],
        input:
            tech_costs=COSTS,
            network="networks/" + RDIR + "elec_s{simpl}_{clusters}_pre_augmentation.nc",
            regions_onshore="resources/"
            + RDIR
            + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
            regions_offshore="resources/"
            + RDIR
            + "bus_regions/regions_offshore_elec_s{simpl}_{clusters}.geojson",
        output:
            network="networks/" + RDIR + "elec_s{simpl}_{clusters}.nc",
        log:
            "logs/" + RDIR + "augmented_line_connections/elec_s{simpl}_{clusters}.log",
        benchmark:
            "benchmarks/" + RDIR + "augmented_line_connections/elec_s{simpl}_{clusters}"
        threads: 1
        resources:
            mem_mb=3000,
        script:
            "scripts/augmented_line_connections.py"


if config["augmented_line_connection"].get("add_to_snakefile", False) == False:

    rule cluster_network:
        params:
            build_shape_options=config["build_shape_options"],
            electricity=config["electricity"],
            costs=config["costs"],
            length_factor=config["lines"]["length_factor"],
            renewable=config["renewable"],
            geo_crs=config["crs"]["geo_crs"],
            countries=config["countries"],
            gadm_layer_id=config["build_shape_options"]["gadm_layer_id"],
            cluster_options=config["cluster_options"],
        input:
            network="networks/" + RDIR + "elec_s{simpl}.nc",
            country_shapes="resources/" + RDIR + "shapes/country_shapes.geojson",
            regions_onshore="resources/"
            + RDIR
            + "bus_regions/regions_onshore_elec_s{simpl}.geojson",
            regions_offshore="resources/"
            + RDIR
            + "bus_regions/regions_offshore_elec_s{simpl}.geojson",
            #gadm_shapes="resources/" + RDIR + "shapes/MAR2.geojson",
            #using this line instead of the following will test updated gadm shapes for MA.
            #To use: downlaod file from the google drive and place it in resources/" + RDIR + "shapes/
            #Link: https://drive.google.com/drive/u/1/folders/1dkW1wKBWvSY4i-XEuQFFBj242p0VdUlM
            gadm_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
            # busmap=ancient('resources/" + RDIR + "bus_regions/busmap_elec_s{simpl}.csv'),
            # custom_busmap=("data/custom_busmap_elec_s{simpl}_{clusters}.csv"
            #                if config["enable"].get("custom_busmap", False) else []),
            tech_costs=COSTS,
        output:
            network="networks/" + RDIR + "elec_s{simpl}_{clusters}.nc",
            regions_onshore="resources/"
            + RDIR
            + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
            regions_offshore="resources/"
            + RDIR
            + "bus_regions/regions_offshore_elec_s{simpl}_{clusters}.geojson",
            busmap="resources/"
            + RDIR
            + "bus_regions/busmap_elec_s{simpl}_{clusters}.csv",
            linemap="resources/"
            + RDIR
            + "bus_regions/linemap_elec_s{simpl}_{clusters}.csv",
        log:
            "logs/" + RDIR + "cluster_network/elec_s{simpl}_{clusters}.log",
        benchmark:
            "benchmarks/" + RDIR + "cluster_network/elec_s{simpl}_{clusters}"
        threads: 1
        resources:
            mem_mb=3000,
        script:
            "scripts/cluster_network.py"


rule add_extra_components:
    input:
        network="networks/" + RDIR + "elec_s{simpl}_{clusters}.nc",
        tech_costs=COSTS,
    output:
        "networks/" + RDIR + "elec_s{simpl}_{clusters}_ec.nc",
    log:
        "logs/" + RDIR + "add_extra_components/elec_s{simpl}_{clusters}.log",
    benchmark:
        "benchmarks/" + RDIR + "add_extra_components/elec_s{simpl}_{clusters}_ec"
    threads: 1
    resources:
        mem_mb=3000,
    script:
        "scripts/add_extra_components.py"


rule prepare_network:
    params:
        links=config["links"],
        lines=config["lines"],
        s_max_pu=config["lines"]["s_max_pu"],
        electricity=config["electricity"],
        costs=config["costs"],
    input:
        "networks/" + RDIR + "elec_s{simpl}_{clusters}_ec.nc",
        tech_costs=COSTS,
    output:
        "networks/" + RDIR + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
    log:
        "logs/" + RDIR + "prepare_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.log",
    benchmark:
        (
            "benchmarks/"
            + RDIR
            + "prepare_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}"
        )
    threads: 1
    resources:
        mem_mb=4000,
    script:
        "scripts/prepare_network.py"


def memory(w):
    factor = 3.0
    for o in w.opts.split("-"):
        m = re.match(r"^(\d+)h$", o, re.IGNORECASE)
        if m is not None:
            factor /= int(m.group(1))
            break
    for o in w.opts.split("-"):
        m = re.match(r"^(\d+)seg$", o, re.IGNORECASE)
        if m is not None:
            factor *= int(m.group(1)) / 8760
            break
    if w.clusters.endswith("m"):
        return int(factor * (18000 + 180 * int(w.clusters[:-1])))
    elif w.clusters.endswith("flex"):
        return int(factor * (18000 + 180 * int(w.clusters[:-4])))
    elif w.clusters == "all":
        return int(factor * (18000 + 180 * 4000))
    elif w.clusters == "min":
        return int(factor * (18000 + 180 * 20))
    else:
        return int(factor * (10000 + 195 * int(w.clusters)))


if config["monte_carlo"]["options"].get("add_to_snakefile", False) == False:

    rule solve_network:
        params:
            solving=config["solving"],
            augmented_line_connection=config["augmented_line_connection"],
        input:
            network="networks/" + RDIR + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
        output:
            "results/" + RDIR + "networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
        log:
            solver=normpath(
                "logs/"
                + RDIR
                + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_solver.log"
            ),
            python="logs/"
            + RDIR
            + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_python.log",
        benchmark:
            (
                "benchmarks/"
                + RDIR
                + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}"
            )
        threads: 20
        resources:
            mem=memory,
        shadow:
            "copy-minimal" if os.name == "nt" else "shallow"
        script:
            "scripts/solve_network.py"


if config["monte_carlo"]["options"].get("add_to_snakefile", False) == True:

    rule monte_carlo:
        params:
            monte_carlo=config["monte_carlo"],
        input:
            "networks/" + RDIR + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
        output:
            "networks/" + RDIR + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}.nc",
        log:
            "logs/"
            + RDIR
            + "prepare_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}.log",
        benchmark:
            (
                "benchmarks/"
                + RDIR
                + "prepare_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}"
            )
        threads: 1
        resources:
            mem_mb=4000,
        script:
            "scripts/monte_carlo.py"

    rule solve_monte:
        input:
            expand(
                "networks/"
                + RDIR
                + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}.nc",
                **config["scenario"],
            ),

    rule solve_network:
        params:
            solving=config["solving"],
            augmented_line_connection=config["augmented_line_connection"],
        input:
            network="networks/"
            + RDIR
            + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}.nc",
        output:
            "results/"
            + RDIR
            + "networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}.nc",
        log:
            solver=normpath(
                "logs/"
                + RDIR
                + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}_solver.log"
            ),
            python="logs/"
            + RDIR
            + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}_python.log",
            memory="logs/"
            + RDIR
            + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}_memory.log",
        benchmark:
            (
                "benchmarks/"
                + RDIR
                + "solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}"
            )
        threads: 20
        resources:
            mem_mb=memory,
        shadow:
            "shallow"
        script:
            "scripts/solve_network.py"

    rule solve_all_networks_monte:
        input:
            expand(
                "results/"
                + RDIR
                + "networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{unc}.nc",
                **config["scenario"],
            ),


def input_make_summary(w):
    # It's mildly hacky to include the separate costs input as first entry
    if w.ll.endswith("all"):
        ll = config["scenario"]["ll"]
        if len(w.ll) == 4:
            ll = [l for l in ll if l[0] == w.ll[0]]
    else:
        ll = w.ll
    return [COSTS] + expand(
        "results/" + RDIR + "networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
        ll=ll,
        **{
            k: config["scenario"][k] if getattr(w, k) == "all" else getattr(w, k)
            for k in ["simpl", "clusters", "opts"]
        },
    )


rule make_summary:
    params:
        electricity=config["electricity"],
        costs=config["costs"],
        ll=config["scenario"]["ll"],
        scenario=config["scenario"],
    input:
        input_make_summary,
        tech_costs=COSTS,
    output:
        directory(
            "results/"
            + RDIR
            + "summaries/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}"
        ),
    log:
        "logs/"
        + RDIR
        + "make_summary/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}.log",
    script:
        "scripts/make_summary.py"


rule prepare_sector_networks:
    input:
        expand(
            RESDIR
            + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}.nc",
            **config["scenario"],
            **config["costs"],
        ),


rule override_res_all_nets:
    input:
        expand(
            RESDIR
            + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_presec.nc",
            **config["scenario"],
            **config["costs"],
            **config["export"],
        ),


rule solve_sector_networks:
    input:
        expand(
            RESDIR
            + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            **config["scenario"],
            **config["costs"],
            **config["export"],
        ),


rule prepare_ports:
    params:
        custom_export=config["custom_data"]["export_ports"],
    output:
        ports="resources/" + SECDIR + "ports.csv",
        export_ports="resources/" + SECDIR + "export_ports.csv",
    script:
        "scripts/prepare_ports.py"


rule prepare_airports:
    params:
        airport_sizing_factor=config["sector"]["airport_sizing_factor"],
        airport_custom_data=config["custom_data"]["airports"],
    output:
        ports="resources/" + SECDIR + "airports.csv",
    script:
        "scripts/prepare_airports.py"


rule prepare_urban_percent:
    output:
        urban_percent="resources/" + SECDIR + "urban_percent.csv",
    script:
        "scripts/prepare_urban_percent.py"


rule prepare_transport_data_input:
    output:
        transport_data_input="resources/" + SECDIR + "transport_data.csv",
    script:
        "scripts/prepare_transport_data_input.py"


if not config["custom_data"]["gas_network"]:

    rule prepare_gas_network:
        params:
            gas_config=config["sector"]["gas"],
            alternative_clustering=config["cluster_options"]["alternative_clustering"],
            countries_list=config["countries"],
            layer_id=config["build_shape_options"]["gadm_layer_id"],
            update=config["build_shape_options"]["update_file"],
            out_logging=config["build_shape_options"]["out_logging"],
            year=config["build_shape_options"]["year"],
            nprocesses=config["build_shape_options"]["nprocesses"],
            contended_flag=config["build_shape_options"]["contended_flag"],
            geo_crs=config["crs"]["geo_crs"],
            custom_gas_network=config["custom_data"]["gas_network"],
        input:
            regions_onshore="resources/"
            + RDIR
            + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        output:
            clustered_gas_network="resources/"
            + SECDIR
            + "gas_networks/gas_network_elec_s{simpl}_{clusters}.csv",
            # TODO: Should be a own snakemake rule
            # gas_network_fig_1="resources/gas_networks/existing_gas_pipelines_{simpl}_{clusters}.png",
            # gas_network_fig_2="resources/gas_networks/clustered_gas_pipelines_{simpl}_{clusters}.png",
        script:
            "scripts/prepare_gas_network.py"


rule prepare_sector_network:
    params:
        costs=config["costs"],
        electricity=config["electricity"],
    input:
        network=RESDIR
        + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_presec.nc",
        costs="resources/" + RDIR + "costs_{planning_horizons}.csv",
        h2_cavern="data/hydrogen_salt_cavern_potentials.csv",
        nodal_energy_totals="resources/"
        + SECDIR
        + "demand/heat/nodal_energy_heat_totals_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        transport="resources/"
        + SECDIR
        + "demand/transport_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        avail_profile="resources/"
        + SECDIR
        + "pattern_profiles/avail_profile_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        dsm_profile="resources/"
        + SECDIR
        + "pattern_profiles/dsm_profile_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        nodal_transport_data="resources/"
        + SECDIR
        + "demand/nodal_transport_data_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        overrides="data/override_component_attrs",
        clustered_pop_layout="resources/"
        + SECDIR
        + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        industrial_demand="resources/"
        + SECDIR
        + "demand/industrial_energy_demand_per_node_elec_s{simpl}_{clusters}_{planning_horizons}_{demand}.csv",
        energy_totals="resources/"
        + SECDIR
        + "energy_totals_{demand}_{planning_horizons}.csv",
        airports="resources/" + SECDIR + "airports.csv",
        ports="resources/" + SECDIR + "ports.csv",
        heat_demand="resources/"
        + SECDIR
        + "demand/heat/heat_demand_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        ashp_cop="resources/"
        + SECDIR
        + "demand/heat/ashp_cop_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        gshp_cop="resources/"
        + SECDIR
        + "demand/heat/gshp_cop_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        solar_thermal="resources/"
        + SECDIR
        + "demand/heat/solar_thermal_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        district_heat_share="resources/"
        + SECDIR
        + "demand/heat/district_heat_share_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        biomass_transport_costs="data/temp_hard_coded/biomass_transport_costs.csv",
        shapes_path="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        pipelines=(
            "data/custom/pipelines.csv"
            if config["custom_data"]["gas_network"]
            else "resources/"
            + SECDIR
            + "gas_networks/gas_network_elec_s{simpl}_{clusters}.csv"
        ),
    output:
        RESDIR
        + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}.nc",
    threads: 1
    resources:
        mem_mb=2000,
    benchmark:
        (
            RESDIR
            + "benchmarks/prepare_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}"
        )
    script:
        "scripts/prepare_sector_network.py"


rule build_ship_profile:
    params:
        snapshots=config["snapshots"],
        ship_opts=config["export"]["ship"],
    output:
        ship_profile="resources/" + SECDIR + "ship_profile_{h2export}TWh.csv",
    script:
        "scripts/build_ship_profile.py"


rule add_export:
    params:
        gadm_level=config["sector"]["gadm_level"],
        alternative_clustering=config["cluster_options"]["alternative_clustering"],
        store=config["export"]["store"],
        store_capital_costs=config["export"]["store_capital_costs"],
        export_profile=config["export"]["export_profile"],
        export_endogenous=config["export"]["endogenous"],
        endogenous_price=config["export"]["endogenous_price"],
        snapshots=config["snapshots"],
        costs=config["costs"],
    input:
        overrides="data/override_component_attrs",
        export_ports="resources/" + SECDIR + "export_ports.csv",
        costs="resources/" + RDIR + "costs_{planning_horizons}.csv",
        ship_profile="resources/" + SECDIR + "ship_profile_{h2export}TWh.csv",
        network=RESDIR
        + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}.nc",
        shapes_path="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
    output:
        RESDIR
        + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
    script:
        "scripts/add_export.py"


rule override_respot:
    params:
        run=run["name"],
        custom_data=config["custom_data"],
        countries=config["countries"],
    input:
        **{
            f"custom_res_pot_{tech}_{planning_horizons}_{discountrate}": "resources/"
            + SECDIR
            + f"custom_renewables/{tech}_{planning_horizons}_{discountrate}_potential.csv"
            for tech in config["custom_data"]["renewables"]
            for discountrate in config["costs"]["discountrate"]
            for planning_horizons in config["scenario"]["planning_horizons"]
        },
        **{
            f"custom_res_ins_{tech}_{planning_horizons}_{discountrate}": "resources/"
            + SECDIR
            + f"custom_renewables/{tech}_{planning_horizons}_{discountrate}_installable.csv"
            for tech in config["custom_data"]["renewables"]
            for discountrate in config["costs"]["discountrate"]
            for planning_horizons in config["scenario"]["planning_horizons"]
        },
        overrides="data/override_component_attrs",
        network="networks/" + RDIR + "elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
        energy_totals="resources/"
        + SECDIR
        + "energy_totals_{demand}_{planning_horizons}.csv",
    output:
        RESDIR
        + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_presec.nc",
    script:
        "scripts/override_respot.py"


rule prepare_transport_data:
    input:
        network="networks/" + RDIR + "elec_s{simpl}_{clusters}.nc",
        energy_totals_name="resources/"
        + SECDIR
        + "energy_totals_{demand}_{planning_horizons}.csv",
        traffic_data_KFZ="data/emobility/KFZ__count",
        traffic_data_Pkw="data/emobility/Pkw__count",
        transport_name="resources/" + SECDIR + "transport_data.csv",
        clustered_pop_layout="resources/"
        + SECDIR
        + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        temp_air_total="resources/"
        + SECDIR
        + "temperatures/temp_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
    output:
        # nodal_energy_totals="resources/nodal_energy_totals_s{simpl}_{clusters}.csv",
        transport="resources/"
        + SECDIR
        + "demand/transport_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        avail_profile="resources/"
        + SECDIR
        + "pattern_profiles/avail_profile_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        dsm_profile="resources/"
        + SECDIR
        + "pattern_profiles/dsm_profile_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        nodal_transport_data="resources/"
        + SECDIR
        + "demand/nodal_transport_data_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
    script:
        "scripts/prepare_transport_data.py"


rule build_cop_profiles:
    params:
        heat_pump_sink_T=config["sector"]["heat_pump_sink_T"],
    input:
        temp_soil_total="resources/"
        + SECDIR
        + "temperatures/temp_soil_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_soil_rural="resources/"
        + SECDIR
        + "temperatures/temp_soil_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_soil_urban="resources/"
        + SECDIR
        + "temperatures/temp_soil_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_air_total="resources/"
        + SECDIR
        + "temperatures/temp_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_air_rural="resources/"
        + SECDIR
        + "temperatures/temp_air_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_air_urban="resources/"
        + SECDIR
        + "temperatures/temp_air_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
    output:
        cop_soil_total="resources/"
        + SECDIR
        + "cops/cop_soil_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_soil_rural="resources/"
        + SECDIR
        + "cops/cop_soil_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_soil_urban="resources/"
        + SECDIR
        + "cops/cop_soil_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_air_total="resources/"
        + SECDIR
        + "cops/cop_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_air_rural="resources/"
        + SECDIR
        + "cops/cop_air_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_air_urban="resources/"
        + SECDIR
        + "cops/cop_air_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
    resources:
        mem_mb=20000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "build_cop_profiles/s{simpl}_{clusters}_{planning_horizons}"
        )
    script:
        "scripts/build_cop_profiles.py"


rule prepare_heat_data:
    input:
        network="networks/" + RDIR + "elec_s{simpl}_{clusters}.nc",
        energy_totals_name="resources/"
        + SECDIR
        + "energy_totals_{demand}_{planning_horizons}.csv",
        clustered_pop_layout="resources/"
        + SECDIR
        + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        temp_air_total="resources/"
        + SECDIR
        + "temperatures/temp_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_soil_total="resources/"
        + SECDIR
        + "cops/cop_soil_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        cop_air_total="resources/"
        + SECDIR
        + "cops/cop_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        solar_thermal_total="resources/"
        + SECDIR
        + "demand/heat/solar_thermal_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        heat_demand_total="resources/"
        + SECDIR
        + "demand/heat/heat_demand_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        heat_profile="data/heat_load_profile_BDEW.csv",
    output:
        nodal_energy_totals="resources/"
        + SECDIR
        + "demand/heat/nodal_energy_heat_totals_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        heat_demand="resources/"
        + SECDIR
        + "demand/heat/heat_demand_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        ashp_cop="resources/"
        + SECDIR
        + "demand/heat/ashp_cop_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        gshp_cop="resources/"
        + SECDIR
        + "demand/heat/gshp_cop_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        solar_thermal="resources/"
        + SECDIR
        + "demand/heat/solar_thermal_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        district_heat_share="resources/"
        + SECDIR
        + "demand/heat/district_heat_share_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
    script:
        "scripts/prepare_heat_data.py"


rule build_base_energy_totals:
    params:
        space_heat_share=config["sector"]["space_heat_share"],
        update_data=config["demand_data"]["update_data"],
        base_year=config["demand_data"]["base_year"],
        countries=config["countries"],
        shift_coal_to_elec=config["sector"]["coal"]["shift_to_elec"],
    input:
        unsd_paths="data/demand/unsd/paths/Energy_Statistics_Database.xlsx",
    output:
        energy_totals_base="resources/" + SECDIR + "energy_totals_base.csv",
        unsd_export_path=directory("data/demand/unsd/data/"),
    script:
        "scripts/build_base_energy_totals.py"


rule prepare_energy_totals:
    params:
        countries=config["countries"],
        base_year=config["demand_data"]["base_year"],
        sector_options=config["sector"],
    input:
        unsd_paths="resources/" + SECDIR + "energy_totals_base.csv",
        efficiency_gains_cagr="data/demand/efficiency_gains_cagr.csv",
        growth_factors_cagr="data/demand/growth_factors_cagr.csv",
        district_heating="data/demand/district_heating.csv",
        fuel_shares="data/demand/fuel_shares.csv",
    output:
        energy_totals="resources/"
        + SECDIR
        + "energy_totals_{demand}_{planning_horizons}.csv",
    script:
        "scripts/prepare_energy_totals.py"


rule build_solar_thermal_profiles:
    params:
        solar_thermal_config=config["solar_thermal"],
        snapshots=config["snapshots"],
    input:
        pop_layout_total="resources/"
        + SECDIR
        + "population_shares/pop_layout_total_{planning_horizons}.nc",
        pop_layout_urban="resources/"
        + SECDIR
        + "population_shares/pop_layout_urban_{planning_horizons}.nc",
        pop_layout_rural="resources/"
        + SECDIR
        + "population_shares/pop_layout_rural_{planning_horizons}.nc",
        regions_onshore="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        cutout="cutouts/"
        + CDIR
        + [c["cutout"] for _, c in config["renewable"].items()][0]
        + ".nc",
        # default to first cutout found
    output:
        solar_thermal_total="resources/"
        + SECDIR
        + "demand/heat/solar_thermal_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        solar_thermal_urban="resources/"
        + SECDIR
        + "demand/heat/solar_thermal_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        solar_thermal_rural="resources/"
        + SECDIR
        + "demand/heat/solar_thermal_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
    resources:
        mem_mb=20000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "build_solar_thermal_profiles/s{simpl}_{clusters}_{planning_horizons}"
        )
    script:
        "scripts/build_solar_thermal_profiles.py"


rule build_population_layouts:
    params:
        planning_horizons=config["scenario"]["planning_horizons"][0],
    input:
        nuts3_shapes="resources/" + RDIR + "shapes/gadm_shapes.geojson",
        urban_percent="resources/" + SECDIR + "urban_percent.csv",
        cutout="cutouts/"
        + CDIR
        + [c["cutout"] for _, c in config["renewable"].items()][0]
        + ".nc",
        # default to first cutout found
    output:
        pop_layout_total="resources/"
        + SECDIR
        + "population_shares/pop_layout_total_{planning_horizons}.nc",
        pop_layout_urban="resources/"
        + SECDIR
        + "population_shares/pop_layout_urban_{planning_horizons}.nc",
        pop_layout_rural="resources/"
        + SECDIR
        + "population_shares/pop_layout_rural_{planning_horizons}.nc",
        gdp_layout="resources/"
        + SECDIR
        + "gdp_shares/gdp_layout_{planning_horizons}.nc",
    resources:
        mem_mb=20000,
    benchmark:
        ("benchmarks/" + SECDIR + "build_population_layouts_{planning_horizons}")
    threads: 8
    script:
        "scripts/build_population_layouts.py"


rule move_hardcoded_files_temp:
    input:
        "data/temp_hard_coded/energy_totals.csv",
    output:
        "resources/" + SECDIR + "energy_totals.csv",
    shell:
        "cp -a data/temp_hard_coded/. resources"


rule build_clustered_population_layouts:
    input:
        pop_layout_total="resources/"
        + SECDIR
        + "population_shares/pop_layout_total_{planning_horizons}.nc",
        pop_layout_urban="resources/"
        + SECDIR
        + "population_shares/pop_layout_urban_{planning_horizons}.nc",
        pop_layout_rural="resources/"
        + SECDIR
        + "population_shares/pop_layout_rural_{planning_horizons}.nc",
        gdp_layout="resources/"
        + SECDIR
        + "gdp_shares/gdp_layout_{planning_horizons}.nc",
        regions_onshore="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        cutout="cutouts/"
        + CDIR
        + [c["cutout"] for _, c in config["renewable"].items()][0]
        + ".nc",
        # default to first cutout found
    output:
        clustered_pop_layout="resources/"
        + SECDIR
        + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        clustered_gdp_layout="resources/"
        + SECDIR
        + "gdp_shares/gdp_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
    resources:
        mem_mb=10000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "build_clustered_population_layouts/s{simpl}_{clusters}_{planning_horizons}"
        )
    script:
        "scripts/build_clustered_population_layouts.py"


rule build_heat_demand:
    params:
        snapshots=config["snapshots"],
    input:
        pop_layout_total="resources/"
        + SECDIR
        + "population_shares/pop_layout_total_{planning_horizons}.nc",
        pop_layout_urban="resources/"
        + SECDIR
        + "population_shares/pop_layout_urban_{planning_horizons}.nc",
        pop_layout_rural="resources/"
        + SECDIR
        + "population_shares/pop_layout_rural_{planning_horizons}.nc",
        regions_onshore="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        cutout="cutouts/"
        + CDIR
        + [c["cutout"] for _, c in config["renewable"].items()][0]
        + ".nc",
        # default to first cutout found
    output:
        heat_demand_urban="resources/"
        + SECDIR
        + "demand/heat/heat_demand_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        heat_demand_rural="resources/"
        + SECDIR
        + "demand/heat/heat_demand_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        heat_demand_total="resources/"
        + SECDIR
        + "demand/heat/heat_demand_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
    resources:
        mem_mb=20000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "build_heat_demand/s{simpl}_{clusters}_{planning_horizons}"
        )
    script:
        "scripts/build_heat_demand.py"


rule build_temperature_profiles:
    params:
        snapshots=config["snapshots"],
    input:
        pop_layout_total="resources/"
        + SECDIR
        + "population_shares/pop_layout_total_{planning_horizons}.nc",
        pop_layout_urban="resources/"
        + SECDIR
        + "population_shares/pop_layout_urban_{planning_horizons}.nc",
        pop_layout_rural="resources/"
        + SECDIR
        + "population_shares/pop_layout_rural_{planning_horizons}.nc",
        regions_onshore="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        cutout="cutouts/"
        + CDIR
        + [c["cutout"] for _, c in config["renewable"].items()][0]
        + ".nc",
        # default to first cutout found
    output:
        temp_soil_total="resources/"
        + SECDIR
        + "temperatures/temp_soil_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_soil_rural="resources/"
        + SECDIR
        + "temperatures/temp_soil_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_soil_urban="resources/"
        + SECDIR
        + "temperatures/temp_soil_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_air_total="resources/"
        + SECDIR
        + "temperatures/temp_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_air_rural="resources/"
        + SECDIR
        + "temperatures/temp_air_rural_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        temp_air_urban="resources/"
        + SECDIR
        + "temperatures/temp_air_urban_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
    resources:
        mem_mb=20000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "build_temperature_profiles/s{simpl}_{clusters}_{planning_horizons}"
        )
    script:
        "scripts/build_temperature_profiles.py"


rule copy_config:
    params:
        summary_dir=config["summary_dir"],
        run=run,
    output:
        folder=directory(SDIR + "configs"),
        config=SDIR + "configs/config.yaml",
    threads: 1
    resources:
        mem_mb=1000,
    benchmark:
        SDIR + "benchmarks/copy_config"
    script:
        "scripts/copy_config.py"


if config["foresight"] == "overnight":

    rule solve_sector_network:
        params:
            solving=config["solving"],
            augmented_line_connection=config["augmented_line_connection"],
        input:
            overrides="data/override_component_attrs",
            # network=RESDIR
            # + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}.nc",
            network=RESDIR
            + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            costs="resources/" + RDIR + "costs_{planning_horizons}.csv",
            configs=SDIR + "configs/config.yaml",  # included to trigger copy_config rule
        output:
            RESDIR
            + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
        shadow:
            "shallow"
        log:
            solver=RESDIR
            + "logs/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export_solver.log",
            python=RESDIR
            + "logs/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export_python.log",
            memory=RESDIR
            + "logs/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export_memory.log",
        threads: 25
        resources:
            mem_mb=config["solving"]["mem"],
        benchmark:
            (
                RESDIR
                + "benchmarks/solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export"
            )
        script:
            "scripts/solve_network.py"


rule make_conf_check:
    output:
        pretty_config="results/" + RDIR + "config_scenarios.html",
    threads: 1
    resources:
        mem_mb=10000,
    script:
        "scripts/make_conf_check.py"


rule make_sector_summary:
    params:
        planning_horizons=config["scenario"]["planning_horizons"],
        results_dir=config["results_dir"],
        summary_dir=config["summary_dir"],
        run=run["name"],
        scenario_config=config["scenario"],
        costs_config=config["costs"],
        h2export_qty=config["export"]["h2export"],
        foresight=config["foresight"],
    input:
        overrides="data/override_component_attrs",
        networks=expand(
            RESDIR
            + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            **config["scenario"],
            **config["costs"],
            **config["export"],
        ),
        costs="resources/" + RDIR + "costs_{planning_horizons}.csv",
        plots=expand(
            RESDIR
            + "maps/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}-costs-all_{planning_horizons}_{discountrate}_{demand}_{h2export}export.pdf",
            **config["scenario"],
            **config["costs"],
            **config["export"],
        ),
    output:
        nodal_costs=SDIR + "csvs/nodal_costs.csv",
        nodal_capacities=SDIR + "csvs/nodal_capacities.csv",
        nodal_cfs=SDIR + "csvs/nodal_cfs.csv",
        cfs=SDIR + "csvs/cfs.csv",
        costs=SDIR + "csvs/costs.csv",
        capacities=SDIR + "csvs/capacities.csv",
        curtailment=SDIR + "csvs/curtailment.csv",
        energy=SDIR + "csvs/energy.csv",
        supply=SDIR + "csvs/supply.csv",
        supply_energy=SDIR + "csvs/supply_energy.csv",
        prices=SDIR + "csvs/prices.csv",
        weighted_prices=SDIR + "csvs/weighted_prices.csv",
        market_values=SDIR + "csvs/market_values.csv",
        price_statistics=SDIR + "csvs/price_statistics.csv",
        metrics=SDIR + "csvs/metrics.csv",
    threads: 2
    resources:
        mem_mb=10000,
    benchmark:
        SDIR + "benchmarks/make_summary"
    script:
        "scripts/make_summary.py"


rule plot_summary:
    input:
        "results/"
        + RDIR
        + "summaries/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}",
    output:
        "results/"
        + RDIR
        + "plots/summary_{summary}_elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}.{ext}",
    log:
        "logs/"
        + RDIR
        + "plot_summary/{summary}_elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{country}_{ext}.log",
    script:
        "scripts/plot_summary.py"


rule plot_network:
    params:
        electricity=config["electricity"],
        costs=config["costs"],
        plotting=config["plotting"],
    input:
        network="results/"
        + RDIR
        + "networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc",
        africa_shape="resources/" + RDIR + "shapes/africa_shape.geojson",
        tech_costs=COSTS,
    output:
        only_map="results/"
        + RDIR
        + "plots/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{attr}.{ext}",
        ext="results/"
        + RDIR
        + "plots/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{attr}_ext.{ext}",
    log:
        "logs/"
        + RDIR
        + "plot_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{attr}_{ext}.log",
    script:
        "scripts/plot_network.py"


rule make_statistics:
    params:
        countries=config["countries"],
        renewable_carriers=config["electricity"]["renewable_carriers"],
        renewable=config["renewable"],
        crs=config["crs"],
        scenario=config["scenario"],
    output:
        stats="results/" + RDIR + "stats.csv",
    threads: 1
    script:
        "scripts/make_statistics.py"


rule plot_sector_network:
    input:
        overrides="data/override_component_attrs",
        network=RESDIR
        + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
    output:
        map=RESDIR
        + "maps/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}-costs-all_{planning_horizons}_{discountrate}_{demand}_{h2export}export.pdf",
    threads: 2
    resources:
        mem_mb=10000,
    benchmark:
        (
            RESDIR
            + "benchmarks/plot_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export"
        )
    script:
        "scripts/plot_network.py"


rule plot_sector_summary:
    input:
        costs=SDIR + "csvs/costs.csv",
        energy=SDIR + "csvs/energy.csv",
        balances=SDIR + "csvs/supply_energy.csv",
    output:
        costs=SDIR + "graphs/costs.pdf",
        energy=SDIR + "graphs/energy.pdf",
        balances=SDIR + "graphs/balances-energy.pdf",
    threads: 2
    resources:
        mem_mb=10000,
    benchmark:
        SDIR + "benchmarks/plot_summary"
    script:
        "scripts/plot_summary.py"


rule build_industrial_database:
    output:
        industrial_database="data/industrial_database.csv",
    script:
        "scripts/build_industrial_database.py"


rule prepare_db:
    params:
        tech_colors=config["plotting"]["tech_colors"],
    input:
        network=RESDIR
        + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
    output:
        db=RESDIR
        + "summaries/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}-costs-all_{planning_horizons}_{discountrate}_{demand}_{h2export}export.csv",
    threads: 2
    resources:
        mem_mb=10000,
    benchmark:
        (
            RESDIR
            + "benchmarks/prepare_db/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export"
        )
    script:
        "scripts/prepare_db.py"


rule build_industrial_distribution_key:  #default data
    params:
        countries=config["countries"],
        gadm_level=config["sector"]["gadm_level"],
        alternative_clustering=config["cluster_options"]["alternative_clustering"],
        industry_database=config["custom_data"]["industry_database"],
    input:
        regions_onshore="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
        clustered_pop_layout="resources/"
        + SECDIR
        + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        clustered_gdp_layout="resources/"
        + SECDIR
        + "gdp_shares/gdp_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        industrial_database="data/industrial_database.csv",
        shapes_path="resources/"
        + RDIR
        + "bus_regions/regions_onshore_elec_s{simpl}_{clusters}.geojson",
    output:
        industrial_distribution_key="resources/"
        + SECDIR
        + "demand/industrial_distribution_key_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
    threads: 1
    resources:
        mem_mb=1000,
    benchmark:
        (
            "benchmarks/"
            + RDIR
            + "build_industrial_distribution_key_elec_s{simpl}_{clusters}_{planning_horizons}"
        )
    script:
        "scripts/build_industrial_distribution_key.py"


rule build_base_industry_totals:  #default data
    params:
        base_year=config["demand_data"]["base_year"],
        countries=config["countries"],
        other_industries=config["demand_data"]["other_industries"],
    input:
        #os.path.dirname(snakemake.input["transactions_path"]) + "/demand/unsd/data/"
        #industrial_production_per_country="data/industrial_production_per_country.csv",
        unsd_export_path="data/demand/unsd/data/",
        energy_totals_base="resources/" + SECDIR + "energy_totals_base.csv",
        transactions_path="data/unsd_transactions.csv",
    output:
        base_industry_totals="resources/"
        + SECDIR
        + "demand/base_industry_totals_{planning_horizons}_{demand}.csv",
    threads: 1
    resources:
        mem_mb=1000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "build_base_industry_totals_{planning_horizons}_{demand}"
        )
    script:
        "scripts/build_base_industry_totals.py"


rule build_industry_demand:  #default data
    params:
        countries=config["countries"],
        industry_demand=config["custom_data"]["industry_demand"],
        base_year=config["demand_data"]["base_year"],
        industry_util_factor=config["sector"]["industry_util_factor"],
        aluminium_year=config["demand_data"]["aluminium_year"],
    input:
        industrial_distribution_key="resources/"
        + SECDIR
        + "demand/industrial_distribution_key_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        #industrial_production_per_country_tomorrow="resources/demand/industrial_production_per_country_tomorrow_{planning_horizons}_{demand}.csv",
        #industrial_production_per_country="data/industrial_production_per_country.csv",
        base_industry_totals="resources/"
        + SECDIR
        + "demand/base_industry_totals_{planning_horizons}_{demand}.csv",
        industrial_database="data/industrial_database.csv",
        costs="resources/" + RDIR + "costs_{planning_horizons}.csv",
        industry_growth_cagr="data/demand/industry_growth_cagr.csv",
    output:
        industrial_energy_demand_per_node="resources/"
        + SECDIR
        + "demand/industrial_energy_demand_per_node_elec_s{simpl}_{clusters}_{planning_horizons}_{demand}.csv",
    threads: 1
    resources:
        mem_mb=1000,
    benchmark:
        (
            "benchmarks/"
            + SECDIR
            + "industrial_energy_demand_per_node_elec_s{simpl}_{clusters}_{planning_horizons}_{demand}.csv"
        )
    script:
        "scripts/build_industry_demand.py"


rule build_existing_heating_distribution:
    params:
        baseyear=config["scenario"]["planning_horizons"][0],
        sector=config["sector"],
        existing_capacities=config["existing_capacities"],
    input:
        existing_heating="data/existing_infrastructure/existing_heating_raw.csv",
        clustered_pop_layout="resources/"
        + SECDIR
        + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
        clustered_pop_energy_layout="resources/"
        + SECDIR
        + "demand/heat/nodal_energy_heat_totals_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        #"resources/population_shares/pop_weighted_energy_totals_s{simpl}_{clusters}.csv",
        district_heat_share="resources/"
        + SECDIR
        + "demand/heat/district_heat_share_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
    output:
        existing_heating_distribution="resources/"
        + SECDIR
        + "heating/existing_heating_distribution_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
    threads: 1
    resources:
        mem_mb=2000,
    log:
        RESDIR
        + "logs/build_existing_heating_distribution_{demand}_s{simpl}_{clusters}_{planning_horizons}.log",
    benchmark:
        RESDIR
        +"benchmarks/build_existing_heating_distribution/{demand}_s{simpl}_{clusters}_{planning_horizons}"
    script:
        "scripts/build_existing_heating_distribution.py"


if config["foresight"] == "myopic":

    rule add_existing_baseyear:
        params:
            baseyear=config["scenario"]["planning_horizons"][0],
            sector=config["sector"],
            existing_capacities=config["existing_capacities"],
            costs=config["costs"],
        input:
            network=RESDIR
            + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            powerplants="resources/" + RDIR + "powerplants.csv",
            busmap_s="resources/" + RDIR + "bus_regions/busmap_elec_s{simpl}.csv",
            busmap=pypsaearth(
                "resources/" + RDIR + "bus_regions/busmap_elec_s{simpl}_{clusters}.csv"
            ),
            clustered_pop_layout="resources/"
            + SECDIR
            + "population_shares/pop_layout_elec_s{simpl}_{clusters}_{planning_horizons}.csv",
            costs=CDIR
            + "costs_{}.csv".format(config["scenario"]["planning_horizons"][0]),
            cop_soil_total="resources/"
            + SECDIR
            + "cops/cop_soil_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
            cop_air_total="resources/"
            + SECDIR
            + "cops/cop_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
            existing_heating_distribution="resources/"
            + SECDIR
            + "heating/existing_heating_distribution_{demand}_s{simpl}_{clusters}_{planning_horizons}.csv",
        output:
            RESDIR
            + "prenetworks-brownfield/elec_s{simpl}_{clusters}_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
        wildcard_constraints:
            # TODO: The first planning_horizon needs to be aligned across scenarios
            # snakemake does not support passing functions to wildcard_constraints
            # reference: https://github.com/snakemake/snakemake/issues/2703
            planning_horizons=config["scenario"]["planning_horizons"][0],  #only applies to baseyear
        threads: 1
        resources:
            mem_mb=2000,
        log:
            RESDIR
            + "logs/add_existing_baseyear_elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.log",
        benchmark:
            RESDIR
            +"benchmarks/add_existing_baseyear/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export"
        script:
            "scripts/add_existing_baseyear.py"

    def input_profile_tech_brownfield(w):
        return {
            f"profile_{tech}": f"resources/"
            + RDIR
            + "renewable_profiles/profile_{tech}.nc"
            for tech in config["electricity"]["renewable_carriers"]
            if tech != "hydro"
        }

    def solved_previous_horizon(w):
        planning_horizons = config["scenario"]["planning_horizons"]
        i = planning_horizons.index(int(w.planning_horizons))
        planning_horizon_p = str(planning_horizons[i - 1])

        return (
            RESDIR
            + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_"
            + planning_horizon_p
            + "_{discountrate}_{demand}_{h2export}export.nc"
        )

    rule add_brownfield:
        params:
            H2_retrofit=config["sector"]["hydrogen"],
            H2_retrofit_capacity_per_CH4=config["sector"]["hydrogen"][
                "H2_retrofit_capacity_per_CH4"
            ],
            threshold_capacity=config["existing_capacities"]["threshold_capacity"],
            snapshots=config["snapshots"],
            # drop_leap_day=config["enable"]["drop_leap_day"],
            carriers=config["electricity"]["renewable_carriers"],
        input:
            # unpack(input_profile_tech_brownfield),
            simplify_busmap="resources/" + RDIR + "bus_regions/busmap_elec_s{simpl}.csv",
            cluster_busmap="resources/"
            + RDIR
            + "bus_regions/busmap_elec_s{simpl}_{clusters}.csv",
            network=RESDIR
            + "prenetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            network_p=solved_previous_horizon,  #solved network at previous time step
            costs=CDIR + "costs_{planning_horizons}.csv",
            cop_soil_total="resources/"
            + SECDIR
            + "cops/cop_soil_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
            cop_air_total="resources/"
            + SECDIR
            + "cops/cop_air_total_elec_s{simpl}_{clusters}_{planning_horizons}.nc",
        output:
            RESDIR
            + "prenetworks-brownfield/elec_s{simpl}_{clusters}_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
        threads: 4
        resources:
            mem_mb=10000,
        log:
            RESDIR
            + "logs/add_brownfield_elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.log",
        benchmark:
            (
                RESDIR
                + "benchmarks/add_brownfield/elec_s{simpl}_ec_{clusters}_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export"
            )
        script:
            "./scripts/add_brownfield.py"

    ruleorder: add_existing_baseyear > add_brownfield

    rule solve_network_myopic:
        params:
            solving=config["solving"],
            foresight=config["foresight"],
            planning_horizons=config["scenario"]["planning_horizons"],
            co2_sequestration_potential=config["scenario"].get(
                "co2_sequestration_potential", 200
            ),
        input:
            overrides="data/override_component_attrs",
            network=RESDIR
            + "prenetworks-brownfield/elec_s{simpl}_{clusters}_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            costs=CDIR + "costs_{planning_horizons}.csv",
            configs=SDIR + "configs/config.yaml",  # included to trigger copy_config rule
        output:
            network=RESDIR
            + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
            # config=RESDIR
            # + "configs/config.elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.yaml",
        shadow:
            "shallow"
        log:
            solver=RESDIR
            + "logs/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export_solver.log",
            python=RESDIR
            + "logs/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export_python.log",
            memory=RESDIR
            + "logs/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export_memory.log",
        threads: 25
        resources:
            mem_mb=config["solving"]["mem"],
        benchmark:
            (
                RESDIR
                + "benchmarks/solve_network/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export"
            )
        script:
            "./scripts/solve_network.py"

    rule solve_all_networks_myopic:
        input:
            networks=expand(
                RESDIR
                + "postnetworks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}_{sopts}_{planning_horizons}_{discountrate}_{demand}_{h2export}export.nc",
                **config["scenario"],
                **config["costs"],
                **config["export"],
            ),


rule run_scenario:
    input:
        diff_config="configs/scenarios/config.{scenario_name}.yaml",
    output:
        touchfile=touch("results/{scenario_name}/scenario.done"),
        copyconfig="results/{scenario_name}/config.yaml",
    threads: 1
    resources:
        mem_mb=5000,
    run:
        from build_test_configs import create_test_config
        import yaml
        from subprocess import run

        # get base configuration file from diff config
        with open(input.diff_config) as f:
            base_config_path = (
                yaml.full_load(f)
                .get("run", {})
                .get("base_config", "config.default.yaml")
            )

            # Ensure the scenario name matches the name of the configuration
        create_test_config(
            input.diff_config,
            {"run": {"name": wildcards.scenario_name}},
            input.diff_config,
        )
        # merge the default config file with the difference
        create_test_config(base_config_path, input.diff_config, "config.yaml")
        run(
            "snakemake -j all solve_all_networks --rerun-incomplete",
            shell=True,
            check=not config["run"]["allow_scenario_failure"],
        )
        run(
            "snakemake -j1 make_statistics --force",
            shell=True,
            check=not config["run"]["allow_scenario_failure"],
        )
        copyfile("config.yaml", output.copyconfig)



rule run_all_scenarios:
    input:
        expand(
            "results/{scenario_name}/scenario.done",
            scenario_name=[
                c.stem.replace("config.", "")
                for c in Path("configs/scenarios").glob("config.*.yaml")
            ],
        ),
