"""Consistency checks between the skill assets and the package.

The consent notice is single-sourced in ``patent_checker/notices/``; the
copies under ``skills/patent-checker/references/`` exist for human reading
and must stay byte-identical. These tests also guard the notice-version
bump discipline and the skill frontmatter.
"""

import re
from pathlib import Path

import pytest

from patent_checker.cleanup import CODE_OWNED_NAMES
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


def test_every_registered_tool_name_appears_in_the_skill() -> None:
    """SKILL.md must mention every MCP tool the server registers (v0.3 R9)."""
    from patent_checker.server.tools import TOOL_NAMES

    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    missing = [name for name in TOOL_NAMES if f"`{name}`" not in text]
    assert missing == [], f"tool names absent from SKILL.md: {missing}"


def test_skill_keeps_consent_on_the_cli_and_names_the_error_prefixes() -> None:
    """Step 0 stays a local CLI step; the three tool-error prefixes are documented."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "`patent-checker consent status`" in text
    assert "`patent-checker consent record --lang <lang>`" in text
    for prefix in ("invalid_input:", "external_api_error:", "ops_not_configured:"):
        assert f"`{prefix}`" in text, prefix
    assert "server_status" in text


def test_skill_artifact_directories_survive_the_default_cleanup() -> None:
    """Where SKILL.md tells the agent to write must not be a name `clean` owns.

    ``patent-checker clean`` removes the data-base entries the package
    itself writes (:data:`patent_checker.cleanup.CODE_OWNED_NAMES`) and
    spares everything else. If SKILL.md ever pointed the agent's artifacts
    at one of those names, the default cleanup would silently delete a
    report.
    """
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\.patent-checker/([A-Za-z0-9_.-]+)", text))

    assert "reports" in named, "SKILL.md no longer names .patent-checker/reports/"
    for name in named:
        assert name not in CODE_OWNED_NAMES, (
            f"SKILL.md writes artifacts to .patent-checker/{name}, which the "
            "default cleanup deletes"
        )


def test_consent_record_is_owned_by_the_package() -> None:
    """The consent record is the package's own file, so it is only removed on request."""
    assert "consent.json" in CODE_OWNED_NAMES


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_operator_notice_files_carry_the_current_version(lang: str) -> None:
    from patent_checker.server.settings import OPERATOR_NOTICE_VERSION, operator_notice_text

    effective, text = operator_notice_text(lang)
    assert effective == lang
    assert OPERATOR_NOTICE_VERSION in text.splitlines()[0]
