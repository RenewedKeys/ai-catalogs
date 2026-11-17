#!/usr/bin/env python3
"""
Probe a model for curated catalogs (PLAN.md section 7).

Checks that the model accepts the configured temperature with strict JSON-schema output,
with and without OpenRouter's ``:online`` suffix, and reports the working output mode,
valid/dropped rows and token use. Every call goes through the budget gate (ledger kind
``probe``), so it counts towards ``MAX_MODEL_CALLS_PER_DAY`` like any other attempt.

    OPENAI_API_KEY=... python scripts/probe_model.py --model some/model [--temperature 0.7]

The key comes only from the environment / ``.env``. Output never includes provider text.
Exit status: 0 if at least one variant worked in strict ``json_schema`` mode, 1 if variants
only worked after a downgrade or failed, 2 for a configuration problem.

Recommendation printed at the end: prefer the plain model unless ``:online`` is at least as
good, because the date is injected into prompts and grounding is not needed for it.
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings  # noqa: E402
from app.curation.budget import Budget, BudgetExceeded, BudgetLimits  # noqa: E402
from app.curation.llm import CuratedLLMService, LLMError, LLMSettings  # noqa: E402

# Neutral probe request: tests format compliance, not any catalog's taste.
PROBE_PROMPT = (
    "List {n} well-known feature films from any era and country. "
    "Use the category 'probe' for each. Return them as the JSON object described."
)
ONLINE_SUFFIX = ":online"


def variants(model: str, include_online: bool) -> list[str]:
    plain = model[: -len(ONLINE_SUFFIX)] if model.endswith(ONLINE_SUFFIX) else model
    return [plain, plain + ONLINE_SUFFIX] if include_online else [plain]


async def probe_one(model: str, args, http_client=None) -> dict:
    llm_settings = LLMSettings(
        model=model, temperature=args.temperature, max_tokens=args.max_tokens, timeout_s=args.timeout
    )
    # One generation may cost up to 1 + 2 downgrades, each with up to 2 retries.
    budget = Budget(BudgetLimits(max_model_calls_per_run=9), catalog_id=None, run_id=None)
    llm = CuratedLLMService(llm_settings, budget, http_client=http_client, use_mode_cache=False)
    report = {"model": model, "temperature": args.temperature}
    try:
        result = await llm.generate(PROBE_PROMPT.format(n=args.count), args.count, kind="probe")
        report.update(
            ok=True,
            mode=result.mode,
            rows=len(result.movies),
            dropped=result.dropped_rows,
            attempts=result.attempts,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            reasoning_tokens=result.reasoning_tokens,
            finish_reason=result.finish_reason,
        )
    except LLMError as e:
        report.update(ok=False, error=e.category, status=e.status_code, hint=e.hint)
    except BudgetExceeded as e:
        report.update(ok=False, error="budget", limit=e.limit)
    finally:
        await llm.aclose()
    return report


def recommend(reports: list[dict]) -> str | None:
    """Plain model unless :online is strictly better (strict mode and more valid rows)."""
    good = [r for r in reports if r.get("ok") and r.get("mode") == "json_schema"]
    if not good:
        return None
    plain = [r for r in good if not r["model"].endswith(ONLINE_SUFFIX)]
    online = [r for r in good if r["model"].endswith(ONLINE_SUFFIX)]
    if plain and online and online[0]["rows"] > plain[0]["rows"]:
        return online[0]["model"]
    return (plain or online)[0]["model"]


async def run(argv: list[str] | None = None, http_client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--model", default=settings.DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--count", type=int, default=10, help="films to request per probe (default 10)")
    parser.add_argument("--no-online", action="store_true", help="skip the :online variant")
    args = parser.parse_args(argv)

    if not settings.OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set (environment or .env)", file=sys.stderr)
        return 2
    try:
        LLMSettings(model=args.model, temperature=args.temperature, max_tokens=args.max_tokens, timeout_s=args.timeout)
    except ValueError as e:
        print(f"invalid settings: {e.errors()[0]['loc'][0]} {e.errors()[0]['msg']}", file=sys.stderr)
        return 2

    reports = []
    for model in variants(args.model, not args.no_online):
        report = await probe_one(model, args, http_client)
        reports.append(report)
        print(json.dumps(report, sort_keys=True))
    best = recommend(reports)
    print(json.dumps({"recommended_model": best}))
    return 0 if best else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
