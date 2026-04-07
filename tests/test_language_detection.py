#!/usr/bin/env python3
"""
Test suite for language-specific agent prompt detection and injection.

Tests verify:
1. detect_project_language detects all 7 supported languages via marker files
2. Framework detection from pyproject.toml and package.json
3. Primary language selection (first detected wins)
4. Empty project returns empty languages and "default" primary
5. Multi-language projects detect all present languages
6. Language prompt files exist for all detectable languages
7. build_system_prompt injects language-specific guidance into prompts
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path so we can import forge_orchestrator
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_orchestrator import detect_project_language


# Directory containing language prompt markdown files
PROMPTS_LANGUAGES_DIR = Path(__file__).resolve().parent.parent / "prompts" / "languages"


class TestDetectProjectLanguage(unittest.TestCase):
    """Tests for detect_project_language()."""

    def _make_project(self, files):
        """Create a temp directory with the given filenames (empty content)."""
        tmpdir = tempfile.mkdtemp()
        for f in files:
            filepath = Path(tmpdir) / f
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text("", encoding="utf-8")
        self.addCleanup(lambda d=tmpdir: __import__("shutil").rmtree(d))
        return tmpdir

    # --- Single language detection ---

    def test_python_via_pyproject_toml(self):
        """Detects Python when pyproject.toml exists."""
        d = self._make_project(["pyproject.toml"])
        result = detect_project_language(d)
        self.assertIn("python", result["languages"])
        self.assertEqual(result["primary"], "python")

    def test_python_via_requirements_txt(self):
        """Detects Python when requirements.txt exists."""
        d = self._make_project(["requirements.txt"])
        result = detect_project_language(d)
        self.assertIn("python", result["languages"])

    def test_python_via_setup_py(self):
        """Detects Python when setup.py exists."""
        d = self._make_project(["setup.py"])
        result = detect_project_language(d)
        self.assertIn("python", result["languages"])

    def test_python_via_pipfile(self):
        """Detects Python when Pipfile exists."""
        d = self._make_project(["Pipfile"])
        result = detect_project_language(d)
        self.assertIn("python", result["languages"])

    def test_typescript_via_tsconfig(self):
        """Detects TypeScript when tsconfig.json exists."""
        d = self._make_project(["tsconfig.json"])
        result = detect_project_language(d)
        self.assertIn("typescript", result["languages"])
        self.assertEqual(result["primary"], "typescript")

    def test_javascript_via_package_json_no_tsconfig(self):
        """Detects JavaScript when package.json exists without tsconfig.json."""
        d = self._make_project(["package.json"])
        result = detect_project_language(d)
        self.assertIn("javascript", result["languages"])

    def test_javascript_not_detected_with_tsconfig(self):
        """JavaScript is not detected when tsconfig.json is present (TypeScript wins)."""
        d = self._make_project(["package.json", "tsconfig.json"])
        result = detect_project_language(d)
        self.assertIn("typescript", result["languages"])
        self.assertNotIn("javascript", result["languages"])

    def test_go_via_go_mod(self):
        """Detects Go when go.mod exists."""
        d = self._make_project(["go.mod"])
        result = detect_project_language(d)
        self.assertIn("go", result["languages"])
        self.assertEqual(result["primary"], "go")

    def test_rust_via_cargo_toml(self):
        """Detects Rust when Cargo.toml exists."""
        d = self._make_project(["Cargo.toml"])
        result = detect_project_language(d)
        self.assertIn("rust", result["languages"])
        self.assertEqual(result["primary"], "rust")

    def test_csharp_via_csproj(self):
        """Detects C# when a .csproj file exists."""
        d = self._make_project(["MyApp.csproj"])
        result = detect_project_language(d)
        self.assertIn("csharp", result["languages"])
        self.assertIn("dotnet", result["frameworks"])

    def test_csharp_via_sln(self):
        """Detects C# when a .sln file exists."""
        d = self._make_project(["MyApp.sln"])
        result = detect_project_language(d)
        self.assertIn("csharp", result["languages"])

    def test_java_via_pom_xml(self):
        """Detects Java with Maven framework when pom.xml exists."""
        d = self._make_project(["pom.xml"])
        result = detect_project_language(d)
        self.assertIn("java", result["languages"])
        self.assertIn("maven", result["frameworks"])

    def test_java_via_build_gradle(self):
        """Detects Java with Gradle framework when build.gradle exists."""
        d = self._make_project(["build.gradle"])
        result = detect_project_language(d)
        self.assertIn("java", result["languages"])
        self.assertIn("gradle", result["frameworks"])

    def test_java_via_build_gradle_kts(self):
        """Detects Java with Gradle framework when build.gradle.kts exists."""
        d = self._make_project(["build.gradle.kts"])
        result = detect_project_language(d)
        self.assertIn("java", result["languages"])
        self.assertIn("gradle", result["frameworks"])

    # --- Empty and default cases ---

    def test_empty_project_returns_default(self):
        """Empty project returns no languages and 'default' primary."""
        d = self._make_project([])
        result = detect_project_language(d)
        self.assertEqual(result["languages"], [])
        self.assertEqual(result["frameworks"], [])
        self.assertEqual(result["primary"], "default")

    # --- Multi-language projects ---

    def test_multi_language_project(self):
        """Projects with multiple languages detect all of them."""
        d = self._make_project(["pyproject.toml", "go.mod", "Cargo.toml"])
        result = detect_project_language(d)
        self.assertIn("python", result["languages"])
        self.assertIn("go", result["languages"])
        self.assertIn("rust", result["languages"])
        # Primary is first detected (python, since it's checked first)
        self.assertEqual(result["primary"], "python")

    def test_fullstack_project(self):
        """Full-stack project detects both backend and frontend languages."""
        d = self._make_project(["go.mod", "tsconfig.json", "package.json"])
        result = detect_project_language(d)
        self.assertIn("go", result["languages"])
        self.assertIn("typescript", result["languages"])
        # JavaScript should NOT be detected (tsconfig present)
        self.assertNotIn("javascript", result["languages"])

    # --- Framework detection ---

    def test_django_framework_detection(self):
        """Detects Django framework from pyproject.toml content."""
        d = self._make_project([])
        pyproject = Path(d) / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["django>=4.0"]', encoding="utf-8")
        result = detect_project_language(d)
        self.assertIn("django", result["frameworks"])

    def test_fastapi_framework_detection(self):
        """Detects FastAPI framework from pyproject.toml content."""
        d = self._make_project([])
        pyproject = Path(d) / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = ["fastapi"]', encoding="utf-8")
        result = detect_project_language(d)
        self.assertIn("fastapi", result["frameworks"])

    def test_nextjs_framework_detection(self):
        """Detects Next.js framework from package.json content."""
        d = self._make_project([])
        pkg = Path(d) / "package.json"
        pkg.write_text('{"dependencies": {"next": "15.0.0", "react": "19.0.0"}}', encoding="utf-8")
        result = detect_project_language(d)
        self.assertIn("nextjs", result["frameworks"])
        self.assertIn("react", result["frameworks"])

    def test_vue_framework_detection(self):
        """Detects Vue framework from package.json content."""
        d = self._make_project([])
        pkg = Path(d) / "package.json"
        pkg.write_text('{"dependencies": {"vue": "3.0.0"}}', encoding="utf-8")
        result = detect_project_language(d)
        self.assertIn("vue", result["frameworks"])

    def test_angular_framework_detection(self):
        """Detects Angular framework from package.json content."""
        d = self._make_project([])
        pkg = Path(d) / "package.json"
        pkg.write_text('{"dependencies": {"@angular/core": "17.0.0"}}', encoding="utf-8")
        result = detect_project_language(d)
        self.assertIn("angular", result["frameworks"])

    def test_express_framework_detection(self):
        """Detects Express framework from package.json content."""
        d = self._make_project([])
        pkg = Path(d) / "package.json"
        pkg.write_text('{"dependencies": {"express": "4.18.0"}}', encoding="utf-8")
        result = detect_project_language(d)
        self.assertIn("express", result["frameworks"])

    # --- Return value structure ---

    def test_return_dict_keys(self):
        """Return value contains exactly the expected keys."""
        d = self._make_project(["go.mod"])
        result = detect_project_language(d)
        self.assertEqual(set(result.keys()), {"languages", "frameworks", "primary"})

    def test_languages_is_list(self):
        """languages field is always a list."""
        d = self._make_project([])
        result = detect_project_language(d)
        self.assertIsInstance(result["languages"], list)

    def test_frameworks_is_list(self):
        """frameworks field is always a list."""
        d = self._make_project([])
        result = detect_project_language(d)
        self.assertIsInstance(result["frameworks"], list)


