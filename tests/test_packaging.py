"""Build patent-checker with ``uv build`` and inspect the resulting archives.

These tests exercise the packaging configuration end to end (a real
``uv build`` run) instead of asserting on ``pyproject.toml`` text, so they
catch mistakes in ``force-include``, PEP 639 metadata, and sdist contents
that a text-only check would miss. The build runs once per test session
(module-scoped fixture) and writes to a pytest ``tmp_path``, never to the
repository's own ``dist/``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

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


def test_wheel_bundles_all_four_consent_notice_files(wheel_names: list[str]) -> None:
    """Both languages of both notices ship in the wheel, not just the English operator one."""
    for expected in (
        "patent_checker/notices/consent-notice.en.md",
        "patent_checker/notices/consent-notice.ja.md",
        "patent_checker/notices/operator-notice.en.md",
        "patent_checker/notices/operator-notice.ja.md",
    ):
        assert expected in wheel_names, expected


def test_wheel_declares_the_patent_checker_console_script(
    built_distributions: dict[str, Path], wheel_names: list[str]
) -> None:
    """``pip install``/``uvx`` exposes a ``patent-checker`` command."""
    prefix = _dist_info_prefix(wheel_names)
    assert f"{prefix}entry_points.txt" in wheel_names
    with zipfile.ZipFile(built_distributions["wheel"]) as archive:
        entry_points = archive.read(f"{prefix}entry_points.txt").decode("utf-8")
    lines = [line.strip() for line in entry_points.splitlines()]
    assert "[console_scripts]" in lines
    assert "patent-checker = patent_checker.cli.main:main" in lines


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


@pytest.fixture(scope="module")
def pyproject_data() -> dict[str, Any]:
    """``pyproject.toml`` を解析して辞書として返す。"""
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def server_json_data() -> dict[str, Any]:
    """``server.json`` を解析して辞書として返す。"""
    return json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))


def test_pyproject_classifiers_mark_production_stable_and_omit_license(
    pyproject_data: dict[str, Any],
) -> None:
    classifiers = pyproject_data["project"]["classifiers"]
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert not any(classifier.startswith("License ::") for classifier in classifiers), classifiers


def test_pyproject_keywords_include_registry_discovery_terms(
    pyproject_data: dict[str, Any],
) -> None:
    keywords = pyproject_data["project"]["keywords"]
    assert "mcp-server" in keywords
    assert "prior-art-search" in keywords


def test_server_json_is_valid_and_matches_pyproject_version(
    pyproject_data: dict[str, Any], server_json_data: dict[str, Any]
) -> None:
    project_version = pyproject_data["project"]["version"]

    assert server_json_data["name"] == "io.github.xhighhongo41/patent-checker"
    assert len(server_json_data["description"]) <= 100
    assert server_json_data["version"] == project_version

    packages = server_json_data["packages"]
    assert packages, "server.json must declare at least one package"
    for package in packages:
        if "version" in package:
            assert package["version"] == project_version, package

    oci_packages = [p for p in packages if p["registryType"] == "oci"]
    assert oci_packages, "server.json must declare an OCI package"
    for package in oci_packages:
        _, _, tag = package["identifier"].rpartition(":")
        assert tag == project_version, package["identifier"]


@pytest.mark.parametrize("readme_name", ["README.md", "README_ja.md"])
def test_readme_declares_mcp_registry_ownership(readme_name: str) -> None:
    lines = (REPO_ROOT / readme_name).read_text(encoding="utf-8").splitlines()[:20]
    assert any("mcp-name: io.github.xhighhongo41/patent-checker" in line for line in lines), lines


def test_wheel_metadata_declares_production_status_and_new_summary(
    built_distributions: dict[str, Path], wheel_names: list[str], pyproject_data: dict[str, Any]
) -> None:
    prefix = _dist_info_prefix(wheel_names)
    with zipfile.ZipFile(built_distributions["wheel"]) as archive:
        metadata = archive.read(f"{prefix}METADATA").decode("utf-8")
    assert "Classifier: Development Status :: 5 - Production/Stable" in metadata
    assert f"Summary: {pyproject_data['project']['description']}" in metadata


def test_dockerfile_declares_the_mcp_registry_server_name_label() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'io.modelcontextprotocol.server.name="io.github.xhighhongo41/patent-checker"' in (
        dockerfile
    )
