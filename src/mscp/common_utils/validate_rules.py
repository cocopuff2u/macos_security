# src/mscp/validate_rules.py

# Standard python modules
import argparse
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

# Local python modules
from . import SCHEMA_PATH, config, open_file
from .logger_instance import logger

# Additional python modules


def get_rule_identifier(rule_file: Path) -> str:
    rule_yaml = open_file(rule_file)

    if "id" in rule_yaml:
        return rule_yaml["id"]
    else:
        return rule_file.stem


def validate_yaml_file(args: argparse.Namespace) -> None:
    schema: dict = open_file(Path(SCHEMA_PATH))
    validator = Draft202012Validator(schema)

    if args.rules_dir:
        yaml_files: list = list(Path(args.rules_dir).rglob("*.y*ml"))
    else:
        yaml_files: list = list(Path(config["defaults"]["rules_dir"]).rglob("*.y*ml"))
        yaml_files += list(Path(config["custom"]["rules_dir"]).rglob("*.y*ml"))

    if not yaml_files:
        logger.error("No YAML files found in rules directory.")
        return

    logger.info(
        f"Validating {len(yaml_files)} YAML files in '{config['defaults']['rules_dir']}'...\n"
    )

    discovered_rules = []
    for yaml in yaml_files:
        data: dict = open_file(yaml)
        if get_rule_identifier(yaml) in discovered_rules:
            print(f"⚠️ WARNING:   {yaml} may be a duplicate rule")
        else:
            discovered_rules.append(get_rule_identifier(yaml))

        try:
            validator.validate(data)
            if args.all_validation:
                print(f"✅ VALID:   {yaml}")
                logger.info(f"✅ VALID:   {yaml}")
        except ValidationError as e:
            print(f"❌ INVALID: {yaml} → {e.message}")
            logger.warning(f"❌ INVALID: {yaml} → {e.message}")
        except Exception as e:
            print(f"⚠️ ERROR:   {yaml} → {e}")
            logger.error(f"⚠️ ERROR:   {yaml} → {e}")


def validate_rule_folder_structure(path_str: str) -> Path:
    """
    Argparse 'type' validator:
    - Ensures PATH exists and is a directory.
    - Ensures root contains only subdirectories (no files).
    - Ensures each subdir contains only YAML files and/or is empty.
    - Disallows nested directories under subfolders (can be toggled).
    """
    ALLOWED_EXTS = {".yaml", ".yml"}

    from ..classes.macsecurityrule import Sectionmap

    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        raise argparse.ArgumentTypeError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise argparse.ArgumentTypeError(f"Path is not a directory: {p}")

    # Inspect contents of root
    root_entries = list(p.iterdir())

    # Root must contain only subdirectories (if you want to allow files, relax this).
    for e in root_entries:
        if e.name.startswith("."):
            continue
        if e.is_file():
            raise argparse.ArgumentTypeError(
                f"Rule files need to be organized in subfolders. '{e.name}' found in root of folder."
            )

    # For each subdirectory: must contain only .yaml/.yml files (or be empty)
    for sub in (e for e in root_entries if e.is_dir()):
        if not sub.name.upper() in Sectionmap.__members__:
            raise argparse.ArgumentTypeError(
                f"'{sub.name}' is not a valid folder name, please organize into the following folders [{', '.join([section.name.lower() for section in Sectionmap])}]. "
            )

        for child in sub.iterdir():
            if child.is_dir():
                raise argparse.ArgumentTypeError(
                    f"'{sub.name}' contains a nested directory '{child.name}'. "
                    "Only YAML files are expected in subfolders."
                )
            if child.is_file() and child.suffix.lower() not in ALLOWED_EXTS:
                raise argparse.ArgumentTypeError(
                    f"'{sub.name}' contains non-YAML file '{child.name}'. "
                    "Allowed extensions: .yaml, .yml"
                )

    return p  # On success, return a canonical Path
