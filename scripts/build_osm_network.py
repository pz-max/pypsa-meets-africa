# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-

import os

import geopandas as gpd
import numpy as np
import pandas as pd
from _helpers import (
    configure_logging,
    create_logger,
    read_geojson,
    read_osm_config,
    to_csv_nafix,
)
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, nearest_points, split
from sklearn.cluster import DBSCAN
from tqdm import tqdm

logger = create_logger(__name__)


# Keep only a predefined set of columns, as otherwise conflicts are possible
# e.g. the columns which names starts with "bus" are mixed up with
# the third-bus specification when executing additional_linkports()
LINES_COLUMNS = [
    "line_id",
    "circuits",
    "tag_type",
    "voltage",
    "bus0",
    "bus1",
    "length",
    "underground",
    "under_construction",
    "tag_frequency",
    "dc",
    "country",
    "geometry",
    "bounds",
]


def line_endings_to_bus_conversion(lines):
    # Assign to every line a start and end point

    lines["bounds"] = lines["geometry"].boundary  # create start and end point

    lines["bus_0_coors"] = lines["bounds"].map(lambda p: p.geoms[0])
    lines["bus_1_coors"] = lines["bounds"].map(lambda p: p.geoms[1])

    # splits into coordinates
    lines["bus0_lon"] = lines["bus_0_coors"].x
    lines["bus0_lat"] = lines["bus_0_coors"].y
    lines["bus1_lon"] = lines["bus_1_coors"].x
    lines["bus1_lat"] = lines["bus_1_coors"].y

    return lines


def set_substations_ids(buses, distance_crs, tol=5000):
    """
    Assigns station IDs to buses based on their proximity.

    Parameters:
    - buses: GeoDataFrame object representing the buses data.
    - distance_crs: Coordinate reference system (CRS) to convert the geometry to.
    - tol: Tolerance distance in chosen CRS to define cluster proximity.

    Returns:
    - None. Modifies the 'station_id' column in the 'buses' GeoDataFrame.

    Example:
    set_substations_ids(buses_data, 'EPSG:3857', tol=5000)
    """

    # Convert the geometry to EPSG:3857
    tmp_geometry = buses.geometry.to_crs(distance_crs)

    coords = tmp_geometry.apply(lambda geom: np.array(geom.coords[0])).to_list()

    # Perform DBSCAN on the coordinates
    db = DBSCAN(eps=tol, min_samples=1).fit(coords)

    # Add the cluster labels to the GeoDataFrame
    buses["station_id"] = db.labels_


def set_lines_ids(lines, buses, distance_crs):
    """
    Function to set line buses ids to the closest bus in the list.
    """
    # set tqdm options for set lines ids
    lines_d = lines.to_crs(distance_crs)
    buses_d = buses.to_crs(distance_crs)

    # initialization
    lines["bus0"] = -1
    lines["bus1"] = -1

    for key, lines_sel in lines_d.groupby(["voltage", "dc"]):
        buses_sel = buses_d.query(f"voltage == {key[0]} and dc == {key[1]}")

        # find the closest node of the bus0 of the line
        bus0_points = np.array(
            list(
                lines_sel.geometry.boundary.apply(
                    lambda x: (x.geoms[0].x, x.geoms[0].y)
                )
            )
        )
        bus1_points = np.array(
            list(
                lines_sel.geometry.boundary.apply(
                    lambda x: (x.geoms[1].x, x.geoms[1].y)
                )
            )
        )
        points_buses = np.array(list(buses_sel.geometry.apply(lambda x: (x.x, x.y))))

        btree = cKDTree(points_buses)
        dist0, idx0 = btree.query(bus0_points, k=1)  # find closest points of bus0
        dist1, idx1 = btree.query(bus1_points, k=1)  # find closest points of bus1

        # set bus0 and bus1
        lines.loc[lines_sel.index, "bus0"] = buses_sel.bus_id.iloc[idx0].values
        lines.loc[lines_sel.index, "bus1"] = buses_sel.bus_id.iloc[idx1].values

        # check if the line starts exactly in the bus0, otherwise modify the linestring
        bus0_linestring = (
            lines.loc[lines_sel.index]
            .apply(
                lambda x: LineString([buses.geometry.loc[x["bus0"]], x["bus_0_coors"]]),
                axis=1,
            )
            .set_crs(crs=lines.crs)
        )
        bus1_linestring = (
            lines.loc[lines_sel.index]
            .apply(
                lambda x: LineString([x["bus_1_coors"], buses.geometry.loc[x["bus1"]]]),
                axis=1,
            )
            .set_crs(crs=lines.crs)
        )

        # update geometry with left and right linestrings to match bus0 and bus1
        lines.loc[lines_sel.index, "geometry"] = (
            lines.loc[lines_sel.index]
            .union(bus0_linestring)
            .union(bus1_linestring)
            .apply(linemerge)
        )

    return lines, buses


