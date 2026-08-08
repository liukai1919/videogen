import json
from pathlib import Path

from videogen_service.config import ServiceConfig
from videogen_service.preflight import Probe, format_checks, has_failure, run_checks
from tests.test_api import make_config

# make_config points t2v/i2v/story at nodes 1 and 2.
ALL_NODES = {
    "1": {"class_type": "X", "inputs": {"text": "", "timeline": ""}},
    "2": {"class_type": "Y", "inputs": {"image": ""}},
}


def reachable(url: str) -> Probe:
    if url.endswith("/api/tags"):
        return Probe(json.dumps({"models": [{"name": "qwen3.6:27b"}]}).encode(), None)
    return Probe(b"{}", None)


def unreachable(url: str) -> Probe:
    return Probe(None, "Connection refused")


def workflow_config(tmp_path: Path, workflow: object) -> ServiceConfig:
    config = make_config(tmp_path)
    for settings in config.modes.values():
        settings.workflow_file.write_text(
            json.dumps(workflow), encoding="utf-8"
        )
    return config


def status_of(checks: list[tuple[str, str, str]] | list, name: str) -> str:
    return next(check.status for check in checks if check.name == name)


def test_a_healthy_machine_passes_every_check(tmp_path: Path) -> None:
    config = workflow_config(tmp_path, ALL_NODES)

    checks = run_checks(config, probe=reachable)

    assert not has_failure(checks)
    assert status_of(checks, "ComfyUI") == "ok"
    assert status_of(checks, "Ollama") == "ok"
    assert "8020" in format_checks(checks)


def test_services_that_are_not_up_yet_only_warn(tmp_path: Path) -> None:
    # ComfyUI and Ollama are routinely started after this service; a warning
    # tells the operator why a render would fail without refusing to boot.
    config = workflow_config(tmp_path, ALL_NODES)

    checks = run_checks(config, probe=unreachable)

    assert not has_failure(checks)
    assert status_of(checks, "ComfyUI") == "warn"
    assert status_of(checks, "Ollama") == "warn"


def test_a_missing_workflow_is_a_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    checks = run_checks(config, probe=reachable)

    assert has_failure(checks)
    assert status_of(checks, "t2v workflow") == "fail"


def test_a_ui_format_workflow_is_caught_before_the_first_render(
    tmp_path: Path,
) -> None:
    config = workflow_config(tmp_path, {"nodes": [], "links": []})

    checks = run_checks(config, probe=reachable)

    assert has_failure(checks)
    assert "Export (API)" in next(
        check.detail for check in checks if check.name == "t2v workflow"
    )


def test_a_pointer_to_a_node_that_is_not_there_is_caught(tmp_path: Path) -> None:
    # config.example.yaml points at node ids from a specific export; a workflow
    # re-exported with different ids would otherwise fail mid-render.
    config = workflow_config(tmp_path, {"999": {"class_type": "X", "inputs": {}}})

    checks = run_checks(config, probe=reachable)

    assert has_failure(checks)
    assert "1.text" in next(
        check.detail for check in checks if check.name == "t2v workflow"
    )


def test_an_ollama_without_the_configured_model_warns(tmp_path: Path) -> None:
    config = workflow_config(tmp_path, ALL_NODES)

    def other_model(url: str) -> Probe:
        if url.endswith("/api/tags"):
            return Probe(json.dumps({"models": [{"name": "llama3:8b"}]}).encode(), None)
        return Probe(b"{}", None)

    checks = run_checks(config, probe=other_model)

    assert status_of(checks, "Ollama") == "warn"
    assert "ollama pull" in next(
        check.detail for check in checks if check.name == "Ollama"
    )


def test_a_disabled_script_feature_stops_asking_for_ollama(tmp_path: Path) -> None:
    config = workflow_config(tmp_path, ALL_NODES)
    config = config.model_copy(
        update={"script": config.script.model_copy(update={"enabled": False})}
    )

    checks = run_checks(config, probe=unreachable)

    assert status_of(checks, "Ollama") == "ok"
    assert status_of(checks, "yt-dlp") == "ok"
