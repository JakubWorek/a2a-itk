#!/usr/bin/env python3
"""Check the shared scenario set still covers each SDK's legacy set.

Story 2.3's acceptance condition is that migrating a repo onto the shared
scenarios doesn't quietly test less than its own file did. Comparing the two
files scenario-by-scenario doesn't answer that, because the merge deliberately
reshapes them: five repos wrote the same test five different ways, several
scenarios collapse into one declaration, and one scenario may now carry three
transports where a repo's version carried one.

So the comparison is at the level of what actually gets exercised. Running a
scenario walks an Eulerian circuit over its edges, once per transport, so the
unit of coverage is::

    (caller, callee, transport, behavior, streaming)

Every such tuple in the legacy set must appear in the new one. This gets the
reshaping right by construction:

  * a scenario over ``[jsonrpc, grpc, http_json]`` covers the repo that only
    listed ``[jsonrpc]``, because it walks the circuit for each;
  * a star over nine agents covers a star over five of them, since a star's
    edges are all SUT-to-peer;
  * a pairwise scenario covers the same pair inside someone else's star.

Coverage may grow — that is the point of merging five sets — so extra tuples
are reported and never fail. Coverage may not shrink.

Usage::

    scripts/scenarios_diff.py --old a2a-python/itk/scenarios.json \\
                              --new scenarios/traversal/pr.yaml
    scripts/scenarios_diff.py --old a2a-go/itk/scenarios_full.json \\
                              --new scenarios/traversal/nightly.yaml

Exit codes: 0 when nothing is lost, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.loader import ScenarioFileError, load_file
from test_suite.scenarios.resolver import ResolutionError, ResolvedScenario, resolve

_ALL_TRANSPORTS = ('jsonrpc', 'grpc', 'http_json')

# (caller, callee, transport, behavior, streaming)
Atom = tuple[str, str, str, str, bool]


def atoms(s: ResolvedScenario) -> set[Atom]:
    """Every hop this scenario exercises, once per transport."""
    pairs = _edge_pairs(s)
    transports = s.protocols or list(_ALL_TRANSPORTS)
    return {
        (caller, callee, t, s.behavior, bool(s.streaming))
        for caller, callee in pairs
        for t in transports
    }


def _edge_pairs(s: ResolvedScenario) -> set[tuple[str, str]]:
    """Resolve index edges to agent pairs; no edges means a complete digraph."""
    if s.edges is None:
        return {(u, v) for u in s.sdks for v in s.sdks if u != v}

    pairs = set()
    for edge in s.edges:
        raw_u, _, raw_v = edge.partition('->')
        u, v = int(raw_u.strip()), int(raw_v.strip())
        pairs.add((s.sdks[u], s.sdks[v]))
    return pairs


def describe(a: Atom) -> str:
    caller, callee, transport, behavior, streaming = a
    return (
        f'{caller} -> {callee}  {transport}  {behavior}'
        f'{" streaming" if streaming else ""}'
    )


def load(paths: list[Path], matrix: Matrix, sut_sdk: str | None) -> list[ResolvedScenario]:
    scenarios: list = []
    for p in paths:
        scenarios.extend(load_file(p))
    return resolve(scenarios, matrix, sut_sdk=sut_sdk)


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', nargs='+', type=Path, required=True,
                        help='The legacy scenario file(s) being replaced.')
    parser.add_argument('--new', nargs='+', type=Path, required=True,
                        help='The shared scenario file(s) replacing them.')
    parser.add_argument('--sut-sdk',
                        help='SUT, for evaluating test_when on the new set.')
    parser.add_argument('--show-extra', action='store_true',
                        help='List the added coverage as well as summarising it.')
    args = parser.parse_args(argv)

    matrix = Matrix.from_default()
    try:
        old = load(args.old, matrix, None)
        new = load(args.new, matrix, args.sut_sdk)
    except (ScenarioFileError, ResolutionError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1

    old_atoms: set[Atom] = set().union(*(atoms(s) for s in old)) if old else set()
    new_atoms: set[Atom] = set().union(*(atoms(s) for s in new)) if new else set()

    missing = old_atoms - new_atoms
    extra = new_atoms - old_atoms

    print(f'old: {len(old):>3} scenarios -> {len(old_atoms):>4} hops covered')
    print(f'new: {len(new):>3} scenarios -> {len(new_atoms):>4} hops covered')
    print(f'retained: {len(old_atoms) - len(missing)}/{len(old_atoms)}')

    if missing:
        print(f'\nMISSING — exercised by the legacy set, not by the new one '
              f'({len(missing)}):')
        for a in sorted(missing):
            print(f'  - {describe(a)}')

    if extra:
        by_pair: dict[tuple[str, str], int] = defaultdict(int)
        for caller, callee, *_ in extra:
            by_pair[(caller, callee)] += 1
        print(f'\nEXTRA — added coverage, not an error ({len(extra)} hops):')
        for (caller, callee), n in sorted(by_pair.items()):
            print(f'  + {caller} -> {callee}  ({n} hops)')
        if args.show_extra:
            for a in sorted(extra):
                print(f'      {describe(a)}')

    if missing:
        print(f'\nFAIL: {len(missing)} hop(s) would stop being tested.')
        return 1
    print('\nOK: all legacy coverage retained'
          + (f', plus {len(extra)} new hops.' if extra else '.'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