def merge_stations_same_station_id(
    buses, delta_lon=0.001, delta_lat=0.001, precision=4
):
    """
    Function to merge buses with same voltage and station_id This function
    iterates over all substation ids and creates a bus_id for every substation
    and voltage level.

    Therefore, a substation with multiple voltage levels is represented
    with different buses, one per voltage level
    """
    # initialize list of cleaned buses
    buses_clean = []

    # initialize the number of buses
    n_buses = 0

    for g_name, g_value in buses.groupby(by="station_id"):
        # average location of the buses having the same station_id
        station_point_x = np.round(g_value.geometry.x.mean(), precision)
        station_point_y = np.round(g_value.geometry.y.mean(), precision)

        # loop for every voltage level in the bus
        # The location of the buses is averaged; in the case of multiple voltage levels for the same station_id,
        # each bus corresponding to a voltage level and each polatity is located at a distance regulated by delta_lon/delta_lat
        v_it = 0
        for v_name, bus_row in g_value.groupby(by=["voltage", "dc"]):
            lon_bus = np.round(station_point_x + v_it * delta_lon, precision)
            lat_bus = np.round(station_point_y + v_it * delta_lat, precision)

            # add the bus
            buses_clean.append(
                [
                    n_buses,  # "bus_id"
                    g_name,  # "station_id"
                    v_name[0],  # "voltage"
                    bus_row["dc"].all(),  # "dc"
                    # bus_row["dc"].all(),  # "dc"
                    "|".join(bus_row["symbol"].unique()),  # "symbol"
                    bus_row["under_construction"].any(),  # "under_construction"
                    "|".join(bus_row["tag_substation"].unique()),  # "tag_substation"
                    bus_row["tag_area"].sum(),  # "tag_area"
                    lon_bus,  # "lon"
                    lat_bus,  # "lat"
                    bus_row["country"].iloc[0],  # "country"
                    Point(
                        lon_bus,
                        lat_bus,
                    ),  # "geometry"
                ]
            )

            # increase counters
            v_it += 1
            n_buses += 1

    # names of the columns
    buses_clean_columns = [
        "bus_id",
        "station_id",
        "voltage",
        "dc",
        "symbol",
        "under_construction",
        "tag_substation",
        "tag_area",
        "lon",
        "lat",
        "country",
        "geometry",
    ]

    return gpd.GeoDataFrame(buses_clean, columns=buses_clean_columns).set_crs(
        crs=buses.crs, inplace=True
    )


def get_ac_frequency(df, fr_col="tag_frequency"):
    """
    # Function to define a default frequency value.

    Attempts to find the most usual non-zero frequency across the
    dataframe; 50 Hz is assumed as a back-up value
    """

    # Initialize a default frequency value
    ac_freq_default = 50

    grid_freq_levels = df[fr_col].value_counts(sort=True, dropna=True)
    if not grid_freq_levels.empty:
        # AC lines frequency shouldn't be 0Hz
        ac_freq_levels = grid_freq_levels.loc[
            grid_freq_levels.index.get_level_values(0) != "0"
        ]
        ac_freq_default = ac_freq_levels.index.get_level_values(0)[0]

    return ac_freq_default


