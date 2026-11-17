"""
Child process for test_cli.py: ``python -m app.curation run`` wired to the fakes (the
production CLI has no fake-injection switch). The parent sets DATABASE_PATH.

Usage: python cli_child.py CATALOG_ID DELAY_S LEASE_TTL_MS HEARTBEAT_S
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from app.core.config import settings  # noqa: E402
from app.curation.__main__ import run_command  # noqa: E402
from app.curation.runner import Runner  # noqa: E402
from tests.unit.curation.llm_fake import API_KEY_SENTINEL  # noqa: E402
from tests.unit.curation.pipeline_fakes import ScriptedLLM, SlowServices, UniverseTMDB, make_universe  # noqa: E402


def main(catalog_id: str, delay: float, ttl_ms: int, heartbeat_s: float) -> int:
    settings.OPENAI_API_KEY = API_KEY_SENTINEL
    settings.OPENAI_BASE_URL = "https://llm.invalid/api/v1"
    films = make_universe(400)
    services = SlowServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films), delay)
    runner = Runner(owner=f"child-{os.getpid()}", services=services, lease_ttl_ms=ttl_ms, heartbeat_s=heartbeat_s)
    return asyncio.run(run_command(catalog_id, apply_first=False, runner=runner))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])))
