from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from time import perf_counter

import pandas as pd
from loguru import logger
import typer
from tqdm import tqdm

from .config import settings
from .logger_utils import setup_logging
from .storage import write_parquet
from .validators import detect_spikes, fill_session_flag, validate_bid_ask
from .vendors import dukascopy as duka

LOG_DIR = Path(settings.data_root) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "cli_run.log"
_CLI_LOG_SINK_ID: int | None = None


def _configure_cli_file_logger() -> None:
    """Ensure file sink is attached; re-add after setup_logging resets sinks."""
    global _CLI_LOG_SINK_ID
    if _CLI_LOG_SINK_ID is not None:
        try:
            logger.remove(_CLI_LOG_SINK_ID)
        except ValueError:
            pass
    _CLI_LOG_SINK_ID = logger.add(
        LOG_FILE,
        level="INFO",
        enqueue=True,
        rotation="20 MB",
        compression="zip",
        backtrace=False,
        diagnose=False,
    )


_configure_cli_file_logger()

app = typer.Typer(help="FX Data Downloader (Dukascopy)")


def _get_registered_command_names() -> list[str]:
    """Return sorted list of CLI command names currently registered in Typer."""
    names: list[str] = []
    for cmd in getattr(app, "registered_commands", []):
        name = getattr(cmd, "name", None)
        if not name and getattr(cmd, "callback", None):
            callback_name = getattr(cmd.callback, "__name__", "")
            if callback_name:
                name = callback_name.replace("_", "-")
        if name:
            names.append(name)
    # Preserve order but avoid duplicates
    seen: set[str] = set()
    ordered_unique = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered_unique.append(name)
    return ordered_unique


def _log_available_commands() -> None:
    """Log the commands exposed by this CLI module for quick verification."""
    names = _get_registered_command_names()
    if names:
        logger.info("Comandos disponibles: {}", ", ".join(names))
    else:
        logger.warning("No se registraron comandos en la aplicación Typer.")


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


@app.command()
def download_ticks(symbol: str = typer.Option(..., help="Símbolo, p.ej. EURUSD"),
                   start: str = typer.Option(..., help="YYYY-MM-DD"),
                   end: str = typer.Option(..., help="YYYY-MM-DD"),
                   clean_intermediate: bool = typer.Option(False, help="Se ignora en esta etapa; reservado para pipeline.")):
    """Descarga ticks L1 de Dukascopy (requiere paquete opcional 'duka')."""
    log = setup_logging()
    s, e = _parse_date(start), _parse_date(end)
    out_dir = Path(settings.data_root) / "raw" / "ticks" / symbol
    csv_path = duka.download_ticks(symbol, s, e, out_dir)
    df = duka.load_ticks_csv(csv_path, symbol)
    # Particionado por symbol/year/month/day
    df["year"] = pd.to_datetime(df["ts_utc"], unit="ms", utc=True).dt.year
    df["month"] = pd.to_datetime(df["ts_utc"], unit="ms", utc=True).dt.month
    df["day"] = pd.to_datetime(df["ts_utc"], unit="ms", utc=True).dt.day
    write_parquet(df, Path(settings.data_root) / "raw" / "ticks_parquet", partition_cols=["symbol","year","month","day"])
    log.info("Ticks guardados en Parquet (particionado).")

