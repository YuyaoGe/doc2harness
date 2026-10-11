"""Execution boundary for child-process state scenarios."""
import hashlib
from dataclasses import dataclass
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError
from .contracts import FrozenValue, StateScenario
from .fixtures import StateFixturePlan, materialize_state_fixture

@dataclass(frozen=True, slots=True)
class StateExecutionPlan:
    scenario_id: str
    fixture: StateFixturePlan
    commands: tuple[str, ...]
    config_keys: tuple[str, ...]
    observed_files: tuple[str, ...]
    execution_parameters: tuple[tuple[str, FrozenValue], ...]
    timeout_seconds: int
    plan_digest: str

def build_state_execution_plan(scenario: StateScenario, /) -> StateExecutionPlan:
    if type(scenario) is not StateScenario:
        raise ContractDocumentError('state execution requires an exact scenario')
    recipe = scenario.recipe
    fixture = materialize_state_fixture(scenario)
    body = {'commands': recipe.commands, 'config_keys': recipe.config_keys, 'fixture_digest': fixture.plan_digest, 'observed_files': recipe.observed_files, 'scenario_id': scenario.definition.scenario_id, 'timeout_seconds': recipe.timeout_seconds}
    digest = 'sha256:' + hashlib.sha256(b'doc2harness.state-child-plan/1\x00' + canonical_json(body)).hexdigest()
    return StateExecutionPlan(scenario.definition.scenario_id, fixture, recipe.commands, recipe.config_keys, recipe.observed_files, recipe.execution_parameters, recipe.timeout_seconds, digest)
__all__ = ['StateExecutionPlan', 'build_state_execution_plan']
