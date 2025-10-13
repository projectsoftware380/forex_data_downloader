from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    data_root: Path = Path("./data")
    parquet_engine: str = "pyarrow"    # or fastparquet
    compression: str = "snappy"        # snappy or zstd
    timezone: str = "UTC"

    class Config:
        env_prefix = "FXD_"
        env_file = ".env"

settings = Settings()