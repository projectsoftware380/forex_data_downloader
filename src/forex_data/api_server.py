
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .cli import (
    build_bars,
    download_ticks,
    run_all,
    setup_logging,
    validate,
    _configure_cli_file_logger,
)

LOG_DIR = Path("data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
API_LOG_FILE = LOG_DIR / "api_server.log"


def _configure_api_logger() -> None:
    logger.remove()
    logger.add(
        API_LOG_FILE,
        level="INFO",
        enqueue=True,
        rotation="20 MB",
        compression="zip",
        backtrace=False,
        diagnose=False,
    )


_configure_api_logger()
setup_logging()
_configure_cli_file_logger()


app = FastAPI(title="Forex Data Downloader API", version="1.0.0")


class DownloadRequest(BaseModel):
    symbol: str = Field(..., description="Símbolo, p.ej. EURUSD")
    start: str = Field(..., description="Fecha inicio YYYY-MM-DD")
    end: str = Field(..., description="Fecha fin YYYY-MM-DD")


class BuildRequest(BaseModel):
    symbol: str
    start: str
    end: str


class ValidateRequest(BaseModel):
    symbol: str
    start: str
    end: str


class RunAllRequest(BaseModel):
    symbols: str
    start: str
    end: str
    max_workers: int = Field(4, ge=1, le=32)
    clean_intermediate: bool = True
    mode: str = Field("ticks", description="Tipo de descarga: ticks u ohlc")
    timeframe: str = Field("1m", description="Timeframe (solo aplica a modo ohlc)")


class StatusEntry(BaseModel):
    task: str
    symbols: List[str]
    started_at: datetime
    duration: float
    status: str
    message: Optional[str] = None


LATEST_PROCESSES: List[StatusEntry] = []
MAX_STATUS_ENTRIES = 50


def _add_status(entry: StatusEntry) -> None:
    LATEST_PROCESSES.append(entry)
    if len(LATEST_PROCESSES) > MAX_STATUS_ENTRIES:
        del LATEST_PROCESSES[0 : len(LATEST_PROCESSES) - MAX_STATUS_ENTRIES]


async def _handle_request(func, data: dict, path: str) -> Dict[str, Any]:
    """Centraliza la ejecución de comandos CLI desde el API."""
    start_time = time.perf_counter()
    started_at = datetime.utcnow()
    if "symbols" in data and isinstance(data["symbols"], str):
        symbols = [sym.strip() for sym in data["symbols"].split(",") if sym.strip()]
    else:
        symbol = data.get("symbol")
        symbols = [symbol] if symbol else ["N/A"]

    logger.info(f"Solicitud recibida en {path}: {data}")
    _configure_cli_file_logger()
    try:
        result = func(**data)
        duration = time.perf_counter() - start_time
        logger.info(f"{func.__name__} ejecutado en {duration:.2f}s con args={data}")
        entry = StatusEntry(
            task=func.__name__,
            symbols=symbols,
            started_at=started_at,
            duration=duration,
            status="ok",
        )
        _add_status(entry)
        response: Dict[str, Any] = {
            "status": "ok",
            "function": func.__name__,
            "duration": round(duration, 2),
            "args": data,
        }
        if result is not None:
            response["result"] = str(result)
        return response
    except Exception as exc:  # noqa: BLE001
        duration = time.perf_counter() - start_time
        _configure_cli_file_logger()
        logger.exception(f"Error en {func.__name__}: {exc}")
        entry = StatusEntry(
            task=func.__name__,
            symbols=symbols,
            started_at=started_at,
            duration=duration,
            status="error",
            message=str(exc),
        )
        _add_status(entry)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/download")
async def download_endpoint(request: Request) -> Dict[str, Any]:
    payload = DownloadRequest(**(await request.json()))
    data = payload.model_dump()
    return await _handle_request(download_ticks, data, request.url.path)


@app.post("/build")
async def build_endpoint(request: Request) -> Dict[str, Any]:
    payload = BuildRequest(**(await request.json()))
    data = payload.model_dump()
    return await _handle_request(build_bars, data, request.url.path)


@app.post("/validate")
async def validate_endpoint(request: Request) -> Dict[str, Any]:
    payload = ValidateRequest(**(await request.json()))
    data = payload.model_dump()
    return await _handle_request(validate, data, request.url.path)


@app.post("/run_all")
async def run_all_endpoint(request: Request) -> Dict[str, Any]:
    payload = RunAllRequest(**(await request.json()))
    data = payload.model_dump()
    return await _handle_request(run_all, data, request.url.path)


@app.get("/status")
async def status_endpoint() -> Dict[str, Any]:
    return {
        "status": "ok",
        "count": len(LATEST_PROCESSES),
        "processes": [entry.dict() for entry in reversed(LATEST_PROCESSES)],
    }


@app.get("/ui", response_class=HTMLResponse)
async def home_ui() -> str:
    """Muestra la interfaz HTML principal."""
    path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled server error en {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": str(exc),
            "path": request.url.path,
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "HTTP Error",
            "status": exc.status_code,
            "detail": exc.detail,
            "path": request.url.path,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation Error",
            "detail": exc.errors(),
            "path": request.url.path,
        },
    )


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "Forex Data Downloader API",
        "version": "1.0.0",
        "documentation": "/docs",
        "status_endpoint": "/status",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.forex_data.api_server:app", host="0.0.0.0", port=8500, reload=True)
