# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""
Creates networks clustered to ``{cluster}`` number of zones with aggregated
buses, generators and transmission corridors.

Relevant Settings
-----------------

.. code:: yaml

    clustering:
        aggregation_strategies:

    focus_weights:

    solving:
        solver:
            name:

    lines:
        length_factor:

.. seealso::
    Documentation of the configuration file ``config.yaml`` at
    :ref:`toplevel_cf`, :ref:`renewable_cf`, :ref:`solving_cf`, :ref:`lines_cf`

Inputs
------

- ``resources/regions_onshore_elec_s{simpl}.geojson``: confer :ref:`simplify`
- ``resources/regions_offshore_elec_s{simpl}.geojson``: confer :ref:`simplify`
- ``resources/busmap_elec_s{simpl}.csv``: confer :ref:`simplify`
- ``networks/elec_s{simpl}.nc``: confer :ref:`simplify`
- ``data/custom_busmap_elec_s{simpl}_{clusters}.csv``: optional input

Outputs
-------

- ``resources/regions_onshore_elec_s{simpl}_{clusters}.geojson``:

    .. image:: /img/regions_onshore_elec_s_X.png
        :width: 33 %

- ``resources/regions_offshore_elec_s{simpl}_{clusters}.geojson``:

    .. image:: /img/regions_offshore_elec_s_X.png
        :width: 33 %

- ``resources/busmap_elec_s{simpl}_{clusters}.csv``: Mapping of buses from ``networks/elec_s{simpl}.nc`` to ``networks/elec_s{simpl}_{clusters}.nc``;
- ``resources/linemap_elec_s{simpl}_{clusters}.csv``: Mapping of lines from ``networks/elec_s{simpl}.nc`` to ``networks/elec_s{simpl}_{clusters}.nc``;
- ``networks/elec_s{simpl}_{clusters}.nc``:

    .. image:: /img/elec_s_X.png
        :width: 40  %

Description
-----------

.. note::

    **Why is clustering used both in** ``simplify_network`` **and** ``cluster_network`` **?**

        Consider for example a network ``networks/elec_s100_50.nc`` in which
        ``simplify_network`` clusters the network to 100 buses and in a second
        step ``cluster_network``` reduces it down to 50 buses.

        In preliminary tests, it turns out, that the principal effect of
        changing spatial resolution is actually only partially due to the
        transmission network. It is more important to differentiate between
        wind generators with higher capacity factors from those with lower
        capacity factors, i.e. to have a higher spatial resolution in the
        renewable generation than in the number of buses.

        The two-step clustering allows to study this effect by looking at
        networks like ``networks/elec_s100_50m.nc``. Note the additional
        ``m`` in the ``{cluster}`` wildcard. So in the example network
        there are still up to 100 different wind generators.

        In combination these two features allow you to study the spatial
        resolution of the transmission network separately from the
        spatial resolution of renewable generators.

    **Is it possible to run the model without the** ``simplify_network`` **rule?**

        No, the network clustering methods in the PyPSA module
        `pypsa.clustering.spatial <https://github.com/PyPSA/PyPSA/blob/master/pypsa/networkclustering.py>`_
        do not work reliably with multiple voltage levels and transformers.

.. tip::
    The rule :mod:`cluster_all_networks` runs
    for all ``scenario`` s in the configuration file
    the rule :mod:`cluster_network`.

Exemplary unsolved network clustered to 512 nodes:

.. image:: /img/elec_s_512.png
    :width: 40  %
    :align: center

Exemplary unsolved network clustered to 256 nodes:

.. image:: /img/elec_s_256.png
    :width: 40  %
    :align: center

Exemplary unsolved network clustered to 128 nodes:

.. image:: /img/elec_s_128.png
    :width: 40  %
    :align: center

Exemplary unsolved network clustered to 37 nodes:

.. image:: /img/elec_s_37.png
    :width: 40  %
    :align: center