def get_transformers(buses, lines):
    """
    Function to create fake transformer lines that connect buses of the same
    station_id at different voltage.
    """

    ac_freq = get_ac_frequency(lines)
    df_transformers = []

    # Transformers should be added between AC buses only
    buses_ac = buses[~buses["dc"]]
    for g_name, g_value in buses_ac.sort_values("voltage", ascending=True).groupby(
        by="station_id"
    ):
        # note: by construction there cannot be more that two buses with the same station_id and same voltage
        n_voltages = len(g_value)

        if n_voltages > 1:
            for id in range(0, n_voltages - 1):
                # when g_value has more than one node, it means that there are multiple voltages for the same bus
                geom_trans = LineString(
                    [g_value.geometry.iloc[id], g_value.geometry.iloc[id + 1]]
                )

                df_transformers.append(
                    [
                        f"transf_{g_name}_{id}",  # "line_id"
                        g_value["bus_id"].iloc[id],  # "bus0"
                        g_value["bus_id"].iloc[id + 1],  # "bus1"
                        g_value.voltage.iloc[id],  # "voltage_bus0"
                        g_value.voltage.iloc[id + 1],  # "voltage_bus0"
                        g_value.country.iloc[id],  # "country"
                        geom_trans,  # "geometry"
                    ]
                )

    # name of the columns
    trasf_columns = [
        "line_id",
        "bus0",
        "bus1",
        "voltage_bus0",
        "voltage_bus1",
        "country",
        "geometry",
    ]

    df_transformers = gpd.GeoDataFrame(df_transformers, columns=trasf_columns)
    if not df_transformers.empty:
        init_index = 0 if lines.empty else lines.index[-1] + 1
        df_transformers.set_index(init_index + df_transformers.index, inplace=True)
    # update line endings
    df_transformers = line_endings_to_bus_conversion(df_transformers)

    return df_transformers


def get_converters(buses, lines):
    """
    Function to create fake converter lines that connect buses of the same
    station_id of different polarities.
    """

    df_converters = []

    for g_name, g_value in buses.sort_values("voltage", ascending=True).groupby(
        by="station_id"
    ):
        # note: by construction there cannot be more that two buses with the same station_id and same voltage
        n_voltages = len(g_value)

        # A converter stations should have both AC and DC parts
        if g_value["dc"].any() & ~g_value["dc"].all():
            dc_voltage = g_value[g_value.dc]["voltage"].values

            for u in dc_voltage:
                id_0 = g_value[g_value["dc"] & g_value["voltage"].isin([u])].index[0]

                ac_voltages = g_value[~g_value.dc]["voltage"]
                # A converter is added between a DC nodes and AC one with the closest voltage
                id_1 = ac_voltages.sub(u).abs().idxmin()

                geom_conv = LineString(
                    [g_value.geometry.loc[id_0], g_value.geometry.loc[id_1]]
                )

                df_converters.append(
                    [
                        f"convert_{g_name}_{id_0}",  # "line_id"
                        g_value["bus_id"].loc[id_0],  # "bus0"
                        g_value["bus_id"].loc[id_1],  # "bus1"
                        False,  # "underground"
                        False,  # "under_construction"
                        g_value.country.loc[id_0],  # "country"
                        geom_conv,  # "geometry"
                    ]
                )

    # name of the columns
    conv_columns = [
        "converter_id",
        "bus0",
        "bus1",
        "underground",
        "under_construction",
        "country",
        "geometry",
    ]

    df_converters = gpd.GeoDataFrame(df_converters, columns=conv_columns).reset_index()

    return df_converters


