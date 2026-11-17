"""
The refresh pipeline (PLAN.md section 5): one catalog definition in, a validated list out.

``Pipeline.run()`` returns a ``PipelineResult`` (the list to publish, plus run statistics) or
raises ``PipelineFailure`` (a controlled error category; the run publishes nothing). It never
writes snapshots, history or jobs itself: the runner (``app.curation.runner``) commits the
result through the fenced commit, so a dry run and a refresh run the same code.

Stages, over one candidate pool kept for the whole run:

1-2.  Render the prompt and generate ``target_count * overgenerate_factor`` suggestions in
      batches of at most 60, each batch told the titles produced so far.
3.    Normalise and dedupe on (normalised title, year).
4.    Static pre-filter (blacklisted titles, franchise title patterns) and history
      pre-filter by title/year. History matches are *held* with the activation they
      matched, not discarded: relax mode re-admits them.
5.    Resolve on TMDB (validated matching, ``TMDBGate.resolve``).
6.    Dedupe on resolved ids (one entry per ``tmdb_id`` and per ``imdb_id``).
7.    Gatekeeper on the details (``tmdb_gate.evaluate``), every rejection counted by reason.
8.    History filter on ``tmdb_id`` / ``imdb_id`` (authoritative); matches are held too.
      With ``exclude_watched``, the viewer's watched films (``app.curation.watched``, loaded
      once at setup) are rejected as ``watched`` right after resolution (before the details
      lookup) and again on the IMDb id from the details. Never held: relax cannot re-admit them.
9.    Selection (``select``): deterministic given the seed; caps and quotas.
10.   Top-up loop while short (computed *after* selection) or a hard quota is unmet, and
      budget remains: one batch per round naming the unmet quotas; then 3-9 again over the
      whole pool. It also stops after ``STALL_ROUNDS`` rounds that add nothing eligible.
11.   Publish decision: ``history_mode`` strict/relax, ``on_shortfall`` fail/publish_partial.
      Relax shrinks the effective window one run at a time (oldest activation first; a
      title counts by its most recent activation) and re-runs 4-9 so re-admitted items are
      resolved and gated like any other.
12.   ``assert_publishable``: checked in code right before the result is handed to the commit.

Classification (stages 4-8) is recomputed from cached lookups on every pass, so it is
idempotent and the stage counts always describe the final pool.
"""

import asyncio
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Mapping, Optional

from app.curation.budget import BudgetExceeded
from app.curation.exclusions import ExclusionInputs, ResolvedExclusions, exclusion_inputs, resolve_exclusions
from app.curation.limits import Limits, gate_rules_from_definition, limits_from_definition
from app.curation.llm import LLMError
from app.curation.prompt import PromptError, render_prompt, seeded_random
from app.curation.tmdb_gate import (
    Candidate,
    TMDBError,
    TMDBGate,
    evaluate,
    normalize_title,
    split_title_year,
    to_candidate,
)
from app.curation.watched import WatchedError, WatchedSet, load_watched
from app.store import db
from app.store.catalogs import SnapshotItem, window_activation_ids

logger = logging.getLogger(__name__)

MAX_BATCH = 60
TOPUP_FACTOR = 3
STALL_ROUNDS = 2
POSTER_BASE = "https://image.tmdb.org/t/p/w500"
TRANSIENT_TMDB = frozenset({"provider_rate_limit", "provider_http_5xx", "timeout", "transport"})


# --------------------------------------------------------------------------- results


class PipelineFailure(Exception):
    """The run cannot publish. ``category`` is a controlled value; never provider text."""

    def __init__(self, category: str, *, status: Optional[int] = None, transient: bool = False, detail: str = ""):
        self.category = category
        self.status = status
        self.transient = transient
        self.detail = detail
        self.stats: dict[str, Any] = {}
        super().__init__(f"{category}: {detail}" if detail else category)


@dataclass
class PipelineResult:
    items: list[SnapshotItem]
    stats: dict[str, Any]
    partial: bool = False


# --------------------------------------------------------------------------- history


