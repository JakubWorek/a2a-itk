"""Binding roles to concrete agents.

The contract this story has to meet is that a migrated scenario produces the
*same* peer list, edges and transports as the legacy one it replaces. The
equivalence tests at the bottom are that contract; the rest cover the
expansion machinery that gets there.
"""

from __future__ import annotations

import pytest

from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.loader import parse_tests
from test_suite.scenarios.resolver import (
    ResolutionError,
    resolve,
    resolve_all,
)
from test_suite.scenarios.topology import normalize_edges


MATRIX = Matrix.from_dict({'sdks': {
    'python': {
        'v10': {'repo': 'a2aproject/a2a-python', 'ref': 'main'},
        'v03': {'repo': 'a2aproject/a2a-python', 'ref': 'v0.3.24+itk'},
    },
    'go': {
        'v10': {'repo': 'a2aproject/a2a-go', 'ref': 'main'},
        'v03': {'repo': 'a2aproject/a2a-go', 'ref': 'v0.3.15+itk',
                'transports': ['jsonrpc', 'grpc']},
    },
    'java': {'v10': {'repo': 'a2aproject/a2a-java', 'ref': 'main'}},
}})


def _one(**overrides):
    base = {
        'schema': 'traversal/v1',
        'name': 'S',
        'roles': {'sut': 'current', 'peers': [{'sdk': 'go', 'line': 'v10'}]},
        'transports': ['jsonrpc'],
        'behavior': 'send_message',
    }
    base.update(overrides)
    return parse_tests([base])


class TestRoleBinding:
    def test_sut_leads_the_agent_list(self):
        """Index 0 starts the traversal and picks the request encoding."""
        [s] = resolve(_one(), MATRIX)
        assert s.sdks == ['current', 'go_v10']

    def test_peers_keep_authored_order(self):
        [s] = resolve(_one(roles={'peers': [
            {'sdk': 'python', 'line': 'v03'},
            {'sdk': 'go', 'line': 'v10'},
        ]}), MATRIX)
        assert s.sdks == ['current', 'python_v03', 'go_v10']

    def test_sut_can_be_excluded(self):
        [s] = resolve(_one(roles={
            'include_sut': False,
            'peers': [{'sdk': 'python', 'line': 'v10'},
                      {'sdk': 'go', 'line': 'v10'}],
        }), MATRIX)
        assert s.sdks == ['python_v10', 'go_v10']

    def test_instance_suffix_survives(self):
        [s] = resolve(_one(roles={
            'include_sut': False,
            'peers': [{'sdk': 'python', 'line': 'v10'},
                      {'sdk': 'python', 'line': 'v10', 'instance': 2}],
        }), MATRIX)
        assert s.sdks == ['python_v10', 'python_v10_2']

    def test_unknown_peer_fails_loudly(self):
        """Caught before CI builds anything, which is the point of resolving
        up front."""
        with pytest.raises(ResolutionError, match='unknown agent'):
            resolve(_one(roles={'peers': [{'sdk': 'cobol', 'line': 'v10'}]}), MATRIX)

    def test_too_few_agents_fails(self):
        with pytest.raises(ResolutionError, match='at least 2'):
            resolve(_one(roles={'include_sut': False,
                                'peers': [{'sdk': 'go', 'line': 'v10'}]}), MATRIX)


class TestPeersAllMacro:
    def test_includes_every_line(self):
        [s] = resolve(_one(roles={'peers': 'all'}), MATRIX)
        assert s.sdks == [
            'current', 'go_v03', 'go_v10', 'java_v10', 'python_v03', 'python_v10',
        ]

    def test_filters_lines_that_cannot_do_the_transport(self):
        """go_v03 has no http_json, so it drops out of an http_json scenario
        instead of being started only to fail."""
        [s] = resolve(_one(roles={'peers': 'all'},
                           transports=['http_json']), MATRIX)
        assert 'go_v03' not in s.sdks
        assert 'go_v10' in s.sdks

    def test_requires_all_requested_transports(self):
        [s] = resolve(_one(roles={'peers': 'all'},
                           transports=['jsonrpc', 'http_json']), MATRIX)
        assert 'go_v03' not in s.sdks

    def test_explicit_peers_are_never_filtered(self):
        """The author named this peer. Dropping it would quietly change what a
        migrated scenario covers."""
        [s] = resolve(_one(roles={'peers': [{'sdk': 'go', 'line': 'v03'}]},
                           transports=['http_json']), MATRIX)
        assert s.sdks == ['current', 'go_v03']

    def test_expansion_order_is_stable(self):
        """Edge indices are positional, so an unstable peer order would make
        two runs of the same scenario incomparable."""
        a = resolve(_one(roles={'peers': 'all'}), MATRIX)[0].sdks
        b = resolve(_one(roles={'peers': 'all'}), MATRIX)[0].sdks
        assert a == b
        assert a[0] == 'current'
        assert a[1:] == sorted(a[1:])