def connect_stations_same_station_id(lines, buses):
    """
    Function to create fake links between substations with the same
    substation_id.
    """
    ac_freq = get_ac_frequency(lines)
    station_id_list = buses.station_id.unique()

    add_lines = []
    from shapely.geometry import LineString

    for s_id in station_id_list:
        buses_station_id = buses[buses.station_id == s_id]

        if len(buses_station_id) > 1:
            for b_it in range(1, len(buses_station_id)):
                add_lines.append(
                    [
                        f"link{buses_station_id}_{b_it}",  # "line_id"
                        buses_station_id.index[0],  # "bus0"
                        buses_station_id.index[b_it],  # "bus1"
                        400000,  # "voltage"
                        1,  # "circuits"
                        0.0,  # "length"
                        False,  # "underground"
                        False,  # "under_construction"
                        "transmission",  # "tag_type"
                        ac_freq,  # "tag_frequency"
                        buses_station_id.country.iloc[0],  # "country"
                        LineString(
                            [
                                buses_station_id.geometry.iloc[0],
                                buses_station_id.geometry.iloc[b_it],
                            ]
                        ),  # "geometry"
                        LineString(
                            [
                                buses_station_id.geometry.iloc[0],
                                buses_station_id.geometry.iloc[b_it],
                            ]
                        ).bounds,  # "bounds"
                        buses_station_id.geometry.iloc[0],  # "bus_0_coors"
                        buses_station_id.geometry.iloc[b_it],  # "bus_1_coors"
                        buses_station_id.lon.iloc[0],  # "bus0_lon"
                        buses_station_id.lat.iloc[0],  # "bus0_lat"
                        buses_station_id.lon.iloc[b_it],  # "bus1_lon"
                        buses_station_id.lat.iloc[b_it],  # "bus1_lat"
                    ]
                )

    # name of the columns
    add_lines_columns = [
        "line_id",
        "bus0",
        "bus1",
        "voltage",
        "circuits",
        "length",
        "underground",
        "under_construction",
        "tag_type",
        "tag_frequency",
        "country",
        "geometry",
        "bounds",
        "bus_0_coors",
        "bus_1_coors",
        "bus0_lon",
        "bus0_lat",
        "bus1_lon",
        "bus1_lat",
    ]

    df_add_lines = gpd.GeoDataFrame(pd.concat(add_lines), columns=add_lines_columns)
    lines = pd.concat([lines, df_add_lines], ignore_index=True)

    return lines


def set_lv_substations(buses):
    """
    Function to set what nodes are lv, thereby setting substation_lv The
    current methodology is to set lv nodes to buses where multiple voltage
    level are found, hence when the station_id is duplicated.
    """
    # initialize column substation_lv to true
    buses["substation_lv"] = True

    # For each station number with multiple buses make lowest voltage `substation_lv = TRUE`
    bus_with_stations_duplicates = buses[
        buses.station_id.duplicated(keep=False)
    ].sort_values(by=["station_id", "voltage"])
    lv_bus_at_station_duplicates = (
        buses[buses.station_id.duplicated(keep=False)]
        .sort_values(by=["station_id", "voltage"])
        .drop_duplicates(subset=["station_id"])
    )
    # Set all buses with station duplicates "False"
    buses.loc[bus_with_stations_duplicates.index, "substation_lv"] = False
    # Set lv_buses with station duplicates "True"
    buses.loc[lv_bus_at_station_duplicates.index, "substation_lv"] = True

    return buses


