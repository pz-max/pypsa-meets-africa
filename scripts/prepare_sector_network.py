# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
import logging
import os
import re
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa
import pytz
import ruamel.yaml
import xarray as xr
from _helpers import (
    BASE_DIR,
    create_dummy_data,
    create_network_topology,
    cycling_shift,
    locate_bus,
    mock_snakemake,
    override_component_attrs,
    prepare_costs,
    safe_divide,
    three_2_two_digits_country,
    two_2_three_digits_country,
)
from prepare_transport_data import prepare_transport_data

logger = logging.getLogger(__name__)

spatial = SimpleNamespace()


def add_lifetime_wind_solar(n, costs):
    """
    Add lifetime for solar and wind generators.
    """
    for carrier in ["solar", "onwind", "offwind"]:
        gen_i = n.generators.index.str.contains(carrier)
        n.generators.loc[gen_i, "lifetime"] = costs.at[carrier, "lifetime"]


def add_carrier_buses(n, carrier, nodes=None):
    """
    Add buses to connect e.g. coal, nuclear and oil plants.
    """

    if nodes is None:
        nodes = vars(spatial)[carrier].nodes
    location = vars(spatial)[carrier].locations

    # skip if carrier already exists
    if carrier in n.carriers.index:
        return

    if not isinstance(nodes, pd.Index):
        nodes = pd.Index(nodes)

    n.add("Carrier", carrier, co2_emissions=costs.at[carrier, "CO2 intensity"])

    n.madd("Bus", nodes, location=location, carrier=carrier)

    # initial fossil reserves
    e_initial = (snakemake.config["fossil_reserves"]).get(carrier, 0) * 1e6
    # capital cost could be corrected to e.g. 0.2 EUR/kWh * annuity and O&M
    n.madd(
        "Store",
        nodes + " Store",
        bus=nodes,
        e_nom_extendable=True,
        e_cyclic=True if e_initial == 0 else False,
        carrier=carrier,
        e_initial=e_initial,
    )

    n.madd(
        "Generator",
        nodes,
        bus=nodes,
        p_nom_extendable=True,
        carrier=carrier,
        marginal_cost=costs.at[carrier, "fuel"],
    )


def add_generation(
    n, costs, existing_capacities=0, existing_efficiencies=None, existing_nodes=None
):
    """
    Adds conventional generation as specified in config.

    Args:
        n (network): PyPSA prenetwork
        costs (dataframe): _description_
        existing_capacities: dictionary containing installed capacities for conventional_generation technologies
        existing_efficiencies: dictionary containing efficiencies for conventional_generation technologies
        existing_nodes: dictionary containing nodes for conventional_generation technologies

    Returns:
        _type_: _description_
    """ """"""

    logger.info("adding electricity generation")

    # Not required, because nodes are already defined in "nodes"
    # nodes = pop_layout.index

    fallback = {"OCGT": "gas"}
    conventionals = options.get("conventional_generation", fallback)

    for generator, carrier in conventionals.items():
        add_carrier_buses(n, carrier)
        carrier_nodes = vars(spatial)[carrier].nodes
        link_names = spatial.nodes + " " + generator
        n.madd(
            "Link",
            link_names,
            bus0=carrier_nodes,
            bus1=spatial.nodes,
            bus2="co2 atmosphere",
            marginal_cost=costs.at[generator, "efficiency"]
            * costs.at[generator, "VOM"],  # NB: VOM is per MWel
            # NB: fixed cost is per MWel
            capital_cost=costs.at[generator, "efficiency"]
            * costs.at[generator, "fixed"],
            p_nom_extendable=(
                True
                if generator
                in snakemake.params.electricity.get("extendable_carriers", dict()).get(
                    "Generator", list()
                )
                else False
            ),
            p_nom=(
                (
                    existing_capacities[generator] / existing_efficiencies[generator]
                ).reindex(link_names, fill_value=0)
                if not existing_capacities == 0
                else 0
            ),  # NB: existing capacities are MWel
            carrier=generator,
            efficiency=(
                existing_efficiencies[generator].reindex(
                    link_names, fill_value=costs.at[generator, "efficiency"]
                )
                if existing_efficiencies is not None
                else costs.at[generator, "efficiency"]
            ),
            efficiency2=costs.at[carrier, "CO2 intensity"],
            lifetime=costs.at[generator, "lifetime"],
        )

        # set the "co2_emissions" of the carrier to 0, as emissions are accounted by link efficiency separately (efficiency to 'co2 atmosphere' bus)
        n.carriers.loc[carrier, "co2_emissions"] = 0


def H2_liquid_fossil_conversions(n, costs):
    """
    Function to add conversions between H2 and liquid fossil Carrier and bus is
    added in add_oil, which later on might be switched to add_generation.
    """

    n.madd(
        "Link",
        spatial.nodes + " Fischer-Tropsch",
        bus0=spatial.nodes + " H2",
        bus1=spatial.oil.nodes,
        bus2=spatial.co2.nodes,
        bus3=spatial.nodes,
        carrier="Fischer-Tropsch",
        efficiency=costs.at["Fischer-Tropsch", "efficiency"],
        capital_cost=costs.at["Fischer-Tropsch", "fixed"]
        * costs.at[
            "Fischer-Tropsch", "efficiency"
        ],  # Use efficiency to convert from EUR/MW_FT/a to EUR/MW_H2/a
        efficiency2=-costs.at["oil", "CO2 intensity"]
        * costs.at["Fischer-Tropsch", "efficiency"],
        efficiency3=-costs.at["Fischer-Tropsch", "electricity-input"]
        / costs.at["Fischer-Tropsch", "hydrogen-input"],
        p_nom_extendable=True,
        p_min_pu=options.get("min_part_load_fischer_tropsch", 0),
        lifetime=costs.at["Fischer-Tropsch", "lifetime"],
    )


