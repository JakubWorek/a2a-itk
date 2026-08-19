"""Known failures: combinations deliberately left out of a run.

Generated scenarios can't carry an ``expected: fail`` marker — with
``peers: all`` the scenario doesn't exist in any file to annotate. So the
exceptions live in one central list instead, matched against resolved
scenarios just before they run.

This is for things that are genuinely broken or unsupported and would
otherwise be red on every run. It is deliberately *not* the place for a
capability limit: if a version line simply doesn't speak a transport, that
belongs in ``matrix.yaml``'s per-line ``transports``, where it also stops the
peer being selected in the first place. Use this when the combination is a
defect rather than a shape.

Every entry needs a ``reason``. An exclusion nobody can explain is
indistinguishable from silently dropped coverage, which is the failure mode
this whole consolidation exists to remove — so exclusions are reported on
every run rather than applied quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ExclusionError(ValueError):
    """Malformed known-failures file."""


@dataclass(frozen=True)
class Exclusion:
    """One rule. Unset fields mean "any"; set fields must all match.

    ``sut_sdk`` and ``unless_sut_sdk`` are here because v0.3 interoperability
    turns out to be a property of the *pair*, not of a version line on its
    own. The same ts_v03 peer works over grpc with a TypeScript counterpart
    and not with a Python one, because the compat layer doing the
    v0.3<->v1.0 translation lives in whichever SDK drives the hop. A per-line
    capability in ``matrix.yaml`` cannot express that; these can.
    """

    reason: str
    agents: frozenset[str] = frozenset()
    transports: frozenset[str] = frozenset()
    behaviors: frozenset[str] = frozenset()
    sut_sdk: frozenset[str] = frozenset()
    unless_sut_sdk: frozenset[str] = frozenset()
    streaming: bool | None = None
    issue: str | None = None

    def matches(  # noqa: PLR0911
        self,
        *,
        sdks: list[str],
        protocols: list[str] | None,
        behavior: str,
        streaming: bool,
        sut_sdk: str | None = None,
    ) -> bool:
        """Does this rule cover the given resolved scenario?"""
        if self.agents and not (self.agents & set(sdks)):
            return False
        if self.transports:
            # Any overlap excludes: a scenario bundling several transports
            # cannot be partially skipped, so if one of them is known-bad the
            # whole scenario has to go. Splitting transports (the default)
            # keeps that from costing anything.
            if not (self.transports & set(protocols or [])):
                return False
        if self.behaviors and behavior not in self.behaviors:
            return False
        if self.sut_sdk and sut_sdk not in self.sut_sdk:
            return False
        if self.unless_sut_sdk and sut_sdk in self.unless_sut_sdk:
            return False
        return not (self.streaming is not None and streaming != self.streaming)

    def scope(self) -> str:
        """The matcher, rendered compactly."""
        bits = []
        if self.sut_sdk:
            bits.append(f'sut={"/".join(sorted(self.sut_sdk))}')
        if self.unless_sut_sdk:
            bits.append(f'sut!={"/".join(sorted(self.unless_sut_sdk))}')
        if self.agents:
            bits.append('/'.join(sorted(self.agents)))
        if self.transports:
            bits.append('/'.join(sorted(self.transports)))
        if self.behaviors:
            bits.append('/'.join(sorted(self.behaviors)))
        if self.streaming is not None:
            bits.append('streaming' if self.streaming else 'non-streaming')
        return ' '.join(bits) or 'everything'

    def summary(self) -> str:
        """Scope, the first sentence of the reason, and the issue link.

        For per-scenario log lines, where the full rationale would repeat
        dozens of times and drown out the run. The link stays: it is the one
        part a reader acts on.
        """
        first = ' '.join(self.reason.split()).split('. ')[0].rstrip('.')
        issue = f' [{self.issue}]' if self.issue else ''
        return f'{self.scope()}: {first}{issue}'

    def describe(self) -> str:
        issue = f' ({self.issue})' if self.issue else ''
        reason = ' '.join(self.reason.split())
        return f'{self.scope()}: {reason}{issue}'


class KnownFailures:
    """The loaded rule set. Empty is normal and valid."""

    def __init__(self, exclusions: list[Exclusion] | None = None) -> None:
        self._exclusions = list(exclusions or [])

    @classmethod
    def from_path(cls, path: Path) -> 'KnownFailures':
        """Load from YAML. A missing file means no exclusions."""
        if not path.is_file():
            return cls()
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        return cls.from_dict(data, str(path))

    @classmethod
    def from_default(cls) -> 'KnownFailures':
        return cls.from_path(_default_path())

    @classmethod
    def from_dict(cls, data: dict, where: str = '<dict>') -> 'KnownFailures':
        if not isinstance(data, dict):
            raise ExclusionError(f'{where}: must be a mapping')
        raw = data.get('exclusions') or []
        if not isinstance(raw, list):
            raise ExclusionError(f'{where}: `exclusions` must be a list')

        out = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ExclusionError(f'{where}: exclusions[{i}] must be a mapping')
            reason = entry.get('reason')
            if not reason or not isinstance(reason, str):
                raise ExclusionError(
                    f'{where}: exclusions[{i}] needs a `reason`. An exclusion '
                    f'nobody can explain is indistinguishable from lost coverage.'
                )
            unknown = set(entry) - {
                'reason', 'agents', 'transports', 'behaviors', 'streaming',
                'issue', 'sut_sdk', 'unless_sut_sdk',
            }
            if unknown:
                raise ExclusionError(
                    f'{where}: exclusions[{i}] has unknown key(s) '
                    f'{sorted(unknown)}'
                )
            if entry.get('sut_sdk') and entry.get('unless_sut_sdk'):
                raise ExclusionError(
                    f'{where}: exclusions[{i}] sets both `sut_sdk` and '
                    f'`unless_sut_sdk`; use one or the other'
                )
            out.append(Exclusion(
                reason=reason,
                agents=frozenset(entry.get('agents') or []),
                transports=frozenset(entry.get('transports') or []),
                behaviors=frozenset(entry.get('behaviors') or []),
                sut_sdk=frozenset(entry.get('sut_sdk') or []),
                unless_sut_sdk=frozenset(entry.get('unless_sut_sdk') or []),
                streaming=entry.get('streaming'),
                issue=entry.get('issue'),
            ))
        return cls(out)

    def find(
        self,
        *,
        sdks: list[str],
        protocols: list[str] | None,
        behavior: str,
        streaming: bool,
        sut_sdk: str | None = None,
    ) -> Exclusion | None:
        """The first rule covering this scenario, if any."""
        for e in self._exclusions:
            if e.matches(
                sdks=sdks, protocols=protocols, behavior=behavior,
                streaming=streaming, sut_sdk=sut_sdk,
            ):
                return e
        return None

    def __len__(self) -> int:
        return len(self._exclusions)

    def __iter__(self):
        return iter(self._exclusions)


def _default_path() -> Path:
    """Repo-root ``known_failures.yaml`` — sibling of ``matrix.yaml``."""
    return Path(__file__).resolve().parents[2] / 'known_failures.yaml'