class TestTopologyExpansion:
    def test_star_edges_from_agent_count(self):
        [s] = resolve(_one(roles={'peers': [
            {'sdk': 'go', 'line': 'v10'}, {'sdk': 'python', 'line': 'v10'},
        ]}), MATRIX)
        assert s.edges == ['0->1', '0->2', '1->0', '2->0']

    def test_euler_defers_to_the_engine(self):
        [s] = resolve(_one(topology='euler'), MATRIX)
        assert s.edges is None

    def test_explicit_edges_override_topology(self):
        [s] = resolve(_one(topology='star', edges=['0->1', '1->0']), MATRIX)
        assert s.edges == ['0->1', '1->0']


class TestCartesianExpansion:
    def test_behaviors_expand(self):
        out = resolve(_one(behavior=None,
                           behaviors=['send_message', 'resubscribe']), MATRIX)
        assert [s.behavior for s in out] == ['send_message', 'resubscribe']

    def test_full_product(self):
        out = resolve(_one(
            behavior=None, behaviors=['send_message', 'resubscribe'],
            transports=None, transport_sets=[['jsonrpc'], ['grpc']],
            streaming_variants=[False, True],
        ), MATRIX)
        assert len(out) == 2 * 2 * 2

    def test_names_stay_unchanged_when_nothing_varies(self):
        """Results are keyed by name and the nightly history is a time series
        per name, so a single-variant scenario must keep the authored name."""
        [s] = resolve(_one(name='Exactly This'), MATRIX)
        assert s.name == 'Exactly This'

    def test_only_varying_axes_appear_in_the_name(self):
        out = resolve(_one(name='Base', streaming_variants=[False, True]), MATRIX)
        assert [s.name for s in out] == [
            'Base - non-streaming', 'Base - streaming',
        ]

    def test_multi_axis_names_are_unique(self):
        out = resolve(_one(
            name='N', behavior=None, behaviors=['send_message', 'resubscribe'],
            transports=None, transport_sets=[['jsonrpc'], ['grpc', 'http_json']],
        ), MATRIX)
        names = [s.name for s in out]
        assert len(set(names)) == len(names)
        assert 'N - send_message - grpc+http_json' in names

    def test_duplicate_names_are_rejected(self):
        """Two scenarios with one name means one silently overwrites the
        other's result."""
        scenarios = parse_tests([
            {'schema': 'traversal/v1', 'name': 'dup',
             'roles': {'peers': [{'sdk': 'go', 'line': 'v10'}]},
             'transports': ['jsonrpc'], 'behavior': 'send_message'},
            {'schema': 'traversal/v1', 'name': 'dup',
             'roles': {'peers': [{'sdk': 'java', 'line': 'v10'}]},
             'transports': ['grpc'], 'behavior': 'send_message'},
        ])
        with pytest.raises(ResolutionError, match='duplicate scenario name'):
            resolve(scenarios, MATRIX)


class TestTestWhen:
    def test_scenario_runs_for_a_listed_sut(self):
        out = resolve(_one(test_when={'sut_sdk': ['python', 'go']}),
                      MATRIX, sut_sdk='python')
        assert len(out) == 1

    def test_scenario_is_skipped_for_an_unlisted_sut(self):
        report = resolve_all(
            _one(test_when={'sut_sdk': ['python']}), MATRIX, sut_sdk='java'
        )
        assert report.scenarios == []
        assert report.skipped and 'java' in report.skipped[0][1]

    def test_no_sut_sdk_means_no_filtering(self):
        """A local run with no SUT declared sees everything."""
        assert len(resolve(_one(test_when={'sut_sdk': ['python']}), MATRIX)) == 1


class TestLegacyPassthrough:
    def test_legacy_scenarios_resolve_unchanged(self):
        scenarios = parse_tests({'tests': [{
            'name': 'Star Topology (Full) - JSONRPC & GRPC',
            'sdks': ['current', 'python_v10', 'go_v03'],
            'edges': ['0->1', '0->2', '1->0', '2->0'],
            'protocols': ['jsonrpc', 'grpc'],
            'behavior': 'send_message',
        }]})
        [s] = resolve(scenarios, MATRIX)
        assert s.sdks == ['current', 'python_v10', 'go_v03']
        assert s.edges == ['0->1', '0->2', '1->0', '2->0']
        assert s.protocols == ['jsonrpc', 'grpc']

    def test_a_batch_may_mix_both_schemas(self):
        """The whole compatibility requirement, in one test."""
        scenarios = parse_tests({'tests': [
            {'name': 'old', 'sdks': ['current', 'go_v10'],
             'behavior': 'send_message', 'protocols': ['jsonrpc']},
            {'schema': 'traversal/v1', 'name': 'new',
             'roles': {'peers': [{'sdk': 'go', 'line': 'v10'}]},
             'transports': ['jsonrpc'], 'behavior': 'send_message'},
        ]})
        out = resolve(scenarios, MATRIX)
        assert [s.name for s in out] == ['old', 'new']
        assert out[0].sdks == out[1].sdks


