"""Consistency checks between the skill assets and the package.

The consent notice is single-sourced in ``patent_checker/notices/``; the
copies under ``skills/patent-checker/references/`` exist for human reading
and must stay byte-identical. These tests also guard the notice-version
bump discipline and the skill frontmatter.
"""

from pathlib import Path

import pytest

from patent_checker.consent import NOTICE_VERSION, notice_text

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "patent-checker"
NOTICES_DIR = REPO_ROOT / "patent_checker" / "notices"

pytestmark = pytest.mark.skipif(
    not SKILL_DIR.is_dir(), reason="skills/ not present (installed package context)"
)


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_skill_notice_copy_is_identical_to_package_canonical(lang: str) -> None:
    canonical = (NOTICES_DIR / f"consent-notice.{lang}.md").read_bytes()
    copy = (SKILL_DIR / "references" / f"consent-notice.{lang}.md").read_bytes()
    assert copy == canonical, (
        f"skills copy of consent-notice.{lang}.md differs from the package "
        "canonical; regenerate the copy instead of editing it"
    )


def test_notice_files_carry_the_current_version() -> None:
    _, en_text = notice_text("en")
    _, ja_text = notice_text("ja")
    assert f"(version {NOTICE_VERSION})" in en_text
    assert f"(バージョン {NOTICE_VERSION})" in ja_text


def test_skill_frontmatter_has_name_and_description() -> None:
    lines = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    closing = lines.index("---", 1)
    frontmatter = lines[1:closing]
    keys = {line.split(":", 1)[0].strip() for line in frontmatter if ":" in line}
    assert "name" in keys
    assert "description" in keys
    assert any(line.startswith("name: patent-checker") for line in frontmatter)


def test_report_template_keeps_the_mandatory_sections() -> None:
    text = (SKILL_DIR / "references" / "report-template.md").read_text(encoding="utf-8")
    for marker in (
        "Disclaimer",
        "Scope and limitations",
        "Design boundaries",
        "Monitoring list",
        "Response record",
        "no verdict",
    ):
        assert marker.lower() in text.lower(), f"missing mandatory element: {marker}"