@app.command()
def build_bars(symbol: str = typer.Option(..., help="Símbolo"),
               start: str = typer.Option(..., help="YYYY-MM-DD"),
               end: str = typer.Option(..., help="YYYY-MM-DD"),
               clean_intermediate: bool = typer.Option(False, help="Se ignora en esta etapa; reservado para pipeline.")):
    """Reconstruye barras 1m (BID/ASK) desde ticks previamente descargados."""
    log = setup_logging()
    s, e = _parse_date(start), _parse_date(end)
    # Cargamos los CSV exportados por `duka` para simplificar el ejemplo
    csv_path = Path(settings.data_root) / "raw" / "ticks" / symbol / f"{symbol}_{s:%Y%m%d}_{e:%Y%m%d}_ticks.csv"
    if not csv_path.exists():
        log.error(f"No se encuentra {csv_path}. Ejecuta primero 'download-ticks'.")
        raise typer.Exit(1)
    ticks = duka.load_ticks_csv(csv_path, symbol)
    bars = duka.ticks_to_bars(ticks, timeframe="1m")
    # Añadimos symbol y session_flag
    bars["symbol"] = symbol
    bars = fill_session_flag(bars)
    # Particionado por symbol/year/month/day
    bars["year"] = pd.to_datetime(bars["ts_utc_open"], unit="ms", utc=True).dt.year
    bars["month"] = pd.to_datetime(bars["ts_utc_open"], unit="ms", utc=True).dt.month
    bars["day"] = pd.to_datetime(bars["ts_utc_open"], unit="ms", utc=True).dt.day
    write_parquet(bars, Path(settings.data_root) / "curated" / "bars_1m", partition_cols=["symbol","year","month","day"])
    log.info("Barras 1m guardadas en Parquet (particionado).")


@app.command()
def validate(symbol: str = typer.Option(..., help="Símbolo"),
             start: str = typer.Option(..., help="YYYY-MM-DD"),
             end: str = typer.Option(..., help="YYYY-MM-DD"),
             clean_intermediate: bool = typer.Option(False, help="Si se activa (pipeline), no genera múltiples archivos intermedios.")):
    """Valida reglas básicas (ask>=bid, spikes) sobre las barras 1m generadas."""
    log = setup_logging()
    s, e = _parse_date(start), _parse_date(end)
    csv_path = Path(settings.data_root) / "raw" / "ticks" / symbol / f"{symbol}_{s:%Y%m%d}_{e:%Y%m%d}_ticks.csv"
    if not csv_path.exists():
        log.error(f"No se encuentra {csv_path}. Ejecuta primero 'download-ticks'.")
        raise typer.Exit(1)
    ticks = duka.load_ticks_csv(csv_path, symbol)
    ticks["mid"] = (ticks["bid"] + ticks["ask"]) / 2.0
    bad = validate_bid_ask(ticks)
    spikes = detect_spikes(ticks, col="mid", window=120, k=7.0)
    log.info(f"Registros ask<bid: {len(bad)} - Spikes detectados: {len(spikes)}")
    # Export simple CSV de hallazgos
    out_dir = Path(settings.data_root) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(bad) > 0:
        bad.to_csv(out_dir / f"{symbol}_{s:%Y%m%d}_{e:%Y%m%d}_bad_bidask.csv", index=False)
    if len(spikes) > 0:
        spikes.to_csv(out_dir / f"{symbol}_{s:%Y%m%d}_{e:%Y%m%d}_spikes.csv", index=False)
    log.info(f"Reportes guardados en {out_dir}")


