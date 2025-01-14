# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-

import glob
import os
import pathlib

import pandas as pd
import pypsa
from _helpers import configure_logging, create_logger, mock_snakemake


def extract_gen_sum(n, variable_id):

    vals = n.generators.groupby("carrier").sum()[variable_id].to_list()
    crrs = n.generators.groupby("carrier").sum()[variable_id].index.to_list()

    return (vals, [variable_id] * len(vals), crrs)


def collect_statistics(n, network_id):
    values = []
    variables = []
    carriers = []

    # TODO account for snapshots_weightings
    overall_load = [n.loads_t.p_set.copy().sum().sum().tolist()]
    values.extend(overall_load)
    variables.extend(["p_set"] * len(overall_load))
    carriers.extend(["physical_load"] * len(overall_load))

    for var in ["p_set", "p_nom_max", "weight"]:
        # NB There is no "weight" column in older PyPSA versions
        values.extend(extract_gen_sum(n, var)[0])
        variables.extend(extract_gen_sum(n, var)[1])
        carriers.extend(extract_gen_sum(n, var)[2])

    network_carriers = list(n.generators.carrier.unique())
    for carr in network_carriers:
        # TODO account for snapshots_weightings
        p_max_pu_cols = n.generators_t.p_max_pu.columns
        carr_cols = p_max_pu_cols[p_max_pu_cols.str.contains(carr)]
        p_max_pu_vals = [n.generators_t.p_max_pu[carr_cols].sum().sum().copy().tolist()]

        p_nm = n.generators.query("carrier in @carr")["p_nom_max"]
        tech_pot_vals = [
            (p_nm * n.generators_t.p_max_pu[carr_cols]).sum().sum().copy().tolist()
        ]

        values.extend(p_max_pu_vals)
        variables.extend(["p_max_pu"] * len(p_max_pu_vals))
        carriers.extend([carr] * len(p_max_pu_vals))

        values.extend(tech_pot_vals)
        variables.extend(["tech_potential"] * len(tech_pot_vals))
        carriers.extend([carr] * len(tech_pot_vals))

    res_df = pd.DataFrame(
        data={"values": values, "variables": variables, "carriers": carriers}
    )
    res_df["network_id"] = network_id + ".nc"

    return res_df


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("conservancy_checks")

    configure_logging(snakemake)

    data_folder = snakemake.input.network_folder

    for fl in list(pathlib.Path(data_folder).glob("*.nc")):
        n = pypsa.Network(fl)

        network_data_df = collect_statistics(n, fl.stem)

        network_data_df.to_csv(data_folder + "/" + fl.stem + "_invar_check.csv")
