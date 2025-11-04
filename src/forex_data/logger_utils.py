import sys

from loguru import logger

def setup_logging(level: str = "INFO"):
    logger.remove()
    logger.add(sys.stdout, level=level, enqueue=True,
               format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>")
    return logger
