# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import lzma
import struct
import sys
import subprocess
import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional
import time
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
import urllib.request

from ..config import settings

_TIMEFRAME_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}
_INV_TIMEFRAME_MAP = {v: k for k, v in _TIMEFRAME_MAP.items()}


def _resolve_timeframe(timeframe: str) -> tuple[str, str]:
    tf = timeframe.lower()
    if tf in _TIMEFRAME_MAP:
        return tf, _TIMEFRAME_MAP[tf]
    if tf in _INV_TIMEFRAME_MAP:
        return _INV_TIMEFRAME_MAP[tf], tf
    raise ValueError(f"Timeframe no soportado: {timeframe}")


# ------------------------------ Utilidades ------------------------------

def _duka_command() -> list[str]:
    """
    Devuelve cómo invocar duka: si existe el ejecutable `duka`, úsalo;
    en caso contrario usa `python -m duka` con este intérprete.
    """
    exe = shutil.which("duka")
    if exe:
        return [exe]
    return [sys.executable, "-m", "duka"]


def _price_scale(symbol: str) -> int:
    """
    Escala de precios para decodificar .bi5. EURUSD -> 1e5, pares JPY -> 1e3.
    """
    s = symbol.upper()
    return 1000 if s.endswith("JPY") else 100000


def _hour_url(symbol: str, dt_utc: datetime) -> str:
    """
    URL de un bloque horario de ticks .bi5 en el datafeed.
    dt_utc debe tener tzinfo=UTC y minuto/segundo=0.
    """
    base = "https://datafeed.dukascopy.com/datafeed"
    y = dt_utc.year
    m = dt_utc.month - 1  # ¡mes cero-indexado en el feed!
    d = dt_utc.day
    h = dt_utc.hour
    return f"{base}/{symbol}/{y:04d}/{m:02d}/{d:02d}/{h:02d}h_ticks.bi5"


