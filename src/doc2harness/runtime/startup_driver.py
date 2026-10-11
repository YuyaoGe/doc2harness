"""Run one ``startup`` scenario against a real artifact in Docker."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from doc2harness.runtime.direct_mock_driver import _digest
from doc2harness.runtime.resource_limits import container_memory_flags


_MARKER = "SBH_STARTUP_RESULT="
_REQUIRES_PYTHON = re.compile(r"requires-python\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _declared_python_floor(artifact: Path) -> tuple[int, int] | None:
    """Read the artifact's declared lower Python bound without inventing one."""
    path = artifact / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _REQUIRES_PYTHON.search(text)
    if match is None:
        return None
    floors = [
        (int(major), int(minor))
        for major, minor in re.findall(r"(?:^|,)\s*>=\s*(\d+)\.(\d+)", match.group(1))
    ]
    return max(floors) if floors else None


def _old_python_wrapper(floor: tuple[int, int], probe: Path, entered: Path) -> str:
    """Make a PATH-first fake old python3 that records both probe and entry."""
    major, minor = floor
    fake = (major, minor - 1) if minor > 0 else (max(0, major - 1), 99)
    return (
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "args=(\"$@\")\n"
        "i=0\n"
        "while (( i < ${#args[@]} )); do\n"
        "  arg=\"${args[$i]}\"\n"
        "  case \"$arg\" in\n"
        "    -E|-I|-s|-S|-B|-u|-O|-OO) ((i += 1)); continue ;;\n"
        "    -V|--version)\n"
        f"      printf 'version\\n' >> {str(probe)!r}\n"
        f"      printf 'Python {fake[0]}.{fake[1]}\\n'\n"
        "      exit 0\n"
        "      ;;\n"
        "    -c)\n"
        "      code=\"${args[$((i + 1))]:-}\"\n"
        "      if [[ \"$code\" == *version* || \"$code\" == *VERSION* || \"$code\" == *hexversion* ]]; then\n"
        f"        printf 'version\\n' >> {str(probe)!r}\n"
        f"        printf '{fake[0]}.{fake[1]}\\n'\n"
        "        exit 0\n"
        "      fi\n"
        "      exit 88\n"
        "      ;;\n"
        "    --) ((i += 1)); break ;;\n"
        "    -*) exit 88 ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        "if (( i < ${#args[@]} )); then\n"
        "  case \"${args[$i]}\" in\n"
        "    *.py|*/entry.py)\n"
        f"      printf 'entry\\n' >> {str(entered)!r}\n"
        "      exit 86\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        "exit 88\n"
    )


def _probe_source() -> str:
    return r'''
import importlib, json, os, sys, traceback
sys.path.insert(0, "/sbh-artifact/src")
sys.path.insert(0, "/sbh-artifact")
module = None
import_error = False
for name in ("yuyao.agent_api", "yuyao.d2a_api", "yuyao.harness_api", "agent_api"):
    try:
        candidate = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", None) in {name, name.split(".")[0]}:
            continue
        import_error = True
        traceback.print_exc()
        continue
    except Exception:
        import_error = True
        traceback.print_exc()
        continue
    if callable(getattr(candidate, "create_session", None)):
        module = candidate
        break
result = {"std": module is not None, "ok": False, "tb": False, "import_tb": import_error}
if module is not None:
    try:
        session = module.create_session()
        result["ok"] = True
        close = getattr(session, "close", None)
        if callable(close):
            close()
    except Exception:
        result["tb"] = True
        traceback.print_exc()
print("SBH_STARTUP_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
'''


def _docker_probe(artifact: Path, home: Path, workspace: Path, env: dict[str, str], timeout: int, argv: list[str]) -> tuple[subprocess.CompletedProcess[str], bool]:
    image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
    command = [
        "docker", "run", *container_memory_flags(), "--rm", "--init", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{artifact}:/sbh-artifact:ro", "-v", f"{home}:/sbh-home:rw",
        "-v", f"{workspace}:/sbh-workspace:rw", "-e", "HOME=/sbh-home",
        # Construct in the writable workspace, exactly as every other driver
        # does.  Without ``-w`` the container starts in the read-only artifact
        # mount, so ``create_session()`` (whose cwd defaults to os.getcwd())
        # tries to initialise a per-cwd memory store under /sbh-artifact and
        # dies with a read-only-filesystem OSError at construction.  The
        # tolerant-startup points then read a "bare constructor traceback" that
        # is the mount's fault, not the product's.
        "-w", "/sbh-workspace",
        "-e", "YUYAO_HOME=/sbh-home/.yuyao", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONPATH=/sbh-artifact/src",
    ]
    for key, value in env.items():
        if key.startswith("D2A_"):
            command.extend(("-e", f"{key}={value}"))
    startup_path = env.get("D2A_STARTUP_PATH")
    if startup_path:
        command.extend(("-e", f"PATH={startup_path}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"))
    command.extend((image, *argv))
    try:
        return subprocess.run(command, cwd=workspace, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False), False
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, -1, exc.stdout or "", exc.stderr or ""), True


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.startup.adapter import build_startup_execution_plan
    from doc2harness.benchmark.harnesses.startup.contracts import StartupObservation, StartupProfile

    scenario_id = os.environ["SBH_SCENARIO_ID"]
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.startup." not in owner.module:
        raise RuntimeError("startup driver received a non-startup scenario")
    scenario = owner.scenario
    plan = build_startup_execution_plan(scenario)
    artifact = Path(os.environ["SBH_ARTIFACT_PATH"])
    recipe = scenario.recipe
    with tempfile.TemporaryDirectory(prefix="sbh-startup-production-") as temporary:
        root = Path(temporary)
        home, workspace = root / "home", root / "workspace"
        home.mkdir(); workspace.mkdir()
        if recipe.config_text is not None:
            config = home / ".yuyao" / "config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(recipe.config_text, encoding="utf-8")
        for relative, content in recipe.home_files:
            path = home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        env = dict(os.environ)
        env.update(dict(recipe.environment))
        result: dict[str, Any] = {"std": False, "ok": False, "tb": False, "import_tb": False}
        timed_out = False
        exact_entry = False
        path_selected = False
        if recipe.profile is StartupProfile.CLEAN_CONSTRUCT:
            completed, timed_out = _docker_probe(
                artifact, home, workspace, env, min(recipe.timeout_seconds, 300),
                ["python3", "-c", _probe_source()],
            )
            for line in str(completed.stdout or "").splitlines():
                if line.startswith(_MARKER):
                    try:
                        parsed = json.loads(line[len(_MARKER):])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        result.update(parsed)
            request_count = 0
            launcher_available = False
            fixture_verified = True
            declared_floor = None
            old_python_probed = False
            old_python_entered = False
            healthy_version_satisfies = False
        else:
            # A successful --help invocation proves only that a launcher exists.
            # It does not prove exact sibling selection, PATH fallback, or
            # ambient-environment isolation, so those facts remain unobserved.
            fixture = root / "launcher-artifact"
            shutil.copytree(artifact, fixture, symlinks=True)
            marker = workspace / "launcher-marker.json"
            entry = fixture / "entry.py"
            if entry.exists() or entry.is_symlink():
                entry.unlink()
            entry.write_text(
                "import json,sys\n"
                "json.dump({'argv': sys.argv[1:], 'entry': __file__, 'python': list(sys.version_info[:2])}, open('/sbh-workspace/launcher-marker.json','w'))\n"
                "raise SystemExit(73)\n",
                encoding="utf-8",
            )
            entry.chmod(0o700)
            task = workspace / "task.json"
            budget = workspace / "budget.json"
            task.write_text('{"task_id":"startup-driver","problem_statement":"controlled"}\n', encoding="utf-8")
            budget.write_text('{"max_steps":1}\n', encoding="utf-8")
            expected = [
                "--task-file", "/sbh-workspace/task.json",
                "--repo-dir", "/sbh-workspace",
                "--model-base-url", "http://127.0.0.1:9",
                "--model-name", "startup-driver-model",
                "--model-api-key", "startup-driver-key",
                "--budget-file", "/sbh-workspace/budget.json",
                "--out-dir", "/sbh-workspace/out",
            ]
            (workspace / "out").mkdir()
            declared_floor = _declared_python_floor(artifact)
            old_probe = workspace / "old-python-probe.log"
            old_entry = workspace / "old-python-entry.log"
            if recipe.profile is StartupProfile.PYTHON_MINIMUM and declared_floor is not None:
                startup_bin = fixture / "sbh-startup-bin"
                startup_bin.mkdir(parents=True, exist_ok=True)
                old_python = startup_bin / "python3"
                old_python.write_text(
                    _old_python_wrapper(
                        declared_floor,
                        Path("/sbh-workspace/old-python-probe.log"),
                        Path("/sbh-workspace/old-python-entry.log"),
                    ),
                    encoding="utf-8",
                )
                old_python.chmod(0o700)
                env["D2A_STARTUP_PATH"] = "/sbh-artifact/sbh-startup-bin"
            completed, timed_out = _docker_probe(
                fixture, home, workspace, env, min(recipe.timeout_seconds, 120),
                ["bash", "/sbh-artifact/run_agent.sh", *expected],
            )
            marker_value: dict[str, Any] = {}
            try:
                marker_value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                marker_value = {}
            request_count = None
            old_python_probed = old_probe.is_file() and bool(old_probe.read_text(encoding="utf-8").strip())
            old_python_entered = old_entry.is_file() and bool(old_entry.read_text(encoding="utf-8").strip())
            launcher_available = (
                bool(marker_value) and completed.returncode == 73
            ) or old_python_probed or old_python_entered
            fixture_verified = launcher_available
            exact_entry = marker_value.get("entry") == "/sbh-artifact/entry.py"
            path_selected = exact_entry and marker_value.get("argv") == expected
            healthy_version = marker_value.get("python")
            healthy_version_satisfies = bool(
                declared_floor is not None
                and isinstance(healthy_version, list)
                and len(healthy_version) == 2
                and all(type(item) is int for item in healthy_version)
                and tuple(healthy_version) >= tuple(declared_floor)
            )
        observation = StartupObservation(
            scenario_id=scenario_id,
            plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"],
            artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            backend_evidence_digest=_digest({"scenario_id": scenario_id, "rc": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}),
            result_present=bool(result) and not timed_out,
            import_traceback=bool(result.get("import_tb")),
            standard_interface=bool(result.get("std")),
            constructed=bool(result.get("ok")),
            constructor_traceback=bool(result.get("tb")),
            request_count=request_count,
            launcher_available=launcher_available,
            fixture_verified=fixture_verified,
            exact_entry=exact_entry,
            path_selected=path_selected,
            wrong_entry=False,
            declared_floor=declared_floor is not None,
            old_python_probed=old_python_probed,
            old_python_entered=old_python_entered,
            healthy_version_satisfies=healthy_version_satisfies,
            poison_selftest=False,
            ambient_path_isolated=False,
            ambient_home_isolated=False,
            process_tree_clean=not timed_out,
            workspace_clean=True,
        )
    judgement = owner.judge(observation)
    return {"observation": observation, "judgement": judgement,
            "module": owner.module, "definition_digest": definition_digest(owner.definition)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = _run_one()
    Path(os.environ["SBH_TYPED_EVENT_PATH"]).write_text(json.dumps({
        "schema": "doc2harness.typed-events/1", "source_digest": os.environ["SBH_SOURCE_DIGEST"],
        "task_digest": os.environ["SBH_TASK_DIGEST"], "budget_digest": os.environ["SBH_BUDGET_DIGEST"],
        "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"], "artifact_id": os.environ["SBH_ARTIFACT_ID"],
        "scenario_id": os.environ["SBH_SCENARIO_ID"], "scenario_module": result["module"],
        "definition_digest": result["definition_digest"],
        "events": [{"type": "startup-observation", "observation": asdict(result["observation"]),
                    "judgement": asdict(result["judgement"])}],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
