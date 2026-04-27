"""Unit tests for the system-prerequisite check (`sf-agent doctor`).

No real subprocess calls — every probe is mocked via monkeypatch on
`shutil.which` + `subprocess.run`. No real env reads — `monkeypatch.setenv`
controls what `check_llm_key` sees.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable

import pytest

from sf_dev_agent.doctor import (
    LLM_KEY_LABELS,
    TOOL_PROBES,
    CheckResult,
    Status,
    ToolProbe,
    all_required_passing,
    check_llm_key,
    doctor,
    main,
    meets_min,
    parse_version,
    probe_tool,
    render_results,
    run_all_checks,
)

# ---------------------------------------------------------------------------
# Version parsing + comparison
# ---------------------------------------------------------------------------

def test_parse_version_simple() -> None:
    assert parse_version("Python 3.12.6") == (3, 12, 6)


def test_parse_version_two_segments() -> None:
    assert parse_version("uv 0.4.18 (1234 abcd)") == (0, 4, 18)


def test_parse_version_v_prefixed() -> None:
    """Node prints `v18.20.4` — leading `v` shouldn't trip the regex."""
    assert parse_version("v18.20.4") == (18, 20, 4)


def test_parse_version_complex_string() -> None:
    """sf CLI's --version is a multi-line block with a version embedded."""
    sf_output = (
        "@salesforce/cli/2.62.6 win32-x64 node-v20.18.0\n"
        "Use 'sf' for fewer keystrokes."
    )
    assert parse_version(sf_output) == (2, 62, 6)


def test_parse_version_none_on_garbage() -> None:
    assert parse_version("this has no version") is None
    assert parse_version("") is None


def test_meets_min_handles_padding() -> None:
    assert meets_min((3, 12), (3, 12)) is True
    assert meets_min((3, 12, 5), (3, 12)) is True
    assert meets_min((3, 11, 9), (3, 12)) is False
    assert meets_min((3, 12), (3, 12, 0)) is True
    assert meets_min(None, (3, 12)) is False
    assert meets_min((3, 12), None) is True
    assert meets_min(None, None) is True


# ---------------------------------------------------------------------------
# Tool probe — monkeypatched subprocess
# ---------------------------------------------------------------------------

def _probe(name: str = "test", binary: str = "fake",
           min_version: tuple[int, ...] | None = (1, 0),
           required: bool = True) -> ToolProbe:
    return ToolProbe(
        name=name, binary=binary, version_args=("--version",),
        min_version=min_version, required=required,
        rationale="testing", install_per_os={"linux": "fake-install"},
    )


def _patch_which(monkeypatch: pytest.MonkeyPatch, *, found_binaries: Iterable[str]) -> None:
    """Make shutil.which return a path for these binaries, None otherwise."""
    found = set(found_binaries)
    monkeypatch.setattr(
        "sf_dev_agent.doctor.shutil.which",
        lambda b: f"/usr/bin/{b}" if b in found else None,
    )


def _patch_subproc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    raises: Exception | None = None,
) -> None:
    def fake_run(*args, **kwargs):  # noqa: ANN001
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=stdout, stderr=stderr,
        )
    monkeypatch.setattr("sf_dev_agent.doctor.subprocess.run", fake_run)


def test_probe_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, found_binaries=[])
    result = probe_tool(_probe(), os_name="linux")
    assert result.status == Status.MISSING
    assert result.install_command == "fake-install"


def test_probe_tool_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, found_binaries=["fake"])
    _patch_subproc(monkeypatch, stdout="fake 1.5.0")
    result = probe_tool(_probe(), os_name="linux")
    assert result.status == Status.OK
    assert result.version == "1.5.0"
    assert result.install_command is None


def test_probe_tool_outdated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, found_binaries=["fake"])
    _patch_subproc(monkeypatch, stdout="fake 0.9.0")
    result = probe_tool(_probe(min_version=(1, 0)), os_name="linux")
    assert result.status == Status.OUTDATED
    assert result.version == "0.9.0"
    assert result.detail and "1.0" in result.detail
    assert result.install_command == "fake-install"


def test_probe_tool_no_min_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """If min_version=None, any found binary passes."""
    _patch_which(monkeypatch, found_binaries=["fake"])
    _patch_subproc(monkeypatch, stdout="fake 0.1.0")
    result = probe_tool(_probe(min_version=None), os_name="linux")
    assert result.status == Status.OK


