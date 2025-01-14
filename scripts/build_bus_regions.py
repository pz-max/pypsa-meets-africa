# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""
Creates Voronoi shapes for each bus representing both onshore and offshore
regions.

Relevant Settings
-----------------

.. code:: yaml

    countries:

.. seealso::
    Documentation of the configuration file ``config.yaml`` at
    :ref:`toplevel_cf`

Inputs
------

- ``resources/country_shapes.geojson``: confer :ref:`shapes`
- ``resources/offshore_shapes.geojson``: confer :ref:`shapes`
- ``networks/base.nc``: confer :ref:`base`

Outputs
-------

- ``resources/regions_onshore.geojson``:

    .. image:: /img/regions_onshore.png
        :width: 33 %

- ``resources/regions_offshore.geojson``:

    .. image:: /img/regions_offshore.png
        :width: 33 %

Description
-----------
"""
import os

import geopandas as gpd
import pandas as pd
import pypsa
from _helpers import REGION_COLS, configure_logging, create_logger

logger = create_logger(__name__)


def custom_voronoi_partition_pts(points, outline, add_bounds_shape=True, multiplier=5):
    """
    Compute the polygons of a voronoi partition of `points` within the polygon
    `outline`

    Attributes
    ----------
    points : Nx2 - ndarray[dtype=float]
    outline : Polygon

    Returns
    -------
    polygons : N - ndarray[dtype=Polygon|MultiPolygon]
    """

    import numpy as np
    from scipy.spatial import Voronoi
    from shapely.geometry import Polygon

    points = np.asarray(points)

    polygons_arr = []

    if len(points) == 1:
        polygons_arr = [outline]
    else:
        xmin, ymin = np.amin(points, axis=0)
        xmax, ymax = np.amax(points, axis=0)

        if add_bounds_shape:
            # check bounds of the shape
            minx_o, miny_o, maxx_o, maxy_o = outline.boundary.bounds
            xmin = min(xmin, minx_o)
            ymin = min(ymin, miny_o)
            xmax = min(xmax, maxx_o)
            ymax = min(ymax, maxy_o)

        xspan = xmax - xmin
        yspan = ymax - ymin

        # to avoid any network positions outside all Voronoi cells, append
        # the corners of a rectangle framing these points
        vcells = Voronoi(
            np.vstack(
                (
                    points,
                    [
                        [xmin - multiplier * xspan, ymin - multiplier * yspan],
                        [xmin - multiplier * xspan, ymax + multiplier * yspan],
                        [xmax + multiplier * xspan, ymin - multiplier * yspan],
                        [xmax + multiplier * xspan, ymax + multiplier * yspan],
                    ],
                )
            )
        )

        polygons_arr = np.empty((len(points),), "object")
        for i in range(len(points)):
            poly = Polygon(vcells.vertices[vcells.regions[vcells.point_region[i]]])

            if not poly.is_valid:
                poly = poly.buffer(0)

            if not outline.is_valid:
                outline = outline.buffer(0)

            poly = poly.intersection(outline)

            polygons_arr[i] = poly

    return polygons_arr


def get_gadm_shape(
    onshore_buses, gadm_shapes, geo_crs="EPSG:4326", metric_crs="EPSG:3857"
):
    geo_regions = gpd.GeoDataFrame(
        onshore_buses[["x", "y"]],
        geometry=gpd.points_from_xy(onshore_buses["x"], onshore_buses["y"]),
        crs=geo_crs,
    ).to_crs(metric_crs)

    join_geos = gpd.sjoin_nearest(
        geo_regions, gadm_shapes.to_crs(metric_crs), how="left"
    )

    # when duplicates, keep only the first entry
    join_geos = join_geos[~join_geos.index.duplicated()]

    gadm_sel = gadm_shapes.loc[join_geos[gadm_shapes.index.name].values]

    return gadm_sel.geometry.values, gadm_sel.index.values


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("build_bus_regions")

    configure_logging(snakemake)

    countries = snakemake.params.countries
    geo_crs = snakemake.params.crs["geo_crs"]
    area_crs = snakemake.params.crs["area_crs"]
    metric_crs = snakemake.params.crs["distance_crs"]

    n = pypsa.Network(snakemake.input.base_network)

    country_shapes = gpd.read_file(snakemake.input.country_shapes).set_index("name")[
        "geometry"
    ]

    offshore_shapes = gpd.read_file(snakemake.input.offshore_shapes)

    offshore_shapes = offshore_shapes.reindex(columns=REGION_COLS).set_index("name")[
        "geometry"
    ]

    gadm_shapes = gpd.read_file(snakemake.input.gadm_shapes).set_index("GADM_ID")

    onshore_regions = []
    offshore_regions = []

    for country in countries:
        c_b = n.buses.country == country
        if n.buses.loc[c_b & n.buses.substation_lv, ["x", "y"]].empty:
            logger.warning(f"No low voltage buses found for {country}!")
            continue

        onshore_shape = country_shapes[country]
        onshore_locs = n.buses.loc[c_b & n.buses.substation_lv, ["x", "y"]]
        gadm_country = gadm_shapes[gadm_shapes.country == country]
        if snakemake.params.alternative_clustering:
            onshore_geometry, shape_id = get_gadm_shape(
                onshore_locs,
                gadm_country,
                geo_crs,
                metric_crs,
            )
        else:
            onshore_geometry = custom_voronoi_partition_pts(
                onshore_locs.values, onshore_shape
            )
            shape_id = 0  # Not used

        temp_region = gpd.GeoDataFrame(
            {
                "name": onshore_locs.index,
                "x": onshore_locs["x"],
                "y": onshore_locs["y"],
                "geometry": onshore_geometry,
                "country": country,
                "shape_id": shape_id,
            },
            crs=geo_crs,
        )
        temp_region = temp_region[
            temp_region.geometry.is_valid & ~temp_region.geometry.is_empty
        ]
        onshore_regions.append(temp_region)

        # These two logging could be commented out
        if country not in offshore_shapes.index:
            logger.warning(f"No off-shore shapes for {country}")
            continue

        offshore_shape = offshore_shapes[country]

        if n.buses.loc[c_b & n.buses.substation_off, ["x", "y"]].empty:
            logger.warning(f"No off-shore substations found for {country}")
            continue
        else:
            offshore_locs = n.buses.loc[c_b & n.buses.substation_off, ["x", "y"]]
            shape_id = 0  # Not used
            offshore_geometry = custom_voronoi_partition_pts(
                offshore_locs.values, offshore_shape
            )
            offshore_regions_c = gpd.GeoDataFrame(
                {
                    "name": offshore_locs.index,
                    "x": offshore_locs["x"],
                    "y": offshore_locs["y"],
                    "geometry": offshore_geometry,
                    "country": country,
                    "shape_id": shape_id,
                },
                crs=country_shapes.crs,
            )
            offshore_regions_c = offshore_regions_c.loc[
                offshore_regions_c.to_crs(area_crs).area > 1e-2
            ]
            offshore_regions_c = offshore_regions_c[
                offshore_regions_c.geometry.is_valid
                & ~offshore_regions_c.geometry.is_empty
            ]
            offshore_regions.append(offshore_regions_c)

    # create geodataframe and remove nan shapes
    onshore_regions = gpd.GeoDataFrame(
        pd.concat(onshore_regions, ignore_index=True),
        crs=country_shapes.crs,
    ).dropna(axis="index", subset=["geometry"])

    if snakemake.params.alternative_clustering:
        # determine isolated buses
        n.determine_network_topology()
        non_isolated_buses = n.buses.duplicated(subset=["sub_network"], keep=False)
        isolated_buses = n.buses[~non_isolated_buses].index
        non_isolated_regions = onshore_regions[
            ~onshore_regions.name.isin(isolated_buses)
        ]
        isolated_regions = onshore_regions[onshore_regions.name.isin(isolated_buses)]

        # Combine regions while prioritizing non-isolated ones
        onshore_regions = pd.concat(
            [non_isolated_regions, isolated_regions]
        ).drop_duplicates("shape_id", keep="first")

        if len(onshore_regions) < len(gadm_country):
            logger.warning(
                f"The number of remaining of buses are less than the number of administrative clusters suggested!"
            )

    onshore_regions = pd.concat([onshore_regions], ignore_index=True).to_file(
        snakemake.output.regions_onshore
    )

    if offshore_regions:
        # if a offshore_regions exists execute below
        pd.concat(offshore_regions, ignore_index=True).to_file(
            snakemake.output.regions_offshore
        )
    else:
        # if no offshore_regions exist save an empty offshore_shape
        offshore_shapes.to_frame().to_file(snakemake.output.regions_offshore)
