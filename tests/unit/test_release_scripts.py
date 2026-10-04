"""The scripts that gate and describe a release."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "scripts" / "bump_version.py").exists():
    # Distribution package builds (e.g. Debian's pybuild) copy only the package and tests.
    raise unittest.SkipTest("release scripts are not part of this tree")
sys.path.insert(0, str(ROOT / "scripts"))

import bump_version  # noqa: E402
import release_notes  # noqa: E402

CHANGELOG = """# Changelog

## [Unreleased]

### Fixed

- A thing.

## [0.6.2] - 2026-01-01

- Older.
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_section_stops_at_next_heading(self):
        text = CHANGELOG.replace("## [Unreleased]", "## [0.7.0] - 2026-10-05")
        self.assertEqual(release_notes.section("0.7.0", text), "### Fixed\n\n- A thing.\n")
        self.assertEqual(release_notes.section("0.6.2", text), "- Older.\n")

    def test_missing_or_empty_section_fails(self):
        with self.assertRaises(SystemExit):
            release_notes.section("9.9.9", CHANGELOG)
        with self.assertRaises(SystemExit):
            release_notes.section("1.0.0", "## [1.0.0]\n\n## [0.9.0]\n- x\n")

    def test_current_changelog_parses(self):
        self.assertIn("Unreleased", (ROOT / "CHANGELOG.md").read_text())


class BumpVersionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for rel in ("pyproject.toml", "apsta_cli/__init__.py", "packaging/arch/PKGBUILD", "debian/changelog"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, self.root / rel)
        (self.root / "CHANGELOG.md").write_text(CHANGELOG)
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def test_bump_updates_every_source_and_releases_changelog(self):
        self.assertEqual(bump_version.bump_version("9.8.7"), 0)
        changelog = (self.root / "CHANGELOG.md").read_text()
        self.assertRegex(changelog, r"## \[Unreleased\]\n\n## \[9\.8\.7\] - \d{4}-\d{2}-\d{2}\n\n### Fixed")
        self.assertIn("apsta (9.8.7-1)", (self.root / "debian/changelog").read_text())

    def test_bump_is_idempotent_for_changelog(self):
        bump_version.bump_version("9.8.7")
        once = (self.root / "CHANGELOG.md").read_text()
        bump_version.release_project_changelog("9.8.7")
        self.assertEqual((self.root / "CHANGELOG.md").read_text(), once)

    def test_rejects_bad_versions(self):
        self.assertEqual(bump_version.bump_version("not-a-version"), 2)


class ReleaseCheckTests(unittest.TestCase):
    def test_current_tree_is_consistent(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import release_check

        versions = set(release_check.versions().values())
        self.assertEqual(len(versions), 1, versions)  # every source agrees
        self.assertEqual(release_check.main("0.0.0-never"), 1)


if __name__ == "__main__":
    unittest.main()