def add_hydrogen(n, costs):
    "function to add hydrogen as an energy carrier with its conversion technologies from and to AC"
    logger.info("Adding hydrogen")

    n.add("Carrier", "H2")

    n.madd(
        "Bus",
        spatial.nodes + " H2",
        location=spatial.nodes,
        carrier="H2",
        x=n.buses.loc[list(spatial.nodes)].x.values,
        y=n.buses.loc[list(spatial.nodes)].y.values,
    )

    # Read hydrogen production options
    h2_options = options.get("hydrogen", {})
    h2_techs = h2_options.get("production", [])
    # H2 production technologies by color
    green_h2_techs = [
        "H2 Electrolysis",
        "Alkaline electrolyzer",
        "PEM electrolyzer",
        "SOEC",
        "Solid biomass steam reforming",
        "Biomass gasification",
        "Biomass gasification CC",
    ]
    grey_h2_techs = [
        "SMR",
        "Natural gas steam reforming",
        "Coal gasification",
        "Heavy oil partial oxidation",
    ]
    blue_h2_techs = ["SMR CC", "Natural gas steam reforming CC", "Coal gasification CC"]

    # Dictionary containing distinct parameters of H2 production technologies
    tech_params = {
        "H2 Electrolysis": {"bus1": spatial.nodes + " grid H2"},
        "Alkaline electrolyzer": {"bus1": spatial.nodes + " grid H2"},
        "PEM electrolyzer": {"bus1": spatial.nodes + " grid H2"},
        "SOEC": {"bus1": spatial.nodes + " grid H2"},
        "Solid biomass steam reforming": {
            "bus0": spatial.biomass.nodes,
            "bus1": spatial.nodes + " grid H2",
            "efficiency2": costs.at["solid biomass", "CO2 intensity"],
        },
        "Biomass gasification": {
            "bus0": spatial.biomass.nodes,
            "bus1": spatial.nodes + " grid H2",
            "efficiency2": costs.at["solid biomass", "CO2 intensity"],
        },
        "Biomass gasification CC": {
            "bus0": spatial.biomass.nodes,
            "bus1": spatial.nodes + " grid H2",
            "bus3": spatial.co2.nodes,
            "efficiency2": costs.at["solid biomass", "CO2 intensity"]
            * (1 - options["cc_fraction"]),
            "efficiency3": costs.at["solid biomass", "CO2 intensity"]
            * options["cc_fraction"],
        },
        "SMR": {
            "bus0": spatial.gas.nodes,
            "bus1": spatial.nodes + " grey H2",
            "efficiency2": costs.at["gas", "CO2 intensity"],
        },
        "SMR CC": {
            "bus0": spatial.gas.nodes,
            "bus1": spatial.nodes + " blue H2",
            "efficiency2": costs.at["gas", "CO2 intensity"]
            * (1 - options["cc_fraction"]),
            "efficiency3": costs.at["gas", "CO2 intensity"] * options["cc_fraction"],
        },
        "Natural gas steam reforming": {
            "bus0": spatial.gas.nodes,
            "bus1": spatial.nodes + " grey H2",
            "efficiency2": costs.at["gas", "CO2 intensity"],
        },
        "Natural gas steam reforming CC": {
            "bus0": spatial.gas.nodes,
            "bus1": spatial.nodes + " blue H2",
            "efficiency2": costs.at["gas", "CO2 intensity"]
            * (1 - options["cc_fraction"]),
            "efficiency3": costs.at["gas", "CO2 intensity"] * options["cc_fraction"],
        },
        "Coal gasification": {
            "bus0": spatial.coal.nodes,
            "bus1": spatial.nodes + " grey H2",
            "efficiency2": costs.at["coal", "CO2 intensity"],
        },
        "Coal gasification CC": {
            "bus0": spatial.coal.nodes,
            "bus1": spatial.nodes + " blue H2",
            "bus3": spatial.co2.nodes,
            "efficiency2": costs.at["coal", "CO2 intensity"]
            * (1 - options["cc_fraction"]),
            "efficiency3": costs.at["coal", "CO2 intensity"] * options["cc_fraction"],
        },
        "Heavy oil partial oxidation": {
            "bus0": spatial.oil.nodes,
            "bus1": spatial.nodes + " grey H2",
            "efficiency2": costs.at["oil", "CO2 intensity"],
        },
    }

    if h2_options.get("hydrogen_colors", False):
        # Add infrastructure for green hydrogen
        if set(h2_techs) & set(green_h2_techs):
            n.madd(
                "Bus",
                nodes + " grid H2",
                location=nodes,
                carrier="grid H2",
                x=n.buses.loc[list(nodes)].x.values,
                y=n.buses.loc[list(nodes)].y.values,
            )

            n.madd(
                "Link",
                nodes + " grid H2",
                bus0=nodes + " grid H2",
                bus1=nodes + " H2",
                p_nom_extendable=True,
                carrier="grid H2",
                efficiency=1,
                capital_cost=0,
            )

        # Add infrastructure for grey hydrogen
        if set(h2_techs) & set(grey_h2_techs):
            n.madd(
                "Bus",
                spatial.nodes + " grey H2",
                location=spatial.nodes,
                carrier="grey H2",
                x=n.buses.loc[list(spatial.nodes)].x.values,
                y=n.buses.loc[list(spatial.nodes)].y.values,
            )

            n.madd(
                "Link",
                spatial.nodes + " grey H2",
                bus0=spatial.nodes + " grey H2",
                bus1=spatial.nodes + " H2",
                carrier="grey H2",
                capital_cost=0,
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

        # Add infrastructure for blue hydrogen
        if set(h2_techs) & set(blue_h2_techs):
            n.madd(
                "Bus",
                spatial.nodes + " blue H2",
                location=spatial.nodes,
                carrier="blue H2",
                x=n.buses.loc[list(spatial.nodes)].x.values,
                y=n.buses.loc[list(spatial.nodes)].y.values,
            )

            n.madd(
                "Link",
                spatial.nodes + " blue H2",
                bus0=spatial.nodes + " blue H2",
                bus1=spatial.nodes + " H2",
                carrier="blue H2",
                capital_cost=0,
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

    # Add hydrogen production technologies
    for h2_tech in h2_techs:
        # Set H2 buses as production output if colors are not used
        if not h2_options.get("hydrogen_colors", False):
            tech_params[h2_tech]["bus1"] = spatial.nodes + " H2"

        # Add electrolysis based green H2 production technologies (links with 2 buses)
        if h2_tech in [
            "H2 Electrolysis",
            "Alkaline electrolyzer",
            "PEM electrolyzer",
            "SOEC",
        ]:
            # H2 production technology name in costs file
            costs_name = "electrolysis" if h2_tech == "H2 Electrolysis" else h2_tech

            n.madd(
                "Link",
                nodes + " " + h2_tech,
                bus0=spatial.nodes,
                bus1=tech_params[h2_tech]["bus1"],
                p_nom_extendable=True,
                carrier=h2_tech,
                efficiency=costs.at[costs_name, "efficiency"],
                capital_cost=costs.at[costs_name, "fixed"],
                lifetime=costs.at[costs_name, "lifetime"],
            )
        # Add H2 production technologies without carbon capture technologies (links with 3 buses)
        if h2_tech in [
            "Solid biomass steam reforming",
            "Biomass gasification",
            "SMR",
            "Natural gas steam reforming",
            "Coal gasification",
            "Heavy oil partial oxidation",
        ]:
            n.madd(
                "Link",
                spatial.nodes + " " + h2_tech,
                bus0=tech_params[h2_tech]["bus0"],
                bus1=tech_params[h2_tech]["bus1"],
                bus2="co2 atmosphere",
                p_nom_extendable=True,
                carrier=h2_tech,
                efficiency=costs.at[h2_tech, "efficiency"],
                efficiency2=tech_params[h2_tech]["efficiency2"],
                capital_cost=costs.at[h2_tech, "fixed"],
                lifetime=costs.at[h2_tech, "lifetime"],
            )
        # Add H2 production technologies with carbon capture technologies (links with 4 buses)
        if h2_tech in [
            "SMR CC",
            "Biomass gasification CC",
            "Natural gas steam reforming CC",
            "Coal gasification CC",
        ]:
            n.madd(
                "Link",
                spatial.nodes + " " + h2_tech,
                bus0=tech_params[h2_tech]["bus0"],
                bus1=tech_params[h2_tech]["bus1"],
                bus2="co2 atmosphere",
                bus3=spatial.co2.nodes,
                p_nom_extendable=True,
                carrier=h2_tech,
                efficiency=costs.at[h2_tech, "efficiency"],
                efficiency2=tech_params[h2_tech]["efficiency2"],
                efficiency3=tech_params[h2_tech]["efficiency3"],
                capital_cost=costs.at[h2_tech, "fixed"],
                lifetime=costs.at[h2_tech, "lifetime"],
            )

    n.madd(
        "Link",
        spatial.nodes + " H2 Fuel Cell",
        bus0=spatial.nodes + " H2",
        bus1=spatial.nodes,
        p_nom_extendable=True,
        carrier="H2 Fuel Cell",
        efficiency=costs.at["fuel cell", "efficiency"],
        # NB: fixed cost is per MWel
        capital_cost=costs.at["fuel cell", "fixed"]
        * costs.at["fuel cell", "efficiency"],
        lifetime=costs.at["fuel cell", "lifetime"],
    )

    cavern_nodes = pd.DataFrame()

    if snakemake.config["sector"]["hydrogen"]["underground_storage"]:
        if snakemake.config["custom_data"]["h2_underground"]:
            custom_cavern = pd.read_csv(
                os.path.join(
                    BASE_DIR,
                    "data/custom/h2_underground_{0}_{1}.csv".format(
                        demand_sc, investment_year
                    ),
                )
            )
            # countries = n.buses.country.unique().to_list()
            countries = snakemake.config["countries"]
            custom_cavern = custom_cavern[custom_cavern.country.isin(countries)]

            cavern_nodes = n.buses[n.buses.country.isin(custom_cavern.country)]

            h2_pot = custom_cavern.set_index("id_region")["storage_cap_MWh"]

            h2_capital_cost = costs.at["hydrogen storage underground", "fixed"]

            # h2_pot.index = cavern_nodes.index

            # n.add("Carrier", "H2 UHS")

            n.madd(
                "Bus",
                nodes + " H2 UHS",
                location=nodes,
                carrier="H2 UHS",
                x=n.buses.loc[list(nodes)].x.values,
                y=n.buses.loc[list(nodes)].y.values,
            )

            n.madd(
                "Store",
                cavern_nodes.index + " H2 UHS",
                bus=cavern_nodes.index + " H2 UHS",
                e_nom_extendable=True,
                e_nom_max=h2_pot.values,
                e_cyclic=True,
                carrier="H2 UHS",
                capital_cost=h2_capital_cost,
            )

            n.madd(
                "Link",
                nodes + " H2 UHS charger",
                bus0=nodes + " H2",
                bus1=nodes + " H2 UHS",
                carrier="H2 UHS charger",
                # efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
                # capital_cost=costs.at["battery inverter", "fixed"],
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

            n.madd(
                "Link",
                nodes + " H2 UHS discharger",
                bus0=nodes + " H2 UHS",
                bus1=nodes + " H2",
                carrier="H2 UHS discharger",
                efficiency=1,
                # capital_cost=costs.at["battery inverter", "fixed"],
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

        else:
            h2_salt_cavern_potential = pd.read_csv(
                snakemake.input.h2_cavern, index_col=0
            ).squeeze()
            h2_cavern_ct = h2_salt_cavern_potential[~h2_salt_cavern_potential.isna()]
            cavern_nodes = n.buses[n.buses.country.isin(h2_cavern_ct.index)]

            h2_capital_cost = costs.at["hydrogen storage underground", "fixed"]

            # assumptions: weight storage potential in a country by population
            # TODO: fix with real geographic potentials
            # convert TWh to MWh with 1e6
            h2_pot = h2_cavern_ct.loc[cavern_nodes.country]
            h2_pot.index = cavern_nodes.index

            # distribute underground potential equally over all nodes #TODO change with real data
            s = pd.Series(h2_pot.index, index=h2_pot.index)
            country_codes = s.str[:2]
            code_counts = country_codes.value_counts()
            fractions = country_codes.map(code_counts).rdiv(1)
            h2_pot = h2_pot * fractions * 1e6

            # n.add("Carrier", "H2 UHS")

            n.madd(
                "Bus",
                nodes + " H2 UHS",
                location=nodes,
                carrier="H2 UHS",
                x=n.buses.loc[list(nodes)].x.values,
                y=n.buses.loc[list(nodes)].y.values,
            )

            n.madd(
                "Store",
                cavern_nodes.index + " H2 UHS",
                bus=cavern_nodes.index + " H2 UHS",
                e_nom_extendable=True,
                e_nom_max=h2_pot.values,
                e_cyclic=True,
                carrier="H2 UHS",
                capital_cost=h2_capital_cost,
            )

            n.madd(
                "Link",
                nodes + " H2 UHS charger",
                bus0=nodes,
                bus1=nodes + " H2 UHS",
                carrier="H2 UHS charger",
                # efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
                capital_cost=0,
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

            n.madd(
                "Link",
                nodes + " H2 UHS discharger",
                bus0=nodes,
                bus1=nodes + " H2 UHS",
                carrier="H2 UHS discharger",
                efficiency=1,
                capital_cost=0,
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

    # hydrogen stored overground (where not already underground)
    h2_capital_cost = costs.at[
        "hydrogen storage tank type 1 including compressor", "fixed"
    ]
    nodes_overground = nodes
    n.madd(
        "Store",
        nodes_overground + " H2 Store Tank",
        bus=nodes_overground + " H2",
        e_nom_extendable=True,
        e_cyclic=True,
        carrier="H2 Store Tank",
        capital_cost=h2_capital_cost,
    )

    # Hydrogen network:
    # -----------------
    def add_links_repurposed_H2_pipelines():
        n.madd(
            "Link",
            h2_links.index + " repurposed",
            bus0=h2_links.bus0.values + " H2",
            bus1=h2_links.bus1.values + " H2",
            p_min_pu=-1,
            p_nom_extendable=True,
            p_nom_max=h2_links.capacity.values
            * 0.8,  # https://gasforclimate2050.eu/wp-content/uploads/2020/07/2020_European-Hydrogen-Backbone_Report.pdf
            length=h2_links.length.values,
            capital_cost=costs.at["H2 (g) pipeline repurposed", "fixed"]
            * h2_links.length.values,
            carrier="H2 pipeline repurposed",
            lifetime=costs.at["H2 (g) pipeline repurposed", "lifetime"],
        )

    def add_links_new_H2_pipelines():
        n.madd(
            "Link",
            h2_links.index,
            bus0=h2_links.bus0.values + " H2",
            bus1=h2_links.bus1.values + " H2",
            p_min_pu=-1,
            p_nom_extendable=True,
            length=h2_links.length.values,
            capital_cost=costs.at["H2 (g) pipeline", "fixed"] * h2_links.length.values,
            carrier="H2 pipeline",
            lifetime=costs.at["H2 (g) pipeline", "lifetime"],
        )

    def add_links_elec_routing_new_H2_pipelines():
        attrs = ["bus0", "bus1", "length"]
        h2_links = pd.DataFrame(columns=attrs)

        candidates = pd.concat(
            {
                "lines": n.lines[attrs],
                "links": n.links.loc[n.links.carrier == "DC", attrs],
            }
        )

        for candidate in candidates.index:
            buses = [
                candidates.at[candidate, "bus0"],
                candidates.at[candidate, "bus1"],
            ]
            buses.sort()
            name = f"H2 pipeline {buses[0]} -> {buses[1]}"
            if name not in h2_links.index:
                h2_links.at[name, "bus0"] = buses[0]
                h2_links.at[name, "bus1"] = buses[1]
                h2_links.at[name, "length"] = candidates.at[candidate, "length"]

        n.madd(
            "Link",
            h2_links.index,
            bus0=h2_links.bus0.values + " H2",
            bus1=h2_links.bus1.values + " H2",
            p_min_pu=-1,
            p_nom_extendable=True,
            length=h2_links.length.values,
            capital_cost=costs.at["H2 (g) pipeline", "fixed"] * h2_links.length.values,
            carrier="H2 pipeline",
            lifetime=costs.at["H2 (g) pipeline", "lifetime"],
        )

    # Add H2 Links:
    if snakemake.config["sector"]["hydrogen"]["network"]:
        h2_links = pd.read_csv(snakemake.input.pipelines)

        # Order buses to detect equal pairs for bidirectional pipelines
        buses_ordered = h2_links.apply(lambda p: sorted([p.bus0, p.bus1]), axis=1)

        # Appending string for carrier specification '_AC', because hydrogen has _AC in bus names
        h2_links["bus0"] = buses_ordered.str[0] + "_AC"
        h2_links["bus1"] = buses_ordered.str[1] + "_AC"

        # Create index column
        h2_links["buses_idx"] = (
            "H2 pipeline " + h2_links["bus0"] + " -> " + h2_links["bus1"]
        )

        # Aggregate pipelines applying mean on length and sum on capacities
        h2_links = h2_links.groupby("buses_idx").agg(
            {"bus0": "first", "bus1": "first", "length": "mean", "capacity": "sum"}
        )

        if len(h2_links) > 0:
            if snakemake.config["sector"]["hydrogen"]["gas_network_repurposing"]:
                add_links_repurposed_H2_pipelines()
            if snakemake.config["sector"]["hydrogen"]["network_routes"] == "greenfield":
                add_links_elec_routing_new_H2_pipelines()
            else:
                add_links_new_H2_pipelines()
        else:
            print(
                "No existing gas network; applying greenfield for H2 network"
            )  # TODO change to logger.info
            add_links_elec_routing_new_H2_pipelines()

        if snakemake.config["sector"]["hydrogen"]["hydrogen_colors"]:
            nuclear_gens_bus = n.generators[
                n.generators.carrier == "nuclear"
            ].bus.values
            buses_with_nuclear = n.buses.loc[nuclear_gens_bus]
            buses_with_nuclear_ind = n.buses.loc[nuclear_gens_bus].index

            # nn.add("Carrier", "nuclear electricity")
            # nn.add("Carrier", "pink H2")

            n.madd(
                "Bus",
                nuclear_gens_bus + " nuclear electricity",
                location=buses_with_nuclear_ind,
                carrier="nuclear electricity",
                x=buses_with_nuclear.x.values,
                y=buses_with_nuclear.y.values,
            )

            n.madd(
                "Bus",
                nuclear_gens_bus + " pink H2",
                location=buses_with_nuclear_ind,
                carrier="pink H2",
                x=buses_with_nuclear.x.values,
                y=buses_with_nuclear.y.values,
            )

            n.generators.loc[n.generators.carrier == "nuclear", "bus"] = (
                n.generators.loc[n.generators.carrier == "nuclear", "bus"]
                + " nuclear electricity"
            )

            n.madd(
                "Link",
                buses_with_nuclear_ind + " nuclear-to-grid",
                bus0=buses_with_nuclear_ind + " nuclear electricity",
                bus1=buses_with_nuclear_ind,
                carrier="nuclear-to-grid",
                capital_cost=0,
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )

            n.madd(
                "Link",
                buses_with_nuclear_ind + " high-temp electrolysis",
                bus0=buses_with_nuclear_ind + " nuclear electricity",
                bus1=buses_with_nuclear_ind + " pink H2",
                carrier="high-temp electrolysis",
                # capital_cost=0,
                p_nom_extendable=True,
                efficiency=costs.at["electrolysis", "efficiency"] + 0.1,
                capital_cost=costs.at["electrolysis", "fixed"]
                + costs.at["electrolysis", "fixed"] * 0.1,
                lifetime=costs.at["electrolysis", "lifetime"],
            )

            n.madd(
                "Link",
                buses_with_nuclear_ind + " pink H2",
                bus0=buses_with_nuclear_ind + " pink H2",
                bus1=buses_with_nuclear_ind + " H2",
                carrier="pink H2",
                # efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
                capital_cost=0,
                p_nom_extendable=True,
                # lifetime=costs.at["battery inverter", "lifetime"],
            )


def define_spatial(nodes, options):
    """
    Namespace for spatial.

    Parameters
    ----------
    nodes : list-like
    """

    global spatial

    spatial.nodes = nodes

    # biomass

    spatial.biomass = SimpleNamespace()

    if options["biomass_transport"]:
        spatial.biomass.nodes = nodes + " solid biomass"
        spatial.biomass.locations = nodes
        spatial.biomass.industry = nodes + " solid biomass for industry"
        spatial.biomass.industry_cc = nodes + " solid biomass for industry CC"
    else:
        spatial.biomass.nodes = ["Earth solid biomass"]
        spatial.biomass.locations = ["Earth"]
        spatial.biomass.industry = ["solid biomass for industry"]
        spatial.biomass.industry_cc = ["solid biomass for industry CC"]

    spatial.biomass.df = pd.DataFrame(vars(spatial.biomass), index=nodes)

    # co2

    spatial.co2 = SimpleNamespace()

    if options["co2_network"]:
        spatial.co2.nodes = nodes + " co2 stored"
        spatial.co2.locations = nodes
        spatial.co2.vents = nodes + " co2 vent"
        # spatial.co2.x = (n.buses.loc[list(nodes)].x.values,)
        # spatial.co2.y = (n.buses.loc[list(nodes)].y.values,)
    else:
        spatial.co2.nodes = ["co2 stored"]
        spatial.co2.locations = ["Earth"]
        spatial.co2.vents = ["co2 vent"]
        # spatial.co2.x = (0,)
        # spatial.co2.y = 0

    spatial.co2.df = pd.DataFrame(vars(spatial.co2), index=nodes)

    # oil

    spatial.oil = SimpleNamespace()

    if options["oil"]["spatial_oil"]:
        spatial.oil.nodes = nodes + " oil"
        spatial.oil.locations = nodes
    else:
        spatial.oil.nodes = ["Earth oil"]
        spatial.oil.locations = ["Earth"]

    # gas

    spatial.gas = SimpleNamespace()

    if options["gas"]["spatial_gas"]:
        spatial.gas.nodes = nodes + " gas"
        spatial.gas.locations = nodes
        spatial.gas.biogas = nodes + " biogas"
        spatial.gas.industry = nodes + " gas for industry"
        if snakemake.config["sector"]["cc"]:
            spatial.gas.industry_cc = nodes + " gas for industry CC"
        spatial.gas.biogas_to_gas = nodes + " biogas to gas"
    else:
        spatial.gas.nodes = ["Earth gas"]
        spatial.gas.locations = ["Earth"]
        spatial.gas.biogas = ["Earth biogas"]
        spatial.gas.industry = ["gas for industry"]
        if snakemake.config["sector"]["cc"]:
            spatial.gas.industry_cc = ["gas for industry CC"]
        spatial.gas.biogas_to_gas = ["Earth biogas to gas"]

    spatial.gas.df = pd.DataFrame(vars(spatial.gas), index=spatial.nodes)

    # coal

    spatial.coal = SimpleNamespace()

    if options["coal"]["spatial_coal"]:
        spatial.coal.nodes = nodes + " coal"
        spatial.coal.locations = nodes
        spatial.coal.industry = nodes + " coal for industry"
    else:
        spatial.coal.nodes = ["Earth coal"]
        spatial.coal.locations = ["Earth"]
        spatial.coal.industry = ["Earth coal for industry"]

    spatial.coal.df = pd.DataFrame(vars(spatial.coal), index=spatial.nodes)

    # lignite

    spatial.lignite = SimpleNamespace()

    if options["lignite"]["spatial_lignite"]:
        spatial.lignite.nodes = nodes + " lignite"
        spatial.lignite.locations = nodes
    else:
        spatial.lignite.nodes = ["Earth lignite"]
        spatial.lignite.locations = ["Earth"]

    spatial.lignite.df = pd.DataFrame(vars(spatial.lignite), index=spatial.nodes)

    return spatial


def add_biomass(n, costs):
    logger.info("adding biomass")

    # TODO get biomass potentials dataset and enable spatially resolved potentials

    # Get biomass and biogas potentials from config and convert from TWh to MWh
    biomass_pot = snakemake.config["sector"]["solid_biomass_potential"] * 1e6  # MWh
    biogas_pot = snakemake.config["sector"]["biogas_potential"] * 1e6  # MWh
    logger.info("Biomass and Biogas potential fetched from config")

    # Convert from total to nodal potentials,
    biomass_pot_spatial = biomass_pot / len(spatial.biomass.nodes)
    biogas_pot_spatial = biogas_pot / len(spatial.gas.biogas)
    logger.info("Biomass potentials spatially resolved equally across all nodes")

    n.add("Carrier", "biogas")
    n.add("Carrier", "solid biomass")

    n.madd(
        "Bus", spatial.gas.biogas, location=spatial.biomass.locations, carrier="biogas"
    )

    n.madd(
        "Bus",
        spatial.biomass.nodes,
        location=spatial.biomass.locations,
        carrier="solid biomass",
    )

    n.madd(
        "Store",
        spatial.gas.biogas,
        bus=spatial.gas.biogas,
        carrier="biogas",
        e_nom=biogas_pot_spatial,
        marginal_cost=costs.at["biogas", "fuel"],
        e_initial=biogas_pot_spatial,
    )

    n.madd(
        "Store",
        spatial.biomass.nodes,
        bus=spatial.biomass.nodes,
        carrier="solid biomass",
        e_nom=biomass_pot_spatial,
        marginal_cost=costs.at["solid biomass", "fuel"],
        e_initial=biomass_pot_spatial,
    )

    biomass_gen = "biomass EOP"
    n.madd(
        "Link",
        spatial.nodes + " biomass EOP",
        bus0=spatial.biomass.nodes,
        bus1=spatial.nodes,
        # bus2="co2 atmosphere",
        marginal_cost=costs.at[biomass_gen, "efficiency"]
        * costs.at[biomass_gen, "VOM"],  # NB: VOM is per MWel
        # NB: fixed cost is per MWel
        capital_cost=costs.at[biomass_gen, "efficiency"]
        * costs.at[biomass_gen, "fixed"],
        p_nom_extendable=True,
        carrier=biomass_gen,
        efficiency=costs.at[biomass_gen, "efficiency"],
        # efficiency2=costs.at["solid biomass", "CO2 intensity"],
        lifetime=costs.at[biomass_gen, "lifetime"],
    )
    n.madd(
        "Link",
        spatial.gas.biogas_to_gas,
        bus0=spatial.gas.biogas,
        bus1=spatial.gas.nodes,
        bus2="co2 atmosphere",
        carrier="biogas to gas",
        capital_cost=costs.loc["biogas upgrading", "fixed"],
        marginal_cost=costs.loc["biogas upgrading", "VOM"],
        efficiency2=-costs.at["gas", "CO2 intensity"],
        p_nom_extendable=True,
    )

    if options["biomass_transport"]:
        # TODO add biomass transport costs
        transport_costs = pd.read_csv(
            snakemake.input.biomass_transport_costs,
            index_col=0,
            keep_default_na=False,
        ).squeeze()

        # add biomass transport
        biomass_transport = create_network_topology(
            n, "biomass transport ", bidirectional=False
        )

        # costs
        countries_not_in_index = set(countries) - set(biomass_transport.index)
        if countries_not_in_index:
            logger.info(
                "No transport values found for {0}, using default value of {1}".format(
                    ", ".join(countries_not_in_index),
                    snakemake.config["sector"]["biomass_transport_default_cost"],
                )
            )

        bus0_costs = biomass_transport.bus0.apply(
            lambda x: transport_costs.get(
                x[:2], snakemake.config["sector"]["biomass_transport_default_cost"]
            )
        )
        bus1_costs = biomass_transport.bus1.apply(
            lambda x: transport_costs.get(
                x[:2], snakemake.config["sector"]["biomass_transport_default_cost"]
            )
        )
        biomass_transport["costs"] = pd.concat([bus0_costs, bus1_costs], axis=1).mean(
            axis=1
        )

        n.madd(
            "Link",
            biomass_transport.index,
            bus0=biomass_transport.bus0 + " solid biomass",
            bus1=biomass_transport.bus1 + " solid biomass",
            p_nom_extendable=True,
            length=biomass_transport.length.values,
            marginal_cost=biomass_transport.costs * biomass_transport.length.values,
            capital_cost=1,
            carrier="solid biomass transport",
        )

    # n.madd(
    #         "Link",
    #         urban_central + " urban central solid biomass CHP",
    #         bus0=spatial.biomass.df.loc[urban_central, "nodes"].values,
    #         bus1=urban_central,
    #         bus2=urban_central + " urban central heat",
    #         carrier="urban central solid biomass CHP",
    #         p_nom_extendable=True,
    #         capital_cost=costs.at[key, "fixed"] * costs.at[key, "efficiency"],
    #         marginal_cost=costs.at[key, "VOM"],
    #         efficiency=costs.at[key, "efficiency"],
    #         efficiency2=costs.at[key, "efficiency-heat"],
    #         lifetime=costs.at[key, "lifetime"],
    #     )

    # AC buses with district heating
    urban_central = n.buses.index[n.buses.carrier == "urban central heat"]
    if not urban_central.empty and options["chp"]:
        urban_central = urban_central.str[: -len(" urban central heat")]

        key = "central solid biomass CHP"

        n.madd(
            "Link",
            urban_central + " urban central solid biomass CHP",
            bus0=spatial.biomass.df.loc[urban_central, "nodes"].values,
            bus1=urban_central,
            bus2=urban_central + " urban central heat",
            carrier="urban central solid biomass CHP",
            p_nom_extendable=True,
            capital_cost=costs.at[key, "fixed"] * costs.at[key, "efficiency"],
            marginal_cost=costs.at[key, "VOM"],
            efficiency=costs.at[key, "efficiency"],
            efficiency2=costs.at[key, "efficiency-heat"],
            lifetime=costs.at[key, "lifetime"],
        )

        if snakemake.config["sector"]["cc"]:
            n.madd(
                "Link",
                urban_central + " urban central solid biomass CHP CC",
                bus0=spatial.biomass.df.loc[urban_central, "nodes"].values,
                bus1=urban_central,
                bus2=urban_central + " urban central heat",
                bus3="co2 atmosphere",
                bus4=spatial.co2.df.loc[urban_central, "nodes"].values,
                carrier="urban central solid biomass CHP CC",
                p_nom_extendable=True,
                capital_cost=costs.at[key, "fixed"] * costs.at[key, "efficiency"]
                + costs.at["biomass CHP capture", "fixed"]
                * costs.at["solid biomass", "CO2 intensity"],
                marginal_cost=costs.at[key, "VOM"],
                efficiency=costs.at[key, "efficiency"]
                - costs.at["solid biomass", "CO2 intensity"]
                * (
                    costs.at["biomass CHP capture", "electricity-input"]
                    + costs.at["biomass CHP capture", "compression-electricity-input"]
                ),
                efficiency2=costs.at[key, "efficiency-heat"]
                + costs.at["solid biomass", "CO2 intensity"]
                * (
                    costs.at["biomass CHP capture", "heat-output"]
                    + costs.at["biomass CHP capture", "compression-heat-output"]
                    - costs.at["biomass CHP capture", "heat-input"]
                ),
                efficiency3=-costs.at["solid biomass", "CO2 intensity"]
                * costs.at["biomass CHP capture", "capture_rate"],
                efficiency4=costs.at["solid biomass", "CO2 intensity"]
                * costs.at["biomass CHP capture", "capture_rate"],
                lifetime=costs.at[key, "lifetime"],
            )


def add_co2(n, costs):
    "add carbon carrier, it's networks and storage units"

    # minus sign because opposite to how fossil fuels used:
    # CH4 burning puts CH4 down, atmosphere up
    n.add("Carrier", "co2", co2_emissions=-1.0)

    # this tracks CO2 in the atmosphere
    n.add(
        "Bus",
        "co2 atmosphere",
        location="Earth",  # TODO Ignoed by pypsa check
        carrier="co2",
    )

    # can also be negative
    n.add(
        "Store",
        "co2 atmosphere",
        e_nom_extendable=True,
        e_min_pu=-1,
        carrier="co2",
        bus="co2 atmosphere",
    )

    # this tracks CO2 stored, e.g. underground
    n.madd(
        "Bus",
        spatial.co2.nodes,
        location=spatial.co2.locations,
        carrier="co2 stored",
        # x=spatial.co2.x[0],
        # y=spatial.co2.y[0],
    )
    """
    co2_stored_x = n.buses.filter(like="co2 stored", axis=0).loc[:, "x"]
    co2_stored_y = n.buses.loc[n.buses[n.buses.carrier == "co2
    stored"].location].y.

    n.buses[n.buses.carrier == "co2 stored"].x = co2_stored_x.values
    n.buses[n.buses.carrier == "co2 stored"].y = co2_stored_y.values
    """

    n.madd(
        "Link",
        spatial.co2.vents,
        bus0=spatial.co2.nodes,
        bus1="co2 atmosphere",
        carrier="co2 vent",
        efficiency=1.0,
        p_nom_extendable=True,
    )

    # logger.info("Adding CO2 network.")
    co2_links = create_network_topology(n, "CO2 pipeline ")

    cost_onshore = (
        (1 - co2_links.underwater_fraction)
        * costs.at["CO2 pipeline", "fixed"]
        * co2_links.length
    )
    cost_submarine = (
        co2_links.underwater_fraction
        * costs.at["CO2 submarine pipeline", "fixed"]
        * co2_links.length
    )
    capital_cost = cost_onshore + cost_submarine

    n.madd(
        "Link",
        co2_links.index,
        bus0=co2_links.bus0.values + " co2 stored",
        bus1=co2_links.bus1.values + " co2 stored",
        p_min_pu=-1,
        p_nom_extendable=True,
        length=co2_links.length.values,
        capital_cost=capital_cost.values,
        carrier="CO2 pipeline",
        lifetime=costs.at["CO2 pipeline", "lifetime"],
    )

    n.madd(
        "Store",
        spatial.co2.nodes,
        e_nom_extendable=True,
        e_nom_max=np.inf,
        capital_cost=options["co2_sequestration_cost"],
        carrier="co2 stored",
        bus=spatial.co2.nodes,
    )

    # logger.info("Adding CO2 network.")
    co2_links = create_network_topology(n, "CO2 pipeline ")

    cost_onshore = (
        (1 - co2_links.underwater_fraction)
        * costs.at["CO2 pipeline", "fixed"]
        * co2_links.length
    )
    cost_submarine = (
        co2_links.underwater_fraction
        * costs.at["CO2 submarine pipeline", "fixed"]
        * co2_links.length
    )
    capital_cost = cost_onshore + cost_submarine


def add_aviation(n, cost):
    all_aviation = ["total international aviation", "total domestic aviation"]

    aviation_demand = (
        energy_totals.loc[countries, all_aviation].sum(axis=1).sum()  # * 1e6 / 8760
    )

    airports = pd.read_csv(snakemake.input.airports, keep_default_na=False)
    airports = airports[airports.country.isin(countries)]

    gadm_layer_id = snakemake.config["build_shape_options"]["gadm_layer_id"]

    airports["gadm_{}".format(gadm_layer_id)] = airports[["x", "y", "country"]].apply(
        lambda airport: locate_bus(
            airport[["x", "y"]],
            airport["country"],
            gadm_layer_id,
            snakemake.input.shapes_path,
            snakemake.config["cluster_options"]["alternative_clustering"],
        ),
        axis=1,
    )
    # To change 3 country code to 2
    # airports["gadm_{}".format(gadm_layer_id)] = airports["gadm_{}".format(gadm_layer_id)].apply(
    # lambda cocode: three_2_two_digits_country(cocode[:3]) + " " + cocode[4:-2])

    airports = airports.set_index("gadm_{}".format(gadm_layer_id))

    ind = pd.DataFrame(n.buses.index[n.buses.carrier == "AC"])

    ind = ind.set_index(n.buses.index[n.buses.carrier == "AC"])
    airports["p_set"] = airports["fraction"].apply(
        lambda frac: frac * aviation_demand * 1e6 / 8760
    )

    airports = pd.concat([airports, ind])

    # airports = airports.fillna(0)

    airports = airports.groupby(airports.index).sum()
    n.madd(
        "Load",
        spatial.nodes,
        suffix=" kerosene for aviation",
        bus=spatial.oil.nodes,
        carrier="kerosene for aviation",
        p_set=airports["p_set"],
    )

    if snakemake.config["sector"]["international_bunkers"]:
        co2 = airports["p_set"].sum() * costs.at["oil", "CO2 intensity"]
    else:
        domestic_to_total = energy_totals["total domestic aviation"] / (
            energy_totals["total international aviation"]
            + energy_totals["total domestic aviation"]
        )

        co2 = (
            airports["p_set"].sum()
            * domestic_to_total
            * costs.at["oil", "CO2 intensity"]
        ).sum()

    n.add(
        "Load",
        "aviation oil emissions",
        bus="co2 atmosphere",
        carrier="oil emissions",
        p_set=-co2,
    )


def add_storage(n, costs):
    "function to add the different types of storage systems"
    logger.info("Add battery storage")

    n.add("Carrier", "battery")

    n.madd(
        "Bus",
        spatial.nodes + " battery",
        location=spatial.nodes,
        carrier="battery",
        x=n.buses.loc[list(spatial.nodes)].x.values,
        y=n.buses.loc[list(spatial.nodes)].y.values,
    )

    n.madd(
        "Store",
        spatial.nodes + " battery",
        bus=spatial.nodes + " battery",
        e_cyclic=True,
        e_nom_extendable=True,
        carrier="battery",
        capital_cost=costs.at["battery storage", "fixed"],
        lifetime=costs.at["battery storage", "lifetime"],
    )

    n.madd(
        "Link",
        spatial.nodes + " battery charger",
        bus0=spatial.nodes,
        bus1=spatial.nodes + " battery",
        carrier="battery charger",
        efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
        capital_cost=costs.at["battery inverter", "fixed"],
        p_nom_extendable=True,
        lifetime=costs.at["battery inverter", "lifetime"],
    )

    n.madd(
        "Link",
        spatial.nodes + " battery discharger",
        bus0=spatial.nodes + " battery",
        bus1=spatial.nodes,
        carrier="battery discharger",
        efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
        marginal_cost=options["marginal_cost_storage"],
        p_nom_extendable=True,
        lifetime=costs.at["battery inverter", "lifetime"],
    )


def h2_hc_conversions(n, costs):
    "function to add the conversion technologies between H2 and hydrocarbons"
    if options["methanation"]:
        n.madd(
            "Link",
            spatial.nodes,
            suffix=" Sabatier",
            bus0=spatial.nodes + " H2",
            bus1=spatial.gas.nodes,
            bus2=spatial.co2.nodes,
            p_nom_extendable=True,
            carrier="Sabatier",
            efficiency=costs.at["methanation", "efficiency"],
            efficiency2=-costs.at["methanation", "efficiency"]
            * costs.at["gas", "CO2 intensity"],
            # costs given per kW_gas
            capital_cost=costs.at["methanation", "fixed"]
            * costs.at["methanation", "efficiency"],
            lifetime=costs.at["methanation", "lifetime"],
        )

    if options["helmeth"]:
        n.madd(
            "Link",
            spatial.nodes,
            suffix=" helmeth",
            bus0=spatial.nodes,
            bus1=spatial.gas.nodes,
            bus2=spatial.co2.nodes,
            carrier="helmeth",
            p_nom_extendable=True,
            efficiency=costs.at["helmeth", "efficiency"],
            efficiency2=-costs.at["helmeth", "efficiency"]
            * costs.at["gas", "CO2 intensity"],
            capital_cost=costs.at["helmeth", "fixed"],
            lifetime=costs.at["helmeth", "lifetime"],
        )


def add_shipping(n, costs):
    ports = pd.read_csv(
        snakemake.input.ports, index_col=None, keep_default_na=False
    ).squeeze()
    ports = ports[ports.country.isin(countries)]

    gadm_layer_id = snakemake.config["build_shape_options"]["gadm_layer_id"]

    all_navigation = ["total international navigation", "total domestic navigation"]

    navigation_demand = (
        energy_totals.loc[countries, all_navigation].sum(axis=1).sum()  # * 1e6 / 8760
    )

    efficiency = (
        options["shipping_average_efficiency"] / costs.at["fuel cell", "efficiency"]
    )

    # check whether item depends on investment year
    shipping_hydrogen_share = get(
        options["shipping_hydrogen_share"], demand_sc + "_" + str(investment_year)
    )

    ports["gadm_{}".format(gadm_layer_id)] = ports[["x", "y", "country"]].apply(
        lambda port: locate_bus(
            port[["x", "y"]],
            port["country"],
            gadm_layer_id,
            snakemake.input["shapes_path"],
            snakemake.config["cluster_options"]["alternative_clustering"],
        ),
        axis=1,
    )

    ports = ports.set_index("gadm_{}".format(gadm_layer_id))

    ind = pd.DataFrame(n.buses.index[n.buses.carrier == "AC"])
    ind = ind.set_index(n.buses.index[n.buses.carrier == "AC"])

    ports["p_set"] = ports["fraction"].apply(
        lambda frac: shipping_hydrogen_share
        * frac
        * navigation_demand
        * efficiency
        * 1e6
        / 8760
        # TODO double check the use of efficiency
    )  # TODO use real data here

    ports = pd.concat([ports, ind]).drop("Bus", axis=1)

    # ports = ports.fillna(0.0)
    ports = ports.groupby(ports.index).sum()

    if options["shipping_hydrogen_liquefaction"]:
        n.madd(
            "Bus",
            spatial.nodes,
            suffix=" H2 liquid",
            carrier="H2 liquid",
            location=spatial.nodes,
        )

        # link the H2 supply to liquified H2
        n.madd(
            "Link",
            spatial.nodes + " H2 liquefaction",
            bus0=spatial.nodes + " H2",
            bus1=spatial.nodes + " H2 liquid",
            carrier="H2 liquefaction",
            efficiency=costs.at["H2 liquefaction", "efficiency"],
            capital_cost=costs.at["H2 liquefaction", "fixed"],
            p_nom_extendable=True,
            lifetime=costs.at["H2 liquefaction", "lifetime"],
        )

        shipping_bus = spatial.nodes + " H2 liquid"
    else:
        shipping_bus = spatial.nodes + " H2"

    if not (
        snakemake.config["policy_config"]["hydrogen"]["is_reference"]
        and snakemake.config["policy_config"]["hydrogen"]["remove_h2_load"]
    ):
        n.madd(
            "Load",
            spatial.nodes,
            suffix=" H2 for shipping",
            bus=shipping_bus,
            carrier="H2 for shipping",
            p_set=ports["p_set"],
        )

    if shipping_hydrogen_share < 1:
        shipping_oil_share = 1 - shipping_hydrogen_share

        ports["p_set"] = ports["fraction"].apply(
            lambda frac: shipping_oil_share * frac * navigation_demand * 1e6 / 8760
        )

        n.madd(
            "Load",
            spatial.nodes,
            suffix=" shipping oil",
            bus=spatial.oil.nodes,
            carrier="shipping oil",
            p_set=ports["p_set"],
        )

        if snakemake.config["sector"]["international_bunkers"]:
            co2 = ports["p_set"].sum() * costs.at["oil", "CO2 intensity"]
        else:
            domestic_to_total = energy_totals["total domestic navigation"] / (
                energy_totals["total domestic navigation"]
                + energy_totals["total international navigation"]
            )

            co2 = (
                ports["p_set"].sum()
                * domestic_to_total
                * costs.at["oil", "CO2 intensity"]
            ).sum()

        n.add(
            "Load",
            "shipping oil emissions",
            bus="co2 atmosphere",
            carrier="shipping oil emissions",
            p_set=-co2,
        )

    if "oil" not in n.buses.carrier.unique():
        n.madd("Bus", spatial.oil.nodes, location=spatial.oil.locations, carrier="oil")
    if "oil" not in n.stores.carrier.unique():
        # could correct to e.g. 0.001 EUR/kWh * annuity and O&M
        n.madd(
            "Store",
            [oil_bus + " Store" for oil_bus in spatial.oil.nodes],
            bus=spatial.oil.nodes,
            e_nom_extendable=True,
            e_cyclic=True,
            carrier="oil",
        )

    if "oil" not in n.generators.carrier.unique():
        n.madd(
            "Generator",
            spatial.oil.nodes,
            bus=spatial.oil.nodes,
            p_nom_extendable=True,
            carrier="oil",
            marginal_cost=costs.at["oil", "fuel"],
        )


def add_industry(n, costs):
    logger.info("adding industrial demand")
    # 1e6 to convert TWh to MWh

    # industrial_demand.reset_index(inplace=True)

    # Add carrier Biomass

    n.madd(
        "Bus",
        spatial.biomass.industry,
        location=spatial.biomass.locations,
        carrier="solid biomass for industry",
    )

    if options["biomass_transport"]:
        p_set = (
            industrial_demand.loc[spatial.biomass.locations, "solid biomass"].rename(
                index=lambda x: x + " solid biomass for industry"
            )
            / 8760
        )
    else:
        p_set = industrial_demand["solid biomass"].sum() / 8760

    n.madd(
        "Load",
        spatial.biomass.industry,
        bus=spatial.biomass.industry,
        carrier="solid biomass for industry",
        p_set=p_set,
    )

    n.madd(
        "Link",
        spatial.biomass.industry,
        bus0=spatial.biomass.nodes,
        bus1=spatial.biomass.industry,
        carrier="solid biomass for industry",
        p_nom_extendable=True,
        efficiency=1.0,
    )
    if snakemake.config["sector"]["cc"]:
        n.madd(
            "Link",
            spatial.biomass.industry_cc,
            bus0=spatial.biomass.nodes,
            bus1=spatial.biomass.industry,
            bus2="co2 atmosphere",
            bus3=spatial.co2.nodes,
            carrier="solid biomass for industry CC",
            p_nom_extendable=True,
            capital_cost=costs.at["cement capture", "fixed"]
            * costs.at["solid biomass", "CO2 intensity"],
            efficiency=0.9,  # TODO: make config option
            efficiency2=-costs.at["solid biomass", "CO2 intensity"]
            * costs.at["cement capture", "capture_rate"],
            efficiency3=costs.at["solid biomass", "CO2 intensity"]
            * costs.at["cement capture", "capture_rate"],
            lifetime=costs.at["cement capture", "lifetime"],
        )

    # CARRIER = FOSSIL GAS

    # nodes = pop_layout.index

    # industrial_demand['TWh/a (MtCO2/a)'] = industrial_demand['TWh/a (MtCO2/a)'].apply(
    #     lambda cocode: two_2_three_digits_country(cocode[:2]) + "." + cocode[3:])

    # industrial_demand.set_index("TWh/a (MtCO2/a)", inplace=True)

    # n.add("Bus", "gas for industry", location="Earth", carrier="gas for industry")
    n.madd(
        "Bus",
        spatial.gas.industry,
        location=spatial.gas.locations,
        carrier="gas for industry",
    )

    gas_demand = industrial_demand.loc[spatial.nodes, "gas"] / 8760.0

    if options["gas"]["spatial_gas"]:
        spatial_gas_demand = gas_demand.rename(index=lambda x: x + " gas for industry")
    else:
        spatial_gas_demand = gas_demand.sum()

    n.madd(
        "Load",
        spatial.gas.industry,
        bus=spatial.gas.industry,
        carrier="gas for industry",
        p_set=spatial_gas_demand,
    )

    n.madd(
        "Link",
        spatial.gas.industry,
        # bus0="Earth gas",
        bus0=spatial.gas.nodes,
        # bus1="gas for industry",
        bus1=spatial.gas.industry,
        bus2="co2 atmosphere",
        carrier="gas for industry",
        p_nom_extendable=True,
        efficiency=1.0,
        efficiency2=costs.at["gas", "CO2 intensity"],
    )
    if snakemake.config["sector"]["cc"]:
        n.madd(
            "Link",
            spatial.gas.industry_cc,
            # suffix=" gas for industry CC",
            # bus0="Earth gas",
            bus0=spatial.gas.nodes,
            bus1=spatial.gas.industry,
            bus2="co2 atmosphere",
            bus3=spatial.co2.nodes,
            carrier="gas for industry CC",
            p_nom_extendable=True,
            capital_cost=costs.at["cement capture", "fixed"]
            * costs.at["gas", "CO2 intensity"],
            efficiency=0.9,
            efficiency2=costs.at["gas", "CO2 intensity"]
            * (1 - costs.at["cement capture", "capture_rate"]),
            efficiency3=costs.at["gas", "CO2 intensity"]
            * costs.at["cement capture", "capture_rate"],
            lifetime=costs.at["cement capture", "lifetime"],
        )

    #################################################### CARRIER = HYDROGEN

    if not (
        snakemake.config["policy_config"]["hydrogen"]["is_reference"]
        and snakemake.config["policy_config"]["hydrogen"]["remove_h2_load"]
    ):
        n.madd(
            "Load",
            nodes,
            suffix=" H2 for industry",
            bus=nodes + " H2",
            carrier="H2 for industry",
            p_set=industrial_demand["hydrogen"].apply(lambda frac: frac / 8760),
        )

    # CARRIER = LIQUID HYDROCARBONS
    n.madd(
        "Load",
        spatial.nodes,
        suffix=" naphtha for industry",
        bus=spatial.oil.nodes,
        carrier="naphtha for industry",
        p_set=industrial_demand["oil"] / 8760,
    )

    #     #NB: CO2 gets released again to atmosphere when plastics decay or kerosene is burned
    #     #except for the process emissions when naphtha is used for petrochemicals, which can be captured with other industry process emissions
    #     #tco2 per hour
    # TODO kerosene for aviation should be added too but in the right func.
    co2_release = [" naphtha for industry"]
    # check land transport

    co2 = (
        n.loads.loc[spatial.nodes + co2_release, "p_set"].sum()
        * costs.at["oil", "CO2 intensity"]
        # - industrial_demand["process emission from feedstock"].sum()
        # / 8760
    )

    n.add(
        "Load",
        "industry oil emissions",
        bus="co2 atmosphere",
        carrier="industry oil emissions",
        p_set=-co2,
    )

    co2 = (
        industrial_demand["coal"].sum()
        * costs.at["coal", "CO2 intensity"]
        # - industrial_demand["process emission from feedstock"].sum()
        / 8760
    )

    n.add(
        "Load",
        "industry coal emissions",
        bus="co2 atmosphere",
        carrier="industry coal emissions",
        p_set=-co2,
    )

    ########################################################### CARRIER = HEAT
    # TODO simplify bus expression
    n.madd(
        "Load",
        spatial.nodes,
        suffix=" low-temperature heat for industry",
        bus=[
            (
                node + " urban central heat"
                if node + " urban central heat" in n.buses.index
                else node + " services urban decentral heat"
            )
            for node in spatial.nodes
        ],
        carrier="low-temperature heat for industry",
        p_set=industrial_demand.loc[spatial.nodes, "low-temperature heat"] / 8760,
    )

    ################################################## CARRIER = ELECTRICITY

    #     # remove today's industrial electricity demand by scaling down total electricity demand
    for ct in n.buses.country.dropna().unique():
        # TODO map onto n.bus.country
        # TODO make sure to check this one, should AC have carrier pf "electricity"?
        loads_i = n.loads.index[
            (n.loads.index.str[:2] == ct) & (n.loads.carrier == "AC")
        ]
        if n.loads_t.p_set.columns.intersection(loads_i).empty:
            continue

    # if not snakemake.config["custom_data"]["elec_demand"]:
    #     # if electricity demand is provided by pypsa-earth, the electricity used
    #     # in industry is included, and need to be removed from the default elec
    #     # demand here, and added as "industry electricity"
    #     factor = (
    #         1
    #         - industrial_demand.loc[loads_i, "current electricity"].sum()
    #         / n.loads_t.p_set[loads_i].sum().sum()
    #     )
    #     n.loads_t.p_set[loads_i] *= factor
    #     industrial_elec = industrial_demand["current electricity"].apply(
    #         lambda frac: frac / 8760
    #     )

    # else:
    industrial_elec = industrial_demand["electricity"] / 8760

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" industry electricity",
        bus=spatial.nodes,
        carrier="industry electricity",
        p_set=industrial_elec,
    )

    n.add("Bus", "process emissions", location="Earth", carrier="process emissions")

    # this should be process emissions fossil+feedstock
    # then need load on atmosphere for feedstock emissions that are currently going to atmosphere via Link Fischer-Tropsch demand
    n.madd(
        "Load",
        spatial.nodes,
        suffix=" process emissions",
        bus="process emissions",
        carrier="process emissions",
        p_set=-(
            #    industrial_demand["process emission from feedstock"]+
            industrial_demand["process emissions"]
        )
        / 8760,
    )

    n.add(
        "Link",
        "process emissions",
        bus0="process emissions",
        bus1="co2 atmosphere",
        carrier="process emissions",
        p_nom_extendable=True,
        efficiency=1.0,
    )

    # assume enough local waste heat for CC
    if snakemake.config["sector"]["cc"]:
        n.madd(
            "Link",
            spatial.co2.locations,
            suffix=" process emissions CC",
            bus0="process emissions",
            bus1="co2 atmosphere",
            bus2=spatial.co2.nodes,
            carrier="process emissions CC",
            p_nom_extendable=True,
            capital_cost=costs.at["cement capture", "fixed"],
            efficiency=1 - costs.at["cement capture", "capture_rate"],
            efficiency2=costs.at["cement capture", "capture_rate"],
            lifetime=costs.at["cement capture", "lifetime"],
        )


def get(item, investment_year=None):
    """
    Check whether item depends on investment year.
    """
    if isinstance(item, dict):
        return item[investment_year]
    else:
        return item


"""
Missing data:
 - transport
 - aviation data
 - nodal_transport_data
 - cycling_shift
 - dsm_profile
 - avail_profile
"""


def add_land_transport(n, costs):
    """
    Function to add land transport to network.
    """
    # TODO options?

    logger.info("adding land transport")

    if options["dynamic_transport"]["enable"] == False:
        fuel_cell_share = get(
            options["land_transport_fuel_cell_share"],
            demand_sc + "_" + str(investment_year),
        )
        electric_share = get(
            options["land_transport_electric_share"],
            demand_sc + "_" + str(investment_year),
        )

    elif options["dynamic_transport"]["enable"] == True:
        fuel_cell_share = options["dynamic_transport"][
            "land_transport_fuel_cell_share"
        ][snakemake.wildcards.opts]
        electric_share = options["dynamic_transport"]["land_transport_electric_share"][
            snakemake.wildcards.opts
        ]

    ice_share = 1 - fuel_cell_share - electric_share

    logger.info("FCEV share: {}".format(fuel_cell_share))
    logger.info("EV share: {}".format(electric_share))
    logger.info("ICEV share: {}".format(ice_share))

    assert ice_share >= 0, "Error, more FCEV and EV share than 1."

    # Nodes are already defined, remove it from here
    # nodes = pop_layout.index

    if electric_share > 0:
        n.add("Carrier", "Li ion")

        n.madd(
            "Bus",
            spatial.nodes,
            location=spatial.nodes,
            suffix=" EV battery",
            carrier="Li ion",
            x=n.buses.loc[list(spatial.nodes)].x.values,
            y=n.buses.loc[list(spatial.nodes)].y.values,
        )

        p_set = (
            electric_share
            * (
                transport[spatial.nodes]
                + cycling_shift(transport[spatial.nodes], 1)
                + cycling_shift(transport[spatial.nodes], 2)
            )
            / 3
        )

        n.madd(
            "Load",
            spatial.nodes,
            suffix=" land transport EV",
            bus=spatial.nodes + " EV battery",
            carrier="land transport EV",
            p_set=p_set,
        )

        p_nom = (
            nodal_transport_data["number cars"]
            * options.get("bev_charge_rate", 0.011)
            * electric_share
        )

        n.madd(
            "Link",
            spatial.nodes,
            suffix=" BEV charger",
            bus0=spatial.nodes,
            bus1=spatial.nodes + " EV battery",
            p_nom=p_nom,
            carrier="BEV charger",
            p_max_pu=avail_profile[spatial.nodes],
            efficiency=options.get("bev_charge_efficiency", 0.9),
            # These were set non-zero to find LU infeasibility when availability = 0.25
            # p_nom_extendable=True,
            # p_nom_min=p_nom,
            # capital_cost=1e6,  #i.e. so high it only gets built where necessary
        )

    if electric_share > 0 and options["v2g"]:
        n.madd(
            "Link",
            spatial.nodes,
            suffix=" V2G",
            bus1=spatial.nodes,
            bus0=spatial.nodes + " EV battery",
            p_nom=p_nom,
            carrier="V2G",
            p_max_pu=avail_profile[spatial.nodes],
            efficiency=options.get("bev_charge_efficiency", 0.9),
        )

    if electric_share > 0 and options["bev_dsm"]:
        e_nom = (
            nodal_transport_data["number cars"]
            * options.get("bev_energy", 0.05)
            * options["bev_availability"]
            * electric_share
        )

        n.madd(
            "Store",
            spatial.nodes,
            suffix=" battery storage",
            bus=spatial.nodes + " EV battery",
            carrier="battery storage",
            e_cyclic=True,
            e_nom=e_nom,
            e_max_pu=1,
            e_min_pu=dsm_profile[spatial.nodes],
        )

    if fuel_cell_share > 0:
        if not (
            snakemake.config["policy_config"]["hydrogen"]["is_reference"]
            and snakemake.config["policy_config"]["hydrogen"]["remove_h2_load"]
        ):
            n.madd(
                "Load",
                nodes,
                suffix=" land transport fuel cell",
                bus=nodes + " H2",
                carrier="land transport fuel cell",
                p_set=fuel_cell_share
                / options["transport_fuel_cell_efficiency"]
                * transport[nodes],
            )

    if ice_share > 0:
        if "oil" not in n.buses.carrier.unique():
            n.madd(
                "Bus", spatial.oil.nodes, location=spatial.oil.locations, carrier="oil"
            )
        ice_efficiency = options["transport_internal_combustion_efficiency"]

        n.madd(
            "Load",
            spatial.nodes,
            suffix=" land transport oil",
            bus=spatial.oil.nodes,
            carrier="land transport oil",
            p_set=ice_share / ice_efficiency * transport[spatial.nodes],
        )

        co2 = (
            ice_share
            / ice_efficiency
            * transport[spatial.nodes].sum().sum()
            / 8760
            * costs.at["oil", "CO2 intensity"]
        )

        n.add(
            "Load",
            "land transport oil emissions",
            bus="co2 atmosphere",
            carrier="land transport oil emissions",
            p_set=-co2,
        )


def create_nodes_for_heat_sector():
    # TODO pop_layout

    # rural are areas with low heating density and individual heating
    # urban are areas with high heating density
    # urban can be split into district heating (central) and individual heating (decentral)

    ct_urban = pop_layout.urban.groupby(pop_layout.ct).sum()
    # distribution of urban population within a country
    pop_layout["urban_ct_fraction"] = pop_layout.urban / pop_layout.ct.map(ct_urban.get)

    sectors = ["residential", "services"]

    h_nodes = {}
    urban_fraction = pop_layout.urban / pop_layout[["rural", "urban"]].sum(axis=1)

    for sector in sectors:
        h_nodes[sector + " rural"] = pop_layout.index
        h_nodes[sector + " urban decentral"] = pop_layout.index

    # maximum potential of urban demand covered by district heating
    central_fraction = options["district_heating"]["potential"]
    # district heating share at each node
    dist_fraction_node = (
        district_heat_share["district heat share"]
        * pop_layout["urban_ct_fraction"]
        / pop_layout["fraction"]
    )
    h_nodes["urban central"] = dist_fraction_node.index
    # if district heating share larger than urban fraction -> set urban
    # fraction to district heating share
    urban_fraction = pd.concat([urban_fraction, dist_fraction_node], axis=1).max(axis=1)
    # difference of max potential and today's share of district heating
    diff = (urban_fraction * central_fraction) - dist_fraction_node
    progress = get(options["district_heating"]["progress"], investment_year)
    dist_fraction_node += diff * progress
    # logger.info(
    #     "The current district heating share compared to the maximum",
    #     f"possible is increased by a progress factor of\n{progress}",
    #     "resulting in a district heating share of",  # "\n{dist_fraction_node}", #TODO fix district heat share
    # )

    return h_nodes, dist_fraction_node, urban_fraction


def add_heat(n, costs):
    # TODO options?
    # TODO pop_layout?

    logger.info("adding heat")

    sectors = ["residential", "services"]

    h_nodes, dist_fraction, urban_fraction = create_nodes_for_heat_sector()

    # NB: must add costs of central heating afterwards (EUR 400 / kWpeak, 50a, 1% FOM from Fraunhofer ISE)

    # exogenously reduce space heat demand
    if options["reduce_space_heat_exogenously"]:
        dE = get(options["reduce_space_heat_exogenously_factor"], investment_year)
        # print(f"assumed space heat reduction of {dE*100} %")
        for sector in sectors:
            heat_demand[sector + " space"] = (1 - dE) * heat_demand[sector + " space"]

    heat_systems = [
        "residential rural",
        "services rural",
        "residential urban decentral",
        "services urban decentral",
        "urban central",
    ]

    for name in heat_systems:
        name_type = "central" if name == "urban central" else "decentral"

        n.add("Carrier", name + " heat")

        n.madd(
            "Bus",
            h_nodes[name] + " {} heat".format(name),
            location=h_nodes[name],
            carrier=name + " heat",
        )

        ## Add heat load

        for sector in sectors:
            # heat demand weighting
            if "rural" in name:
                factor = 1 - urban_fraction[h_nodes[name]]
            elif "urban central" in name:
                factor = dist_fraction[h_nodes[name]]
            elif "urban decentral" in name:
                factor = urban_fraction[h_nodes[name]] - dist_fraction[h_nodes[name]]
            else:
                raise NotImplementedError(
                    f" {name} not in " f"heat systems: {heat_systems}"
                )

            if sector in name:
                heat_load = (
                    heat_demand[[sector + " water", sector + " space"]]
                    .groupby(level=1, axis=1)
                    .sum()[h_nodes[name]]
                    .multiply(factor)
                )

        if name == "urban central":
            heat_load = (
                heat_demand.groupby(level=1, axis=1)
                .sum()[h_nodes[name]]
                .multiply(
                    factor * (1 + options["district_heating"]["district_heating_loss"])
                )
            )

        n.madd(
            "Load",
            h_nodes[name],
            suffix=f" {name} heat",
            bus=h_nodes[name] + f" {name} heat",
            carrier=name + " heat",
            p_set=heat_load,
        )

        ## Add heat pumps

        heat_pump_type = "air" if "urban" in name else "ground"

        costs_name = f"{name_type} {heat_pump_type}-sourced heat pump"
        cop = {"air": ashp_cop, "ground": gshp_cop}
        efficiency = (
            cop[heat_pump_type][h_nodes[name]]
            if options["time_dep_hp_cop"]
            else costs.at[costs_name, "efficiency"]
        )

        n.madd(
            "Link",
            h_nodes[name],
            suffix=f" {name} {heat_pump_type} heat pump",
            bus0=h_nodes[name],
            bus1=h_nodes[name] + f" {name} heat",
            carrier=f"{name} {heat_pump_type} heat pump",
            efficiency=efficiency,
            capital_cost=costs.at[costs_name, "efficiency"]
            * costs.at[costs_name, "fixed"],
            p_nom_extendable=True,
            lifetime=costs.at[costs_name, "lifetime"],
        )

        if options["tes"]:
            n.add("Carrier", name + " water tanks")

            n.madd(
                "Bus",
                h_nodes[name] + f" {name} water tanks",
                location=h_nodes[name],
                carrier=name + " water tanks",
            )

            n.madd(
                "Link",
                h_nodes[name] + f" {name} water tanks charger",
                bus0=h_nodes[name] + f" {name} heat",
                bus1=h_nodes[name] + f" {name} water tanks",
                efficiency=costs.at["water tank charger", "efficiency"],
                carrier=name + " water tanks charger",
                p_nom_extendable=True,
            )

            n.madd(
                "Link",
                h_nodes[name] + f" {name} water tanks discharger",
                bus0=h_nodes[name] + f" {name} water tanks",
                bus1=h_nodes[name] + f" {name} heat",
                carrier=name + " water tanks discharger",
                efficiency=costs.at["water tank discharger", "efficiency"],
                p_nom_extendable=True,
            )

            if isinstance(options["tes_tau"], dict):
                tes_time_constant_days = options["tes_tau"][name_type]
            else:  # TODO add logger
                # logger.warning("Deprecated: a future version will require you to specify 'tes_tau' ",
                # "for 'decentral' and 'central' separately.")
                tes_time_constant_days = (
                    options["tes_tau"] if name_type == "decentral" else 180.0
                )

            # conversion from EUR/m^3 to EUR/MWh for 40 K diff and 1.17 kWh/m^3/K
            capital_cost = (
                costs.at[name_type + " water tank storage", "fixed"] / 0.00117 / 40
            )

            n.madd(
                "Store",
                h_nodes[name] + f" {name} water tanks",
                bus=h_nodes[name] + f" {name} water tanks",
                e_cyclic=True,
                e_nom_extendable=True,
                carrier=name + " water tanks",
                standing_loss=1 - np.exp(-1 / 24 / tes_time_constant_days),
                capital_cost=capital_cost,
                lifetime=costs.at[name_type + " water tank storage", "lifetime"],
            )

        if options["boilers"]:
            key = f"{name_type} resistive heater"

            n.madd(
                "Link",
                h_nodes[name] + f" {name} resistive heater",
                bus0=h_nodes[name],
                bus1=h_nodes[name] + f" {name} heat",
                carrier=name + " resistive heater",
                efficiency=costs.at[key, "efficiency"],
                capital_cost=costs.at[key, "efficiency"] * costs.at[key, "fixed"],
                p_nom_extendable=True,
                lifetime=costs.at[key, "lifetime"],
            )

            key = f"{name_type} gas boiler"

            n.madd(
                "Link",
                h_nodes[name] + f" {name} gas boiler",
                p_nom_extendable=True,
                bus0=spatial.gas.nodes,
                bus1=h_nodes[name] + f" {name} heat",
                bus2="co2 atmosphere",
                carrier=name + " gas boiler",
                efficiency=costs.at[key, "efficiency"],
                efficiency2=costs.at["gas", "CO2 intensity"],
                capital_cost=costs.at[key, "efficiency"] * costs.at[key, "fixed"],
                lifetime=costs.at[key, "lifetime"],
            )

        if options["solar_thermal"]:
            n.add("Carrier", name + " solar thermal")

            n.madd(
                "Generator",
                h_nodes[name],
                suffix=f" {name} solar thermal collector",
                bus=h_nodes[name] + f" {name} heat",
                carrier=name + " solar thermal",
                p_nom_extendable=True,
                capital_cost=costs.at[name_type + " solar thermal", "fixed"],
                p_max_pu=solar_thermal[h_nodes[name]],
                lifetime=costs.at[name_type + " solar thermal", "lifetime"],
            )

        if options["chp"] and name == "urban central":
            # add gas CHP; biomass CHP is added in biomass section
            n.madd(
                "Link",
                h_nodes[name] + " urban central gas CHP",
                bus0=spatial.gas.nodes,
                bus1=h_nodes[name],
                bus2=h_nodes[name] + " urban central heat",
                bus3="co2 atmosphere",
                carrier="urban central gas CHP",
                p_nom_extendable=True,
                capital_cost=costs.at["central gas CHP", "fixed"]
                * costs.at["central gas CHP", "efficiency"],
                marginal_cost=costs.at["central gas CHP", "VOM"],
                efficiency=costs.at["central gas CHP", "efficiency"],
                efficiency2=costs.at["central gas CHP", "efficiency"]
                / costs.at["central gas CHP", "c_b"],
                efficiency3=costs.at["gas", "CO2 intensity"],
                lifetime=costs.at["central gas CHP", "lifetime"],
            )
            if snakemake.config["sector"]["cc"]:
                n.madd(
                    "Link",
                    h_nodes[name] + " urban central gas CHP CC",
                    # bus0="Earth gas",
                    bus0=spatial.gas.nodes,
                    bus1=h_nodes[name],
                    bus2=h_nodes[name] + " urban central heat",
                    bus3="co2 atmosphere",
                    bus4=spatial.co2.df.loc[h_nodes[name], "nodes"].values,
                    carrier="urban central gas CHP CC",
                    p_nom_extendable=True,
                    capital_cost=costs.at["central gas CHP", "fixed"]
                    * costs.at["central gas CHP", "efficiency"]
                    + costs.at["biomass CHP capture", "fixed"]
                    * costs.at["gas", "CO2 intensity"],
                    marginal_cost=costs.at["central gas CHP", "VOM"],
                    efficiency=costs.at["central gas CHP", "efficiency"]
                    - costs.at["gas", "CO2 intensity"]
                    * (
                        costs.at["biomass CHP capture", "electricity-input"]
                        + costs.at[
                            "biomass CHP capture", "compression-electricity-input"
                        ]
                    ),
                    efficiency2=costs.at["central gas CHP", "efficiency"]
                    / costs.at["central gas CHP", "c_b"]
                    + costs.at["gas", "CO2 intensity"]
                    * (
                        costs.at["biomass CHP capture", "heat-output"]
                        + costs.at["biomass CHP capture", "compression-heat-output"]
                        - costs.at["biomass CHP capture", "heat-input"]
                    ),
                    efficiency3=costs.at["gas", "CO2 intensity"]
                    * (1 - costs.at["biomass CHP capture", "capture_rate"]),
                    efficiency4=costs.at["gas", "CO2 intensity"]
                    * costs.at["biomass CHP capture", "capture_rate"],
                    lifetime=costs.at["central gas CHP", "lifetime"],
                )

        if options["chp"] and options["micro_chp"] and name != "urban central":
            n.madd(
                "Link",
                h_nodes[name] + f" {name} micro gas CHP",
                p_nom_extendable=True,
                # bus0="Earth gas",
                bus0=spatial.gas.nodes,
                bus1=h_nodes[name],
                bus2=h_nodes[name] + f" {name} heat",
                bus3="co2 atmosphere",
                carrier=name + " micro gas CHP",
                efficiency=costs.at["micro CHP", "efficiency"],
                efficiency2=costs.at["micro CHP", "efficiency-heat"],
                efficiency3=costs.at["gas", "CO2 intensity"],
                capital_cost=costs.at["micro CHP", "fixed"],
                lifetime=costs.at["micro CHP", "lifetime"],
            )


def average_every_nhours(n, offset):
    # logger.info(f'Resampling the network to {offset}')
    m = n.copy(with_time=False)

    snapshot_weightings = n.snapshot_weightings.resample(offset.casefold()).sum()
    m.set_snapshots(snapshot_weightings.index)
    m.snapshot_weightings = snapshot_weightings

    for c in n.iterate_components():
        pnl = getattr(m, c.list_name + "_t")
        for k, df in c.pnl.items():
            if not df.empty:
                if c.list_name == "stores" and k == "e_max_pu":
                    pnl[k] = df.resample(offset.casefold()).min()
                elif c.list_name == "stores" and k == "e_min_pu":
                    pnl[k] = df.resample(offset.casefold()).max()
                else:
                    pnl[k] = df.resample(offset.casefold()).mean()

    return m


def add_dac(n, costs):
    heat_carriers = ["urban central heat", "services urban decentral heat"]
    heat_buses = n.buses.index[n.buses.carrier.isin(heat_carriers)]
    locations = n.buses.location[heat_buses]

    efficiency2 = -(
        costs.at["direct air capture", "electricity-input"]
        + costs.at["direct air capture", "compression-electricity-input"]
    )
    efficiency3 = -(
        costs.at["direct air capture", "heat-input"]
        - costs.at["direct air capture", "compression-heat-output"]
    )

    n.madd(
        "Link",
        heat_buses.str.replace(" heat", " DAC"),
        bus0="co2 atmosphere",
        bus1=spatial.co2.df.loc[locations, "nodes"].values,
        bus2=locations.values,
        bus3=heat_buses,
        carrier="DAC",
        capital_cost=costs.at["direct air capture", "fixed"],
        efficiency=1.0,
        efficiency2=efficiency2,
        efficiency3=efficiency3,
        p_nom_extendable=True,
        lifetime=costs.at["direct air capture", "lifetime"],
    )


def add_services(n, costs):
    nhours = n.snapshot_weightings.generators.sum()
    buses = spatial.nodes.intersection(n.loads_t.p_set.columns)

    profile_residential = normalize_by_country(
        n.loads_t.p_set[buses].reindex(columns=spatial.nodes, fill_value=0.0)
    ).fillna(0)

    p_set_elec = p_set_from_scaling(
        "services electricity", profile_residential, energy_totals, nhours
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" services electricity",
        bus=spatial.nodes,
        carrier="services electricity",
        p_set=p_set_elec,
    )
    p_set_biomass = p_set_from_scaling(
        "services biomass", profile_residential, energy_totals, nhours
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" services biomass",
        bus=spatial.biomass.nodes,
        carrier="services biomass",
        p_set=p_set_biomass,
    )

    # co2 = (
    #     p_set_biomass.sum().sum() * costs.at["solid biomass", "CO2 intensity"]
    # ) / 8760

    # n.add(
    #     "Load",
    #     "services biomass emissions",
    #     bus="co2 atmosphere",
    #     carrier="biomass emissions",
    #     p_set=-co2,
    # )
    p_set_oil = p_set_from_scaling(
        "services oil", profile_residential, energy_totals, nhours
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" services oil",
        bus=spatial.oil.nodes,
        carrier="services oil",
        p_set=p_set_oil,
    )

    # TODO check with different snapshot settings
    co2 = p_set_oil.sum(axis=1).mean() * costs.at["oil", "CO2 intensity"]

    n.add(
        "Load",
        "services oil emissions",
        bus="co2 atmosphere",
        carrier="oil emissions",
        p_set=-co2,
    )

    p_set_gas = p_set_from_scaling(
        "services gas", profile_residential, energy_totals, nhours
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" services gas",
        bus=spatial.gas.nodes,
        carrier="services gas",
        p_set=p_set_gas,
    )

    # TODO check with different snapshot settings
    co2 = p_set_gas.sum(axis=1).mean() * costs.at["gas", "CO2 intensity"]

    n.add(
        "Load",
        "services gas emissions",
        bus="co2 atmosphere",
        carrier="gas emissions",
        p_set=-co2,
    )


def add_agriculture(n, costs):
    n.madd(
        "Load",
        spatial.nodes,
        suffix=" agriculture electricity",
        bus=spatial.nodes,
        carrier="agriculture electricity",
        p_set=nodal_energy_totals.loc[spatial.nodes, "agriculture electricity"]
        * 1e6
        / 8760,
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" agriculture oil",
        bus=spatial.oil.nodes,
        carrier="agriculture oil",
        p_set=nodal_energy_totals.loc[spatial.nodes, "agriculture oil"] * 1e6 / 8760,
    )
    co2 = (
        nodal_energy_totals.loc[spatial.nodes, "agriculture oil"]
        * 1e6
        / 8760
        * costs.at["oil", "CO2 intensity"]
    ).sum()

    n.add(
        "Load",
        "agriculture oil emissions",
        bus="co2 atmosphere",
        carrier="oil emissions",
        p_set=-co2,
    )


def normalize_by_country(df, droplevel=False):
    """
    Auxiliary function to normalize a dataframe by the country.

    If droplevel is False (default), the country level is added to the
    column index If droplevel is True, the original column format is
    preserved
    """
    ret = df.T.groupby(df.columns.str[:2]).apply(lambda x: x / x.sum().sum()).T
    if droplevel:
        return ret.droplevel(0, axis=1)
    else:
        return ret


def group_by_node(df, multiindex=False):
    """
    Auxiliary function to group a dataframe by the node name.
    """
    ret = df.T.groupby(df.columns.str.split(" ").str[0]).sum().T
    if multiindex:
        ret.columns = pd.MultiIndex.from_tuples(zip(ret.columns.str[:2], ret.columns))
    return ret


def normalize_and_group(df, multiindex=False):
    """
    Function to concatenate normalize_by_country and group_by_node.
    """
    return group_by_node(
        normalize_by_country(df, droplevel=True), multiindex=multiindex
    )


def p_set_from_scaling(col, scaling, energy_totals, nhours):
    """
    Function to create p_set from energy_totals, using the per-unit scaling
    dataframe.
    """
    return (
        1e6
        / nhours
        * scaling.mul(energy_totals[col], level=0).droplevel(level=0, axis=1)
    )


def add_residential(n, costs):
    # need to adapt for many countries #TODO

    # if snakemake.config["custom_data"]["heat_demand"]:
    # heat_demand_index=n.loads_t.p.filter(like='residential').filter(like='heat').dropna(axis=1).index
    # oil_res_index=n.loads_t.p.filter(like='residential').filter(like='oil').dropna(axis=1).index

    nhours = n.snapshot_weightings.generators.sum()

    heat_ind = (
        n.loads_t.p_set.filter(like="residential")
        .filter(like="heat")
        .dropna(axis=1)
        .columns
    )
    heat_shape_raw = normalize_by_country(n.loads_t.p_set[heat_ind])
    heat_shape = heat_shape_raw.rename(
        columns=n.loads.bus.map(n.buses.location), level=1
    )
    heat_shape = heat_shape.T.groupby(level=[0, 1]).sum().T

    n.loads_t.p_set[heat_ind] = 1e6 * heat_shape_raw.mul(
        energy_totals["total residential space"]
        + energy_totals["total residential water"]
        - energy_totals["residential heat biomass"]
        - energy_totals["residential heat oil"]
        - energy_totals["residential heat gas"],
        level=0,
    ).droplevel(level=0, axis=1).div(nhours)

    heat_oil_demand = p_set_from_scaling(
        "residential heat oil", heat_shape, energy_totals, nhours
    )
    heat_biomass_demand = p_set_from_scaling(
        "residential heat biomass", heat_shape, energy_totals, nhours
    )

    heat_gas_demand = p_set_from_scaling(
        "residential heat gas", heat_shape, energy_totals, nhours
    )

    res_index = spatial.nodes.intersection(n.loads_t.p_set.columns)
    profile_residential_raw = normalize_by_country(n.loads_t.p_set[res_index])
    profile_residential = profile_residential_raw.rename(
        columns=n.loads.bus.map(n.buses.location), level=1
    )
    profile_residential = profile_residential.T.groupby(level=[0, 1]).sum().T

    p_set_oil = (
        p_set_from_scaling(
            "residential oil", profile_residential, energy_totals, nhours
        )
        + heat_oil_demand
    )

    p_set_biomass = (
        p_set_from_scaling(
            "residential biomass", profile_residential, energy_totals, nhours
        )
        + heat_biomass_demand
    )

    p_set_gas = (
        p_set_from_scaling(
            "residential gas", profile_residential, energy_totals, nhours
        )
        + heat_gas_demand
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" residential oil",
        bus=spatial.oil.nodes,
        carrier="residential oil",
        p_set=p_set_oil,
    )

    # TODO: check 8760 compatibility with different snapshot settings
    co2 = p_set_oil.sum(axis=1).mean() * costs.at["oil", "CO2 intensity"]

    n.add(
        "Load",
        "residential oil emissions",
        bus="co2 atmosphere",
        carrier="oil emissions",
        p_set=-co2,
    )
    n.madd(
        "Load",
        spatial.nodes,
        suffix=" residential biomass",
        bus=spatial.biomass.nodes,
        carrier="residential biomass",
        p_set=p_set_biomass,
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" residential gas",
        bus=spatial.gas.nodes,
        carrier="residential gas",
        p_set=p_set_gas,
    )

    # TODO: check 8760 compatibility with different snapshot settings
    co2 = p_set_gas.sum(axis=1).mean() * costs.at["gas", "CO2 intensity"]

    n.add(
        "Load",
        "residential gas emissions",
        bus="co2 atmosphere",
        carrier="gas emissions",
        p_set=-co2,
    )

    for country in countries:
        rem_heat_demand = (
            energy_totals.loc[country, "total residential space"]
            + energy_totals.loc[country, "total residential water"]
            - energy_totals.loc[country, "residential heat biomass"]
            - energy_totals.loc[country, "residential heat oil"]
            - energy_totals.loc[country, "residential heat gas"]
        )

        heat_buses = (n.loads_t.p_set.filter(regex="heat").filter(like=country)).columns

        safe_division = safe_divide(
            n.loads_t.p_set.filter(like=country)[heat_buses],
            n.loads_t.p_set.filter(like=country)[heat_buses].sum().sum(),
        )
        n.loads_t.p_set.loc[:, heat_buses] = np.where(
            ~np.isnan(safe_division),
            safe_division * rem_heat_demand * 1e6 / nhours,
            0.0,
        )

    # Revise residential electricity demand
    buses = n.buses[n.buses.carrier == "AC"].index.intersection(n.loads_t.p_set.columns)

    profile_pu = normalize_by_country(n.loads_t.p_set[buses]).fillna(0)
    n.loads_t.p_set.loc[:, buses] = p_set_from_scaling(
        "electricity residential", profile_pu, energy_totals, nhours
    )


def add_electricity_distribution_grid(n, costs):
    logger.info("Adding electricity distribution network")
    nodes = pop_layout.index

    n.madd(
        "Bus",
        nodes + " low voltage",
        location=nodes,
        carrier="low voltage",
        unit="MWh_el",
    )

    n.madd(
        "Link",
        nodes + " electricity distribution grid",
        bus0=nodes,
        bus1=nodes + " low voltage",
        p_nom_extendable=True,
        p_min_pu=-1,
        carrier="electricity distribution grid",
        efficiency=1,
        lifetime=costs.at["electricity distribution grid", "lifetime"],
        capital_cost=costs.at["electricity distribution grid", "fixed"],
    )

    # deduct distribution losses from electricity demand as these are included in total load
    # https://nbviewer.org/github/Open-Power-System-Data/datapackage_timeseries/blob/2020-10-06/main.ipynb
    if (
        efficiency := options["transmission_efficiency"]
        .get("electricity distribution grid", {})
        .get("efficiency_static")
    ):
        logger.info(
            f"Deducting distribution losses from electricity demand: {np.around(100*(1-efficiency), decimals=2)}%"
        )
        n.loads_t.p_set.loc[:, n.loads.carrier == "AC"] *= efficiency

    # move AC loads to low voltage buses
    ac_loads = n.loads.index[n.loads.carrier == "AC"]
    n.loads.loc[ac_loads, "bus"] += " low voltage"

    # move industry, rail transport, agriculture and services electricity to low voltage
    loads = n.loads.index[n.loads.carrier.str.contains("electricity")]
    n.loads.loc[loads, "bus"] += " low voltage"

    bevs = n.links.index[n.links.carrier == "BEV charger"]
    n.links.loc[bevs, "bus0"] += " low voltage"

    v2gs = n.links.index[n.links.carrier == "V2G"]
    n.links.loc[v2gs, "bus1"] += " low voltage"

    hps = n.links.index[n.links.carrier.str.contains("heat pump")]
    n.links.loc[hps, "bus0"] += " low voltage"

    rh = n.links.index[n.links.carrier.str.contains("resistive heater")]
    n.links.loc[rh, "bus0"] += " low voltage"

    mchp = n.links.index[n.links.carrier.str.contains("micro gas")]
    n.links.loc[mchp, "bus1"] += " low voltage"

    if options.get("solar_rooftop", False):
        logger.info("Adding solar rooftop technology")
        # set existing solar to cost of utility cost rather the 50-50 rooftop-utility
        solar = n.generators.index[n.generators.carrier == "solar"]
        n.generators.loc[solar, "capital_cost"] = costs.at["solar-utility", "fixed"]
        pop_solar = pop_layout.total.rename(index=lambda x: x + " solar")

        # add max solar rooftop potential assuming 0.1 kW/m2 and 20 m2/person,
        # i.e. 2 kW/person (population data is in thousands of people) so we get MW
        potential = 0.1 * 20 * pop_solar

        n.madd(
            "Generator",
            solar,
            suffix=" rooftop",
            bus=n.generators.loc[solar, "bus"] + " low voltage",
            carrier="solar rooftop",
            p_nom_extendable=True,
            p_nom_max=potential.loc[solar],
            marginal_cost=n.generators.loc[solar, "marginal_cost"],
            capital_cost=costs.at["solar-rooftop", "fixed"],
            efficiency=n.generators.loc[solar, "efficiency"],
            p_max_pu=n.generators_t.p_max_pu[solar],
            lifetime=costs.at["solar-rooftop", "lifetime"],
        )

    if options.get("home_battery", False):
        logger.info("Adding home battery technology")
        n.add("Carrier", "home battery")

        n.madd(
            "Bus",
            nodes + " home battery",
            location=nodes,
            carrier="home battery",
            unit="MWh_el",
        )

        n.madd(
            "Store",
            nodes + " home battery",
            bus=nodes + " home battery",
            location=nodes,
            e_cyclic=True,
            e_nom_extendable=True,
            carrier="home battery",
            capital_cost=costs.at["home battery storage", "fixed"],
            lifetime=costs.at["battery storage", "lifetime"],
        )

        n.madd(
            "Link",
            nodes + " home battery charger",
            bus0=nodes + " low voltage",
            bus1=nodes + " home battery",
            carrier="home battery charger",
            efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
            capital_cost=costs.at["home battery inverter", "fixed"],
            p_nom_extendable=True,
            lifetime=costs.at["battery inverter", "lifetime"],
        )

        n.madd(
            "Link",
            nodes + " home battery discharger",
            bus0=nodes + " home battery",
            bus1=nodes + " low voltage",
            carrier="home battery discharger",
            efficiency=costs.at["battery inverter", "efficiency"] ** 0.5,
            marginal_cost=options["marginal_cost_storage"],
            p_nom_extendable=True,
            lifetime=costs.at["battery inverter", "lifetime"],
        )


# def add_co2limit(n, Nyears=1.0, limit=0.0):
#     print("Adding CO2 budget limit as per unit of 1990 levels of", limit)

#     countries = n.buses.country.dropna().unique()

#     sectors = emission_sectors_from_opts(opts)

#     # convert Mt to tCO2
#     co2_totals = 1e6 * pd.read_csv(snakemake.input.co2_totals_name, index_col=0)

#     co2_limit = co2_totals.loc[countries, sectors].sum().sum()

#     co2_limit *= limit * Nyears

#     n.add(
#         "GlobalConstraint",
#         "CO2Limit",
#         carrier_attribute="co2_emissions",
#         sense="<=",
#         constant=co2_limit,
#     )


def add_custom_water_cost(n):
    for country in countries:
        water_costs = pd.read_csv(
            os.path.join(
                BASE_DIR,
                "resources/custom_data/{}_water_costs.csv".format(country),
                sep=",",
                index_col=0,
            )
        )
        water_costs = water_costs.filter(like=country, axis=0).loc[spatial.nodes]
        electrolysis_links = n.links.filter(like=country, axis=0).filter(
            like="lectrolysis", axis=0
        )

        elec_index = n.links[
            (n.links.carrier == "H2 Electrolysis")
            & (n.links.bus0.str.contains(country))
        ].index
        n.links.loc[elec_index, "marginal_cost"] = water_costs.values
        # n.links.filter(like=country, axis=0).filter(like='lectrolysis', axis=0)["marginal_cost"] = water_costs.values
        # n.links.filter(like=country, axis=0).filter(like='lectrolysis', axis=0).apply(lambda x: water_costs[x.index], axis=0)
        # print(n.links.filter(like=country, axis=0).filter(like='lectrolysis', axis=0).marginal_cost)


def add_rail_transport(n, costs):
    p_set_elec = nodal_energy_totals.loc[spatial.nodes, "electricity rail"]
    p_set_oil = (nodal_energy_totals.loc[spatial.nodes, "total rail"]) - p_set_elec

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" rail transport oil",
        bus=spatial.oil.nodes,
        carrier=" rail transport oil",
        p_set=p_set_oil * 1e6 / 8760,
    )

    n.madd(
        "Load",
        spatial.nodes,
        suffix=" rail transport electricity",
        bus=spatial.nodes,
        carrier=" rail transport electricity",
        p_set=p_set_elec * 1e6 / 8760,
    )


def get_capacities_from_elec(n, carriers, component):
    """
    Gets capacities and efficiencies for {carrier} in n.{component} that were
    previously assigned in add_electricity.
    """
    component_list = ["generators", "storage_units", "links", "stores"]
    component_dict = {name: getattr(n, name) for name in component_list}
    e_nom_carriers = ["stores"]
    nom_col = {x: "e_nom" if x in e_nom_carriers else "p_nom" for x in component_list}
    eff_col = "efficiency"

    capacity_dict = {}
    efficiency_dict = {}
    node_dict = {}
    for carrier in carriers:
        capacity_dict[carrier] = component_dict[component].query("carrier in @carrier")[
            nom_col[component]
        ]
        efficiency_dict[carrier] = component_dict[component].query(
            "carrier in @carrier"
        )[eff_col]
        node_dict[carrier] = component_dict[component].query("carrier in @carrier")[
            "bus"
        ]

    return capacity_dict, efficiency_dict, node_dict


def remove_elec_base_techs(n):
    """
    Remove conventional generators (e.g. OCGT, oil) build in electricity-only network,
    since they're re-added here using links.
    """
    conventional_generators = options.get("conventional_generation", {})
    to_remove = pd.Index(conventional_generators.keys())
    # remove only conventional_generation carriers present in the network
    to_remove = pd.Index(
        snakemake.params.electricity.get("conventional_carriers", [])
    ).intersection(to_remove)

    if to_remove.empty:
        return

    logger.info(f"Removing Generators with carrier {list(to_remove)}")
    names = n.generators.index[n.generators.carrier.isin(to_remove)]
    for name in names:
        n.remove("Generator", name)
    n.carriers.drop(to_remove, inplace=True, errors="ignore")


def remove_carrier_related_components(n, carriers_to_drop):
    """
    Removes carrier related components, such as "Carrier", "Generator", "Link", "Store", and "Storage Unit"
    """
    # remove carriers
    n.carriers.drop(carriers_to_drop, inplace=True, errors="ignore")

    # remove buses, generators, stores, and storage units with carrier to remote
    for c in n.iterate_components(["Bus", "Generator", "Store", "StorageUnit"]):
        logger.info(f"Removing {c.list_name} with carrier {list(carriers_to_drop)}")
        names = c.df.index[c.df.carrier.isin(carriers_to_drop)]
        if c.name == "Bus":
            buses_to_remove = names
        n.mremove(c.name, names)

    # remove links connected to buses that were removed
    links_to_remove = n.links.query(
        "bus0 in @buses_to_remove or bus1 in @buses_to_remove or bus2 in @buses_to_remove or bus3 in @buses_to_remove or bus4 in @buses_to_remove"
    ).index
    logger.info(
        f"Removing links with carrier {list(n.links.loc[links_to_remove].carrier.unique())}"
    )
    n.mremove("Link", links_to_remove)


if __name__ == "__main__":
    if "snakemake" not in globals():
        # from helper import mock_snakemake #TODO remove func from here to helper script
        snakemake = mock_snakemake(
            "prepare_sector_network",
            simpl="",
            clusters="4",
            ll="c1",
            opts="Co2L-4H",
            planning_horizons="2030",
            sopts="144H",
            discountrate=0.071,
            demand="AB",
        )

    # Load population layout
    pop_layout = pd.read_csv(snakemake.input.clustered_pop_layout, index_col=0)

    # Load all sector wildcards
    options = snakemake.config["sector"]

    # Load input network
    overrides = override_component_attrs(snakemake.input.overrides)
    n = pypsa.Network(snakemake.input.network, override_component_attrs=overrides)

    # Fetch the country list from the network
    # countries = list(n.buses.country.unique())
    countries = snakemake.config["countries"]
    # Locate all the AC buses
    nodes = n.buses[
        n.buses.carrier == "AC"
    ].index  # TODO if you take nodes from the index of buses of n it's more than pop_layout
    # clustering of regions must be double checked.. refer to regions onshore

    # Add location. TODO: move it into pypsa-earth
    n.buses.location = n.buses.index

    # Set carrier of AC loads
    n.loads.loc[nodes, "carrier"] = "AC"

    Nyears = n.snapshot_weightings.generators.sum() / 8760

    # Fetch wildcards
    investment_year = int(snakemake.wildcards.planning_horizons[-4:])
    demand_sc = snakemake.wildcards.demand  # loading the demand scenrario wildcard

    # Prepare the costs dataframe
    costs = prepare_costs(
        snakemake.input.costs,
        snakemake.params.costs["USD2013_to_EUR2013"],
        snakemake.params.costs["fill_values"],
        Nyears,
    )

    # Define spatial for biomass and co2. They require the same spatial definition
    spatial = define_spatial(pop_layout.index, options)

    if snakemake.config["foresight"] in ["myopic", "perfect"]:
        add_lifetime_wind_solar(n, costs)

    # TODO logging

    nodal_energy_totals = pd.read_csv(
        snakemake.input.nodal_energy_totals,
        index_col=0,
        keep_default_na=False,
        na_values=[""],
    )
    energy_totals = pd.read_csv(
        snakemake.input.energy_totals,
        index_col=0,
        keep_default_na=False,
        na_values=[""],
    )
    # Get the data required for land transport
    # TODO Leon, This contains transport demand, right? if so let's change it to transport_demand?
    transport = pd.read_csv(snakemake.input.transport, index_col=0, parse_dates=True)

    avail_profile = pd.read_csv(
        snakemake.input.avail_profile, index_col=0, parse_dates=True
    )
    dsm_profile = pd.read_csv(
        snakemake.input.dsm_profile, index_col=0, parse_dates=True
    )
    nodal_transport_data = pd.read_csv(  # TODO This only includes no. of cars, change name to something descriptive?
        snakemake.input.nodal_transport_data, index_col=0
    )

    # Load data required for the heat sector
    heat_demand = pd.read_csv(
        snakemake.input.heat_demand, index_col=0, header=[0, 1], parse_dates=True
    ).fillna(0)
    # Ground-sourced heatpump coefficient of performance
    gshp_cop = pd.read_csv(
        snakemake.input.gshp_cop, index_col=0, parse_dates=True
    )  # only needed with heat dep. hp cop allowed from config
    # TODO add option heat_dep_hp_cop to the config

    # Air-sourced heatpump coefficient of performance
    ashp_cop = pd.read_csv(
        snakemake.input.ashp_cop, index_col=0, parse_dates=True
    )  # only needed with heat dep. hp cop allowed from config

    # Solar thermal availability profiles
    solar_thermal = pd.read_csv(
        snakemake.input.solar_thermal, index_col=0, parse_dates=True
    )
    gshp_cop = pd.read_csv(snakemake.input.gshp_cop, index_col=0, parse_dates=True)

    # Share of district heating at each node
    district_heat_share = pd.read_csv(snakemake.input.district_heat_share, index_col=0)

    # Load data required for aviation and navigation
    # TODO follow the same structure as land transport and heat

    # Load industry demand data
    industrial_demand = pd.read_csv(
        snakemake.input.industrial_demand, index_col=0, header=0
    )  # * 1e6

    ##########################################################################
    ############## Functions adding different carrires and sectors ###########
    ##########################################################################

    # read existing installed capacities of generators
    if options.get("keep_existing_capacities", False):
        existing_capacities, existing_efficiencies, existing_nodes = (
            get_capacities_from_elec(
                n,
                carriers=options.get("conventional_generation").keys(),
                component="generators",
            )
        )
    else:
        existing_capacities, existing_efficiencies, existing_nodes = 0, None, None

    add_co2(n, costs)  # TODO add costs

    # remove conventional generators built in elec-only model
    remove_elec_base_techs(n)

    add_generation(n, costs, existing_capacities, existing_efficiencies, existing_nodes)

    # remove H2 and battery technologies added in elec-only model
    remove_carrier_related_components(n, carriers_to_drop=["H2", "battery"])

    add_hydrogen(n, costs)  # TODO add costs

    add_storage(n, costs)

    H2_liquid_fossil_conversions(n, costs)

    h2_hc_conversions(n, costs)
    add_heat(n, costs)
    add_biomass(n, costs)

    add_industry(n, costs)

    add_shipping(n, costs)

    # Add_aviation runs with dummy data
    add_aviation(n, costs)

    # prepare_transport_data(n)

    add_land_transport(n, costs)

    # if snakemake.config["custom_data"]["transport_demand"]:
    add_rail_transport(n, costs)

    # if snakemake.config["custom_data"]["custom_sectors"]:
    add_agriculture(n, costs)
    add_residential(n, costs)
    add_services(n, costs)

    if options.get("electricity_distribution_grid", False):
        add_electricity_distribution_grid(n, costs)

    sopts = snakemake.wildcards.sopts.split("-")

    for o in sopts:
        m = re.match(r"^\d+h$", o, re.IGNORECASE)
        if m is not None:
            n = average_every_nhours(n, m.group(0))
            break

    # TODO add co2 limit here, if necessary
    # co2_limit_pu = eval(sopts[0][5:])
    # co2_limit = co2_limit_pu *
    # # Add co2 limit
    # co2_limit = 1e9
    # n.add(
    #     "GlobalConstraint",
    #     "CO2Limit",
    #     carrier_attribute="co2_emissions",
    #     sense="<=",
    #     constant=co2_limit,
    # )

    if options["dac"]:
        add_dac(n, costs)

    if snakemake.config["custom_data"]["water_costs"]:
        add_custom_water_cost(n)

    n.export_to_netcdf(snakemake.output[0])

    # TODO changes in case of myopic oversight