def _fetch_bi5_hour(symbol: str, dt_utc: datetime) -> pd.DataFrame | None:
    """
    Descarga y decodifica un .bi5 horario -> DataFrame con columnas:
    ts_utc, bid, ask, bid_vol, ask_vol
    """
    url = _hour_url(symbol, dt_utc)
    req = urllib.request.Request(url, headers={"User-Agent": "forex_data_downloader/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except Exception as e:
        logger.debug(f"GET fallo {url}: {e!r}")
        return None
    if not raw:
        return None

    try:
        dec = lzma.decompress(raw)
    except lzma.LZMAError as e:
        logger.debug(f"LZMA fallo {url}: {e!r}")
        return None
    if not dec:
        return None

    rec_size = 20
    n = len(dec) // rec_size
    if n == 0:
        return None

    buf = io.BytesIO(dec)
    unpack = struct.Struct(">iiiii").unpack  # big-endian: ms, ask, bid, askVol, bidVol
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)

    hour_start = dt_utc.replace(minute=0, second=0, microsecond=0)
    base_epoch_ms = int(hour_start.timestamp() * 1000)
    scale = _price_scale(symbol)

    rows = []
    for _ in range(n):
        chunk = buf.read(rec_size)
        if len(chunk) < rec_size:
            break
        ms, ask_i, bid_i, askv_i, bidv_i = unpack(chunk)
        ts_ms = base_epoch_ms + ms
        rows.append((ts_ms, bid_i / scale, ask_i / scale, bidv_i, askv_i))

    if not rows:
        return None
    return pd.DataFrame(rows, columns=["ts_utc", "bid", "ask", "bid_vol", "ask_vol"])


def _range_hours(start: datetime, end: datetime):
    """
    Generador de horas UTC [start, end) con paso de 1h.
    Respeta hora/tz originales; se redondea start hacia abajo y end hacia arriba.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)

    if start >= end:
        return

    cur = start.replace(minute=0, second=0, microsecond=0)
    stop = end.replace(minute=0, second=0, microsecond=0)
    if end > stop:
        stop += timedelta(hours=1)

    while cur < stop:
        yield cur
        cur += timedelta(hours=1)


# ------------------------------ API pública ------------------------------

def download_ticks(symbol: str, start: datetime, end: datetime, out_dir: Path, threads: int = 4) -> Path:
    """
    Intenta descargar ticks con el CLI `duka`; si el resultado es vacío,
    hace fallback al datafeed directo y genera el CSV unificado.

    Retorna la ruta del CSV unificado:
      {out_dir}/{symbol}_{YYYYMMDD}_{YYYYMMDD}_ticks.csv
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_dir = out_dir / f"duka_{symbol}_{start:%Y%m%d}_{end:%Y%m%d}"
    run_dir.mkdir(parents=True, exist_ok=True)

    out_csv = out_dir / f"{symbol}_{start:%Y%m%d}_{end:%Y%m%d}_ticks.csv"

    # ---------- 1) Intento con CLI duka ----------
    cmd = _duka_command() + [
        symbol,
        "-s", start.strftime("%Y-%m-%d"),
        "-e", end.strftime("%Y-%m-%d"),
        "-f", str(run_dir),
        "--header",
        "-t", str(threads),
    ]
    logger.info("Running: " + " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)

    # Guardar stdout/stderr para diagnóstico
    try:
        (run_dir / "duka.stdout.txt").write_text(res.stdout or "", encoding="utf-8")
        (run_dir / "duka.stderr.txt").write_text(res.stderr or "", encoding="utf-8")
    except Exception:
        pass

    if res.returncode != 0:
        logger.warning(f"duka CLI devolvió código {res.returncode}. Intentaré fallback directo.")

    # Unificar cualquier CSV que haya dejado duka (ignorando los vacíos)
    dfs: list[pd.DataFrame] = []
    csv_files = sorted(p for p in run_dir.glob("*.csv"))
    non_empty_partials: list[Path] = []
    for fp in csv_files:
        try:
            tmp = pd.read_csv(fp)
        except Exception as e:
            logger.warning(f"No pude leer {fp.name}: {e}")
            continue
        if tmp.empty or len(tmp.columns) == 0:
            logger.warning(f"Empty CSV {fp.name} (skipping)")
            try:
                fp.unlink()
            except Exception as unlink_err:
                logger.debug(f"No se pudo eliminar {fp}: {unlink_err!r}")
            continue

        tmp = tmp.rename(columns={c: c.lower() for c in tmp.columns})
        # duka puede traer "time" o "timestamp"
        if "ts_utc" in tmp.columns:
            pass
        else:
            if "time" in tmp.columns:
                ts = pd.to_datetime(tmp["time"], utc=True, errors="coerce")
            elif "timestamp" in tmp.columns:
                ts = pd.to_datetime(tmp["timestamp"], utc=True, errors="coerce")
            else:
                logger.warning(f"{fp.name} no tiene columna de tiempo reconocible (skipping)")
                continue
            if ts.isna().all():
                logger.warning(f"{fp.name} tiene tiempos no parseables (skipping)")
                continue
            tmp["ts_utc"] = (ts.view("int64") // 10**6).astype("int64")

        if not {"bid", "ask"}.issubset(tmp.columns):
            logger.warning(f"{fp.name} no tiene bid/ask (skipping)")
            continue

        # Completar opcionales
        if "bid_vol" not in tmp.columns:
            tmp["bid_vol"] = np.nan
        if "ask_vol" not in tmp.columns:
            tmp["ask_vol"] = np.nan
        if "quality_flag" not in tmp.columns:
            tmp["quality_flag"] = 0
        tmp["symbol"] = symbol
        non_empty_partials.append(fp)

        dfs.append(tmp[["ts_utc", "bid", "ask", "bid_vol", "ask_vol", "symbol", "quality_flag"]])

    if dfs:
        full = pd.concat(dfs, axis=0, ignore_index=True, sort=False).sort_values("ts_utc")
        full.to_csv(out_csv, index=False)
        total_rows = len(full)
        logger.info(f"Consolidated {total_rows:,} ticks into {out_csv}")
        for fp in non_empty_partials:
            if fp.exists():
                try:
                    fp.unlink()
                except Exception as unlink_err:
                    logger.debug(f"No se pudo eliminar {fp}: {unlink_err!r}")
        return out_csv

    for fp in csv_files:
        if fp.exists():
            try:
                fp.unlink()
            except Exception as unlink_err:
                logger.debug(f"No se pudo eliminar {fp}: {unlink_err!r}")

    logger.warning(
        "No se pudo extraer ningun tick valido del CLI. Intentando fallback directo (datafeed.dukascopy.com)..."
    )

    # ---------- 2) Fallback directo contra el datafeed ----------
    frames: list[pd.DataFrame] = []
    ok_hours = 0
    total_hours = 0
    for h in _range_hours(start, end):
        total_hours += 1
        dfh = _fetch_bi5_hour(symbol, h)
        if dfh is None or dfh.empty:
            continue
        ok_hours += 1
        frames.append(dfh)

    if not frames:
        # como ultimo recurso, escribimos CSV vacio con esquema para no romper el pipeline
        empty = pd.DataFrame(
            columns=["ts_utc", "bid", "ask", "bid_vol", "ask_vol", "symbol", "quality_flag"]
        )
        empty.to_csv(out_csv, index=False)
        logger.warning(
            f"Fallback directo tampoco obtuvo datos (0/{total_hours} horas). "
            f"Escribi CSV vacio: {out_csv}"
        )
        return out_csv

    full = pd.concat(frames, axis=0, ignore_index=True, sort=False).sort_values("ts_utc")
    full["symbol"] = symbol
    full["quality_flag"] = 0
    full.to_csv(out_csv, index=False)
    logger.info(f"Fallback OK: {ok_hours}/{total_hours} horas con datos. CSV: {out_csv}")
    logger.info(f"Consolidated {len(full):,} ticks into {out_csv}")
    return out_csv


def load_ticks_csv(csv_path: Path, symbol: str) -> pd.DataFrame:
    """
    Lee el CSV unificado de ticks y devuelve dataframe normalizado.
    """
    # 1) leer primero
    df = pd.read_csv(csv_path)

    # 2) normalizar nombres de columnas
    df = df.rename(columns=str.lower)

    # 3) validar columnas mínimas
    required = {"ts_utc", "bid", "ask"}
    if not required.issubset(df.columns):
        missing = required - set(df.columns)
        raise ValueError(f"CSV inválido. Faltan columnas: {missing}")

    # 4) completar opcionales
    if "bid_vol" not in df.columns:
        import numpy as np
        df["bid_vol"] = np.nan
    if "ask_vol" not in df.columns:
        import numpy as np
        df["ask_vol"] = np.nan
    if "quality_flag" not in df.columns:
        df["quality_flag"] = 0
    if "symbol" not in df.columns:
        df["symbol"] = symbol

    # 5) ordenar y reordenar columnas
    df = df.sort_values("ts_utc")
    return df[["ts_utc", "bid", "ask", "bid_vol", "ask_vol", "symbol", "quality_flag"]]

def ticks_to_bars(ticks: pd.DataFrame, timeframe: str = "1m") -> pd.DataFrame:
    """
    Construye OHLC para BID/ASK + volumen y tick_count a partir de ticks.
    """
    tf_key, freq = _resolve_timeframe(timeframe)

    ticks = ticks.sort_values("ts_utc").copy()
    ticks["dt"] = pd.to_datetime(ticks["ts_utc"], unit="ms", utc=True)
    ticks = ticks.set_index("dt")

    bid_ohlc = ticks["bid"].resample(freq).ohlc()
    ask_ohlc = ticks["ask"].resample(freq).ohlc()
    tick_count = ticks["bid"].resample(freq).count().rename("tick_count")
    bid_volume = ticks["bid_vol"].fillna(0).resample(freq).sum().rename("bid_volume")
    ask_volume = ticks["ask_vol"].fillna(0).resample(freq).sum().rename("ask_volume")

    df = (
        pd.concat(
            [
                bid_ohlc.add_prefix("bid_"),
                ask_ohlc.add_prefix("ask_"),
                bid_volume,
                ask_volume,
                tick_count,
            ],
            axis=1,
        )
        .dropna(how="any")
        .reset_index()
        .rename(columns={"dt": "ts_open"})
    )
    df["ts_utc_open"] = (df["ts_open"].view("int64") // 10**6).astype("int64")
    df = df.drop(columns=["ts_open"])

    df = df.rename(
        columns={
            "bid_open": "bid_o",
            "bid_high": "bid_h",
            "bid_low": "bid_l",
            "bid_close": "bid_c",
            "ask_open": "ask_o",
            "ask_high": "ask_h",
            "ask_low": "ask_l",
            "ask_close": "ask_c",
        }
    )
    df["timeframe"] = tf_key

    columns = [
        "ts_utc_open",
        "bid_o",
        "bid_h",
        "bid_l",
        "bid_c",
        "ask_o",
        "ask_h",
        "ask_l",
        "ask_c",
        "bid_volume",
        "ask_volume",
        "tick_count",
        "timeframe",
    ]
    return df[columns]


def consolidate_final_output(
    symbol: str,
    start: str,
    end: str,
    mode: str = "ticks",
    timeframe: Optional[str] = None,
    remove_intermediate: bool = True,
) -> Path:
    """
    Consolida datos de ticks, barras y reportes en un único Parquet final.
    Si remove_intermediate es True, elimina directorios intermedios tras consolidar.
    """
    data_root = Path(settings.data_root)
    start_key = start.replace("-", "")
    end_key = end.replace("-", "")
    tf_key = (timeframe or "").lower() or None

    final_dir = data_root / "final" / symbol
    final_dir.mkdir(parents=True, exist_ok=True)
    suffix_parts = [mode]
    if tf_key:
        suffix_parts.append(tf_key)
    final_name = f"{symbol}_{start_key}_{end_key}_{'_'.join(suffix_parts)}.parquet"
    final_path = final_dir / final_name

    frames: list[pd.DataFrame] = []

    if mode == "ticks":
        ticks_path = data_root / "raw" / "ticks" / symbol / f"{symbol}_{start_key}_{end_key}_ticks.csv"
        if ticks_path.exists():
            try:
                ticks_df = pd.read_csv(ticks_path)
                ticks_df["data_type"] = "ticks"
                ticks_df["source_path"] = str(ticks_path)
                frames.append(ticks_df)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"No se pudo incluir ticks de {ticks_path}: {exc}")

        bars_root = data_root / "curated" / "bars_1m" / f"symbol={symbol}"
        if bars_root.exists():
            try:
                bars_df = pd.read_parquet(bars_root)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"No se pudo leer parquet particionado en {bars_root}: {exc}")
            else:
                bars_df["data_type"] = "bars_1m"
                bars_df["source_path"] = str(bars_root)
                frames.append(bars_df)

        reports_root = data_root / "reports"
        if reports_root.exists():
            pattern = f"{symbol}_{start_key}_{end_key}_*.csv"
            for report_file in reports_root.glob(pattern):
                try:
                    report_df = pd.read_csv(report_file)
                    report_df["data_type"] = f"report:{report_file.stem}"
                    report_df["source_path"] = str(report_file)
                    frames.append(report_df)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"No se pudo leer reporte {report_file}: {exc}")
    elif mode == "ohlc":
        ohlc_root = data_root / "raw" / "ohlc" / symbol
        if ohlc_root.exists():
            pattern = f"{symbol}_{start_key}_{end_key}_*.csv"
            for ohlc_file in ohlc_root.glob(pattern):
                try:
                    ohlc_df = pd.read_csv(ohlc_file)
                    ohlc_df["data_type"] = f"ohlc:{ohlc_file.stem}"
                    ohlc_df["source_path"] = str(ohlc_file)
                    frames.append(ohlc_df)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"No se pudo incluir {ohlc_file}: {exc}")
        reports_root = data_root / "reports"
        if reports_root.exists():
            pattern = f"{symbol}_{start_key}_{end_key}_*.csv"
            for report_file in reports_root.glob(pattern):
                try:
                    report_df = pd.read_csv(report_file)
                    report_df["data_type"] = f"report:{report_file.stem}"
                    report_df["source_path"] = str(report_file)
                    frames.append(report_df)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"No se pudo leer reporte {report_file}: {exc}")

    if frames:
        final_df = pd.concat(frames, ignore_index=True, sort=False)
    else:
        final_df = pd.DataFrame()

    final_df["mode"] = mode
    final_df["timeframe"] = tf_key
    final_df["symbol"] = symbol

    final_df.to_parquet(final_path, index=False)
    logger.info(f"Consolidated {symbol} data into {final_path}")

    if remove_intermediate and final_path.exists():
        to_remove: list[Path] = []
        if mode == "ticks":
            to_remove.append(data_root / "raw" / "ticks" / symbol)
            ticks_parquet_root = data_root / "raw" / "ticks_parquet"
            if ticks_parquet_root.exists():
                to_remove.extend(p for p in ticks_parquet_root.glob(f"symbol={symbol}") if p.exists())
            bars_parquet_root = data_root / "curated" / "bars_1m"
            if bars_parquet_root.exists():
                to_remove.extend(p for p in bars_parquet_root.glob(f"symbol={symbol}") if p.exists())
        elif mode == "ohlc":
            to_remove.append(data_root / "raw" / "ohlc" / symbol)
            to_remove.append(data_root / "raw" / "ticks" / symbol)

        for path in to_remove:
            if path.exists():
                try:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"No se pudo eliminar {path}: {exc}")
        logger.info(f"Removed intermediate directories for {symbol} (mode={mode})")

    return final_path


