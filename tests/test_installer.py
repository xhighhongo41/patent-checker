"""Tests for the ``patent_checker.installer`` package.

# --- Skill source resolution ---
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patent_checker.installer.errors import InstallerError
from patent_checker.installer.skill import skill_source

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_skill_source_prefers_the_wheel_bundled_copy_when_present(tmp_path: Path) -> None:
    package_dir = tmp_path / "patent_checker"
    bundled = package_dir / "_skill" / "patent-checker"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("bundled\n", encoding="utf-8")

    result = skill_source(package_dir=package_dir)

    assert result == bundled


def test_skill_source_falls_back_to_the_checkout_layout_when_no_bundled_copy_exists(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    package_dir = repo_root / "patent_checker"
    checkout = repo_root / "skills" / "patent-checker"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("checkout\n", encoding="utf-8")

    result = skill_source(package_dir=package_dir)

    assert result == checkout


def test_skill_source_skips_a_bundled_copy_missing_skill_md(tmp_path: Path) -> None:
    repo_root = tmp_path
    package_dir = repo_root / "patent_checker"
    bundled = package_dir / "_skill" / "patent-checker"
    bundled.mkdir(parents=True)
    (bundled / "references").mkdir()
    (bundled / "references" / "report-template.md").write_text("stray\n", encoding="utf-8")
    checkout = repo_root / "skills" / "patent-checker"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("checkout\n", encoding="utf-8")

    result = skill_source(package_dir=package_dir)

    assert result == checkout


def test_skill_source_raises_installer_error_when_neither_candidate_exists(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "patent_checker"

    with pytest.raises(InstallerError):
        skill_source(package_dir=package_dir)


def test_skill_source_default_resolves_to_the_repository_checkout() -> None:
    result = skill_source()

    assert result == REPO_ROOT / "skills" / "patent-checker"
    assert (result / "SKILL.md").is_file()
