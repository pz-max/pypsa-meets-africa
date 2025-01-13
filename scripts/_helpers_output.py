# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-

import logging

import yaml

logger = logging.getLogger(__name__)

# definitions of output blocks ------------------------------------------------
# model_keys = [
#   "version", "tutorial", "foresight", "countries",
#   "scenario", "snapshots", "enable", "run",
#   "clean_osm_data_options", "build_osm_network", "base_network",
#   "build_shape_options",
#   "electricity", "lines", "links", "transformers",
#   "load_options", "demand_data",
#   "augmented_line_connection",
#   "cluster_options",
#   "atlite", "renewable", "solar_thermal",
#   "policy_config", "fossil_reserves",
#   "sector", "export", "industry"
#   "custom_data", "existing_capacities", "costs",
#   "monte_carlo", "solving",
#   "plotting", "crs",
# ]

major_run_keys = [
    "version",
    "tutorial",
    "foresight",
    "countries",
    "enable",
    "run",
]
major_model_keys = [
    "scenario",
    "snapshots",
]
grid_model_keys = [
    "clean_osm_data_options",
    "build_osm_network",
    "base_network",
]
electrycity_model_keys = ["electricity"]

# visual settings -------------------------------------------------------------
style = """<style type='text/css'>
html {
  font-family: Courier;
}
r {
  color: #ff0000;
  background-color: coral;
}
g {
  color: #00ff00;
  background-color: coral;
}
b {
  color: #0000ff;
  background-color: grey;
}
blue_bg {
  color: #36454F;
  background-color: #A7C8FF;
}
mint_bg {
  color: #36454F;
  background-color: #B7E5D5;
}
coral_bg {
  color: #36454F;
  background-color: #FF9E9E;
}
lavend_bg {
  color: #36454F;
  background-color: #D8ADEF;
}
gold_bg {
  color: #36454F;
  background-color: #F9C6A1;
}
</style>"""


def write_html(style, fl, type, str_):
    fl.write("<%(type)s>%(str)s</%(type)s>" % {"type": type, "str": str_})


def write_dict_key(dict, fl_name="out.html", style=style):

    # define styles to be used in htmls generated below
    coral_bg = "coral_bg"
    mint_bg = "mint_bg"
    blue_bg = "blue_bg"

    f = open(fl_name, "w")

    f.write("<html>")
    f.write(style)

    write_html(
        style=style,
        fl=f,
        type=coral_bg,
        str_="------------------------------------------ <br />",
    )
    write_html(style=style, fl=f, type=mint_bg, str_=yaml.dump(dict))
    write_html(style=style, fl=f, type=mint_bg, str_="<br />")
    write_html(
        style=style,
        fl=f,
        type=blue_bg,
        str_="------------------------------------------ <br />",
    )

    f.write("</html>")