def download_ohlc(
    symbol: str,
    start: str,
    end: str,
    timeframe: str,
    remove_intermediate: bool = True,
) -> Path:
    """
    Descarga datos OHLC + Volumen a partir de ticks para el símbolo y rango indicados.
    """
    tf_key, _ = _resolve_timeframe(timeframe)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")

    data_root = Path(settings.data_root)
    start_key = start.replace("-", "")
    end_key = end.replace("-", "")

    tick_dir = data_root / "raw" / "ticks" / symbol
    tick_dir.mkdir(parents=True, exist_ok=True)

    total_start = time.perf_counter()
    ticks_csv = download_ticks(symbol, start_dt, end_dt, tick_dir)
    ticks_df = load_ticks_csv(ticks_csv, symbol)
    if ticks_df.empty:
        logger.warning(f"No se obtuvieron ticks para {symbol} entre {start} y {end}.")

    bars_df = ticks_to_bars(ticks_df, timeframe=tf_key)
    bars_df["symbol"] = symbol
    bars_df["mode"] = "ohlc"
    bars_df["timeframe"] = tf_key

    raw_dir = data_root / "raw" / "ohlc" / symbol
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_csv = raw_dir / f"{symbol}_{start_key}_{end_key}_{tf_key}.csv"
    bars_df.to_csv(raw_csv, index=False)

    final_dir = data_root / "final" / symbol
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / f"{symbol}_{start_key}_{end_key}_ohlc_{tf_key}.parquet"
    bars_df.to_parquet(final_path, index=False)

    duration = time.perf_counter() - total_start
    logger.info(
        f"Descarga OHLC {tf_key} para {symbol} completada en {duration:.2f}s "
        f"({len(bars_df)} registros). Archivo: {final_path}"
    )

    if remove_intermediate:
        for path in [raw_dir, tick_dir]:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        logger.info(f"Removed intermediate OHLC directories for {symbol}")

    return final_path