@dataclass
class HistoryWindow:
    """
    The history window, by recency index: 0 is the newest non-revoked activation. An entry's
    index is that of its *most recent* activation, so shrinking the window (dropping the
    oldest activation) frees exactly the titles last shown in it.
    """

    runs: int
    ids: dict[Any, int] = field(default_factory=dict)  # tmdb_id (int) / imdb_id (str) -> index
    titles: dict[str, list[tuple[Optional[int], int]]] = field(default_factory=dict)  # norm -> [(year, index)]
    prompt_titles: list[tuple[str, Optional[int], int]] = field(default_factory=list)  # newest first

    def id_index(self, tmdb_id: Optional[int], imdb_id: Optional[str]) -> Optional[int]:
        found = [self.ids[k] for k in (tmdb_id, imdb_id) if k is not None and k in self.ids]
        return min(found) if found else None

    def title_index(self, norm: str, year: Optional[int]) -> Optional[int]:
        found = [i for y, i in self.titles.get(norm, ()) if year is None or y is None or abs(y - year) <= 1]
        return min(found) if found else None

    def prompt_history(self, size: int) -> list[tuple[str, Optional[int]]]:
        return [(t, y) for t, y, i in self.prompt_titles if i < size]


def load_history(conn, catalog_id: str, runs: int) -> HistoryWindow:
    window = HistoryWindow(runs=runs)
    if runs <= 0:
        return window
    activation_ids = window_activation_ids(conn, catalog_id, runs)
    if not activation_ids:
        window.runs = 0
        return window
    window.runs = len(activation_ids)
    rank = {a: i for i, a in enumerate(activation_ids)}
    placeholders = ",".join("?" * len(activation_ids))
    rows = conn.execute(
        f"SELECT activation_id, tmdb_id, imdb_id, title, year FROM history WHERE activation_id IN ({placeholders})",
        activation_ids,
    ).fetchall()
    seen_titles: set[tuple[str, Optional[int]]] = set()
    for row in sorted(rows, key=lambda r: rank[r["activation_id"]]):
        index = rank[row["activation_id"]]
        for key in (row["tmdb_id"], row["imdb_id"]):
            if key is not None and key not in window.ids:
                window.ids[key] = index
        norm = normalize_title(row["title"] or "")
        if norm:
            entries = window.titles.setdefault(norm, [])
            if all(y != row["year"] for y, _ in entries):
                entries.append((row["year"], index))
            if (norm, row["year"]) not in seen_titles:
                seen_titles.add((norm, row["year"]))
                window.prompt_titles.append((row["title"], row["year"], index))
    return window


# --------------------------------------------------------------------------- selection


@dataclass(frozen=True)
class PoolItem:
    tmdb_id: int
    imdb_id: str
    year: int
    collection_id: Optional[int]
    director_ids: Optional[tuple[int, ...]]
    category: str = ""

    @property
    def decade(self) -> int:
        return self.year // 10 * 10


@dataclass
class Selection:
    selected: list[PoolItem]
    unmet_hard: dict[int, int]  # decade -> missing count
    unmet_soft: dict[str, int]  # category -> missing count


def _category_key(text: str) -> str:
    return " ".join((text or "").casefold().split())


