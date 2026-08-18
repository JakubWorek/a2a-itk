"""Scenario execution pipeline, shared by the service and the CLI.

Given a list of :class:`Scenario` objects this module does the whole job:
resolve each agent identifier to a :class:`TargetSpec`, start one shared
cluster, run every scenario against it, and tear the cluster down.

Two front ends call it and neither owns any pipeline logic of its own:

  * ``itk_service_v2.py`` — the HTTP ``/run`` handler every SDK's
    ``run_itk.sh`` POSTs to. Maps the errors raised here onto status codes.
  * ``run_tests.py`` — the local CLI, for running scenarios on your own
    machine without standing up a container or checking out an SDK.

Keeping the pipeline in one place is the point: a second copy would drift,
and "the local runner and CI disagree" is exactly the class of bug this
whole consolidation exists to remove.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from test_suite.agent_table import AgentTable
from test_suite.launcher import Cluster, TargetSpec
from test_suite.launcher.fetch import resolve_ref
from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.loader import parse_tests
from test_suite.scenarios.resolver import ResolvedScenario, resolve_all
from test_suite.launcher.spec import Kind
from testlib import execute_itk_test


logger = logging.getLogger(__name__)


# The SUT identifier. Unlike every other id it doesn't map through
# matrix.yaml — it's whatever is sitting at config.mount_dir().
SUT_ID = 'current'


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


# One scenario to execute, with roles already bound to concrete agents.
# Field-for-field the schema of an entry in an SDK's ``scenarios.json``, so a
# legacy file runs unchanged; a traversal/v1 scenario becomes one of these
# (or several) via the resolver first. Everything below this point sees only
# this shape and never learns which schema it came from.
Scenario = ResolvedScenario


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's outcome, plus enough of its definition to record it.

    The extra fields exist because the nightly metrics processor used to
    recover them by matching the result name back against the scenario file
    — and silently dropped any scenario whose name it couldn't find. That
    made renaming a scenario, or generating one, quietly erase it from the
    published history. Carrying the metadata with the result removes the
    lookup, and the failure mode with it.
    """

    passed: bool
    sdks: list[str]
    edges: list[str] | None = None
    protocols: list[str] | None = None
    behavior: str | None = None
    streaming: bool = False
    tier: str | None = None
    expected: str | None = None


class ClusterStartupError(RuntimeError):
    """One or more agents failed to start.

    Carries per-agent detail so the caller can say *which* peer died and at
    which stage, rather than reporting a blanket failure.
    """

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        super().__init__(
            'Cluster startup failed: '
            + '; '.join(f'{sid}: {detail}' for sid, detail in failures)
        )