def _process_symbol(symbol: str, start: str, end: str, mode: str, timeframe: str, clean_intermediate: bool) -> None:
    """
    Ejecuta la tubería completa (download -> build -> validate) para un símbolo.
    Lanza excepciones para que el caller maneje los fallos sin detener otros símbolos.
    """
    _configure_cli_file_logger()
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    symbol_start = perf_counter()
    logger.info(
        f"Iniciando procesamiento de {symbol} [{start_dt:%Y-%m-%d}, {end_dt:%Y-%m-%d}) | "
        f"Modo: {mode} | Timeframe: {timeframe}"
    )

    final_path: Path | None = None

    if mode == "ticks":
        steps = (
            ("download_ticks", download_ticks),
            ("build_bars", build_bars),
            ("validate", validate),
        )

        for step_name, step_fn in steps:
            step_start = perf_counter()
            try:
                step_fn(symbol=symbol, start=start, end=end, clean_intermediate=clean_intermediate)
            except Exception as exc:  # noqa: BLE001
                _configure_cli_file_logger()
                logger.exception(f"{step_name} falló para {symbol}: {exc}")
                logger.warning(f"Saltando el resto de pasos para {symbol} tras el fallo en {step_name}.")
                raise
            else:
                duration = perf_counter() - step_start
                _configure_cli_file_logger()
                logger.info(f"{step_name} completado para {symbol} en {duration:.2f}s")

        final_path = duka.consolidate_final_output(
            symbol,
            start,
            end,
            mode="ticks",
            timeframe="1m",
            remove_intermediate=clean_intermediate,
        )
    elif mode == "ohlc":
        final_path = duka.download_ohlc(
            symbol=symbol,
            start=start,
            end=end,
            timeframe=timeframe,
            remove_intermediate=clean_intermediate,
        )
    else:
        raise typer.BadParameter("Modo inválido. Usa 'ticks' o 'ohlc'.")

    symbol_duration = perf_counter() - symbol_start

    _configure_cli_file_logger()
    logger.info(
        f"Procesamiento de {symbol} finalizado en {symbol_duration:.2f}s. "
        f"Archivo final: {final_path}"
    )


@app.command()
def run_all(
    symbols: str = typer.Option(..., help="Lista de símbolos separada por coma, p.ej. EURUSD,GBPUSD"),
    start: str = typer.Option(..., help="YYYY-MM-DD"),
    end: str = typer.Option(..., help="YYYY-MM-DD"),
    max_workers: int = typer.Option(4, help="Número máximo de hilos para procesar símbolos en paralelo"),
    mode: str = typer.Option("ticks", help="Tipo de descarga: ticks u ohlc"),
    timeframe: str = typer.Option("1m", help="Timeframe (solo aplica a modo ohlc: 1m,5m,15m,1h,4h,1d)"),
    clean_intermediate: bool = typer.Option(True, help="Consolidar y eliminar archivos intermedios al finalizar cada símbolo"),
) -> None:
    """
    Ejecuta download_ticks, build_bars y validate para cada símbolo indicado.
    Continúa con el siguiente símbolo si ocurre un error.
    """
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    symbol_list = [sym.strip().upper() for sym in symbols.split(",") if sym.strip()]
    if not symbol_list:
        raise typer.BadParameter("Debes especificar al menos un símbolo válido.")

    mode = mode.lower()
    timeframe = timeframe.lower()
    valid_modes = {"ticks", "ohlc"}
    if mode not in valid_modes:
        raise typer.BadParameter("Modo inválido. Usa 'ticks' o 'ohlc'.")

    valid_timeframes = {"1m", "5m", "15m", "1h", "4h", "1d"}
    if mode == "ohlc" and timeframe not in valid_timeframes:
        raise typer.BadParameter("Timeframe inválido para modo OHLC. Usa 1m,5m,15m,1h,4h,1d.")

    setup_logging()
    _configure_cli_file_logger()

    overall_start = perf_counter()
    logger.info(f"run_all iniciado a las {datetime.now().isoformat(timespec='seconds')}")
    logger.info(f"Procesando símbolos {symbol_list} en rango [{start_dt:%Y-%m-%d}, {end_dt:%Y-%m-%d})")
    logger.info(f"Modo de descarga: {mode} | Timeframe: {timeframe} | clean_intermediate={clean_intermediate}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_process_symbol, symbol, start, end, mode, timeframe, clean_intermediate): symbol
            for symbol in symbol_list
        }
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Procesando símbolos",
            unit="símbolo",
        ):
            symbol = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                _configure_cli_file_logger()
                logger.exception(f"{symbol} falló: {exc}")

    total_duration = perf_counter() - overall_start
    _configure_cli_file_logger()
    logger.info(
        f"run_all completado para {len(symbol_list)} símbolos en {total_duration:.2f}s "
        f"con {max_workers} hilos. clean_intermediate={clean_intermediate}"
    )


if __name__ == "__main__":
    setup_logging()
    _configure_cli_file_logger()
    _log_available_commands()
    app()
