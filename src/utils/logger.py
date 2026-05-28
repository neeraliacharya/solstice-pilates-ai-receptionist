import logging
import sys

_configured = False


def setup_logging() -> None:
    """
    Configure the root logger once.
    All module loggers propagate to root by default, so this single handler
    captures every log line across the entire app — including from threads.
    Called at import time AND from the FastAPI lifespan so it survives any
    logging resets uvicorn performs at startup.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers that may have been added by uvicorn
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)

    # ── Silence noisy third-party libraries ──────────────────────────────────
    # These flood the terminal with low-value HTTP / auth chatter.
    for noisy in (
        "httpx", "httpcore", "httpcore.http11",
        "googleapiclient.discovery", "googleapiclient.discovery_cache",
        "google.auth.transport", "google.auth.transport.requests",
        "urllib3", "urllib3.connectionpool",
        "asyncio",
        # Groq SDK internal HTTP client — prints full request options and response
        # headers on every API call, which drowns real log lines.
        "groq._base_client",
        "groq",
        "openai._base_client",
        "openai",
        "httpx",
        "google_auth_httplib2",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Keep uvicorn's own loggers visible but let them use the root handler
    for uvicorn_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvl = logging.getLogger(uvicorn_name)
        uvl.handlers.clear()      # don't double-print via uvicorn's own handler
        uvl.propagate = True      # let records reach our root handler
        uvl.setLevel(logging.INFO)

    _configured = True


# Run at import time so loggers created during module load already work.
setup_logging()


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  All output flows through the root handler."""
    setup_logging()               # idempotent — ensures root is configured
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True       # explicit: records flow up to root handler
    return logger