def merge_stations_lines_by_station_id_and_voltage(
    lines, buses, geo_crs, distance_crs, tol=2000
):
    """
    Function to merge close stations and adapt the line datasets to adhere to
    the merged dataset.
    """

    logger.info(
        "Stage 4a/5: Set substation ids with tolerance of %.2f km" % (tol / 1000)
    )

    # set substation ids
    set_substations_ids(buses, distance_crs, tol=tol)

    logger.info("Stage 4b/5: Merge substations with the same id")

    # merge buses with same station id and voltage
    if not buses.empty:
        buses = merge_stations_same_station_id(buses)

    logger.info("Stage 4c/5: Specify the bus ids of the line endings")

    # set the bus ids to the line dataset
    lines, buses = set_lines_ids(lines, buses, distance_crs)

    # drop lines starting and ending in the same node
    lines.drop(lines[lines["bus0"] == lines["bus1"]].index, inplace=True)
    # update line endings
    lines = line_endings_to_bus_conversion(lines)

    # set substation_lv
    set_lv_substations(buses)

    logger.info("Stage 4d/5: Add converters to lines")

    # append fake converters
    # lines = pd.concat([lines, converters], ignore_index=True)

    # reset index
    lines.reset_index(drop=True, inplace=True)
    # if len(links) > 0:
    #     links.reset_index(drop=True, inplace=True)

    return lines, buses


def fix_overpassing_lines(lines, buses, distance_crs, tol=1):
    """
    Snap buses to lines that are within a certain tolerance. It does this by
    first buffering the buses by the tolerance distance, and then performing a
    spatial join to find all lines that intersect with the buffers. For each
    group of lines that intersect with a buffer, the function identifies the
    points that overpass the line (i.e., are not snapped to the line), and then
    snaps those points to the nearest point on the line. The line is then split
    at each snapped point, resulting in a new set of lines that are snapped to
    the buses. The function returns a GeoDataFrame containing the snapped
    lines, and the original GeoDataFrame containing the buses.

    Parameters
    ----------
    lines : GeoDataFrame
        GeoDataFrame containing the lines
    buses : GeoDataFrame
        GeoDataFrame containing the buses
    distance_crs : str
        Coordinate reference system to use for distance calculations
    tol : float
        Tolerance in meters to snap the buses to the lines

    Returns
    -------
    lines : GeoDataFrame
        GeoDataFrame containing the lines
    """
    if lines.empty:
        return lines, buses

    df_l = lines.copy()  # can use lines directly without copying
    # drop all columns except id and geometry for buses
    df_p = buses.copy()

    line_id_str = "line_id"
    bus_id_str = "bus_id"

    # change crs to distance based
    df_l = df_l.to_crs(distance_crs)
    df_p = df_p.to_crs(distance_crs)

    # set index to bus_id
    df_p.set_index(bus_id_str, inplace=True)

    # Buffer points to create areas for spatial join
    buffer_df = df_p.buffer(tol).to_frame()

    # Spatial join to find lines intersecting point buffers
    joined = gpd.sjoin(df_l, buffer_df, how="inner", op="intersects")

    # group lines by their index
    group_lines = joined.groupby(level=0)

    # iterate over the groups, TODO: change to apply
    for i, group in group_lines:
        line_id = df_l.loc[i, line_id_str]  # pick the line id that represents the group
        line_geom = df_l.loc[i, "geometry"]

        # number of points that intersect with the line
        num_points = len(group)

        # get the indices of the points that intersect with the line
        points_indexes = group["index_right"].tolist()

        # get the geometries of the points that intersect with the line
        all_points = df_p.loc[points_indexes, "geometry"]

        # discard points related to the extrema points (the buses) of each line
        distance_from_buses = all_points.distance(line_geom.boundary)
        overpassing_points = list(all_points[distance_from_buses > tol])

        # if no overpassing points are identified, skip iteration
        if len(overpassing_points) == 0:
            continue

        # find all the nearest points on the line to the points that intersect with the line
        nearest_points_list = [
            nearest_points(line_geom, point)[0] for point in overpassing_points
        ]

        # sort the nearest points based on their distance from the start point of the line
        nearest_points_list.sort(key=lambda point: line_geom.project(point))

        # split the line at each nearest point using the split function
        split_line = [line_geom]
        for point in nearest_points_list:
            # Split the line at the current point
            # The split function returns a GeometryCollection, so we need to convert it to a list
            split_lines = split(split_line[-1], point)
            split_line = split_line[:-1] + list(split_lines.geoms)

        # convert the split line to a multilinestring
        split_line = MultiLineString(split_line)

        # replace the line with the split line in lines df
        df_l.loc[i, "geometry"] = split_line

    # explode the multilinestrings (not recommended, but included for completion)
    # exploding the df should be done at the last step
    # if an operation requires separate lines, it should be done using df.explode().apply(your_function)
    # which is a lot more memory efficient
    df_l = df_l.explode(index_parts=True).reset_index()

    # revise line_id to account for part index
    df_l[line_id_str] = (
        df_l[line_id_str].astype(str) + "_" + df_l["level_1"].astype(str)
    )
    df_l.drop(columns=["level_0", "level_1"], inplace=True)

    # update line endings (included for completion, the scope of the function should be limited to fixing overpassing lines)
    # commented out due to errors in the bus conversion function
    # df_l = line_endings_to_bus_conversion(df_l)

    # update length
    df_l["length"] = df_l.to_crs(distance_crs).geometry.length

    # return to original crs
    df_l = df_l.to_crs(lines.crs)

    # remove lines that are rings (included for completion), TODO: this should be a separate function
    df_l = df_l[~df_l.geometry.is_ring].reset_index(drop=True)

    # buses should not be returned as they are not changed, but included for completion
    return df_l, buses