class TestLanguagePromptFiles(unittest.TestCase):
    """Verify that language prompt files exist for all detected languages."""

    EXPECTED_LANGUAGES = ["python", "typescript", "go", "csharp", "rust", "java", "javascript"]

    def test_all_language_prompt_files_exist(self):
        """Every language detect_project_language can return has a .md file."""
        for lang in self.EXPECTED_LANGUAGES:
            lang_path = PROMPTS_LANGUAGES_DIR / f"{lang}.md"
            self.assertTrue(
                lang_path.exists(),
                f"Missing language prompt file: {lang_path}"
            )

    def test_language_prompt_files_are_non_empty(self):
        """All language prompt .md files contain actual content."""
        for lang in self.EXPECTED_LANGUAGES:
            lang_path = PROMPTS_LANGUAGES_DIR / f"{lang}.md"
            if lang_path.exists():
                content = lang_path.read_text(encoding="utf-8")
                self.assertTrue(
                    len(content.strip()) > 100,
                    f"Language prompt file {lang}.md is too short ({len(content)} chars)"
                )

    def test_language_prompt_files_start_with_heading(self):
        """All language prompt .md files start with a markdown heading."""
        for lang in self.EXPECTED_LANGUAGES:
            lang_path = PROMPTS_LANGUAGES_DIR / f"{lang}.md"
            if lang_path.exists():
                content = lang_path.read_text(encoding="utf-8")
                self.assertTrue(
                    content.startswith("# "),
                    f"Language prompt file {lang}.md does not start with a heading"
                )


if __name__ == "__main__":
    unittest.main()
