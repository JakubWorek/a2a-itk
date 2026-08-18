"""ITK service — the HTTP ``/run`` handler.

The wire contract is backwards compatible: ``/health``, port 8000, and the
legacy request/response schema every SDK's ``run_itk.sh`` POSTs its
``scenarios.json`` against all behave exactly as before. What is new is that
``/run`` also accepts ``traversal/v1`` scenarios, and may be sent a batch
mixing both — which is what lets each SDK migrate on its own schedule
instead of all five having to cut over at once.

This module is deliberately thin. All the actual work — parsing scenarios,
binding roles against ``matrix.yaml``, starting the cluster, executing —
lives in :mod:`itk_runner`, which ``run_tests.py`` also drives. Everything
here is HTTP concerns: request shape and mapping runner errors onto status
codes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import itk_runner
from itk_runner import ClusterStartupError
from test_suite.launcher import InfraFailure, PermanentError
from test_suite.launcher.matrix import MatrixError
from test_suite.scenarios.loader import ScenarioFileError
from test_suite.scenarios.resolver import ResolutionError


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire schema
#
# The request is deliberately untyped here. Two scenario schemas are live at
# once — the legacy shape every SDK's scenarios.json still uses, and
# traversal/v1 — and each SDK migrates on its own schedule, so the endpoint
# has to take either. Validation happens in test_suite.scenarios, which owns
# both schemas; duplicating one of them as a pydantic model here would just
# give the two definitions somewhere to drift apart.
#
# The response is typed, and its existing fields are unchanged.
# ---------------------------------------------------------------------------


class RunTestsRequest(BaseModel):
    tests: list[dict[str, Any]]
    # Which SDK is under test. Only used to evaluate a scenario's `test_when`,
    # so a shared scenario set can carry entries that don't apply to every
    # SUT. Absent means nothing is filtered, which is what every SDK sending
    # its own scenarios.json today wants.
    sut_sdk: str | None = None


class TestResultDetails(BaseModel):
    """One scenario's outcome.

    ``passed``/``sdks``/``edges`` are the original three and are unchanged.
    The rest were added so the nightly metrics processor no longer has to
    recover a scenario's definition by matching its name back against the
    scenario file — a lookup that silently dropped anything it couldn't
    match. Optional, so an older consumer reading only the first three is
    unaffected.
    """

    passed: bool
    sdks: list[str]
    edges: list[str] | None = None
    protocols: list[str] | None = None
    behavior: str | None = None
    streaming: bool = False
    tier: str | None = None
    expected: str | None = None


class RunTestsResponse(BaseModel):
    results: dict[str, TestResultDetails]
    all_passed: bool


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


app = FastAPI(title='ITK Service')


@app.get('/health')
async def health() -> dict[str, str]:
    """Frozen — SDK run_itk.sh scripts poll this before POSTing /run."""
    return {'status': 'ok'}


@app.post('/run', response_model=RunTestsResponse)
async def run_tests(request: RunTestsRequest) -> RunTestsResponse:
    """Run a batch of scenarios in either schema.

    The legacy request shape every SDK's ``scenarios.json`` targets is
    unchanged. A ``traversal/v1`` entry is recognised by its ``schema`` key
    and resolved against ``matrix.yaml`` first; a batch may mix the two.
    """
    if not request.tests:
        raise HTTPException(status_code=400, detail='No tests provided')

    try:
        scenarios = itk_runner.prepare(
            {'tests': request.tests}, sut_sdk=request.sut_sdk,
        )
    except (ScenarioFileError, ResolutionError) as e:
        # Malformed or unbindable scenario — the caller sent something we
        # can't run. 400 rather than 500: nothing here is retryable.
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not scenarios:
        # Every scenario was filtered out by test_when. Refused rather than
        # returning an empty pass, which would read as a green run that
        # tested nothing.
        raise HTTPException(
            status_code=400,
            detail=(
                f'No scenarios apply to sut_sdk={request.sut_sdk!r}; '
                f'all {len(request.tests)} were filtered out by test_when'
            ),
        )

    try:
        results = await itk_runner.run_scenarios(
            scenarios, log_dir=_agent_log_dir(),
        )
    except (MatrixError, PermanentError) as e:
        # Bad matrix entry, bogus agent id, unknown ref — none retryable.
        # 400: the caller sent a spec we can't fulfil.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except InfraFailure as e:
        # Transient (ls-remote timed out after retries) — caller can retry.
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ClusterStartupError as e:
        # At least one peer's startup was transient-class (build hiccup,
        # git flake); a retry can recover. Detail names which peer.
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception('Test execution failed')
        raise HTTPException(status_code=500, detail=f'Execution error: {e!s}') from e

    typed = {
        name: TestResultDetails(
            passed=r.passed,
            sdks=r.sdks,
            edges=r.edges,
            protocols=r.protocols,
            behavior=r.behavior,
            streaming=r.streaming,
            tier=r.tier,
            expected=r.expected,
        )
        for name, r in results.items()
    }
    return RunTestsResponse(
        results=typed, all_passed=all(r.passed for r in typed.values()),
    )


def _agent_log_dir() -> Path | None:
    """``/app/logs`` when the operator bind-mounted it, else no capture.

    ``run_itk.sh`` mounts it when ``ITK_LOG_LEVEL=DEBUG``; with it present,
    agent stdout/stderr lands in per-agent files so a readiness or spawn
    failure can be diagnosed after the container is gone.
    """
    d = Path('/app/logs')
    return d if d.is_dir() else None


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)  # noqa: S104
