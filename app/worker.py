"""
Worker service entry point: ``python -m app.worker``.

PLACEHOLDER (milestone 1). The scheduler, lease and refresh pipeline arrive in milestone 6
(see PLAN.md, sections 4 and 11). Until then this process only idles, so that the Compose
layout (separate ``web`` and ``worker`` services, one image) and signal handling can be
exercised. It opens no port and does no work.
"""

import asyncio
import signal

from app.core.logging import logger


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    logger.info("Worker started (placeholder: no scheduler yet)")
    await stop.wait()
    logger.info("Worker stopping")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
