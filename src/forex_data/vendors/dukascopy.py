# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import lzma
import struct
import sys
import subprocess
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
import urllib.request


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
    midnight = datetime(dt_utc.year, dt_utc.month, dt_utc.day, tzinfo=timezone.utc)
    scale = _price_scale(symbol)

    rows = []
    for _ in range(n):
        chunk = buf.read(rec_size)
        if len(chunk) < rec_size:
            break
        ms, ask_i, bid_i, askv_i, bidv_i = unpack(chunk)
        ts = midnight + timedelta(milliseconds=ms)
        rows.append((int(ts.timestamp()), bid_i / scale, ask_i / scale, bidv_i, askv_i))

    if not rows:
        return None
    return pd.DataFrame(rows, columns=["ts_utc", "bid", "ask", "bid_vol", "ask_vol"])


def _range_hours(start: datetime, end: datetime):
    """
    Generador de horas UTC [start, end) con paso de 1h.
    start/end son fechas sin tz -> las tratamos como UTC a medianoche.
    """
    cur = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    stop = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
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
    for fp in csv_files:
        try:
            tmp = pd.read_csv(fp)
        except Exception as e:
            logger.warning(f"No pude leer {fp.name}: {e}")
            continue
        if tmp.empty or len(tmp.columns) == 0:
            logger.warning(f"Empty CSV {fp.name} (skipping)")
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
            tmp["ts_utc"] = (ts.astype("int64") // 10**9).astype("int64")

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

        dfs.append(tmp[["ts_utc", "bid", "ask", "bid_vol", "ask_vol", "symbol", "quality_flag"]])

    if dfs:
        full = pd.concat(dfs, axis=0, ignore_index=True).sort_values("ts_utc")
        full.to_csv(out_csv, index=False)
        logger.info(f"Ticks unificados en {out_csv}")
        return out_csv

    logger.warning(
        "No se pudo extraer ningún tick válido del CLI. Intentando fallback directo (datafeed.dukascopy.com)…"
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
        # como último recurso, escribimos CSV vacío con esquema para no romper el pipeline
        empty = pd.DataFrame(
            columns=["ts_utc", "bid", "ask", "bid_vol", "ask_vol", "symbol", "quality_flag"]
        )
        empty.to_csv(out_csv, index=False)
        logger.warning(
            f"Fallback directo tampoco obtuvo datos (0/{total_hours} horas). "
            f"Escribí CSV vacío: {out_csv}"
        )
        return out_csv

    full = pd.concat(frames, axis=0, ignore_index=True).sort_values("ts_utc")
    full["symbol"] = symbol
    full["quality_flag"] = 0
    full.to_csv(out_csv, index=False)
    logger.info(f"Fallback OK: {ok_hours}/{total_hours} horas con datos. CSV: {out_csv}")
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

def ticks_to_bars_1m(ticks: pd.DataFrame) -> pd.DataFrame:
    """
    Construye OHLC 1m para BID/ASK + tick_count a partir de ticks.
    """
    ticks = ticks.sort_values("ts_utc").copy()
    ticks["dt"] = pd.to_datetime(ticks["ts_utc"], unit="s", utc=True)

    bid_ohlc = ticks.set_index("dt")["bid"].resample("1min").ohlc()
    ask_ohlc = ticks.set_index("dt")["ask"].resample("1min").ohlc()
    tick_count = ticks.set_index("dt")["bid"].resample("1min").count().rename("tick_count")

    df = pd.concat([bid_ohlc.add_prefix("bid_"), ask_ohlc.add_prefix("ask_"), tick_count], axis=1).dropna(how="any")
    df = df.reset_index().rename(columns={"dt": "ts_open"})
    df["ts_utc_open"] = (df["ts_open"].astype("int64") // 10**9).astype("int64")
    df = df.drop(columns=["ts_open"])

    df = df[
        ["ts_utc_open", "bid_open", "bid_high", "bid_low", "bid_close",
         "ask_open", "ask_high", "ask_low", "ask_close", "tick_count"]
    ].rename(columns={
        "bid_open": "bid_o", "bid_high": "bid_h", "bid_low": "bid_l", "bid_close": "bid_c",
        "ask_open": "ask_o", "ask_high": "ask_h", "ask_low": "ask_l", "ask_close": "ask_c",
    })
    return df