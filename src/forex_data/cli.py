from __future__ import annotations
import typer
from typing import Optional
from datetime import datetime
from pathlib import Path
import pandas as pd

from .logging import setup_logging
from .config import settings
from .storage import write_parquet
from .vendors import dukascopy as duka
from .validators import validate_bid_ask, detect_spikes, fill_session_flag

app = typer.Typer(help="FX Data Downloader (Dukascopy)")

def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")

@app.command()
def download_ticks(symbol: str = typer.Option(..., help="Símbolo, p.ej. EURUSD"),
                   start: str = typer.Option(..., help="YYYY-MM-DD"),
                   end: str = typer.Option(..., help="YYYY-MM-DD")):
    """Descarga ticks L1 de Dukascopy (requiere paquete opcional 'duka')."""
    log = setup_logging()
    s, e = _parse_date(start), _parse_date(end)
    out_dir = Path(settings.data_root) / "raw" / "ticks" / symbol
    csv_path = duka.download_ticks(symbol, s, e, out_dir)
    df = duka.load_ticks_csv(csv_path, symbol)
    # Particionado por symbol/year/month/day
    df["year"] = pd.to_datetime(df["ts_utc"], unit="s", utc=True).dt.year
    df["month"] = pd.to_datetime(df["ts_utc"], unit="s", utc=True).dt.month
    df["day"] = pd.to_datetime(df["ts_utc"], unit="s", utc=True).dt.day
    write_parquet(df, Path(settings.data_root) / "raw" / "ticks_parquet", partition_cols=["symbol","year","month","day"])
    log.info("Ticks guardados en Parquet (particionado).")

@app.command()
def build_bars(symbol: str = typer.Option(..., help="Símbolo"),
               start: str = typer.Option(..., help="YYYY-MM-DD"),
               end: str = typer.Option(..., help="YYYY-MM-DD")):
    """Reconstruye barras 1m (BID/ASK) desde ticks previamente descargados."""
    log = setup_logging()
    s, e = _parse_date(start), _parse_date(end)
    # Cargamos los CSV exportados por `duka` para simplificar el ejemplo
    csv_path = Path(settings.data_root) / "raw" / "ticks" / symbol / f"{symbol}_{s:%Y%m%d}_{e:%Y%m%d}_ticks.csv"
    if not csv_path.exists():
        log.error(f"No se encuentra {csv_path}. Ejecuta primero 'download-ticks'.")
        raise typer.Exit(1)
    ticks = duka.load_ticks_csv(csv_path, symbol)
    bars = duka.ticks_to_bars_1m(ticks)
    # Añadimos symbol y session_flag
    bars["symbol"] = symbol
    bars = fill_session_flag(bars)
    # Particionado por symbol/year/month/day
    bars["year"] = pd.to_datetime(bars["ts_utc_open"], unit="s", utc=True).dt.year
    bars["month"] = pd.to_datetime(bars["ts_utc_open"], unit="s", utc=True).dt.month
    bars["day"] = pd.to_datetime(bars["ts_utc_open"], unit="s", utc=True).dt.day
    write_parquet(bars, Path(settings.data_root) / "curated" / "bars_1m", partition_cols=["symbol","year","month","day"])
    log.info("Barras 1m guardadas en Parquet (particionado).")

@app.command()
def validate(symbol: str = typer.Option(..., help="Símbolo"),
             start: str = typer.Option(..., help="YYYY-MM-DD"),
             end: str = typer.Option(..., help="YYYY-MM-DD")):
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

if __name__ == "__main__":
    app()