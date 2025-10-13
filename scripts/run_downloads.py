import sys
import argparse
import subprocess
from pathlib import Path
from typing import List

try:
    import pandas as pd
except ImportError:
    print("Error: The 'pandas' package is not installed. Please install it by running: pip install pandas")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("Error: The 'pyyaml' package is not installed. Please install it by running: pip install pyyaml")
    sys.exit(1)

try:
    from loguru import logger
except ImportError:
    print("Error: The 'loguru' package is not installed. Please install it by running: pip install loguru")
    sys.exit(1)


# Configure logger for the script
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>")

def load_config(path: Path) -> dict:
    """Loads the YAML configuration file."""
    if not path.is_file():
        logger.error(f"Configuration file not found at: {path}")
        sys.exit(1)
    logger.info(f"Loading configuration from: {path}")
    with open(path, "r") as f:
        return yaml.safe_load(f)

def run_command(command: List[str], dry_run: bool = False) -> str:
    """Runs a command or logs it for a dry run. Returns 'success', 'error', or 'dry_run'."""
    cmd_str = ' '.join(command)
    if dry_run:
        logger.info(f"[DRY RUN] Would execute: {cmd_str}")
        return 'dry_run'

    logger.info(f"Executing: {cmd_str}")
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        if result.stdout:
            logger.debug(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            logger.warning(f"STDERR:\n{result.stderr}")
        logger.success(f"Successfully executed: {cmd_str}")
        return 'success'
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Command failed: {e}")
        return 'error'

def main():
    """Main orchestrator function to run downloads based on the config file."""
    STATUS = {"executed": 0, "skipped": 0, "errors": 0}

    parser = argparse.ArgumentParser(description="Orchestrator for downloading and processing forex data.")
    parser.add_argument("--config", default="configs/downloads.yml", help="Path to the YAML configuration file.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands that would be executed without running them.")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    
    cli_module = config["cli"]["module"]
    symbols = config["symbols"]
    options = config.get("options", {})
    
    for dataset in config.get("datasets", []):
        kind = dataset.get("kind")
        start_date_str = str(dataset.get("start"))
        end_date_str = str(dataset.get("end"))
        
        try:
            end_inclusive = pd.to_datetime(end_date_str) - pd.Timedelta(days=1)
            date_range = pd.date_range(start=start_date_str, end=end_inclusive, freq='D')
        except ValueError as e:
            logger.error(f"Invalid date range for dataset '{kind}': {e}. Skipping entire dataset.")
            try:
                num_days = (pd.to_datetime(end_date_str) - pd.to_datetime(start_date_str)).days
                STATUS["errors"] += len(symbols) * num_days
            except ValueError:
                STATUS["errors"] += len(symbols)
            continue

        out_dir = Path(dataset.get("out_dir"))
        out_dir.mkdir(parents=True, exist_ok=True)

        command_name = config.get("cli", {}).get("commands", {}).get(kind)
        if not command_name:
            logger.warning(f"No CLI command found for dataset kind: '{kind}'. Skipping.")
            continue

        logger.info(f"Processing dataset: '{kind.upper()}' for period [{start_date_str}, {end_date_str})")

        for symbol in symbols:
            logger.info(f"--- Symbol: {symbol} ---")
            
            for process_date in date_range:
                day_start_str = process_date.strftime('%Y-%m-%d')
                day_end_str = (process_date + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                
                logger.info(f"---- Date Range: [{day_start_str}, {day_end_str}) ----")

                if options.get("skip_if_exists", False):
                    year, month, day = process_date.year, process_date.month, process_date.day
                    partition_path = out_dir / f"symbol={symbol}" / f"year={year}" / f"month={month}" / f"day={day}"
                    if partition_path.exists() and any(partition_path.glob("*.parquet")):
                         logger.info(f"Data for {symbol} on {day_start_str} found in {partition_path}. Skipping.")
                         STATUS["skipped"] += 1
                         continue

                command = [
                    sys.executable,
                    "-m",
                    cli_module,
                    command_name,
                    "--symbol",
                    symbol,
                    "--start",
                    day_start_str,
                    "--end",
                    day_end_str, # Use same day for start and end
                ]
                
                result = run_command(command, dry_run=args.dry_run)
                if result == 'success':
                    STATUS["executed"] += 1
                elif result == 'error':
                    STATUS["errors"] += 1
    
    logger.info("="*40)
    logger.info("Orchestration Summary")
    logger.info("="*40)
    logger.info(f"Tasks Executed: {STATUS['executed']}")
    logger.info(f"Tasks Skipped:  {STATUS['skipped']}")
    logger.info(f"Tasks with Errors: {STATUS['errors']}")
    logger.info("="*40)

    if STATUS["errors"] > 0:
        logger.error("Completed with errors.")
        sys.exit(1)
    
    logger.success("Completed successfully.")
    sys.exit(0)

if __name__ == "__main__":
    main()

