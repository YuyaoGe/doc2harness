"""Execution adapter joining frozen fixtures to an injected real-process backend."""

from __future__ import annotations
import hashlib
from dataclasses import dataclass
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockEntrypoint,
    DirectMockFactKey,
    DirectMockMode,
    DirectMockOracleProfile,
    DirectMockRequiredFlag,
    DirectMockScenario,
)
from doc2harness.benchmark.harnesses.direct.fixtures import (
    DirectMockFixturePlan,
    materialize_direct_mock_fixture,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError


def _digest(value: str, label: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any((character not in "0123456789abcdef" for character in value[7:]))
    ):
        raise ContractDocumentError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DirectMockExecutionPlan:
    """Complete path-free instruction handed to the runtime authority."""

    scenario_id: str
    mode: DirectMockMode
    entrypoint: DirectMockEntrypoint
    omitted_flag: DirectMockRequiredFlag | None
    timeout_seconds: int
    fixture: DirectMockFixturePlan
    base_url_suffix: str
    model_name: str
    reject_substring: str
    stale_pwd_probe: bool
    initialize_git: bool
    oracle_profile: DirectMockOracleProfile
    tool_family: tuple[str, ...]
    required_tool_families: tuple[tuple[str, ...], ...]
    request_limit: int | None
    body_limit: int | None
    required_facts: tuple[DirectMockFactKey, ...]
    variant_labels: tuple[str, ...]
    plan_digest: str

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or not self.scenario_id:
            raise ContractDocumentError("direct mock execution id is invalid")
        if type(self.mode) is not DirectMockMode or type(self.entrypoint) is not DirectMockEntrypoint:
            raise ContractDocumentError("direct mock execution vocabulary is invalid")
        if self.omitted_flag is not None and type(self.omitted_flag) is not DirectMockRequiredFlag:
            raise ContractDocumentError("direct mock execution omitted flag is invalid")
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1 or self.timeout_seconds > 900:
            raise ContractDocumentError("direct mock execution timeout is invalid")
        if type(self.fixture) is not DirectMockFixturePlan or self.fixture.scenario_id != self.scenario_id:
            raise ContractDocumentError("direct mock execution fixture differs")
        if (
            type(self.base_url_suffix) is not str
            or ".." in self.base_url_suffix
            or len(self.base_url_suffix) > 1024
        ):
            raise ContractDocumentError("direct mock execution base-url suffix is invalid")
        if type(self.model_name) is not str or not self.model_name or len(self.model_name) > 512:
            raise ContractDocumentError("direct mock execution model is invalid")
        if type(self.reject_substring) is not str or len(self.reject_substring) > 1024:
            raise ContractDocumentError("direct mock execution rejection substring is invalid")
        if type(self.stale_pwd_probe) is not bool or type(self.initialize_git) is not bool:
            raise ContractDocumentError("direct mock execution repository setup is invalid")
        if type(self.oracle_profile) is not DirectMockOracleProfile:
            raise ContractDocumentError("direct mock execution oracle profile is invalid")
        if type(self.tool_family) is not tuple or any((type(item) is not str for item in self.tool_family)):
            raise ContractDocumentError("direct mock execution tool family is invalid")
        if type(self.required_tool_families) is not tuple or any(
            (
                type(family) is not tuple or any((type(item) is not str for item in family))
                for family in self.required_tool_families
            )
        ):
            raise ContractDocumentError("direct mock execution required tool families are invalid")
        for value, name, maximum in (
            (self.request_limit, "request", 4096),
            (self.body_limit, "body", 64 * 1024 * 1024),
        ):
            if value is not None and (type(value) is not int or value < 1 or value > maximum):
                raise ContractDocumentError(f"direct mock execution {name} limit is invalid")
        if type(self.required_facts) is not tuple or any(
            (type(item) is not DirectMockFactKey for item in self.required_facts)
        ):
            raise ContractDocumentError("direct mock execution required facts are invalid")
        if type(self.variant_labels) is not tuple or any(
            (type(item) is not str for item in self.variant_labels)
        ):
            raise ContractDocumentError("direct mock execution variant labels are invalid")
        expected = _plan_digest(
            self.scenario_id,
            self.mode,
            self.entrypoint,
            self.omitted_flag,
            self.timeout_seconds,
            self.fixture.plan_digest,
            self.base_url_suffix,
            self.model_name,
            self.reject_substring,
            self.stale_pwd_probe,
            self.initialize_git,
            self.oracle_profile,
            self.tool_family,
            self.required_tool_families,
            self.request_limit,
            self.body_limit,
            self.required_facts,
            self.variant_labels,
        )
        if self.plan_digest != expected:
            raise ContractDocumentError("direct mock execution digest does not bind its plan")


def _plan_digest(
    scenario_id: str,
    mode: DirectMockMode,
    entrypoint: DirectMockEntrypoint,
    omitted_flag: DirectMockRequiredFlag | None,
    timeout_seconds: int,
    fixture_digest: str,
    base_url_suffix: str = "",
    model_name: str = "litellm_proxy/mock",
    reject_substring: str = "",
    stale_pwd_probe: bool = False,
    initialize_git: bool = False,
    oracle_profile: DirectMockOracleProfile = DirectMockOracleProfile.UNDECLARED,
    tool_family: tuple[str, ...] = (),
    required_tool_families: tuple[tuple[str, ...], ...] = (),
    request_limit: int | None = None,
    body_limit: int | None = None,
    required_facts: tuple[DirectMockFactKey, ...] = (),
    variant_labels: tuple[str, ...] = (),
) -> str:
    body = {
        "entrypoint": entrypoint.value,
        "fixture_digest": fixture_digest,
        "mode": mode.value,
        "omitted_flag": omitted_flag.value if omitted_flag is not None else None,
        "scenario_id": scenario_id,
        "timeout_seconds": timeout_seconds,
        "base_url_suffix": base_url_suffix,
        "model_name": model_name,
        "reject_substring": reject_substring,
        "stale_pwd_probe": stale_pwd_probe,
        "initialize_git": initialize_git,
        "oracle_profile": oracle_profile.value,
        "tool_family": list(tool_family),
        "request_limit": request_limit,
        "body_limit": body_limit,
        "required_facts": [item.value for item in required_facts],
        "variant_labels": list(variant_labels),
    }
    if required_tool_families:
        body["required_tool_families"] = [list(family) for family in required_tool_families]
    return (
        "sha256:"
        + hashlib.sha256(b"doc2harness.direct-mock-execution-plan/1\x00" + canonical_json(body)).hexdigest()
    )


def build_direct_mock_execution_plan(scenario: DirectMockScenario, /) -> DirectMockExecutionPlan:
    if type(scenario) is not DirectMockScenario:
        raise ContractDocumentError("direct mock plan requires an exact scenario")
    recipe = scenario.recipe
    fixture = materialize_direct_mock_fixture(scenario)
    timeout = recipe.timeout_seconds if recipe.timeout_seconds is not None else 90
    digest = _plan_digest(
        scenario.definition.scenario_id,
        recipe.mode,
        recipe.entrypoint,
        recipe.omitted_flag,
        timeout,
        fixture.plan_digest,
        recipe.base_url_suffix,
        recipe.model_name,
        recipe.reject_substring,
        recipe.stale_pwd_probe,
        recipe.initialize_git,
        recipe.oracle_profile,
        recipe.tool_family,
        recipe.required_tool_families,
        recipe.request_limit,
        recipe.body_limit,
        recipe.required_facts,
        recipe.variant_labels,
    )
    return DirectMockExecutionPlan(
        scenario.definition.scenario_id,
        recipe.mode,
        recipe.entrypoint,
        recipe.omitted_flag,
        timeout,
        fixture,
        recipe.base_url_suffix,
        recipe.model_name,
        recipe.reject_substring,
        recipe.stale_pwd_probe,
        recipe.initialize_git,
        recipe.oracle_profile,
        recipe.tool_family,
        recipe.required_tool_families,
        recipe.request_limit,
        recipe.body_limit,
        recipe.required_facts,
        recipe.variant_labels,
        digest,
    )


__all__ = [
    "DirectMockExecutionPlan",
    "build_direct_mock_execution_plan",
]
