"""Known failures: excluding combinations that are broken, visibly.

The rule these tests protect is that an exclusion is never silent. A skipped
scenario that nobody is told about is indistinguishable from coverage that
quietly disappeared, which is the exact failure this consolidation exists to
remove.
"""

from __future__ import annotations

import pytest

from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.exclusions import (
    Exclusion,
    ExclusionError,
    KnownFailures,
)
from test_suite.scenarios.loader import parse_tests
from test_suite.scenarios.resolver import resolve_all


MATRIX = Matrix.from_dict({'sdks': {
    'python': {'v10': {'repo': 'a/py', 'ref': 'main'}},
    'go': {'v10': {'repo': 'a/go', 'ref': 'main'}},
}})


def _scenario(**over):
    base = {
        'schema': 'traversal/v1', 'name': 'S',
        'roles': {'peers': [{'sdk': 'go', 'line': 'v10'}]},
        'transports': ['jsonrpc', 'grpc'],
        'behavior': 'send_message',
    }
    base.update(over)
    return parse_tests([base])


def _match(exclusion, **over):
    kwargs = {
        'sdks': ['current', 'go_v10'], 'protocols': ['grpc'],
        'behavior': 'send_message', 'streaming': False,
    }
    kwargs.update(over)
    return exclusion.matches(**kwargs)


class TestMatching:
    def test_agent_and_transport(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}),
                      transports=frozenset({'grpc'}))
        assert _match(e) is True
        assert _match(e, protocols=['jsonrpc']) is False
        assert _match(e, sdks=['current', 'python_v10']) is False

    def test_unset_fields_mean_any(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}))
        assert _match(e, protocols=['jsonrpc']) is True
        assert _match(e, behavior='resubscribe') is True

    def test_all_set_fields_must_match(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}),
                      behaviors=frozenset({'push_notification'}))
        assert _match(e) is False
        assert _match(e, behavior='push_notification') is True

    def test_streaming_is_matched_exactly(self):
        e = Exclusion(reason='r', streaming=True)
        assert _match(e, streaming=False) is False
        assert _match(e, streaming=True) is True

    def test_streaming_false_is_not_treated_as_unset(self):
        """`streaming: false` must mean non-streaming only, not 'any'."""
        e = Exclusion(reason='r', streaming=False)
        assert _match(e, streaming=True) is False
        assert _match(e, streaming=False) is True

    def test_any_transport_overlap_excludes(self):
        """A bundled scenario can't be partially skipped, so one known-bad
        transport takes the whole thing."""
        e = Exclusion(reason='r', transports=frozenset({'http_json'}))
        assert _match(e, protocols=['jsonrpc', 'http_json']) is True


class TestParsing:
    def test_empty_file_is_valid(self):
        assert len(KnownFailures.from_dict({'exclusions': []})) == 0

    def test_missing_key_is_valid(self):
        assert len(KnownFailures.from_dict({})) == 0

    def test_reason_is_required(self):
        """An exclusion nobody can explain is lost coverage wearing a hat."""
        with pytest.raises(ExclusionError, match='needs a `reason`'):
            KnownFailures.from_dict({'exclusions': [{'agents': ['go_v10']}]})

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ExclusionError, match='unknown key'):
            KnownFailures.from_dict({'exclusions': [
                {'reason': 'r', 'transport': 'grpc'},  # singular typo
            ]})

    def test_missing_file_means_no_exclusions(self, tmp_path):
        assert len(KnownFailures.from_path(tmp_path / 'nope.yaml')) == 0

    def test_shipped_file_parses(self):
        KnownFailures.from_default()


class TestAppliedDuringResolution:
    def test_matching_scenario_is_excluded(self):
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'transports': ['grpc'],
             'reason': 'go v1 grpc hop hangs'},
        ]})
        report = resolve_all(_scenario(), MATRIX, known_failures=known)
        assert [s.protocols[0] for s in report.scenarios] == ['jsonrpc']

    def test_exclusion_is_reported_not_silent(self):
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'transports': ['grpc'],
             'reason': 'go v1 grpc hop hangs'},
        ]})
        report = resolve_all(_scenario(), MATRIX, known_failures=known)
        assert len(report.skipped) == 1
        name, why = report.skipped[0]
        assert 'grpc' in name
        assert 'known failure' in why
        assert 'go v1 grpc hop hangs' in why

    def test_issue_link_is_surfaced(self):
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'reason': 'r', 'issue': 'http://b/123'},
        ]})
        report = resolve_all(_scenario(), MATRIX, known_failures=known)
        assert 'http://b/123' in report.skipped[0][1]

    def test_no_exclusions_changes_nothing(self):
        report = resolve_all(_scenario(), MATRIX, known_failures=KnownFailures())
        assert len(report.scenarios) == 2
        assert report.skipped == []

    def test_legacy_scenarios_are_not_excluded(self):
        """Legacy files are the baseline being migrated from; reinterpreting
        them would make the coverage comparison meaningless."""
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'reason': 'everything go is broken'},
        ]})
        legacy = parse_tests({'tests': [{
            'name': 'old', 'sdks': ['current', 'go_v10'],
            'behavior': 'send_message', 'protocols': ['grpc'],
        }]})
        report = resolve_all(legacy, MATRIX, known_failures=known)
        assert len(report.scenarios) == 1

    def test_transport_granularity_needs_the_split(self):
        """Excluding one transport only works because scenarios carry one.
        Bundled, the rule would take the good transports down too."""
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'transports': ['grpc'], 'reason': 'r'},
        ]})
        split = resolve_all(_scenario(), MATRIX, known_failures=known)
        bundled = resolve_all(
            _scenario(transports=None, transport_sets=[['jsonrpc', 'grpc']]),
            MATRIX, known_failures=known,
        )
        assert len(split.scenarios) == 1      # jsonrpc survives
        assert len(bundled.scenarios) == 0    # whole bundle goes
