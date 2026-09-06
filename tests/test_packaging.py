"""Build patent-checker with ``uv build`` and inspect the resulting archives.

These tests exercise the packaging configuration end to end (a real
``uv build`` run) instead of asserting on ``pyproject.toml`` text, so they
catch mistakes in ``force-include``, PEP 639 metadata, and sdist contents
that a text-only check would miss. The build runs once per test session
(module-scoped fixture) and writes to a pytest ``tmp_path``, never to the
repository's own ``dist/``.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "patent-checker"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Run ``uv build`` once and return the paths to the wheel and sdist."""
    out_dir = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))
    assert len(wheels) == 1, wheels
    assert len(sdists) == 1, sdists
    return {"wheel": wheels[0], "sdist": sdists[0]}


@pytest.fixture(scope="module")
def wheel_names(built_distributions: dict[str, Path]) -> list[str]:
    """Return the file names archived in the built wheel."""
    with zipfile.ZipFile(built_distributions["wheel"]) as archive:
        return archive.namelist()


def _dist_info_prefix(names: list[str]) -> str:
    """Return the ``<name>-<version>.dist-info/`` prefix used by the wheel."""
    for name in names:
        if name.endswith(".dist-info/METADATA"):
            return name[: -len("METADATA")]
    raise AssertionError("no .dist-info/METADATA entry found in wheel")


def test_wheel_bundles_the_skill_and_notice_files(wheel_names: list[str]) -> None:
    for expected in (
        "patent_checker/_skill/patent-checker/SKILL.md",
        "patent_checker/_skill/patent-checker/references/consent-notice.en.md",
        "patent_checker/_skill/patent-checker/references/report-template.md",
        "patent_checker/notices/operator-notice.en.md",
    ):
        assert expected in wheel_names, expected
    prefix = _dist_info_prefix(wheel_names)
    assert f"{prefix}licenses/LICENSE" in wheel_names


def test_wheel_metadata_declares_the_license_expression_and_author(
    built_distributions: dict[str, Path], wheel_names: list[str]
) -> None:
    prefix = _dist_info_prefix(wheel_names)
    with zipfile.ZipFile(built_distributions["wheel"]) as archive:
        metadata = archive.read(f"{prefix}METADATA").decode("utf-8")
    assert "License-Expression: Apache-2.0" in metadata
    assert "Author: xhighhongo41" in metadata
    assert "Author-email" not in metadata


def test_bundled_skill_files_are_byte_identical_to_the_repository_copy(
    built_distributions: dict[str, Path],
) -> None:
    with zipfile.ZipFile(built_distributions["wheel"]) as archive:
        source_files = [path for path in SKILL_DIR.rglob("*") if path.is_file()]
        assert source_files, "skills/patent-checker is unexpectedly empty"
        for source_file in source_files:
            relative = source_file.relative_to(SKILL_DIR).as_posix()
            archive_name = f"patent_checker/_skill/patent-checker/{relative}"
            assert archive.read(archive_name) == source_file.read_bytes(), archive_name


def test_sdist_bundles_the_skill_source_tree(built_distributions: dict[str, Path]) -> None:
    with tarfile.open(built_distributions["sdist"], "r:gz") as archive:
        names = archive.getnames()
    assert any(name.endswith("skills/patent-checker/SKILL.md") for name in names), names
