# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Build industrial distribution keys from hotmaps database.
"""

import logging
import os
import uuid
from distutils.version import StrictVersion
from itertools import product

import geopandas as gpd
import pandas as pd
from _helpers import locate_bus, three_2_two_digits_country
from shapely.geometry import Point

logger = logging.getLogger(__name__)
gpd_version = StrictVersion(gpd.__version__)


def map_industry_to_buses(df, countries, gadm_level, shapes_path, gadm_clustering):
    """
    Load hotmaps database of industrial sites and map onto bus regions. Build
    industrial demand... Change name and add other functions.

    Function similar to aviation/shipping. Use functions to disaggregate.
    Only cement not steel - proof of concept.
    Change hotmaps to more descriptive name, etc.
    """
    df = locate_bus(df, countries, gadm_level, shapes_path, gadm_clustering).set_index(
        "gadm_" + str(gadm_level)
    )
    return df


def build_nodal_distribution_key(
    industrial_database, regions, industry, countries
):  # returns percentage of co2 emissions
    """
    Build nodal distribution keys for each sector.
    """

    # countries = regions["name"].str[:2].unique()

    keys = pd.DataFrame(index=regions.name, columns=industry, dtype=float)

    pop = pd.read_csv(
        snakemake.input.clustered_pop_layout,
        index_col=0,
        keep_default_na=False,
        na_values=[""],
    )

    gdp = pd.read_csv(
        snakemake.input.clustered_gdp_layout,
        index_col=0,
        keep_default_na=False,
        na_values=[""],
    )

    # pop["country"] = pop.index.str[:2]
    keys["population"] = pop["total"].values / pop["total"].sum()

    keys["gdp"] = gdp["total"].values / gdp["total"].sum()

    for tech, country in product(industry, countries):
        regions_ct = regions.name[regions.name.str.contains(country)]

        facilities = industrial_database.query(
            "country == @country and industry == @tech"
        )
        # TODO adapt for facilities with production values not emissions
        if not facilities.empty:
            indicator = facilities["capacity"]
            if indicator.sum() == 0:
                key = pd.Series(1 / len(facilities), facilities.index)
            else:
                # TODO BEWARE: this is a strong assumption
                # indicator = indicator.fillna(0)
                key = indicator / indicator.sum()
            key = (
                key.groupby(facilities.index).sum().reindex(regions_ct, fill_value=0.0)
            )
        else:
            key = keys.loc[regions_ct, "gdp"]

        keys.loc[regions_ct, tech] = key
    keys["country"] = pop["ct"]
    return keys


def match_technology(df):
    industry_mapping = {
        "Integrated steelworks": "iron and steel",
        "DRI + Electric arc": "iron and steel",
        "Electric arc": "iron and steel",
        "Cement": "non-metallic minerals",
        "HVC": "chemical and petrochemical",
        "Paper": "paper pulp and print",
        "Aluminium": "non-ferrous metals",
    }

    df["industry"] = df["technology"].map(industry_mapping)
    return df


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "build_industrial_distribution_key",
            clusters="4",
            planning_horizons=2050,
            demand="AB",
        )

    regions = gpd.read_file(snakemake.input.regions_onshore)
    shapes_path = snakemake.input.shapes_path

    gadm_level = snakemake.params.gadm_level
    countries = snakemake.params.countries
    gadm_clustering = snakemake.params.alternative_clustering

    # countries = ["EG", "BH"]

    if regions["name"][0][
        :3
    ].isalpha():  # TODO clean later by changing all codes to 2 letters
        regions["name"] = regions["name"].apply(
            lambda name: three_2_two_digits_country(name[:3]) + name[3:]
        )

    if snakemake.params.industry_database:
        logger.info(
            "Using custom industry database from 'data/custom/industrial_database.csv' instead of default"
        )
        geo_locs = pd.read_csv(
            "data/custom/industrial_database.csv",
            sep=",",
            header=0,
            keep_default_na=False,  # , index_col=0
        )
        geo_locs["industry"] = geo_locs["technology"]
    else:
        logger.info("Using default industry database")
        geo_locs = pd.read_csv(
            snakemake.input.industrial_database,
            sep=",",
            header=0,
            keep_default_na=False,  # , index_col=0
        )
        geo_locs = geo_locs[geo_locs["country"].isin(countries)]
        geo_locs["capacity"] = pd.to_numeric(geo_locs.capacity)

        # Call the function to add the "industry" column
        df_with_industry = match_technology(geo_locs)

    geo_locs.capacity = pd.to_numeric(geo_locs.capacity)

    geo_locs = geo_locs[geo_locs.quality != "nonexistent"]

    industry = geo_locs.industry.unique()

    industrial_database = map_industry_to_buses(
        geo_locs[geo_locs.quality != "unavailable"],
        countries,
        gadm_level,
        shapes_path,
        gadm_clustering,
    )

    keys = build_nodal_distribution_key(
        industrial_database, regions, industry, countries
    )

    keys.to_csv(snakemake.output.industrial_distribution_key)