@dataclass
class _Plan:
    """Agent identifiers resolved to launch targets, in a stable order."""

    ids: list[str] = field(default_factory=list)
    specs: list[TargetSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialisation of runs
# ---------------------------------------------------------------------------

# A run owns a whole cluster: host ports, the launcher's build cache, and
# the fixed container port the notification server binds. Two runs at once in
# one process would contend on all three, so they're serialised. Both front
# ends go through here, so one lock covers both.
#
# (Until Story 2.2 this also guarded the process-global agent registry.
# That's gone — ports now travel as an AgentTable argument — but the
# resource contention above is reason enough to keep the lock.)
_execution_lock = asyncio.Lock()

_matrix: Matrix | None = None


def get_matrix() -> Matrix:
    """Load ``matrix.yaml`` once per process.

    Lazy so a malformed matrix surfaces on the first run rather than
    preventing the service from starting at all (``/health`` stays green
    either way, which is useful when debugging).
    """
    global _matrix
    if _matrix is None:
        _matrix = Matrix.from_default()
    return _matrix


def set_matrix(matrix: Matrix | None) -> None:
    """Override the cached matrix. For tests and for ``--matrix``."""
    global _matrix
    _matrix = matrix


# ---------------------------------------------------------------------------
# Input: either scenario schema -> executable scenarios
# ---------------------------------------------------------------------------


def prepare(raw_tests: object, *, sut_sdk: str | None = None) -> list[Scenario]:
    """Parse and resolve whatever the caller supplied into runnable scenarios.

    Accepts a ``{"tests": [...]}`` mapping or a bare list, holding legacy
    entries, ``traversal/v1`` entries, or a mixture. Both front ends funnel
    through here so a scenario behaves the same over HTTP and on the CLI, and
    so an SDK can migrate one scenario at a time.

    Args:
        raw_tests: The parsed scenario document.
        sut_sdk: Which SDK is under test, for evaluating ``test_when``. When
            omitted nothing is filtered.

    Returns:
        Executable scenarios, roles already bound to concrete agents.

    Raises:
        test_suite.scenarios.loader.ScenarioFileError: Malformed input.
        test_suite.scenarios.resolver.ResolutionError: A scenario names a
            peer the matrix doesn't have, or can't be bound.
    """
    report = resolve_all(parse_tests(raw_tests), get_matrix(), sut_sdk=sut_sdk)
    if report.skipped:
        # Logged at warning, individually: a skip that scrolls past unnoticed
        # is indistinguishable from coverage that quietly vanished.
        logger.warning('%d scenario(s) skipped:', len(report.skipped))
        for name, why in report.skipped:
            logger.warning('  %s — %s', name, why)
    logger.info('%d scenario(s) to run', len(report.scenarios))
    return report.scenarios


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def build_specs(sdk_ids: list[str]) -> dict[str, TargetSpec]:
    """Turn scenario-level agent IDs into TargetSpecs.

    Blocks on ``git ls-remote`` per unique (repo, ref), so callers on an
    event loop should run it via :func:`asyncio.to_thread`.

    Refs resolve to SHAs once, here at plan time, so a branch tip that moves
    mid-run cannot mix versions across peers — that's the rule
    ``TargetSpec`` enforces by rejecting anything but a 40-hex SHA.

    Each unique (repo, ref) resolves once even when several identifiers map
    to it (``python_v10`` and ``python_v10_2`` share one entry).

    Raises:
        test_suite.launcher.matrix.MatrixError: Unknown agent id, or a
            malformed matrix.
        test_suite.launcher.errors.PermanentError: The ref does not exist.
        test_suite.launcher.errors.InfraFailure: ls-remote kept timing out.
    """
    matrix = get_matrix()
    sha_cache: dict[tuple[str, str], str] = {}
    specs: dict[str, TargetSpec] = {}

    for sdk_id in sdk_ids:
        if sdk_id == SUT_ID:
            specs[sdk_id] = TargetSpec(kind=Kind.MOUNT)
            continue

        entry = matrix.resolve(sdk_id)
        cache_key = (entry.repo, entry.ref)
        if cache_key not in sha_cache:
            sha_cache[cache_key] = resolve_ref(entry.repo, entry.ref)
        specs[sdk_id] = TargetSpec(
            kind=Kind.CHECKOUT,
            repo=entry.repo,
            sha=sha_cache[cache_key],
        )
    return specs


def _plan(scenarios: list[Scenario]) -> _Plan:
    """Union every agent the batch needs, so each is spawned exactly once."""
    all_ids = sorted({sdk for s in scenarios for sdk in s.sdks})
    specs_by_id = build_specs(all_ids)
    ids = list(specs_by_id.keys())
    return _Plan(ids=ids, specs=[specs_by_id[i] for i in ids])


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def run_scenarios(
    scenarios: list[Scenario],
    *,
    log_dir: Path | None = None,
) -> dict[str, ScenarioResult]:
    """Start one cluster and run every scenario against it.

    Args:
        scenarios: What to run. Must be non-empty.
        log_dir: If set, each agent's stdout/stderr is captured to
            ``<log_dir>/agent_<id>.log``.

    Returns:
        Results keyed by scenario name. A scenario with ``build_subtests``
        contributes one entry per expanded subgraph, not just one for itself.

    Raises:
        ValueError: ``scenarios`` is empty.
        ClusterStartupError: At least one agent didn't come up.
        test_suite.launcher.matrix.MatrixError: Unknown agent id.
        test_suite.launcher.errors.PermanentError: Unresolvable ref.
        test_suite.launcher.errors.InfraFailure: Transient fetch failure.
    """
    if not scenarios:
        raise ValueError('No scenarios provided')

    async with _execution_lock:
        return await _run_locked(scenarios, log_dir=log_dir)


async def _run_locked(
    scenarios: list[Scenario],
    *,
    log_dir: Path | None,
) -> dict[str, ScenarioResult]:
    logger.info(
        'Planning cluster for %d scenario(s): %s',
        len(scenarios),
        sorted({sdk for s in scenarios for sdk in s.sdks}),
    )
    # ls-remote per peer blocks; keep it off the event loop.
    plan = await asyncio.to_thread(_plan, scenarios)

    # Readable per-agent log filenames so whoever is tailing them can tell
    # which peer is which. Positional, so `python_v10` and `python_v10_2`
    # get separate files even though they resolve to the same spec.
    log_names = [f'agent_{sid}' for sid in plan.ids]

    with Cluster(log_dir=log_dir) as cluster:
        outcomes = await asyncio.to_thread(
            cluster.start_all, plan.specs, log_names=log_names,
        )

        failures = [
            (plan.ids[i], f'{o.error.stage.value}: {o.error}')
            for i, o in enumerate(outcomes)
            if not o.ok()
        ]
        if failures:
            raise ClusterStartupError(failures)

        # Where the agents we just started are listening. Passed down the
        # call chain rather than published to a global, so nothing can leak
        # into the next run.
        agents = AgentTable.from_handles(
            {plan.ids[i]: o.handle for i, o in enumerate(outcomes)}
        )
        logger.info('Cluster up: %r', agents)

        # Sequential on purpose — the cluster is shared, and running
        # scenarios concurrently against it overloads the agents.
        results: dict[str, ScenarioResult] = {}
        for scenario in scenarios:
            logger.info("Executing scenario '%s'", scenario.name)
            raw = await execute_itk_test(
                sdks=scenario.sdks,
                behavior=scenario.behavior,
                agents=agents,
                edges=scenario.edges,
                scenario_name=scenario.name,
                protocols=scenario.protocols,
                streaming=scenario.streaming,
                build_subtests=scenario.build_subtests,
            )
            for name, details in raw.items():
                # sdks/edges come from the execution because a subtest runs a
                # smaller graph than its parent scenario declares; everything
                # else is a property of the scenario and is copied across.
                results[name] = ScenarioResult(
                    passed=bool(details['passed']),
                    sdks=list(details['sdks']),
                    edges=details.get('edges'),
                    protocols=scenario.protocols,
                    behavior=scenario.behavior,
                    streaming=scenario.streaming,
                    tier=scenario.tier,
                    expected=scenario.expected,
                )
    return results



