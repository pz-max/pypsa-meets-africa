# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""
Proposed code structure:
X read network (.nc-file)
X add export bus
X connect hydrogen buses (advanced: only ports, not all) to export bus
X add store and connect to export bus
X (add load and connect to export bus) only required if the "store" option fails

Possible improvements:
- Select port buses automatically (with both voronoi and gadm clustering). Use data/ports.csv?
"""


import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import pypsa
from _helpers import locate_bus, override_component_attrs, prepare_costs

logger = logging.getLogger(__name__)


def select_ports(n):
    """
    This function selects the buses where ports are located.
    """

    ports = pd.read_csv(
        snakemake.input.export_ports,
        index_col=None,
        keep_default_na=False,
    ).squeeze()

    gadm_level = snakemake.params.gadm_level

    ports = locate_bus(
        ports,
        countries,
        gadm_level,
        snakemake.input.shapes_path,
        snakemake.params.alternative_clustering,
    )

    # TODO: revise if ports quantity and property by shape become relevant
    # drop duplicated entries
    gcol = "gadm_{}".format(gadm_level)
    ports_sel = ports.loc[~ports[gcol].duplicated(keep="first")].set_index(gcol)

    # Select the hydrogen buses based on nodes with ports
    hydrogen_buses_ports = n.buses.loc[ports_sel.index + " H2"]
    hydrogen_buses_ports.index.name = "Bus"

    return hydrogen_buses_ports


def add_export(n, hydrogen_buses_ports, export_profile):
    country_shape = gpd.read_file(snakemake.input["shapes_path"])
    # Find most northwestern point in country shape and get x and y coordinates
    country_shape = country_shape.to_crs(
        "EPSG:3395"
    )  # Project to Mercator projection (Projected)

    # Get coordinates of the most western and northern point of the country and add a buffer of 2 degrees (equiv. to approx 220 km)
    x_export = country_shape.geometry.centroid.x.min() - 2
    y_export = country_shape.geometry.centroid.y.max() + 2

    # add export bus
    n.add(
        "Bus",
        "H2 export bus",
        carrier="H2",
        location="Earth",
        x=x_export,
        y=y_export,
    )

    # add export links
    logger.info("Adding export links")
    n.madd(
        "Link",
        names=hydrogen_buses_ports.index + " export",
        bus0=hydrogen_buses_ports.index,
        bus1="H2 export bus",
        p_nom_extendable=True,
    )

    export_links = n.links[n.links.index.str.contains("export")]
    logger.info(export_links)

    # add store depending on config settings

    if snakemake.params.store == True:
        if snakemake.params.store_capital_costs == "no_costs":
            capital_cost = 0
        elif snakemake.params.store_capital_costs == "standard_costs":
            capital_cost = costs.at[
                "hydrogen storage tank type 1 including compressor", "fixed"
            ]
        else:
            logger.error(
                f"Value {snakemake.params.store_capital_costs} for ['export']['store_capital_costs'] is not valid"
            )

        n.add(
            "Store",
            "H2 export store",
            bus="H2 export bus",
            e_nom_extendable=True,
            carrier="H2",
            e_initial=0,  # actually not required, since e_cyclic=True
            marginal_cost=0,
            capital_cost=capital_cost,
            e_cyclic=True,
        )

    elif snakemake.params.store == False:
        pass

    if snakemake.params.export_endogenous:
        # add endogenous export by implementing a negative generation
        n.add(
            "Generator",
            "H2 export load",
            bus="H2 export bus",
            carrier="H2",
            sign=-1,
            p_nom_extendable=True,
            marginal_cost=snakemake.params.endogenous_price * (-1),
        )

    else:
        # add exogenous export by implementing a load
        n.add(
            "Load",
            "H2 export load",
            bus="H2 export bus",
            carrier="H2",
            p_set=export_profile,
        )

    return


def create_export_profile():
    """
    This function creates the export profile based on the annual export demand
    and resamples it to temp resolution obtained from the wildcard.
    """

    # convert TWh to MWh
    export_h2 = eval(snakemake.wildcards["h2export"]) * 1e6

    if snakemake.params.export_profile == "constant":
        export_profile = export_h2 / 8760
        snapshots = pd.date_range(freq="h", **snakemake.params.snapshots)
        export_profile = pd.Series(export_profile, index=snapshots)

    elif snakemake.params.export_profile == "ship":
        # Import hydrogen export ship profile and check if it matches the export demand obtained from the wildcard
        export_profile = pd.read_csv(snakemake.input.ship_profile, index_col=0)
        export_profile.index = pd.to_datetime(export_profile.index)
        export_profile = pd.Series(
            export_profile["profile"], index=pd.to_datetime(export_profile.index)
        )

        if np.abs(export_profile.sum() - export_h2) > 1:  # Threshold of 1 MWh
            logger.error(
                f"Sum of ship profile ({export_profile.sum()/1e6} TWh) does not match export demand ({export_h2} TWh)"
            )
            raise ValueError(
                f"Sum of ship profile ({export_profile.sum()/1e6} TWh) does not match export demand ({export_h2} TWh)"
            )

    # Resample to temporal resolution defined in wildcard "sopts" with pandas resample
    sopts = snakemake.wildcards.sopts.split("-")
    export_profile = export_profile.resample(sopts[0].casefold()).mean()

    # revise logger msg
    export_type = snakemake.params.export_profile
    logger.info(
        f"The yearly export demand is {export_h2/1e6} TWh, profile generated based on {export_type} method and resampled to {sopts[0]}"
    )

    return export_profile


if __name__ == "__main__":
    if "snakemake" not in globals():

        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "add_export",
            simpl="",
            clusters="4",
            ll="c1",
            opts="Co2L-4H",
            planning_horizons="2030",
            sopts="144H",
            discountrate="0.071",
            demand="AB",
            h2export="120",
            # configfile="test/config.test1.yaml",
        )

    overrides = override_component_attrs(snakemake.input.overrides)
    n = pypsa.Network(snakemake.input.network, override_component_attrs=overrides)
    countries = list(n.buses.country.unique())

    # Create export profile
    export_profile = create_export_profile()

    # Prepare the costs dataframe
    Nyears = n.snapshot_weightings.generators.sum() / 8760

    costs = prepare_costs(
        snakemake.input.costs,
        snakemake.params.costs["USD2013_to_EUR2013"],
        snakemake.params.costs["fill_values"],
        Nyears,
    )

    # get hydrogen export buses/ports
    hydrogen_buses_ports = select_ports(n)

    # add export value and components to network
    add_export(n, hydrogen_buses_ports, export_profile)

    n.export_to_netcdf(snakemake.output[0])

    logger.info("Network successfully exported")
