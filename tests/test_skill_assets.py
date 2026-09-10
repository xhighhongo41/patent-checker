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
    for prefix in (
        "invalid_input:",
        "external_api_error:",
        "ops_not_configured:",
        "upstream_data:",
    ):
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


def test_skill_documents_the_cache_and_cleanup_commands() -> None:
    """SKILL.md must tell the agent how caching shows up and how traces are removed (v0.4)."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "`patent-checker clean`" in text
    assert "`patent-checker cache status`" in text
    assert "--yes" in text
    assert "`search_cache_dir`" in text
    assert "--refresh" in text


# --- README.md / README_ja.md ---

README = REPO_ROOT / "README.md"
README_JA = REPO_ROOT / "README_ja.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

REQUIRED_README_SECTIONS = (
    "## Important notices",
    "## How it works",
    "## Prerequisites",
    "## Install the server",
    "## Install the Skill and connect your agent",
    "## Usage",
    "## Configuration",
    "## Cache and clean-up",
    "## Security model",
    "## Data sources and fair use",
    "## Updating",
    "## Changelog",
    "## License",
)


def _declared_minor_version() -> str:
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    major, minor = version.split(".")[:2]
    return f"v{major}.{minor}"


def test_readme_covers_the_required_sections() -> None:
    text = README.read_text(encoding="utf-8")
    missing = [heading for heading in REQUIRED_README_SECTIONS if heading not in text]
    assert not missing, f"README.md lacks sections: {missing}"


def test_readme_and_japanese_readme_share_one_structure() -> None:
    en = README.read_text(encoding="utf-8").splitlines()
    ja = README_JA.read_text(encoding="utf-8").splitlines()
    en_headings = [line for line in en if line.startswith("#")]
    ja_headings = [line for line in ja if line.startswith("#")]
    assert len(en_headings) == len(ja_headings), (en_headings, ja_headings)
    en_fences = sum(1 for line in en if line.startswith("```"))
    ja_fences = sum(1 for line in ja if line.startswith("```"))
    assert en_fences == ja_fences
    assert "README_ja.md" in "\n".join(en[:5])
    assert "README.md" in "\n".join(ja[:5])


@pytest.mark.parametrize("path", [README, README_JA], ids=["en", "ja"])
def test_readme_status_line_names_the_declared_minor_version(path: Path) -> None:
    status_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"(Status|状態).*v[0-9]+\.[0-9]+", line)
    ]
    assert status_lines, f"{path.name} has no status line"
    assert _declared_minor_version() in status_lines[0]


def test_readme_documents_every_variable_in_env_example() -> None:
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    declared = set(re.findall(r"^#?\s*(PATENT_CHECKER_[A-Z_]+)=", env_example, re.M))
    assert declared, ".env.example declares no variables"
    text = README.read_text(encoding="utf-8")
    undocumented = sorted(name for name in declared if name not in text)
    assert not undocumented, f"README.md does not mention: {undocumented}"


def test_readme_states_the_no_verdict_boundary_and_the_installer_flow() -> None:
    text = README.read_text(encoding="utf-8")
    assert "does not decide whether anything infringes" in text
    for needle in (
        "patent-checker install",
        "docker compose up",
        "--token-file",
        "--token-env",
        "--dry-run",
        "--show-operator-notice",
        "PATENT_CHECKER_OPERATOR_CONSENT=1.0",
        "401",
        "cache status",
        "patent-checker clean",
    ):
        assert needle in text, needle


DOCS = REPO_ROOT / "docs"


def test_skill_carries_no_internal_version_history() -> None:
    """The shipped Skill text must not cite development version numbers (v1.0)."""
    for path in [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bv0\.[0-9]", text), f"{path.name} still cites a v0.x version"


@pytest.mark.parametrize(
    ("readme", "sections"),
    [
        (
            README,
            [
                "## The bearer token",
                "## Without an EPO OPS account (degraded mode)",
                "## Uninstalling",
                "## Updating",
                "### If the server answers 401",
            ],
        ),
        (
            README_JA,
            [
                "## ベアラートークン",
                "## EPO OPS アカウントが無い場合(縮退モード)",
                "## アンインストール",
                "## 更新",
                "### サーバーが 401 を返す場合",
            ],
        ),
    ],
    ids=["en", "ja"],
)
def test_readme_has_the_release_sections(readme: Path, sections: list[str]) -> None:
    """v1.0: token, degraded mode, uninstall and update are dedicated sections."""
    text = readme.read_text(encoding="utf-8")
    for heading in sections:
        assert heading in text, heading


@pytest.mark.parametrize(
    "name", ["deploy-lan.md", "deploy-lan_ja.md", "mcp-clients.md", "mcp-clients_ja.md"]
)
def test_docs_pages_exist_and_are_linked_from_the_readme(name: str) -> None:
    """Each docs page ships in both languages and the README points at it."""
    page = DOCS / name
    assert page.is_file(), name
    readme = README_JA if name.endswith("_ja.md") else README
    assert name in readme.read_text(encoding="utf-8"), f"{readme.name} does not link {name}"
    counterpart = (
        name.replace("_ja.md", ".md") if name.endswith("_ja.md") else name.replace(".md", "_ja.md")
    )
    assert counterpart in page.read_text(encoding="utf-8"), f"{name} does not link {counterpart}"


def test_readme_avoids_developer_only_remarks() -> None:
    """v1.0: the README is for users; internal wording must not creep back in."""
    text = README.read_text(encoding="utf-8")
    for phrase in ("pre-release", "Keep it that way", "this README", "proof of concept"):
        assert phrase not in text, phrase