def force_ac_lines(df, col="tag_frequency"):
    """
    Function that forces all PyPSA lines to be AC lines.

    A network can contain AC and DC power lines that are modelled as
    PyPSA "Line" component. When DC lines are available, their power
    flow can be controlled by their converter. When it is artificially
    converted into AC, this feature is lost. However, for debugging and
    preliminary analysis, it can be useful to bypass problems.
    """
    # TODO: default frequency may be by country
    default_ac_frequency = 50

    df["tag_frequency"] = default_ac_frequency
    df["dc"] = False

    return df


def add_buses_to_empty_countries(country_list, fp_country_shapes, buses):
    """
    Function to add a bus for countries missing substation data.
    """
    country_shapes = gpd.read_file(fp_country_shapes).set_index("name")["geometry"]
    bus_country_list = buses["country"].unique().tolist()

    # it may happen that bus_country_list contains entries not relevant as a country name (e.g. "not found")
    # difference can't give negative values; the following will return only relevant country names
    no_data_countries = list(set(country_list).difference(set(bus_country_list)))

    if len(no_data_countries) > 0:
        logger.info(
            f"No buses for the following countries: {no_data_countries}. Adding a node for everyone of them."
        )
        no_data_countries_shape = (
            country_shapes[country_shapes.index.isin(no_data_countries) == True]
            .reset_index()
            .to_crs(geo_crs)
        )
        length = len(no_data_countries)
        df = gpd.GeoDataFrame(
            {
                "voltage": [220000] * length,
                "country": no_data_countries_shape["name"],
                "lon": no_data_countries_shape["geometry"].centroid.x,
                "lat": no_data_countries_shape["geometry"].centroid.y,
                "bus_id": np.arange(len(buses) + 1, len(buses) + (length + 1), 1),
                "station_id": [np.nan] * length,
                # All lines for the countries with NA bus data are assumed to be AC
                "dc": [False] * length,
                "under_construction": [False] * length,
                "tag_area": [0.0] * length,
                "symbol": ["substation"] * length,
                "tag_substation": ["transmission"] * length,
                "geometry": no_data_countries_shape["geometry"].centroid,
                "substation_lv": [True] * length,
            },
            crs=geo_crs,
        ).astype(
            buses.dtypes.to_dict()
        )  # keep the same dtypes as buses
        buses = gpd.GeoDataFrame(
            pd.concat([buses, df], ignore_index=True).reset_index(drop=True),
            crs=buses.crs,
        )

        # update country list by buses dataframe
        bus_country_list = buses["country"].unique().tolist()

    non_allocated_countries = list(
        set(country_list).symmetric_difference(set(bus_country_list))
    )

    if len(non_allocated_countries) > 0:
        logger.error(
            f"There following countries could not be allocated properly: {non_allocated_countries}"
        )

    return buses


