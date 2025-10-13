from __future__ import annotations
from pathlib import Path
import pandas as pd
from .config import settings

def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def write_parquet(df: pd.DataFrame, path: Path, partition_cols: list[str] | None = None):
    _ensure_dir(path if path.suffix else path)
    kwargs = dict(engine=settings.parquet_engine, compression=settings.compression)
    if partition_cols:
        df.to_parquet(path, partition_cols=partition_cols, index=False, **kwargs)
    else:
        df.to_parquet(path, index=False, **kwargs)

def read_parquet(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    return pd.read_parquet(path, columns=columns, engine=settings.parquet_engine)