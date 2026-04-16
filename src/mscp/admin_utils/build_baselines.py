# mscp/admin_utils/build_all_baselines.py

# Standard python modules
import argparse
from pathlib import Path

# Local python modules
from ..common_utils import (
    config,
    logger,
    mscp_data,
    remove_dir_contents,
)
from ..classes import Macsecurityrule
from ..generate import (
    generate_baseline,
)
from ..generate.baseline import (
    collect_tags_and_benchmarks,
)


def build_all_baselines(args: argparse.Namespace) -> None:
    """Build all baselines supported in MSCP"""
    logger.info("Building all supported baselines...")

    # clear existing default baselines
    baselines_dir = Path(config["defaults"].get("baseline_dir", ""))
    remove_dir_contents(baselines_dir)

    # set default args expected by generate_baseline
    args.tailor = False
    args.list_tags = False
    args.controls = False

    # exclude generating baselines for these keys
    excluded_tags = {"arm64", "i368", "inherent", "manual", "n_a", "none", "permanent"}

    all_rules: list[Macsecurityrule] = Macsecurityrule.collect_all_rules(
        args.os_name, args.os_version, args.tailor, parent_values="Default"
    )

    all_tags, benchmark_map = collect_tags_and_benchmarks(all_rules)

    all_tags[:] = [x for x in all_tags if x not in excluded_tags]

    # process every discovered benchmark and generate a baseline file
    for keyword, platforms in benchmark_map.items():
        args.keyword = keyword
        for platform in platforms:
            args.os_name = platform.lower()
            generate_baseline(args, admin=True)

    # process every discovered tag and generate a baseline file for every supported platform
    for platform in mscp_data["versions"]["platforms"]:
        args.os_name = platform

        for tag in all_tags:
            args.keyword = tag
            generate_baseline(args, admin=True)
