"""Validate and execute a YAML-defined doc2harness run.

A run must name an executable production backend in ``executor.command`` (or
through ``SBH_PRODUCTION_EXECUTOR``); there is no built-in stand-in that would
let a misconfigured run look like it scored something.  ``--dry-run`` validates
the complete plan and writes no model result; without it, each configured model
is invoked through the declared executor, respecting the configured concurrency
limit.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


class RunConfigError(ValueError):
    """Raised when a run configuration is incomplete or unsafe."""


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RunConfigError(
            "PyYAML is required for YAML run configs; install the run requirements"
        ) from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunConfigError(f"cannot read YAML config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunConfigError("top-level YAML value must be a mapping")
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunConfigError(f"{name} must be a mapping")
    return value


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _repo_path(repo_root: Path, value: Any, name: str) -> Path:
    raw = Path(_nonempty_string(value, name)).expanduser()
    return raw if raw.is_absolute() else repo_root / raw


def _catalog_ids(repo_root: Path) -> tuple[str, ...]:
    from doc2harness.benchmark.catalog import (
        load_catalog_sources,
        parse_catalog_root,
    )

    package = repo_root / "src" / "doc2harness"
    benchmark = package / "benchmark"
    root = parse_catalog_root((benchmark / "catalog_root.toml").read_bytes())
    resources = {
        root.taxonomy_resource: (benchmark / "taxonomy.toml").read_bytes(),
        **{
            shard.resource: (package / Path(*shard.resource.segments)).read_bytes()
            for shard in root.shards
        },
    }
    definitions = load_catalog_sources(
        root,
        resources,
        lambda module, export: __import__(module, fromlist=[export]).__dict__[export],
    )
    return tuple(item.scenario_id for item in definitions)


def _expand(value: str) -> str:
    return os.path.expandvars(value)


def _command(executor: dict[str, Any], *, allow_missing: bool) -> list[str]:
    configured = executor.get("command")
    if configured is None or configured == []:
        env_command = os.environ.get("SBH_PRODUCTION_EXECUTOR", "").strip()
        if not env_command:
            if allow_missing:
                return []
            raise RunConfigError(
                "executor.command is missing; set a production executor explicitly "
                "(SBH_PRODUCTION_EXECUTOR may provide one)"
            )
        configured = shlex.split(env_command)
    if isinstance(configured, str):
        configured = shlex.split(configured)
    if not isinstance(configured, list) or not configured or not all(
        isinstance(item, str) and item.strip() for item in configured
    ):
        raise RunConfigError("executor.command must be a non-empty string/list command")
    return [_expand(item) for item in configured]


def _validate(
    repo_root: Path,
    config_path: Path,
    document: dict[str, Any],
    required_models: tuple[str, ...] = (),
    required_count: int | None = None,
) -> dict[str, Any]:
    if document.get("schema") != "doc2harness.run-config/1":
        raise RunConfigError("schema must be doc2harness.run-config/1")
    run = _mapping(document.get("run"), "run")
    selection = _mapping(document.get("selection"), "selection")
    executor = _mapping(document.get("executor"), "executor")
    models_value = document.get("models")
    if not isinstance(models_value, list) or not models_value:
        raise RunConfigError("models must be a non-empty list")
    catalog = _catalog_ids(repo_root)
    requested = selection.get("mode", "all")
    if requested == "all":
        selected = catalog
    elif requested == "ids_file":
        ids_file = _repo_path(repo_root, selection.get("ids_file"), "selection.ids_file")
        try:
            selected = tuple(
                line.strip()
                for line in ids_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        except OSError as exc:
            raise RunConfigError(f"cannot read selection.ids_file: {ids_file}") from exc
    else:
        raise RunConfigError("selection.mode must be all or ids_file")
    if not selected or len(selected) != len(set(selected)):
        raise RunConfigError("selection must contain unique non-empty scenario IDs")
    if required_count is not None and len(selected) != required_count:
        raise RunConfigError(
            f"selection must contain exactly {required_count} scenario IDs, found {len(selected)}"
        )
    unknown = sorted(set(selected) - set(catalog))
    if unknown:
        raise RunConfigError(f"selection contains unknown scenario IDs: {unknown[:5]}")
    if required_models:
        names = tuple(_nonempty_string(_mapping(item, "model").get("id"), "model.id") for item in models_value)
        if names != required_models:
            raise RunConfigError(f"models must be exactly {list(required_models)}, found {list(names)}")
    command = _command(executor, allow_missing=bool(document.get("_dry_run")))
    output_dir = _repo_path(repo_root, run.get("output_dir", "run-results"), "run.output_dir")
    if not output_dir.is_relative_to(repo_root):
        raise RunConfigError("run.output_dir must remain inside the repository")
    concurrency = run.get("concurrency", 1)
    if type(concurrency) is not int or concurrency < 1:
        raise RunConfigError("run.concurrency must be a positive integer")
    return {
        "repo_root": str(repo_root),
        "config_path": str(config_path),
        "run": run,
        "selection": {"mode": requested, "count": len(selected), "ids": selected},
        "models": models_value,
        "executor": {"command": command, "output_dir": str(output_dir)},
    }


def _execute(plan: dict[str, Any], *, dry_run: bool) -> int:
    output_dir = Path(plan["executor"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = output_dir / "run-plan.json"
    manifest.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    selection_file = output_dir / "selected-scenarios.txt"
    selection_file.write_text(
        "".join(f"{scenario_id}\n" for scenario_id in plan["selection"]["ids"]),
        encoding="utf-8",
    )
    if dry_run:
        print(json.dumps({"status": "VALID", "selection_count": plan["selection"]["count"], "models": [item["id"] for item in plan["models"]]}, ensure_ascii=False))
        return 0
    command = plan["executor"]["command"]
    if not command:
        raise RunConfigError(
            "no production executor configured; set executor.command or "
            "SBH_PRODUCTION_EXECUTOR before a real run"
        )
    def run_model(model: Any) -> tuple[str, int]:
        model_map = _mapping(model, "model")
        model_id = _nonempty_string(model_map.get("id"), "model.id")
        environment = os.environ.copy()
        environment.update(
            {
                str(key): _expand(str(value))
                for key, value in _mapping(model_map.get("env", {}), "model.env").items()
            }
        )
        environment["SBH_RUN_CONFIG"] = plan["config_path"]
        environment["SBH_MODEL_ID"] = model_id
        environment["SBH_SCENARIO_COUNT"] = str(plan["selection"]["count"])
        environment["SBH_IDS_FILE"] = str(selection_file)
        environment["SBH_RUN_OUTPUT_DIR"] = plan["executor"]["output_dir"]
        environment["SBH_CATALOG_CONCURRENCY"] = str(
            plan["run"].get("catalog_concurrency", 1)
        )
        environment["SBH_CATALOG_TIMEOUT"] = str(
            plan["run"].get("catalog_timeout", 900)
        )
        print(
            f"[doc2harness] starting model={model_id} "
            f"scenarios={plan['selection']['count']}",
            flush=True,
        )
        result = subprocess.run(command, cwd=Path(plan["repo_root"]), env=environment, check=False)
        return model_id, result.returncode

    concurrency = min(int(plan["run"].get("concurrency", 1)), len(plan["models"]))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(run_model, model) for model in plan["models"]]
        for future in as_completed(futures):
            model_id, returncode = future.result()
            if returncode != 0:
                print(
                    f"[doc2harness] executor failed model={model_id} rc={returncode}",
                    file=sys.stderr,
                )
                return returncode
    print(json.dumps({"status": "COMPLETED", "selection_count": plan["selection"]["count"], "models": [item["id"] for item in plan["models"]]}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-models", default="")
    parser.add_argument("--require-count", type=int)
    args = parser.parse_args()
    try:
        repo_root = args.repo_root.resolve()
        config_path = args.config.resolve()
        sys.path.insert(0, str(repo_root / "src"))
        document = _load_yaml(config_path)
        document["_dry_run"] = args.dry_run
        plan = _validate(
            repo_root,
            config_path,
            document,
            tuple(item for item in args.require_models.split(",") if item),
            args.require_count,
        )
        return _execute(plan, dry_run=args.dry_run)
    except (OSError, RunConfigError, ImportError) as exc:
        print(f"error[SBH-RUN-CONFIG]: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
