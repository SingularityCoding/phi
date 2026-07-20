from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import SecretStr

from phi.doctor import (
    DoctorCheck,
    DoctorDependencies,
    DoctorReport,
    DoctorStatus,
    McpProbeResult,
    run_doctor,
)
from phi.mcp import McpDiagnostic
from phi.model import ModelConfig, ModelInfo
from phi.settings import Settings


def _settings(tmp_path: Path, *, secret: str = "test-secret") -> Settings:
    return Settings(
        base_url="https://proxy.example/v1",
        api_key=SecretStr(secret),
        default_model="model-a",
        request_timeout_seconds=10,
        session_dir=tmp_path / "sessions",
    )


def _write_skill(path: Path, *, valid: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    description = "description: Test workflow.\n" if valid else ""
    path.write_text(
        f"---\nname: {path.parent.name}\n{description}---\nUse the test workflow.\n",
        encoding="utf-8",
    )


def _write_agent(path: Path, *, valid: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "" if valid else "unexpected: true\n"
    path.write_text(
        "---\n"
        f"name: {path.parent.name}\n"
        "description: Test specialist.\n"
        f"{extra}"
        "---\n"
        "Handle the test task.\n",
        encoding="utf-8",
    )


def _checks(report: DoctorReport) -> dict[str, DoctorCheck]:
    return {check.id: check for check in report.checks}


async def test_standard_doctor_reports_complete_readiness_without_deep_probes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "AGENTS.md").write_text("Project instructions.\n", encoding="utf-8")
    _write_skill(tmp_path / ".phi" / "skills" / "test" / "SKILL.md")
    _write_agent(tmp_path / ".phi" / "agents" / "test" / "AGENT.md")
    mcp_path = tmp_path / ".phi" / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "configured": {
                        "command": sys.executable,
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)

    async def discover_models(config: ModelConfig) -> list[ModelInfo]:
        assert config.default_model == "model-a"
        return [ModelInfo("model-a", max_input_tokens=128_000, max_output_tokens=16_000)]

    async def unexpected_model_probe(config: ModelConfig) -> None:
        raise AssertionError(f"standard doctor called the Model probe for {config.default_model}")

    async def unexpected_mcp_probe(config, cwd: Path) -> McpProbeResult:
        raise AssertionError(f"standard doctor started MCP from {cwd}: {config}")

    report = await run_doctor(
        tmp_path,
        deep=False,
        dependencies=DoctorDependencies(
            settings_factory=lambda: settings,
            model_discovery=discover_models,
            model_probe=unexpected_model_probe,
            mcp_probe=unexpected_mcp_probe,
        ),
    )

    checks = _checks(report)
    assert report.healthy is True
    assert report.mode == "standard"
    assert tuple(checks) == (
        "configuration.settings",
        "configuration.model",
        "configuration.default-model",
        "workspace.cwd",
        "workspace.session-storage",
        "workspace.instructions",
        "workspace.skills",
        "workspace.agent-definitions",
        "workspace.mcp-configuration",
        "model.discovery",
        "model.default-model",
        "model.context-limit",
    )
    assert all(check.status is DoctorStatus.PASS for check in checks.values()), [
        (check.id, check.status, check.details) for check in checks.values()
    ]
    assert checks["workspace.instructions"].summary == "AGENTS.md loaded"
    assert checks["workspace.skills"].summary == "1 loaded"
    assert checks["workspace.agent-definitions"].summary == "1 loaded"
    assert checks["workspace.mcp-configuration"].summary == "1 enabled · 0 disabled"
    assert "input 128000" in checks["model.context-limit"].summary
    document = report.to_document()
    assert document["schema_version"] == 1
    assert document["healthy"] is True
    assert "test-secret" not in json.dumps(document)


async def test_doctor_warns_for_optional_configuration_and_unknown_context_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_skill(tmp_path / ".phi" / "skills" / "invalid" / "SKILL.md", valid=False)
    _write_agent(tmp_path / ".phi" / "agents" / "invalid" / "AGENT.md", valid=False)
    mcp_path = tmp_path / ".phi" / "mcp.json"
    mcp_path.write_text(
        json.dumps({"mcpServers": {"missing": {"command": "definitely-not-on-path"}}}),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)

    async def discover_models(config: ModelConfig) -> list[ModelInfo]:
        del config
        return [ModelInfo("model-a")]

    async def unused_model_probe(config: ModelConfig) -> None:
        del config

    async def unused_mcp_probe(config, cwd: Path) -> McpProbeResult:
        del config, cwd
        return McpProbeResult(0, (), ())

    report = await run_doctor(
        tmp_path,
        deep=False,
        dependencies=DoctorDependencies(
            settings_factory=lambda: settings,
            model_discovery=discover_models,
            model_probe=unused_model_probe,
            mcp_probe=unused_mcp_probe,
        ),
    )

    checks = _checks(report)
    assert report.healthy is True
    assert checks["workspace.skills"].status is DoctorStatus.WARN
    assert checks["workspace.agent-definitions"].status is DoctorStatus.WARN
    assert checks["workspace.mcp-configuration"].status is DoctorStatus.WARN
    assert checks["model.context-limit"].status is DoctorStatus.WARN
    remediation = checks["model.context-limit"].remediation
    assert remediation is not None
    assert "PHI_COMPACTION_MAX_INPUT_TOKENS" in remediation


async def test_doctor_keeps_independent_workspace_checks_when_model_settings_fail(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    secret = "arbitrary-provider-secret"
    settings = _settings(tmp_path, secret=secret).model_copy(
        update={"request_timeout_seconds": float("inf")}
    )
    discovery_called = False

    async def unexpected_discovery(config: ModelConfig) -> list[ModelInfo]:
        del config
        nonlocal discovery_called
        discovery_called = True
        return []

    async def unused_model_probe(config: ModelConfig) -> None:
        del config

    async def unused_mcp_probe(config, cwd: Path) -> McpProbeResult:
        del config, cwd
        return McpProbeResult(0, (), ())

    report = await run_doctor(
        tmp_path,
        deep=False,
        dependencies=DoctorDependencies(
            settings_factory=lambda: settings,
            model_discovery=unexpected_discovery,
            model_probe=unused_model_probe,
            mcp_probe=unused_mcp_probe,
        ),
    )

    checks = _checks(report)
    assert report.healthy is False
    assert checks["configuration.model"].status is DoctorStatus.FAIL
    assert checks["workspace.cwd"].status is DoctorStatus.PASS
    assert checks["model.discovery"].status is DoctorStatus.SKIP
    assert checks["model.discovery"].blocked_by == ("configuration.model",)
    assert discovery_called is False
    assert secret not in json.dumps(report.to_document())


async def test_deep_doctor_probes_model_and_reports_mcp_degradation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    mcp_path = tmp_path / ".phi" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {"command": sys.executable},
                    "bad": {"command": sys.executable},
                }
            }
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    model_probes: list[str] = []
    mcp_probes: list[Path] = []

    async def discover_models(config: ModelConfig) -> list[ModelInfo]:
        del config
        return [ModelInfo("model-a", max_input_tokens=64_000)]

    async def probe_model(config: ModelConfig) -> None:
        model_probes.append(config.default_model)

    async def probe_mcp(config, cwd: Path) -> McpProbeResult:
        assert set(config.servers) == {"good", "bad"}
        mcp_probes.append(cwd)
        return McpProbeResult(
            enabled_count=2,
            connected=(("good", 3),),
            diagnostics=(McpDiagnostic("bad", "handshake failed"),),
        )

    report = await run_doctor(
        tmp_path,
        deep=True,
        dependencies=DoctorDependencies(
            settings_factory=lambda: settings,
            model_discovery=discover_models,
            model_probe=probe_model,
            mcp_probe=probe_mcp,
        ),
    )

    checks = _checks(report)
    assert report.mode == "deep"
    assert report.healthy is True
    assert model_probes == ["model-a"]
    assert mcp_probes == [tmp_path]
    assert checks["deep.model-request"].status is DoctorStatus.PASS
    assert checks["deep.mcp-servers"].status is DoctorStatus.WARN
    assert checks["deep.mcp-servers"].summary == "1 of 2 connected"
    assert "handshake failed" in checks["deep.mcp-servers"].details[0]


async def test_deep_model_probe_failure_is_fatal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = _settings(tmp_path)

    async def discover_models(config: ModelConfig) -> list[ModelInfo]:
        del config
        return [ModelInfo("model-a", max_input_tokens=64_000)]

    async def failed_model_probe(config: ModelConfig) -> None:
        del config
        raise RuntimeError("streaming response failed")

    async def probe_mcp(config, cwd: Path) -> McpProbeResult:
        del config, cwd
        return McpProbeResult(0, (), ())

    report = await run_doctor(
        tmp_path,
        deep=True,
        dependencies=DoctorDependencies(
            settings_factory=lambda: settings,
            model_discovery=discover_models,
            model_probe=failed_model_probe,
            mcp_probe=probe_mcp,
        ),
    )

    check = _checks(report)["deep.model-request"]
    assert report.healthy is False
    assert check.status is DoctorStatus.FAIL
    assert check.remediation is not None
