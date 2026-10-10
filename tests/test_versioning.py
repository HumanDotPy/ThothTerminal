from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import thoth_terminal
from thoth_terminal.versioning import read_project_version, set_project_version, validate_version


class VersioningTests(unittest.TestCase):
    def test_runtime_version_matches_project_version(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(thoth_terminal.__version__, read_project_version(root))

    def test_installed_prerelease_matches_source_release_identity(self) -> None:
        from thoth_terminal._version import resolve_version

        with patch("thoth_terminal._version.checkout_root", return_value=None), patch(
            "thoth_terminal._version.importlib.metadata.version", return_value="0.3.1b1"
        ):
            self.assertEqual(resolve_version(), "0.3.1-beta.1")

    def test_semver_validation_and_targeted_update(self) -> None:
        self.assertEqual(validate_version("1.2.3-rc.1+build.9"), "1.2.3-rc.1+build.9")
        with self.assertRaises(ValueError):
            validate_version("v1.2")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                '[project]\nname = "thoth-terminal"\nversion = "0.1.0"\n\n[tool.example]\nversion = "9.9.9"\n',
                encoding="utf-8",
            )
            set_project_version(root, "0.2.0")
            self.assertEqual(read_project_version(root), "0.2.0")
            self.assertIn('version = "9.9.9"', pyproject.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
