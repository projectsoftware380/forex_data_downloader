from __future__ import annotations
import pandas as pd
import numpy as np

def validate_bid_ask(df: pd.DataFrame) -> pd.DataFrame:
    bad = df[df["ask"] < df["bid"]]
    return bad

def detect_spikes(df: pd.DataFrame, col: str = "mid", window: int = 60, k: float = 5.0) -> pd.DataFrame:
    x = df[col].astype(float)
    median = x.rolling(window, min_periods=window//2).median()
    mad = (x - median).abs().rolling(window, min_periods=window//2).median()
    z = (x - median) / (1.4826 * mad.replace(0, np.nan))
    return df[np.abs(z) > k]

def fill_session_flag(df_1m: pd.DataFrame) -> pd.DataFrame:
    # Sesiones simplificadas por hora UTC
    hour = pd.to_datetime(df_1m["ts_utc_open"], unit="s").dt.hour
    # 0 Asia(0-7), 1 Londres(7-12), 2 NY(12-20), 3 Overnight(20-24)
    session = pd.cut(hour, bins=[-1,6,11,19,24], labels=[0,1,2,3]).astype(int)
    df_1m["session_flag"] = session
    return df_1m