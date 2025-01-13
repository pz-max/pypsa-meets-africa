# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""
Creates summaries of aggregated energy and costs as ``.csv`` files.

Relevant Settings
-----------------
.. code:: yaml

.. seealso::
    Documentation of the configuration file ``config.yaml``

Inputs
------

Outputs
-------

Description
-----------

"""

import os

import pandas as pd
import pypsa
from _helpers import configure_logging
from _helpers_output import write_dict_key
from add_electricity import create_logger, load_costs, update_transmission_costs

idx = pd.IndexSlice

logger = create_logger(__name__)

if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake(
            "make_conf_check",
        )

    configure_logging(snakemake)

    write_dict_key(
        dict=snakemake.config["electricity"], fl_name=snakemake.output.pretty_config
    )