class TestBundledSmokeEquivalence:
    """scenarios/traversal/smoke.yaml must resolve exactly like smoke.json.

    The worked example of a migration, and the pattern Story 2.3 applies to
    the rest of the corpus.
    """

    def _load(self, name):
        from pathlib import Path
        from test_suite.scenarios.loader import load_file
        root = Path(__file__).resolve().parents[1]
        return resolve(load_file(root / 'scenarios' / name), Matrix.from_default())

    def _key(self, s):
        return (
            s.name, tuple(s.sdks), s.behavior,
            tuple(sorted(s.protocols or [])), s.streaming,
            normalize_edges(s.edges, len(s.sdks)),
        )

    def test_identical(self):
        old = {self._key(s) for s in self._load('smoke.json')}
        new = {self._key(s) for s in self._load('traversal/smoke.yaml')}
        assert new == old


class TestExpandPerPeer:
    """One SUT-plus-one scenario per peer — the dominant nightly shape."""

    def test_one_scenario_per_peer(self):
        out = resolve(_one(roles={'peers': 'all'}, expand='per_peer'), MATRIX)
        assert [s.sdks for s in out] == [
            ['current', 'go_v03'], ['current', 'go_v10'],
            ['current', 'java_v10'], ['current', 'python_v03'],
            ['current', 'python_v10'],
        ]

    def test_peer_name_is_appended(self):
        out = resolve(_one(name='N', roles={'peers': 'all'},
                           expand='per_peer'), MATRIX)
        assert 'N - go_v10' in [s.name for s in out]

    def test_peer_placeholder_is_substituted(self):
        out = resolve(_one(name='vs {peer} - send', roles={'peers': 'all'},
                           expand='per_peer'), MATRIX)
        assert 'vs go_v10 - send' in [s.name for s in out]

    def test_transports_are_intersected_not_dropped(self):
        """Unlike `together`, a partially-capable peer still runs — over the
        transports it does speak. This is what reproduces "current vs go_v03"
        on jsonrpc+grpc beside "current vs python_v10" on all three."""
        out = resolve(_one(roles={'peers': 'all'}, expand='per_peer',
                           transports=['jsonrpc', 'grpc', 'http_json']), MATRIX)
        by_peer = {s.sdks[1]: s.protocols for s in out}
        assert by_peer['go_v03'] == ['jsonrpc', 'grpc']
        assert by_peer['python_v10'] == ['jsonrpc', 'grpc', 'http_json']

    def test_peer_sharing_no_transport_is_skipped(self):
        out = resolve(_one(roles={'peers': 'all'}, expand='per_peer',
                           transports=['http_json']), MATRIX)
        assert 'go_v03' not in [s.sdks[1] for s in out]

    def test_together_drops_the_same_peer_instead(self):
        """The two modes differ precisely here: in one graph a peer that can't
        speak the transport has to leave, because the hop to it would fail."""
        [s] = resolve(_one(roles={'peers': 'all'}, expand='together',
                           transports=['http_json']), MATRIX)
        assert 'go_v03' not in s.sdks


class TestIncludeOwnLines:
    """The SUT against its own SDK's released lines."""

    def _scenario(self, **kw):
        return _one(roles={
            'include_own_lines': True,
            'peers': [{'sdk': 'go', 'line': 'v10'}],
        }, **kw)

    def test_adds_the_suts_own_lines(self):
        [s] = resolve(self._scenario(), MATRIX, sut_sdk='python')
        assert s.sdks == ['current', 'go_v10', 'python_v03', 'python_v10']

    def test_adds_nothing_without_a_sut_sdk(self):
        """A local run doesn't know which SDK it is; it just runs the core."""
        [s] = resolve(self._scenario(), MATRIX)
        assert s.sdks == ['current', 'go_v10']

    def test_does_not_duplicate_an_explicit_peer(self):
        [s] = resolve(self._scenario(), MATRIX, sut_sdk='go')
        assert s.sdks == ['current', 'go_v10', 'go_v03']
        assert len(s.sdks) == len(set(s.sdks))

    def test_own_line_is_transport_filtered_under_together(self):
        """go_v03 can't do http_json, so it can't join an http_json graph."""
        [s] = resolve(
            _one(roles={'include_own_lines': True, 'peers': [
                {'sdk': 'python', 'line': 'v10'}]},
                transports=['http_json']),
            MATRIX, sut_sdk='go',
        )
        assert s.sdks == ['current', 'python_v10', 'go_v10']

    def test_unknown_sut_sdk_adds_nothing(self):
        [s] = resolve(self._scenario(), MATRIX, sut_sdk='cobol')
        assert s.sdks == ['current', 'go_v10']
