# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import sys

CUTOUT = "cutout.nc"


def terminate_if_cutout_exists(subprocess_check=False):
    """
    Check if any of the requested cutout files exist.
    If that's the case, terminate execution to avoid data loss.
    """
    if subprocess_check:
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", type=int, default=snakemake.Mode.default)
        parser.add_argument("--target-jobs", type=str, nargs="+", default=[])
        args, _ = parser.parse_known_args(sys.argv)
        if args.mode == snakemake.Mode.subprocess and "build_cutout" not in " ".join(
            args.target_jobs
        ):
            return

    if os.path.exists(CUTOUT):
        os.remove(CUTOUT)
        raise Exception(
            f"A cutout file {CUTOUT} exists and risks to be overwritten. "
            "If this is an intended behavior, please move or delete this file and re-run the rule."
        )


terminate_if_cutout_exists(subprocess_check=config.get("subprocess_check", False))


rule remove_cutout:
    input:
        cutout=CUTOUT,
    run:
        os.remove(input.cutout)


rule build_cutout:
    output:
        cutout=CUTOUT,
    shell:
        "echo build_cutout > {output.cutout}"
