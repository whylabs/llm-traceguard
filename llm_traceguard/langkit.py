import os
import logging

logger = logging.getLogger(__name__)
_has_langkit = False
_endpoint = None


def init():
    _endpoint = os.environ.get("LANGKIT_ENDPOINT")
    if _endpoint is None:
        logger.info("LangKit endpoint is not set")
        return
    logger.info(f"Using LangKit endpoint: {_endpoint}")


def fetch_metrics():
    pass


async def fetch_metrics_async():
    pass
