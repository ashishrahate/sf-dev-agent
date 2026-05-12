"""Tests for the minimal-repo build script (Item 4).

Builds the bundle into a tmp directory and asserts the manifest matches:
  - allowlisted paths copied
  - blocklisted paths absent
  - README + .env.example templated (not copied from source)
  - pyproject.toml byte-identical to source

The validate step (`uv sync` + pytest --collect-only) isn't exercised in
unit tests — it requires network access and a fresh uv cache slot. Manual
runs via `python scripts/build_minimal_repo.py` cover it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# Add the scripts/ dir to path so the test can import the module directly.
import sys

sys.path.insert(
    0, str((Path(__file__).resolve().parent.parent / "scripts").resolve()),
)
import build_minimal_repo as bmr  # noqa: E402


@pytest.fixture
def src_root() -> Path:
    """Real source repo — the build script's natural target."""
    return Path(__file__).resolve().parent.parent


def test_build_copies_allowlisted_paths(
    src_root: Path, tmp_path: Path,
) -> None:
    """Every required allowlist entry appears under the output directory."""
    out = tmp_path / "min"
    manifest = bmr.build_minimal_repo(src_root, out)

    for rel in bmr.INCLUDE_PATHS:
        assert (out / rel).exists(), f"missing in bundle: {rel}"

    assert set(bmr.INCLUDE_PATHS).issubset(manifest["copied"])


def test_build_writes_templates(src_root: Path, tmp_path: Path) -> None:
    """README + .env.example are written from templates, not copied."""
    out = tmp_path / "min"
    bmr.build_minimal_repo(src_root, out)

    readme = (out / "README.md").read_text(encoding="utf-8")
    # The minimal README points back at the canonical repo for full context.
    assert "minimal distribution variant" in readme

    env = (out / ".env.example").read_text(encoding="utf-8")
    # Stripped variant references the wizard but not the full design notes.
    assert "sf-agent setup" in env
    assert "AGENT_WORKSPACE" not in env  # canonical-only knob


def test_build_excludes_blocklisted_paths(
    src_root: Path, tmp_path: Path,
) -> None:
    """The value of this script is what it leaves out."""
    out = tmp_path / "min"
    bmr.build_minimal_repo(src_root, out)

    for rel in bmr.EXCLUDED_PATHS:
        # Either entirely absent or empty — either is acceptable.
        assert not (out / rel).exists(), f"unexpected in bundle: {rel}"


def test_pyproject_byte_identical(src_root: Path, tmp_path: Path) -> None:
    """The bundle is a strict subset, not a transform — pyproject.toml in
    particular must match exactly so dependency pins stay stable."""
    out = tmp_path / "min"
    bmr.build_minimal_repo(src_root, out)
    src_bytes = (src_root / "pyproject.toml").read_bytes()
    dst_bytes = (out / "pyproject.toml").read_bytes()
    assert src_bytes == dst_bytes


def test_uv_lock_copied(src_root: Path, tmp_path: Path) -> None:
    """uv.lock pins the resolved versions; must travel verbatim."""
    out = tmp_path / "min"
    bmr.build_minimal_repo(src_root, out)
    assert (out / "uv.lock").exists()


def test_pycache_not_copied(
    src_root: Path, tmp_path: Path,
) -> None:
    """__pycache__ directories under src/ or tests/ shouldn't follow into
    the bundle. We can't guarantee they exist in source, but if they do
    the build must filter them."""
    out = tmp_path / "min"
    bmr.build_minimal_repo(src_root, out)
    leaked = list(out.rglob("__pycache__"))
    assert leaked == []


def test_build_overwrite_replaces_existing(
    src_root: Path, tmp_path: Path,
) -> None:
    """Default `overwrite=True` wipes and rebuilds — useful for repeatable
    CI runs."""
    out = tmp_path / "min"
    out.mkdir()
    (out / "stale.txt").write_text("leftover", encoding="utf-8")
    bmr.build_minimal_repo(src_root, out, overwrite=True)
    assert not (out / "stale.txt").exists()
    assert (out / "src").exists()


def test_build_overwrite_false_refuses_existing(
    src_root: Path, tmp_path: Path,
) -> None:
    out = tmp_path / "min"
    out.mkdir()
    with pytest.raises(FileExistsError):
        bmr.build_minimal_repo(src_root, out, overwrite=False)


def test_build_missing_source_raises(tmp_path: Path) -> None:
    """If the source repo path is bogus, fail loudly — don't silently
    produce an empty bundle."""
    with pytest.raises(FileNotFoundError):
        bmr.build_minimal_repo(tmp_path / "nope", tmp_path / "out")


def test_build_optional_license_skipped_when_missing(
    tmp_path: Path,
) -> None:
    """When a path in OPTIONAL_INCLUDE_PATHS isn't in the source tree, the
    build still succeeds without copying it."""
    # Construct a synthetic source root with only the required allowlist.
    src = tmp_path / "src_repo"
    src.mkdir()
    (src / "src").mkdir()
    (src / "tests").mkdir()
    (src / "pyproject.toml").write_text("[project]\nname='x'", encoding="utf-8")
    (src / "uv.lock").write_text("# lock", encoding="utf-8")
    # No LICENSE on purpose.

    out = tmp_path / "synthetic_out"
    manifest = bmr.build_minimal_repo(src, out)
    assert "LICENSE" not in manifest["copied"]
    assert (out / "README.md").exists()


def test_cli_no_validate(
    src_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end CLI smoke: --no-validate skips the heavyweight uv steps."""
    out = tmp_path / "cli_out"
    rc = bmr.main([
        "--source", str(src_root),
        "--out", str(out),
        "--no-validate",
    ])
    assert rc == 0
    log = capsys.readouterr().out
    assert "Skipped validation" in log
    assert (out / "src").exists()