def test_probe_tool_unparseable_version_still_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binary responds but no version pattern in output → OK with first line."""
    _patch_which(monkeypatch, found_binaries=["fake"])
    _patch_subproc(monkeypatch, stdout="installed; everything is fine")
    result = probe_tool(_probe(), os_name="linux")
    assert result.status == Status.OK
    assert result.version == "installed; everything is fine"


def test_probe_tool_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, found_binaries=["fake"])
    _patch_subproc(
        monkeypatch,
        raises=subprocess.TimeoutExpired(cmd="fake --version", timeout=10),
    )
    result = probe_tool(_probe(), os_name="linux")
    assert result.status == Status.ERROR
    assert result.detail and "TimeoutExpired" in result.detail


def test_probe_tool_uses_per_os_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OS lookup falls back to 'other' when requested OS isn't known."""
    p = ToolProbe(
        name="x", binary="x", version_args=("--version",),
        min_version=(1, 0), required=True, rationale="r",
        install_per_os={"linux": "linux-cmd", "other": "other-cmd"},
    )
    _patch_which(monkeypatch, found_binaries=[])
    linux_result = probe_tool(p, os_name="linux")
    assert linux_result.install_command == "linux-cmd"

    bsd_result = probe_tool(p, os_name="freebsd")
    assert bsd_result.install_command == "other-cmd"


# ---------------------------------------------------------------------------
# LLM key check
# ---------------------------------------------------------------------------

def test_check_llm_key_none_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    result = check_llm_key()
    assert result.status == Status.MISSING
    assert result.required is True


def test_check_llm_key_one_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-fake")
    result = check_llm_key()
    assert result.status == Status.OK
    assert "Gemini" in (result.version or "")


def test_check_llm_key_empty_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "   ")
    result = check_llm_key()
    assert result.status == Status.MISSING


def test_check_llm_key_multiple_set_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    result = check_llm_key()
    assert result.status == Status.OK
    assert "Gemini" in (result.version or "")
    assert "OpenAI" in (result.version or "")


# ---------------------------------------------------------------------------
# run_all_checks + verdict
# ---------------------------------------------------------------------------

def test_run_all_checks_returns_one_per_probe_plus_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, found_binaries=[])
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    results = run_all_checks(os_name="linux")
    assert len(results) == len(TOOL_PROBES) + 1
    # The LLM-key check is the last entry by convention.
    assert results[-1].name == "LLM API key"


def test_all_required_passing_strict() -> None:
    ok_required = CheckResult(
        name="r", status=Status.OK, version="x", rationale="r",
        required=True, install_command=None,
    )
    missing_required = CheckResult(
        name="m", status=Status.MISSING, version=None, rationale="r",
        required=True, install_command="i",
    )
    missing_optional = CheckResult(
        name="o", status=Status.MISSING, version=None, rationale="r",
        required=False, install_command="i",
    )
    assert all_required_passing([ok_required, missing_optional]) is True
    assert all_required_passing([ok_required, missing_required]) is False
    assert all_required_passing([]) is True


# ---------------------------------------------------------------------------
# Rendering — make sure no state crashes the table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", list(Status))
def test_render_results_handles_every_status(status: Status) -> None:
    """The rich Table builder must not raise for any status combination."""
    result = CheckResult(
        name="x", status=status, version="1.0", rationale="r",
        required=True, install_command="i", detail="d",
    )
    table = render_results([result])
    # Smoke: must be a Table with one row.
    assert table.row_count == 1


def test_render_results_handles_optional_missing() -> None:
    """An optional-missing result renders yellow (not red)."""
    result = CheckResult(
        name="opt", status=Status.MISSING, version=None, rationale="r",
        required=False, install_command="i",
    )
    table = render_results([result])
    assert table.row_count == 1


# ---------------------------------------------------------------------------
# doctor() exit code
# ---------------------------------------------------------------------------

def test_doctor_exits_zero_when_everything_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(
        monkeypatch,
        found_binaries=[p.binary for p in TOOL_PROBES],
    )
    _patch_subproc(monkeypatch, stdout="anything 99.99.99")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-fake")
    for env in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}:
        monkeypatch.delenv(env, raising=False)

    assert doctor(install=False) == 0


def test_doctor_exits_one_when_required_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, found_binaries=[])
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)

    assert doctor(install=False) == 1


def test_doctor_exits_zero_when_only_optional_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required pass + optional fail → still zero exit (warning only)."""
    required_bins = {p.binary for p in TOOL_PROBES if p.required}
    _patch_which(monkeypatch, found_binaries=required_bins)
    _patch_subproc(monkeypatch, stdout="x 99.0.0")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-fake")
    for env in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}:
        monkeypatch.delenv(env, raising=False)

    assert doctor(install=False) == 0


def test_doctor_install_flag_does_not_change_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, found_binaries=[])
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)

    assert doctor(install=True) == 1


# ---------------------------------------------------------------------------
# main() argv parsing
# ---------------------------------------------------------------------------

def test_main_with_no_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, found_binaries=[])
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    assert main([]) == 1


def test_main_with_install_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, found_binaries=[])
    for env in LLM_KEY_LABELS:
        monkeypatch.delenv(env, raising=False)
    assert main(["--install"]) == 1
