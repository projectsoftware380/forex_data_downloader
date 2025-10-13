from typing import Literal
from dataclasses import dataclass

SessionFlag = Literal[0,1,2,3]  # 0=Asia,1=Londres,2=NY,3=Overnight

@dataclass(frozen=True)
class TickSchema:
    ts_utc: int
    bid: float
    ask: float
    bid_vol: float | None
    ask_vol: float | None
    symbol: str
    quality_flag: int