"""

import os
from functools import reduce

import geopandas as gpd
import numpy as np
import pandas as pd
import pyomo.environ as po
import pypsa
from _helpers import (
    REGION_COLS,
    configure_logging,
    create_logger,
    get_aggregation_strategies,
    locate_bus,
    update_p_nom_max,
)
from add_electricity import load_costs
from build_shapes import add_gdp_data, add_population_data
from pypsa.clustering.spatial import (
    busmap_by_greedy_modularity,
    busmap_by_hac,
    busmap_by_kmeans,
    get_clustering_from_busmap,
)
from shapely.geometry import Point

idx = pd.IndexSlice

logger = create_logger(__name__)


def normed(x):
    return (x / x.sum()).fillna(0.0)


def weighting_for_country(n, x):
    conv_carriers = {"OCGT", "CCGT", "PHS", "hydro"}
    gen = n.generators.loc[n.generators.carrier.isin(conv_carriers)].groupby(
        "bus"
    ).p_nom.sum().reindex(n.buses.index, fill_value=0.0) + n.storage_units.loc[
        n.storage_units.carrier.isin(conv_carriers)
    ].groupby(
        "bus"
    ).p_nom.sum().reindex(
        n.buses.index, fill_value=0.0
    )
    load = n.loads_t.p_set.mean().groupby(n.loads.bus).sum()

    b_i = x.index
    g = normed(gen.reindex(b_i, fill_value=0))
    l = normed(load.reindex(b_i, fill_value=0))

    w = g + l

    if w.max() == 0.0:
        logger.warning(
            f"Null weighting for buses of country {x.country.iloc[0]}: returned default uniform weighting"
        )
        return pd.Series(1.0, index=w.index)
    else:
        return (w * (100.0 / w.max())).clip(lower=1.0).astype(int)


def get_feature_for_hac(n, buses_i=None, feature=None):
    if buses_i is None:
        buses_i = n.buses.index

    if feature is None:
        feature = "solar+onwind-time"

    carriers = feature.split("-")[0].split("+")
    if "offwind" in carriers:
        carriers.remove("offwind")
        carriers = np.append(
            carriers, n.generators.carrier.filter(like="offwind").unique()
        )

    if feature.split("-")[1] == "cap":
        feature_data = pd.DataFrame(index=buses_i, columns=carriers)
        for carrier in carriers:
            gen_i = n.generators.query("carrier == @carrier").index
            attach = (
                n.generators_t.p_max_pu[gen_i]
                .mean()
                .rename(index=n.generators.loc[gen_i].bus)
            )
            feature_data[carrier] = attach

    if feature.split("-")[1] == "time":
        feature_data = pd.DataFrame(columns=buses_i)
        for carrier in carriers:
            gen_i = n.generators.query("carrier == @carrier").index
            attach = n.generators_t.p_max_pu[gen_i].rename(
                columns=n.generators.loc[gen_i].bus
            )
            feature_data = pd.concat([feature_data, attach], axis=0)[buses_i]

        feature_data = feature_data.T
        # timestamp raises error in sklearn >= v1.2:
        feature_data.columns = feature_data.columns.astype(str)

    feature_data = feature_data.fillna(0)

    return feature_data


def distribute_clusters(
    inputs,
    build_shape_options,
    country_list,
    distribution_cluster,
    n,
    n_clusters,
    focus_weights=None,
    solver_name=None,
):
    """
    Determine the number of clusters per country.
    """

    year = build_shape_options["year"]
    update = build_shape_options["update_file"]
    out_logging = build_shape_options["out_logging"]
    nprocesses = build_shape_options["nprocesses"]

    if solver_name is None:
        solver_name = snakemake.config["solving"]["solver"]["name"]

    if distribution_cluster == ["load"]:
        L = (
            n.loads_t.p_set.mean()
            .groupby(n.loads.bus)
            .sum()
            .reindex(n.buses.index, fill_value=0.0)
            .groupby([n.buses.country, n.buses.sub_network])
            .sum()
            .pipe(normed)
        )
        countries_in_L = pd.unique(L.index.get_level_values(0))
        assert len(countries_in_L) == len(n.buses.country.unique()), (
            "The following countries have no load: "
            f"{list(set(countries_in_L).symmetric_difference(set(n.buses.country.unique())))}"
        )
        distribution_factor = L

    if distribution_cluster == ["pop"]:
        df_pop_c = gpd.read_file(inputs.country_shapes).rename(
            columns={"name": "country"}
        )
        add_population_data(
            df_pop_c, country_list, "standard", year, update, out_logging
        )
        P = df_pop_c.loc[:, ("country", "pop")]
        n_df = n.buses.copy()[["country", "sub_network"]]

        pop_dict = P.set_index("country")["pop"].to_dict()
        n_df["pop"] = n_df["country"].map(pop_dict)

        distribution_factor = (
            n_df.groupby(["country", "sub_network"]).sum().pipe(normed).squeeze()
        )

    if distribution_cluster == ["gdp"]:
        df_gdp_c = gpd.read_file(inputs.country_shapes).rename(
            columns={"name": "country"}
        )
        add_gdp_data(
            df_gdp_c,
            year,
            update,
            out_logging,
            name_file_nc="GDP_PPP_1990_2015_5arcmin_v2.nc",
        )

        G = df_gdp_c.loc[:, ("country", "gdp")]
        n_df = n.buses.copy()[["country", "sub_network"]]

        gdp_dict = G.set_index("country")["gdp"].to_dict()
        n_df["gdp"] = n_df["country"].map(gdp_dict)

        distribution_factor = (
            n_df.groupby(["country", "sub_network"]).sum().pipe(normed).squeeze()
        )

    # TODO: 1. Check if sub_networks can be added here i.e. ["country", "sub_network"]
    N = n.buses.groupby(["country", "sub_network"]).size()

    assert (
        n_clusters >= len(N) and n_clusters <= N.sum()
    ), f"Number of clusters must be {len(N)} <= n_clusters <= {N.sum()} for this selection of countries."

    if focus_weights is not None:
        total_focus = sum(list(focus_weights.values()))

        assert (
            total_focus <= 1.0
        ), "The sum of focus weights must be less than or equal to 1."

        for country, weight in focus_weights.items():
            distribution_factor[country] = weight / len(distribution_factor[country])

        remainder = [
            c not in focus_weights.keys()
            for c in distribution_factor.index.get_level_values("country")
        ]
        distribution_factor[remainder] = distribution_factor.loc[remainder].pipe(
            normed
        ) * (1 - total_focus)

        logger.warning("Using custom focus weights for determining number of clusters.")

    assert np.isclose(
        distribution_factor.sum(), 1.0, rtol=1e-3
    ), f"Country weights L must sum up to 1.0 when distributing clusters. Is {distribution_factor.sum()}."

    m = po.ConcreteModel()

    def n_bounds(model, *n_id):
        """
        Create a function that makes a bound pair for pyomo.

        Use n_bounds(model, n_id) if N is Single-Index
        Use n_bounds(model, *n_id) if N is Multi-Index
        Example: https://pyomo.readthedocs.io/en/stable/pyomo_modeling_components/Variables.html

        Returns
        -------
        bounds = A function (or Python object) that gives a (lower,upper) bound pair i.e.(1,10) for the variable
        """
        return (1, N[n_id])

    m.n = po.Var(list(distribution_factor.index), bounds=n_bounds, domain=po.Integers)
    m.tot = po.Constraint(expr=(po.summation(m.n) == n_clusters))
    m.objective = po.Objective(
        expr=sum(
            (m.n[i] - distribution_factor.loc[i] * n_clusters) ** 2
            for i in distribution_factor.index
        ),
        sense=po.minimize,
    )

    opt = po.SolverFactory(solver_name)
    if not opt.has_capability("quadratic_objective"):
        logger.warning(
            f"The configured solver `{solver_name}` does not support quadratic objectives. Falling back to `ipopt`."
        )
        opt = po.SolverFactory("ipopt")

    results = opt.solve(m)
    assert (
        results["Solver"][0]["Status"] == "ok"
    ), f"Solver returned non-optimally: {results}"

    return (
        pd.Series(m.n.get_values(), index=distribution_factor.index).round().astype(int)
    )


def busmap_for_gadm_clusters(inputs, n, gadm_level, geo_crs, country_list):

    buses = locate_bus(
        n.buses,
        country_list,
        gadm_level,
        inputs.gadm_shapes,
        gadm_clustering=True,
    )

    buses["gadm_subnetwork"] = (
        buses["gadm_{}".format(gadm_level)] + "_" + buses["carrier"].astype(str)
    )
    busmap = buses["gadm_subnetwork"]

    return busmap


def busmap_for_n_clusters(
    inputs,
    build_shape_options,
    country_list,
    distribution_cluster,
    n,
    n_clusters,
    solver_name,
    focus_weights=None,
    algorithm="kmeans",
    feature=None,
    **algorithm_kwds,
):
    if algorithm == "kmeans":
        algorithm_kwds.setdefault("n_init", 1000)
        algorithm_kwds.setdefault("max_iter", 30000)
        algorithm_kwds.setdefault("tol", 1e-6)
        algorithm_kwds.setdefault("random_state", 0)

    def fix_country_assignment_for_hac(n):
        from scipy.sparse import csgraph

        # overwrite country of nodes that are disconnected from their country-topology
        for country in n.buses.country.unique():
            m = n[n.buses.country == country].copy()

            _, labels = csgraph.connected_components(
                m.adjacency_matrix(), directed=False
            )

            component = pd.Series(labels, index=m.buses.index)
            component_sizes = component.value_counts()

            if len(component_sizes) > 1:
                disconnected_bus = component[
                    component == component_sizes.index[-1]
                ].index[0]

                disconn_bus_line = n.lines.query(
                    "bus0 == @disconnected_bus or bus1 == @disconnected_bus"
                )

                if not disconn_bus_line.empty:
                    neighbor_bus = disconn_bus_line.iloc[0][["bus0", "bus1"]]
                    new_country = list(
                        set(n.buses.loc[neighbor_bus].country) - set([country])
                    )[0]

                    logger.info(
                        f"overwriting country `{country}` of bus `{disconnected_bus}` "
                        f"to new country `{new_country}`, because it is disconnected "
                        "from its initial inter-country transmission grid."
                    )
                    n.buses.at[disconnected_bus, "country"] = new_country
        return n

    if algorithm == "hac":
        feature = get_feature_for_hac(n, buses_i=n.buses.index, feature=feature)
        n = fix_country_assignment_for_hac(n)

    if (algorithm != "hac") and (feature is not None):
        logger.warning(
            f"Keyword argument feature is only valid for algorithm `hac`. "
            f"Given feature `{feature}` will be ignored."
        )

    n.determine_network_topology()
    # n.lines.loc[:, "sub_network"] = "0"  # current fix

    if n.buses.country.nunique() > 1:
        n_clusters = distribute_clusters(
            inputs,
            build_shape_options,
            country_list,
            distribution_cluster,
            n,
            n_clusters,
            focus_weights=focus_weights,
            solver_name=solver_name,
        )

    # TODO Check if `reduce_network()` is used
    def reduce_network(n, buses):
        nr = pypsa.Network()
        nr.import_components_from_dataframe(buses, "Bus")
        nr.import_components_from_dataframe(
            n.lines.loc[
                n.lines.bus0.isin(buses.index) & n.lines.bus1.isin(buses.index)
            ],
            "Line",
        )
        return nr

    def busmap_for_country(x):
        # A number of the countries in the clustering can be > 1
        if isinstance(n_clusters, pd.Series):
            n_cluster_c = n_clusters[x.name]
            if isinstance(x.name, tuple):
                prefix = x.name[0] + x.name[1] + " "
            else:
                prefix = x.name + " "
        else:
            n_cluster_c = n_clusters
            prefix = x.name[0] + x.name[1] + " "

        logger.debug(f"Determining busmap for country {prefix[:-1]}")
        if len(x) == 1:
            return pd.Series(prefix + "0", index=x.index)
        weight = weighting_for_country(n, x)

        if algorithm == "kmeans":
            return prefix + busmap_by_kmeans(
                n, weight, n_cluster_c, buses_i=x.index, **algorithm_kwds
            )
        elif algorithm == "hac":
            return prefix + busmap_by_hac(
                # TODO Check consistency (fix for TypeError: 'int' object is not subscriptable in case of a single country)
                n,
                n_cluster_c,
                buses_i=x.index,
                feature=feature.loc[x.index],
                # n, n_clusters[x.name], buses_i=x.index, feature=feature.loc[x.index]
            )
        elif algorithm == "modularity":
            return prefix + busmap_by_greedy_modularity(
                # TODO Check consistency (fix for TypeError: 'int' object is not subscriptable in case of a single country)
                n,
                n_cluster_c,
                buses_i=x.index,
                # n, n_clusters[x.name], buses_i=x.index
            )
        else:
            raise ValueError(
                f"`algorithm` must be one of 'kmeans' or 'hac'. Is {algorithm}."
            )

    return (
        n.buses.groupby(
            # ["country"],
            ["country", "sub_network"],  # TODO: 2. Add sub_networks (see previous TODO)
            group_keys=False,
        )
        .apply(busmap_for_country, include_groups=False)
        .squeeze(axis=0)
        .rename("busmap")
    )


def clustering_for_n_clusters(
    inputs,
    config,
    n,
    n_clusters,
    alternative_clustering,
    gadm_layer_id,
    geo_crs,
    country_list,
    distribution_cluster,
    build_shape_options,
    custom_busmap=False,
    aggregate_carriers=None,
    line_length_factor=1.25,
    aggregation_strategies=dict(),
    solver_name="cbc",
    algorithm="kmeans",
    feature=None,
    extended_link_costs=0,
    focus_weights=None,
):
    bus_strategies, generator_strategies = get_aggregation_strategies(
        aggregation_strategies
    )

    if not isinstance(custom_busmap, pd.Series):
        if alternative_clustering:
            busmap = busmap_for_gadm_clusters(
                inputs, n, gadm_layer_id, geo_crs, country_list
            )
        else:
            busmap = busmap_for_n_clusters(
                inputs,
                build_shape_options,
                country_list,
                distribution_cluster,
                n,
                n_clusters,
                solver_name,
                focus_weights,
                algorithm,
                feature,
            )
    else:
        busmap = custom_busmap

    clustering = get_clustering_from_busmap(
        n,
        busmap,
        bus_strategies=bus_strategies,
        aggregate_generators_weighted=True,
        aggregate_generators_carriers=aggregate_carriers,
        aggregate_one_ports=["Load", "StorageUnit"],
        line_length_factor=line_length_factor,
        generator_strategies=generator_strategies,
        scale_link_capital_costs=False,
    )

    if not n.links.empty:
        nc = clustering.network
        nc.links["underwater_fraction"] = (
            n.links.eval("underwater_fraction * length").div(nc.links.length).dropna()
        )
        nc.links["capital_cost"] = nc.links["capital_cost"].add(
            (nc.links.length - n.links.length)
            .clip(lower=0)
            .mul(extended_link_costs)
            .dropna(),
            fill_value=0,
        )
    if not n.lines.loc[n.lines.carrier == "DC"].empty:
        clustering.network.lines["underwater_fraction"] = 0

    return clustering


def save_to_geojson(s, fn):
    if os.path.exists(fn):
        os.unlink(fn)
    df = s.reset_index()
    schema = {**gpd.io.file.infer_schema(df), "geometry": "Unknown"}
    df.to_file(fn, driver="GeoJSON", schema=schema)


def cluster_regions(busmaps, inputs, output):
    busmap = reduce(lambda x, y: x.map(y), busmaps[1:], busmaps[0])

    for which in ("regions_onshore", "regions_offshore"):
        # regions = gpd.read_file(getattr(input, which)).set_index("name")
        regions = gpd.read_file(getattr(inputs, which))
        regions = regions.reindex(columns=REGION_COLS).set_index("name")
        aggfunc = dict(x="mean", y="mean", country="first")
        regions_c = regions.dissolve(busmap, aggfunc=aggfunc)
        regions_c.index.name = "name"
        regions_c = regions_c.reset_index()
        regions_c.to_file(getattr(output, which))


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "cluster_network", network="elec", simpl="", clusters="4"
        )
    configure_logging(snakemake)

    inputs, outputs, config = snakemake.input, snakemake.output, snakemake.config

    n = pypsa.Network(inputs.network)

    alternative_clustering = snakemake.params.cluster_options["alternative_clustering"]
    distribution_cluster = snakemake.params.cluster_options["distribute_cluster"]
    gadm_layer_id = snakemake.params.build_shape_options["gadm_layer_id"]
    focus_weights = snakemake.params.get("focus_weights", None)
    country_list = snakemake.params.countries
    geo_crs = snakemake.params.geo_crs

    renewable_carriers = pd.Index(
        [
            tech
            for tech in n.generators.carrier.unique()
            if tech in snakemake.params.renewable  # TODO ror not cap
        ]
    )

    exclude_carriers = snakemake.params.cluster_options["cluster_network"].get(
        "exclude_carriers", []
    )
    aggregate_carriers = set(n.generators.carrier) - set(exclude_carriers)

    n.determine_network_topology()
    if snakemake.wildcards.clusters.endswith("m"):
        n_clusters = int(snakemake.wildcards.clusters[:-1])
        aggregate_carriers = snakemake.params.electricity.get("conventional_carriers")
    elif snakemake.wildcards.clusters.endswith("flex"):
        n_clusters = min(len(n.buses), int(snakemake.wildcards.clusters[:-4]))
    elif snakemake.wildcards.clusters == "all":
        n_clusters = len(n.buses)
    elif snakemake.wildcards.clusters == "min":
        n_clusters = n.buses.groupby(["country", "sub_network"]).size().count()
    else:
        n_clusters = int(snakemake.wildcards.clusters)
        aggregate_carriers = None

    if n_clusters == len(n.buses):
        # Fast-path if no clustering is necessary
        busmap = n.buses.index.to_series()
        linemap = n.lines.index.to_series()
        clustering = pypsa.clustering.spatial.Clustering(
            n, busmap, linemap, linemap, pd.Series(dtype="O")
        )
    elif len(n.buses) < n_clusters:
        logger.error(
            f"Desired number of clusters ({n_clusters}) higher than the number of buses ({len(n.buses)})"
        )
    else:
        line_length_factor = snakemake.params.length_factor
        Nyears = n.snapshot_weightings.objective.sum() / 8760
        hvac_overhead_cost = load_costs(
            snakemake.input.tech_costs,
            snakemake.params.costs,
            snakemake.params.electricity,
            Nyears,
        ).at["HVAC overhead", "capital_cost"]

        def consense(x):
            v = x.iat[0]
            assert (
                x == v
            ).all() or x.isnull().all(), "The `potential` configuration option must agree for all renewable carriers, for now!"
            return v

        aggregation_strategies = snakemake.params.cluster_options.get(
            "aggregation_strategies", {}
        )
        # translate str entries of aggregation_strategies to pd.Series functions:
        aggregation_strategies = {
            p: {k: getattr(pd.Series, v) for k, v in aggregation_strategies[p].items()}
            for p in aggregation_strategies.keys()
        }
        custom_busmap = False  # snakemake.params.custom_busmap custom busmap is depreciated https://github.com/pypsa-meets-earth/pypsa-earth/pull/694
        if custom_busmap:
            busmap = pd.read_csv(
                snakemake.input.custom_busmap, index_col=0, squeeze=True
            )
            busmap.index = busmap.index.astype(str)
            logger.info(f"Imported custom busmap from {snakemake.input.custom_busmap}")
        cluster_config = snakemake.config.get("cluster_options", {}).get(
            "cluster_network", {}
        )
        clustering = clustering_for_n_clusters(
            inputs,
            config,
            n,
            n_clusters,
            alternative_clustering,
            gadm_layer_id,
            geo_crs,
            country_list,
            distribution_cluster,
            snakemake.params.build_shape_options,
            custom_busmap,
            aggregate_carriers,
            line_length_factor,
            aggregation_strategies,
            snakemake.config["solving"]["solver"]["name"],
            cluster_config.get("algorithm", "hac"),
            cluster_config.get("feature", "solar+onwind-time"),
            extended_link_costs=hvac_overhead_cost,
            focus_weights=focus_weights,
        )

    update_p_nom_max(clustering.network)
    clustering.network.meta = dict(
        snakemake.config, **dict(wildcards=dict(snakemake.wildcards))
    )
    clustering.network.export_to_netcdf(outputs.network)
    for attr in (
        "busmap",
        "linemap",
    ):  # also available: linemap_positive, linemap_negative
        getattr(clustering, attr).to_csv(outputs[attr])

    cluster_regions((clustering.busmap,), inputs, outputs)
