import logging
import logging.handlers
import os

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "omegaclaw.log")
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

def setup_logging():
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    level = LOG_LEVEL if LOG_LEVEL in valid_levels else "INFO"
    if level != LOG_LEVEL:
        print(f"[logger] Invalid LOG_LEVEL='{LOG_LEVEL}', defaulting to INFO")

    root_logger = logging.getLogger()

    if any(isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root_logger.handlers):
        return

    formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    root_logger.setLevel(level)

    if not any(type(h) is logging.StreamHandler for h in root_logger.handlers):
        stdout_handler = logging.StreamHandler()
        stdout_handler.setFormatter(formatter)
        root_logger.addHandler(stdout_handler)

    # file handler — falls back to stdout only if the log directory is not writable
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a"):
            pass
        file_handler = logging.handlers.TimedRotatingFileHandler(
            LOG_FILE, when="midnight", backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except PermissionError:
        root_logger.warning(f"Cannot write to log directory {LOG_DIR} — falling back to stdout only.")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

# MeTTa bridge — called via py-call from .metta files; module identifies the source file
def log_debug(msg: str, module: str = "metta") -> None:
    logging.getLogger(module).debug(msg)

def log_info(msg: str, module: str = "metta") -> None:
    logging.getLogger(module).info(msg)

def log_warning(msg: str, module: str = "metta") -> None:
    logging.getLogger(module).warning(msg)

def log_error(msg: str, module: str = "metta") -> None:
    logging.getLogger(module).error(msg)
