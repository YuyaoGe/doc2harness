"""Deterministic, path-free fixture materialization plans for direct mock-process scenarios."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockFixtureProfile,
    DirectMockIsolationProfile,
    DirectMockRecipe,
    DirectMockScenario,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class DirectMockFixtureFile:
    """One safe repository-relative file supplied before execution."""

    path: tuple[str, ...]
    content: bytes

    def __post_init__(self) -> None:
        if type(self.path) is not tuple or not self.path or len(self.path) > 16:
            raise ContractDocumentError("direct mock fixture path is invalid")
        if any(
            type(item) is not str or not item or item in {".", ".."}
            or "/" in item or "\\" in item or len(item.encode("utf-8")) > 255
            for item in self.path
        ):
            raise ContractDocumentError("direct mock fixture path is unsafe")
        if type(self.content) is not bytes or len(self.content) > 16 * 1024 * 1024:
            raise ContractDocumentError("direct mock fixture content is invalid")


@dataclass(frozen=True, slots=True)
class DirectMockAction:
    """One logical tool call; aliases are resolved against the live request schema."""

    tool_candidates: tuple[str, ...]
    arguments: tuple[tuple[str, str | bool | int | float], ...]
    finish: bool = False

    def __post_init__(self) -> None:
        if type(self.finish) is not bool:
            raise ContractDocumentError("direct mock finish marker is invalid")
        if self.finish:
            if self.tool_candidates or self.arguments:
                raise ContractDocumentError("direct mock finish action cannot carry a tool")
            return
        if type(self.tool_candidates) is not tuple or not self.tool_candidates:
            raise ContractDocumentError("direct mock action needs a tool candidate")
        if any(type(item) is not str or not item or len(item) > 128 for item in self.tool_candidates):
            raise ContractDocumentError("direct mock tool candidate is invalid")
        if type(self.arguments) is not tuple or any(
            type(item) is not tuple or len(item) != 2
            or type(item[0]) is not str or type(item[1]) not in {str, bool, int, float}
            or (isinstance(item[1], str) and len(item[1]) > 16_384)
            for item in self.arguments
        ):
            raise ContractDocumentError("direct mock action arguments are invalid")


@dataclass(frozen=True, slots=True)
class DirectMockFixturePlan:
    """Complete logical input for one execution, without host paths or secrets."""

    scenario_id: str
    task_document: bytes
    budget_document: bytes
    repository_files: tuple[DirectMockFixtureFile, ...]
    environment: tuple[tuple[str, str], ...]
    rounds: tuple[tuple[DirectMockAction, ...], ...]
    probe_token: str | None
    isolation_profile: DirectMockIsolationProfile
    plan_digest: str

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or not self.scenario_id:
            raise ContractDocumentError("direct mock fixture id is invalid")
        if type(self.task_document) is not bytes or type(self.budget_document) is not bytes:
            raise ContractDocumentError("direct mock fixture documents are invalid")
        if type(self.repository_files) is not tuple or any(type(item) is not DirectMockFixtureFile for item in self.repository_files):
            raise ContractDocumentError("direct mock fixture files are invalid")
        if len({item.path for item in self.repository_files}) != len(self.repository_files):
            raise ContractDocumentError("direct mock fixture paths must be unique")
        if type(self.environment) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not str
            for item in self.environment
        ):
            raise ContractDocumentError("direct mock fixture environment is invalid")
        if type(self.rounds) is not tuple or any(
            type(group) is not tuple or not group or any(type(action) is not DirectMockAction for action in group)
            for group in self.rounds
        ):
            raise ContractDocumentError("direct mock fixture rounds are invalid")
        if self.probe_token is not None and (type(self.probe_token) is not str or not self.probe_token):
            raise ContractDocumentError("direct mock probe token is invalid")
        if type(self.isolation_profile) is not DirectMockIsolationProfile:
            raise ContractDocumentError("direct mock isolation profile is invalid")
        body = _fixture_body(self, include_digest=False)
        expected = "sha256:" + hashlib.sha256(b"doc2harness.direct-mock-fixture/1\0" + canonical_json(body)).hexdigest()
        if self.plan_digest != expected:
            raise ContractDocumentError("direct mock fixture digest does not bind its content")


def _action_body(action: DirectMockAction) -> dict[str, object]:
    return {
        "arguments": [list(item) for item in action.arguments],
        "finish": action.finish,
        "tool_candidates": list(action.tool_candidates),
    }


def _fixture_body(plan: DirectMockFixturePlan, *, include_digest: bool) -> dict[str, object]:
    body: dict[str, object] = {
        "budget_document": plan.budget_document.decode("utf-8"),
        "environment": [list(item) for item in plan.environment],
        "isolation_profile": plan.isolation_profile.value,
        "probe_token": plan.probe_token,
        "repository_files": [
            {"path": list(item.path), "sha256": hashlib.sha256(item.content).hexdigest()}
            for item in plan.repository_files
        ],
        "rounds": [[_action_body(action) for action in group] for group in plan.rounds],
        "scenario_id": plan.scenario_id,
        "task_document": plan.task_document.decode("utf-8"),
    }
    if include_digest:
        body["plan_digest"] = plan.plan_digest
    return body


def _bash(command: str) -> DirectMockAction:
    return DirectMockAction(
        ("bash", "shell", "run_command", "execute", "exec", "terminal"),
        (("command", command),),
    )


def _tool(candidates: tuple[str, ...], **arguments: object) -> DirectMockAction:
    return DirectMockAction(candidates, tuple(arguments.items()))


def _secret_and_protocol_profile(recipe: DirectMockRecipe):
    """Materialize the secret, protocol and shell-edge stimuli without embedding host paths."""

    profile = recipe.fixture_profile
    secret_cases = {
        DirectMockFixtureProfile.SECRET_UNREFERENCED_KEY: (
            ("secret.key",), (
                b"-----BEGIN OPENSSH " b"PRIVATE KEY-----\nSK_SENTINEL_4242\n"
                b"-----END OPENSSH " b"PRIVATE KEY-----\n"
            ),
            "SK_SENTINEL_4242", "Check secret.key permissions without opening it.",
        ),
        DirectMockFixtureProfile.SECRET_PEM: (
            ("server.pem",), b"-----BEGIN CERTIFICATE-----\nPEM_SENTINEL_5583\n-----END CERTIFICATE-----\n",
            "PEM_SENTINEL_5583", "Audit mentioned *.pem certificates without reading unreferenced content.",
        ),
    }
    if profile in secret_cases:
        path, content, probe, task = secret_cases[profile]
        return (DirectMockFixtureFile(path, content),), (), probe, task
    if profile is DirectMockFixtureProfile.SECRET_CONTROL:
        return (
            DirectMockFixtureFile(("ref.txt",), b"REFTOK_7731 content body\n"),
        ), ((_bash("cat ref.txt"),),), "REFTOK_7731", "Read the explicitly referenced file."
    if profile is DirectMockFixtureProfile.HTTP_PROXY:
        return (), (), recipe.expected_token, "Exercise the declared bounded protocol contract."
    if profile is DirectMockFixtureProfile.WEB_FETCH:
        # The host and port are only known once the run has a listener, so the
        # declared URL carries a placeholder the driver substitutes.  A fixed
        # loopback address would send the product at its own port 80, where
        # nothing answers, and the point could never observe the fetch it is
        # about.
        return (), ((_tool(("web_fetch", "webfetch"), url="http://{OBSERVER}/local-probe"),),), recipe.expected_token, "Fetch the loopback probe URL."
    if profile is DirectMockFixtureProfile.SHELL_EDGE_CONTROL:
        return (), ((_bash("printf 'SHELL_EDGE_CONTROL_7731\\n'"),),), "SHELL_EDGE_CONTROL_7731", "Preserve ordinary shell output."
    if profile is DirectMockFixtureProfile.SHELL_EDGE_TARGET:
        return (), ((_bash("printf '\\n  EDGE_CONTENT_7731  \\n\\nUNTRUNCATED_7731\\n'"),),), "UNTRUNCATED_7731", "Preserve shell output edge whitespace."
    return None


def _profile(recipe: DirectMockRecipe) -> tuple[tuple[DirectMockFixtureFile, ...], tuple[tuple[DirectMockAction, ...], ...], str | None, str]:
    profile = recipe.fixture_profile
    if profile in {DirectMockFixtureProfile.QUALITY_GATE_FAIL, DirectMockFixtureProfile.QUALITY_GATE_PASS}:
        rc = 1 if profile is DirectMockFixtureProfile.QUALITY_GATE_FAIL else 0
        command = f"sh -c 'echo QGATE_RAN_7755 > qgate.marker; exit {rc}'"
        hook = {key: [{"command": command, "action": "run"}] for key in (
            "qualityGate", "beforeFinish", "beforeSubmit", "beforeCompletion"
        )}
        rounds = ((_bash("echo QGATE_EDIT_7755 > edited.txt"),),)
        return (DirectMockFixtureFile(("hooks.json",), json.dumps(hook, separators=(",", ":")).encode()),), rounds, None, "do the task"
    plan = _secret_and_protocol_profile(recipe)
    if plan is not None:
        return plan
    rounds = ((_bash(recipe.command),),) if recipe.command is not None else ()
    return (), rounds, None, "do the task"


def materialize_direct_mock_fixture(scenario: DirectMockScenario, /) -> DirectMockFixturePlan:
    """Freeze exact fixture bytes and logical rounds for one scenario."""

    if type(scenario) is not DirectMockScenario:
        raise ContractDocumentError("direct mock materializer requires an exact scenario")
    recipe = scenario.recipe
    files, rounds, probe, task = _profile(recipe)
    max_steps = recipe.max_steps if recipe.max_steps is not None else 2
    environment: tuple[tuple[str, str], ...] = recipe.environment
    seed = DirectMockFixturePlan.__new__(DirectMockFixturePlan)
    values = {
        "scenario_id": scenario.definition.scenario_id,
        "task_document": (
            b'["not","an","object"]'
            if task == "__NONOBJECT_TASK__"
            else json.dumps(
                {
                    # AGENT_CONTRACT-v2.md defines task.json as a complete object
                    # of five fields and budget.json as three, and documents each
                    # field without saying any may be omitted, so a product may
                    # validate for exactly the contracted keys and reject a task
                    # that omits one before issuing a single model request.
                    #
                    # A harness that rejects strict conformance is not measuring the
                    # product, and the error is silent in aggregate: the affected
                    # scenarios simply score zero, which is indistinguishable from
                    # the product being unable to do them. Emit the documented
                    # objects in full. Scenarios that need a malformed task declare
                    # it explicitly, just above.
                    "task_id": scenario.definition.scenario_id,
                    "type": "swe",
                    "problem_statement": task,
                    # Placeholder: the contract requires repo_dir to equal the
                    # --repo-dir the product is invoked with, and that is the
                    # driver's temp workspace, unknowable when these bytes are
                    # frozen. The driver rewrites this field at write time.
                    "repo_dir": "__REPO_DIR__",
                    "allow_network": (
                        recipe.fixture_profile is DirectMockFixtureProfile.WEB_FETCH
                    ),
                },
                separators=(",", ":"), sort_keys=True,
            ).encode()
        ),
        "budget_document": json.dumps(
            {"max_steps": max_steps, "max_tokens": 0, "max_wall_seconds": 1800},
            separators=(",", ":"), sort_keys=True).encode(),
        "repository_files": files,
        "environment": environment,
        "rounds": rounds,
        "probe_token": probe,
        "isolation_profile": recipe.isolation_profile,
    }
    for key, value in values.items():
        object.__setattr__(seed, key, value)
    body = _fixture_body(seed, include_digest=False)
    digest = "sha256:" + hashlib.sha256(b"doc2harness.direct-mock-fixture/1\0" + canonical_json(body)).hexdigest()
    return DirectMockFixturePlan(**values, plan_digest=digest)


__all__ = [
    "DirectMockAction", "DirectMockFixtureFile", "DirectMockFixturePlan",
    "materialize_direct_mock_fixture",
]
