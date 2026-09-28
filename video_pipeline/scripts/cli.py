from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure scripts directory is in sys.path for flat imports
scripts_dir = Path(__file__).resolve().parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from config import load_config
from database import Database
from engines import EngineError, EngineSeam
from main import process_file
from utils import setup_logging


def validate_input_file(path_str: str) -> str:
    path = Path(path_str)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Input video file does not exist or is not a file: {path_str}")
    return str(path.resolve())


def parse_trim(val: str) -> tuple[float, float]:
    parts = val.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Trim must be in format START:END (e.g. 1.0:4.0), got '{val}'")
    try:
        start = float(parts[0].strip())
        end = float(parts[1].strip())
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"Invalid float values in trim '{val}': {e}") from e
    return (start, end)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="CleanVideos single-file CLI runner: process video through horizontal and vertical pipeline.",
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        type=validate_input_file,
        help="Path to the input video file to process.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Path to output directory for generated deliverables.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-processing even if already completed in database.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without modifying files.",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Optional path to config.yaml configuration file.",
    )
    parser.add_argument(
        "--separator",
        type=str,
        default=None,
        help="Command override for audio separation engine.",
    )
    parser.add_argument(
        "--cropper",
        type=str,
        default=None,
        help="Command override for vertical cropper engine.",
    )
    parser.add_argument(
        "--trim",
        type=parse_trim,
        default=None,
        help="Trim video temporal range in format START:END (e.g. 1.0:4.0).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)
    if args.output:
        cfg["output_dir"] = str(Path(args.output).resolve())

    logger = setup_logging(cfg["logs_dir"])
    db = Database(cfg["state_db"])
    engines = EngineSeam(separator=args.separator, cropper=args.cropper)

    if args.dry_run:
        logger.info(f"[DRY-RUN] Would process file: {args.input} with output dir: {cfg['output_dir']}")
        return 0

    try:
        process_file(args.input, cfg, db, logger, engines=engines, force=args.force, trim=args.trim)
        return 0
    except Exception as exc:
        logger.error(f"Processing failed: {exc}")
        sys.stderr.write(f"Error: {exc}\n")
        return 1



if __name__ == "__main__":
    sys.exit(main())