def built_network(
    inputs,
    outputs,
    build_osm_network_config,
    countries_config,
    geo_crs,
    distance_crs,
    lines_cols_standard,
    force_ac=False,
):
    logger.info("Stage 1/5: Read input data")
    osm_clean_columns = read_osm_config("osm_clean_columns")
    buses = read_geojson(
        inputs["substations"],
        osm_clean_columns["substation"].keys(),
        dtype=osm_clean_columns["substation"],
    )
    lines = read_geojson(
        inputs["lines"],
        osm_clean_columns["line"].keys(),
        dtype=osm_clean_columns["line"],
    )

    lines = line_endings_to_bus_conversion(lines)

    if force_ac:
        logger.info(
            "Stage 2/5: AC and DC network: disabled, forced buses and lines to AC"
        )
        lines = force_ac_lines(lines)
        buses["dc"] = False
    else:
        logger.info("Stage 2/5: AC and DC network: enabled")

    # Address the overpassing line issue Step 3/5
    if build_osm_network_config.get("split_overpassing_lines", False):
        tol = build_osm_network_config.get("overpassing_lines_tolerance", 1)
        logger.info("Stage 3/5: Avoid nodes overpassing lines: enabled with tolerance")

        lines, buses = fix_overpassing_lines(lines, buses, distance_crs, tol=tol)
    else:
        logger.info("Stage 3/5: Avoid nodes overpassing lines: disabled")

    # Add bus to countries with no buses
    buses = add_buses_to_empty_countries(countries_config, inputs.country_shapes, buses)

    # METHOD to merge buses with same voltage and within tolerance Step 4/5
    if build_osm_network_config.get("group_close_buses", False):
        tol = build_osm_network_config.get("group_tolerance_buses", 500)
        logger.info(
            f"Stage 4/5: Aggregate close substations: enabled with tolerance {tol} m"
        )
        lines, buses = merge_stations_lines_by_station_id_and_voltage(
            lines, buses, geo_crs, distance_crs, tol=tol
        )
    else:
        logger.info("Stage 4/5: Aggregate close substations: disabled")

    logger.info("Stage 5/5: Add augmented substation to country with no data")

    # get transformers: modelled as lines connecting buses with different voltage
    transformers = get_transformers(buses, lines)

    # get converters: currently modelled as links connecting buses with different polarity
    converters = get_converters(buses, lines)

    logger.info("Save outputs")

    # create clean directory if not already exist
    if not os.path.exists(outputs["lines"]):
        os.makedirs(os.path.dirname(outputs["lines"]), exist_ok=True)

    lines = lines[lines_cols_standard]

    to_csv_nafix(lines, outputs["lines"])  # Generate CSV
    to_csv_nafix(converters, outputs["converters"])  # Generate CSV
    to_csv_nafix(transformers, outputs["transformers"])  # Generate CSV

    # create clean directory if not already exist
    if not os.path.exists(outputs["substations"]):
        os.makedirs(os.path.dirname(outputs["substations"]), exist_ok=True)
    # Generate CSV
    to_csv_nafix(buses, outputs["substations"])

    return None


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        snakemake = mock_snakemake("build_osm_network")

    configure_logging(snakemake)

    # load default crs
    geo_crs = snakemake.params.crs["geo_crs"]
    distance_crs = snakemake.params.crs["distance_crs"]
    force_ac = snakemake.params.build_osm_network.get("force_ac", False)
    build_osm_network = snakemake.params.build_osm_network
    countries = snakemake.params.countries

    built_network(
        snakemake.input,
        snakemake.output,
        build_osm_network,
        countries,
        geo_crs,
        distance_crs,
        lines_cols_standard=LINES_COLUMNS,
        force_ac=force_ac,
    )