def select(
    pool: list[PoolItem],
    target: int,
    limits: Limits,
    seed: str,
    rank: Optional[Callable[[PoolItem], int]] = None,
) -> Selection:
    """
    Stage 9. Deterministic for a given pool *set* and seed: the pool is put in ``tmdb_id``
    order before the seeded shuffle, so arrival order does not matter. Greedy by quota need:
    hard decade minimums first, then the soft category minimums, then fill; every pick
    respects ``max_per_collection`` and ``director_cap`` (films with no collection are
    independent). Re-run from scratch each round, so items chosen in an earlier round can
    be swapped out to satisfy a hard quota.

    ``rank`` (lower first, stable over the shuffle) is used after a relaxed history window:
    unseen films first, then repeats from the oldest activation, so relaxing repeats as
    little and as long ago as possible.
    """
    order = sorted(pool, key=lambda p: p.tmdb_id)
    seeded_random(seed, "select").shuffle(order)
    if rank is not None:
        order.sort(key=rank)
    chosen: list[PoolItem] = []
    taken: set[int] = set()
    per_collection: Counter = Counter()
    per_director: Counter = Counter()

    def fits(item: PoolItem) -> bool:
        if item.tmdb_id in taken:
            return False
        if limits.max_per_collection is not None and item.collection_id is not None:
            if per_collection[item.collection_id] >= limits.max_per_collection:
                return False
        if limits.director_cap is not None and item.director_ids:
            if any(per_director[d] >= limits.director_cap for d in item.director_ids):
                return False
        return True

    def take(item: PoolItem) -> None:
        chosen.append(item)
        taken.add(item.tmdb_id)
        if item.collection_id is not None:
            per_collection[item.collection_id] += 1
        for d in item.director_ids or ():
            per_director[d] += 1

    def fill(matches, need: int) -> None:
        have = sum(1 for c in chosen if matches(c))
        for item in order:
            if have >= need or len(chosen) >= target:
                return
            if matches(item) and fits(item):
                take(item)
                have += 1

    for decade, need in sorted(limits.decade_min.items()):
        fill(lambda p, d=decade: p.decade == d, need)
    categories = {_category_key(c): n for c, n in limits.category_min.items()}
    for category, need in sorted(categories.items()):
        fill(lambda p, c=category: _category_key(p.category) == c, need)
    fill(lambda p: True, target)

    unmet_hard = {}
    for decade, need in limits.decade_min.items():
        have = sum(1 for c in chosen if c.decade == decade)
        if have < need:
            unmet_hard[decade] = need - have
    unmet_soft = {}
    for category, need in limits.category_min.items():
        have = sum(1 for c in chosen if _category_key(c.category) == _category_key(category))
        if have < need:
            unmet_soft[category] = need - have
    seeded_random(seed, "final").shuffle(chosen)
    return Selection(chosen, unmet_hard, unmet_soft)


def scaled_quotas(limits: Limits, count: int, target: int) -> dict[int, int]:
    """Hard quotas for a partial list of ``count`` items: proportional, rounded down."""
    return {d: math.floor(n * count / target) for d, n in limits.decade_min.items()}


def unmet_lines(selection: Selection, shortfall: int) -> list[str]:
    lines = [f"At least {n} more film(s) released in the {d}s." for d, n in sorted(selection.unmet_hard.items())]
    lines += [f'At least {n} more film(s) with category "{c}".' for c, n in sorted(selection.unmet_soft.items())]
    if shortfall > 0 and not lines:
        lines.append(f"{shortfall} more qualifying film(s) not suggested before.")
    return lines


# --------------------------------------------------------------------------- pipeline


@dataclass
class Suggestion:
    title: str
    year: Optional[int]
    category: str
    norm: str


@dataclass
class Classified:
    """Stage 4-8 outcome of one pass."""

    eligible: list[PoolItem]
    held_title: int
    held_id: int
    counts: dict[str, int]
    rejects: Counter


@dataclass
class RunSettings:
    target_count: int
    request_total: int
    history_window_runs: int
    history_mode: str
    on_shortfall: str
    min_accept: Optional[int]

    @classmethod
    def from_definition(cls, d: Mapping[str, Any]) -> "RunSettings":
        target = int(d.get("target_count") or 100)
        factor = float(d.get("overgenerate_factor") or 1.6)
        return cls(
            target_count=target,
            request_total=max(target, math.ceil(target * factor)),
            history_window_runs=int(d.get("history_window_runs", 7) if d.get("history_window_runs") is not None else 7),
            history_mode=d.get("history_mode") or "strict",
            on_shortfall=d.get("on_shortfall") or "fail",
            min_accept=d.get("min_accept"),
        )


def batch_sizes(total: int, max_batch: int = MAX_BATCH) -> list[int]:
    """160 -> [60, 60, 40]."""
    out = []
    while total > 0:
        out.append(min(max_batch, total))
        total -= out[-1]
    return out


class Pipeline:
    """
    One run. ``llm`` is a ``CuratedLLMService`` (its ``Budget`` is the per-attempt gate);
    ``gate`` a ``TMDBGate`` whose rules are set from the definition here.
    """

    def __init__(
        self,
        *,
        catalog_id: str,
        definition: Mapping[str, Any],
        exclusions_json: Optional[str],
        seed: str,
        today: date,
        llm: Any,
        gate: TMDBGate,
        db_path: Optional[str] = None,
        watched_client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.catalog_id = catalog_id
        self.definition = dict(definition)
        self.exclusions_json = exclusions_json
        self.seed = seed
        self.today = today
        self.llm = llm
        self.gate = gate
        self.db_path = db_path
        self.watched_client_factory = watched_client_factory
        self.watched: Optional[WatchedSet] = None  # set at setup when exclude_watched
        self.settings = RunSettings.from_definition(self.definition)
        self.limits = limits_from_definition(self.definition)

        self.suggestions: dict[tuple[str, Optional[int]], Suggestion] = {}
        self._resolved: dict[tuple[str, Optional[int]], Optional[int]] = {}
        self._details: dict[int, Optional[dict]] = {}
        self._title_index: dict[tuple[str, Optional[int]], Optional[int]] = {}
        self.generated_rows = 0
        self.dropped_rows = 0
        self.calls = 0
        self.topup_rounds = 0
        self.prompt_hash: Optional[str] = None
        self.llm_error: Optional[LLMError] = None
        self.budget_stop: Optional[BudgetExceeded] = None
        self.size = 0  # effective history window
        self.history = HistoryWindow(runs=0)
        self.inputs: ExclusionInputs = ExclusionInputs()
        self.title_patterns: list[re.Pattern] = []

    # ---------------------------------------------------------------- setup

    async def _setup(self) -> None:
        try:
            self.inputs = exclusion_inputs(self.definition)
        except ValueError:
            raise PipelineFailure("config", detail="invalid exclusions") from None
        resolved = ResolvedExclusions.from_json(self.exclusions_json)
        if resolved is None or resolved.inputs_rev != self.inputs.rev:
            # Not resolved at apply time (e.g. a definition stored by other means): resolve
            # in memory now with the same automatic picks; never run with a filter missing.
            resolved = await resolve_exclusions(self.gate.client if self.inputs.needs_tmdb else None, self.inputs)
        self.gate.rules = gate_rules_from_definition(
            self.definition,
            blacklist_tmdb_ids=resolved.blacklist(self.inputs),
            franchise_denylist=resolved.denylist(self.inputs),
        )
        self.title_patterns = [re.compile(p, re.IGNORECASE) for p in self.inputs.title_patterns]
        self.blacklist_norms = [(normalize_title(q.title), q.year) for q in self.inputs.titles]

        def _load():
            with db.session(self.db_path) as conn:
                return load_history(conn, self.catalog_id, self.settings.history_window_runs)

        self.history = await asyncio.to_thread(_load)
        self.size = self.history.runs
        if self.definition.get("exclude_watched") is True:
            await self._load_watched()

    async def _load_watched(self) -> None:
        """Before any model call, so a missing key or an MDBList outage spends no budget."""
        if self.watched_client_factory is None:
            raise PipelineFailure("config", detail="exclude_watched needs a watched-history source")
        try:
            self.watched = await load_watched(self.watched_client_factory, db_path=self.db_path)
        except WatchedError as e:
            if e.category == "config":
                logger.error("[%s] exclude_watched is set but MDBLIST_API_KEY is not", self.catalog_id)
                raise PipelineFailure("config", detail="exclude_watched is set but MDBLIST_API_KEY is not") from None
            logger.warning("[%s] watched history unavailable: %s", self.catalog_id, e.category)
            raise PipelineFailure(e.category, status=e.status_code, transient=e.transient) from None

    def _is_watched(self, tmdb_id: Optional[int] = None, imdb_id: Optional[str] = None) -> bool:
        return self.watched is not None and self.watched.contains(tmdb_id, imdb_id)

    # ---------------------------------------------------------------- stages 1-3

    def _already(self) -> list[tuple[str, Optional[int]]]:
        return [(s.title, s.year) for s in self.suggestions.values()]

    async def _generate(self, request_count: int, kind: str, unmet: list[str] = ()) -> bool:
        """One generation call. False when generation must stop (budget or provider error)."""
        try:
            rendered = render_prompt(
                self.definition,
                today=self.today,
                seed=self.seed,
                history=self.history.prompt_history(self.size),
                already_this_run=self._already(),
                unmet=list(unmet),
                target_count=self.settings.target_count,
                request_count=request_count,
                watched=self.watched.titles if self.watched is not None else (),
            )
        except PromptError:
            raise PipelineFailure("config", detail="prompt failed to render") from None
        if self.prompt_hash is None:
            self.prompt_hash = rendered.prompt_hash
        try:
            result = await self.llm.generate(rendered.text, request_count, kind=kind)
        except BudgetExceeded as e:
            self.budget_stop = e
            return False
        except LLMError as e:
            self.llm_error = e
            logger.warning("[%s] generation stopped: %s", self.catalog_id, e.category)
            return False
        self.calls += 1
        self.generated_rows += len(result.movies)
        self.dropped_rows += result.dropped_rows
        for movie in result.movies:
            title, year = split_title_year(movie.title, movie.year)
            norm = normalize_title(title)
            if not norm:
                continue
            key = (norm, year)
            if key not in self.suggestions:
                self.suggestions[key] = Suggestion(title, year, movie.category, norm)
        return True

    # ---------------------------------------------------------------- stages 4-8

    def _static_reason(self, s: Suggestion) -> Optional[str]:
        for norm, year in self.blacklist_norms:
            if s.norm == norm and (year is None or s.year is None or abs(s.year - year) <= 1):
                return "blacklisted_title"
        if any(rx.search(s.title) for rx in self.title_patterns):
            return "franchise_title"
        return None

    def _title_held(self, key, s: Suggestion) -> Optional[int]:
        if key not in self._title_index:
            self._title_index[key] = self.history.title_index(s.norm, s.year)
        return self._title_index[key]

    async def _lookups(self) -> None:
        """Fill the resolution and details caches for everything the next pass will inspect."""
        to_resolve = []
        for key, s in self.suggestions.items():
            if key in self._resolved or self._static_reason(s):
                continue
            index = self._title_held(key, s)
            if index is not None and index < self.size:
                continue
            to_resolve.append(key)
        results = await asyncio.gather(*(self.gate.resolve(*k_title) for k_title in self._titles(to_resolve)))
        for key, tmdb_id in zip(to_resolve, results):
            self._resolved[key] = tmdb_id
        ids = sorted(
            {t for t in self._resolved.values() if t is not None and t not in self._details and not self._is_watched(t)}
        )
        details = await asyncio.gather(*(self.gate.details(t) for t in ids))
        self._details.update(zip(ids, details))

    def _titles(self, keys) -> list[tuple[str, Optional[int]]]:
        return [(self.suggestions[k].title, self.suggestions[k].year) for k in keys]

    async def _classify(self) -> Classified:
        await self._lookups()
        counts: Counter = Counter()
        rejects: Counter = Counter()
        eligible: list[PoolItem] = []
        seen_tmdb: set[int] = set()
        seen_imdb: set[str] = set()
        held_title = held_id = 0
        for key, s in self.suggestions.items():
            counts["unique"] += 1
            reason = self._static_reason(s)
            if reason:
                rejects[reason] += 1
                continue
            index = self._title_held(key, s)
            if index is not None and index < self.size:
                held_title += 1
                continue
            tmdb_id = self._resolved.get(key)
            if tmdb_id is None:
                rejects["no_match"] += 1
                continue
            if tmdb_id in seen_tmdb:
                counts["duplicate_id"] += 1
                continue
            seen_tmdb.add(tmdb_id)
            if self._is_watched(tmdb_id):
                rejects["watched"] += 1
                continue
            details = self._details.get(tmdb_id)
            if details is None:
                rejects["not_found"] += 1
                continue
            reasons = evaluate(details, self.gate.rules, self.today)
            if reasons:
                rejects[reasons[0].value] += 1
                continue
            candidate = to_candidate(details)
            if candidate.imdb_id in seen_imdb:
                counts["duplicate_id"] += 1
                continue
            seen_imdb.add(candidate.imdb_id)
            if self._is_watched(imdb_id=candidate.imdb_id):
                rejects["watched"] += 1
                continue
            index = self.history.id_index(candidate.tmdb_id, candidate.imdb_id)
            if index is not None and index < self.size:
                held_id += 1
                continue
            eligible.append(_pool_item(candidate, s.category))
        counts["held_for_history"] = held_title + held_id
        counts["eligible"] = len(eligible)
        return Classified(eligible, held_title, held_id, dict(counts), rejects)

    # ---------------------------------------------------------------- run

    def _repeat_rank(self, item: PoolItem) -> int:
        index = self.history.id_index(item.tmdb_id, item.imdb_id)
        return 0 if index is None else 1 + self.history.runs - index

    def _shortfall(self, selection: Selection) -> int:
        return self.settings.target_count - len(selection.selected)

    def _satisfied(self, selection: Selection) -> bool:
        return self._shortfall(selection) <= 0 and not selection.unmet_hard

    async def _round(self) -> tuple[Classified, Selection]:
        classified = await self._classify()
        rank = self._repeat_rank if self.size < self.history.runs else None
        selection = select(classified.eligible, self.settings.target_count, self.limits, self.seed, rank)
        return classified, selection

    async def run(self) -> PipelineResult:
        try:
            return await self._run()
        except TMDBError as e:
            failure = PipelineFailure(e.category, status=e.status_code, transient=e.category in TRANSIENT_TMDB)
            failure.stats = self.stats()
            raise failure from None
        except PipelineFailure as e:
            if not e.stats:
                e.stats = self.stats()
            raise

    async def _run(self) -> PipelineResult:
        await self._setup()
        for n in batch_sizes(self.settings.request_total):
            if not await self._generate(n, "generate"):
                break
        classified, selection = await self._round()

        stalled = 0
        while not self._satisfied(selection) and self.llm_error is None and self.budget_stop is None:
            gap = max(self._shortfall(selection), sum(selection.unmet_hard.values()))
            request = min(MAX_BATCH, max(gap, 1) * TOPUP_FACTOR)
            before = len(classified.eligible)
            self.topup_rounds += 1
            if not await self._generate(request, "topup", unmet_lines(selection, self._shortfall(selection))):
                break
            classified, selection = await self._round()
            stalled = stalled + 1 if len(classified.eligible) <= before else 0
            if stalled >= STALL_ROUNDS:
                break

        self.relaxed_window: Optional[int] = None
        if self.settings.history_mode == "relax" and not self._satisfied(selection) and self.size > 0:
            while self.size > 0 and not self._satisfied(selection):
                self.size -= 1
                classified, selection = await self._round()
            self.relaxed_window = self.size

        self.classified, self.selection = classified, selection
        count = len(selection.selected)
        if self._satisfied(selection):
            required, quotas, partial = self.settings.target_count, dict(self.limits.decade_min), False
        elif (
            self.settings.on_shortfall == "publish_partial"
            and self.settings.min_accept is not None
            and count >= self.settings.min_accept
        ):
            required, partial = count, True
            quotas = scaled_quotas(self.limits, count, self.settings.target_count)
            if any(sum(1 for p in selection.selected if p.decade == d) < n for d, n in quotas.items()):
                raise self._failure()
        else:
            raise self._failure()

        items = self.assert_publishable(selection.selected, required, quotas)
        return PipelineResult(items=items, stats=self.stats(), partial=partial)

    def _failure(self) -> PipelineFailure:
        if self.llm_error is not None:
            e = self.llm_error
            return PipelineFailure(e.category, status=e.status_code, transient=e.transient)
        if self.budget_stop is not None and not self.suggestions:
            return PipelineFailure("budget", detail=self.budget_stop.limit)
        selection = self.selection
        detail = f"{len(selection.selected)}/{self.settings.target_count} selected"
        if selection.unmet_hard:
            detail += "; unmet decade quotas " + ", ".join(f"{d}s:{n}" for d, n in sorted(selection.unmet_hard.items()))
        return PipelineFailure("shortfall", detail=detail)

    # ---------------------------------------------------------------- stage 12

    def assert_publishable(self, selected: list[PoolItem], required: int, quotas: dict[int, int]) -> list[SnapshotItem]:
        """
        The final assertion, re-checked from the TMDB details right before commit: exactly
        ``required`` items (never more than ``target_count``); unique ids; every item passes
        the gatekeeper and the static filters; caps hold; hard quotas met; nothing from the
        effective history window. Any violation is a bug: the run fails (``assertion``).
        """

        def fail(why: str) -> PipelineFailure:
            logger.error("[%s] final assertion failed: %s", self.catalog_id, why)
            return PipelineFailure("assertion", detail=why)

        if len(selected) != required or required > self.settings.target_count or required < 1:
            raise fail(f"{len(selected)} items, expected {required}")
        if len({p.tmdb_id for p in selected}) != len(selected) or len({p.imdb_id for p in selected}) != len(selected):
            raise fail("duplicate ids")
        per_collection: Counter = Counter()
        per_director: Counter = Counter()
        items = []
        for p in selected:
            details = self._details.get(p.tmdb_id)
            if details is None or evaluate(details, self.gate.rules, self.today):
                raise fail(f"tmdb {p.tmdb_id} fails the gatekeeper")
            candidate = to_candidate(details)
            if candidate.imdb_id != p.imdb_id:
                raise fail(f"tmdb {p.tmdb_id} imdb id changed")
            if any(
                rx.search(t) for rx in self.title_patterns for t in (candidate.title, candidate.original_title) if t
            ):
                raise fail(f"tmdb {p.tmdb_id} matches a franchise title pattern")
            if self.definition.get("exclude_watched") is True and (
                self.watched is None or self.watched.contains(p.tmdb_id, p.imdb_id)
            ):
                raise fail(f"tmdb {p.tmdb_id} is a watched film")
            if self.history.id_index(p.tmdb_id, p.imdb_id) is not None and (
                self.history.id_index(p.tmdb_id, p.imdb_id) < self.size
            ):
                raise fail(f"tmdb {p.tmdb_id} is inside the history window")
            if p.collection_id is not None:
                per_collection[p.collection_id] += 1
            for d in p.director_ids or ():
                per_director[d] += 1
            items.append(_snapshot_item(candidate))
        if self.limits.max_per_collection is not None and any(
            n > self.limits.max_per_collection for n in per_collection.values()
        ):
            raise fail("collection cap exceeded")
        if self.limits.director_cap is not None and any(n > self.limits.director_cap for n in per_director.values()):
            raise fail("director cap exceeded")
        for decade, need in quotas.items():
            if sum(1 for p in selected if p.decade == decade) < need:
                raise fail(f"decade quota {decade}s not met")
        return items

    # ---------------------------------------------------------------- stats

    def repeats(self) -> int:
        selection = getattr(self, "selection", None)
        if selection is None:
            return 0
        return sum(1 for p in selection.selected if self.history.id_index(p.tmdb_id, p.imdb_id) is not None)

    def stats(self) -> dict[str, Any]:
        classified = getattr(self, "classified", None)
        selection = getattr(self, "selection", None)
        counts = {
            "model_calls_ok": self.calls,
            "topup_rounds": self.topup_rounds,
            "generated": self.generated_rows,
            "dropped_rows": self.dropped_rows,
        }
        rejects: dict[str, int] = {}
        if classified is not None:
            counts.update(classified.counts)
            rejects = dict(sorted(classified.rejects.items()))
        if selection is not None:
            counts["selected"] = len(selection.selected)
            if selection.unmet_hard:
                counts["unmet_hard"] = sum(selection.unmet_hard.values())
            if selection.unmet_soft:
                counts["unmet_soft"] = sum(selection.unmet_soft.values())
        if self.budget_stop is not None:
            counts["stopped_by_budget"] = 1
        if self.watched is not None:
            counts["watched_known"] = len(self.watched)
        relaxed = getattr(self, "relaxed_window", None)
        return {
            "stage_counts_json": json.dumps(counts, sort_keys=True),
            "reject_reasons_json": json.dumps(rejects, sort_keys=True),
            "prompt_hash": self.prompt_hash,
            "relaxed_window": relaxed,
            "repeats": self.repeats() if relaxed is not None else 0,
        }


def _pool_item(candidate: Candidate, category: str) -> PoolItem:
    return PoolItem(
        tmdb_id=candidate.tmdb_id,
        imdb_id=candidate.imdb_id,
        year=candidate.year,
        collection_id=candidate.collection_id,
        director_ids=candidate.director_ids,
        category=category or "",
    )


def _snapshot_item(candidate: Candidate) -> SnapshotItem:
    return SnapshotItem(
        imdb_id=candidate.imdb_id,
        title=candidate.title,
        year=candidate.year or None,
        tmdb_id=candidate.tmdb_id,
        poster=f"{POSTER_BASE}{candidate.poster_path}" if candidate.poster_path else None,
        description=candidate.overview or None,
    )
