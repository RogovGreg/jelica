from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


DOCS_ROOT = Path(__file__).resolve().parents[1]


class DocumentationPipelineTests(unittest.TestCase):
    def test_supported_locale_without_source_fails_before_toolchain(self) -> None:
        result = subprocess.run(
            [str(DOCS_ROOT / "build.sh"), "all", "--locale", "ru"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Documentation source for locale is unavailable: ru", result.stderr)
        self.assertNotIn("Docker", result.stderr)

    def test_generator_preserves_unicode_locale_and_heading_anchors_for_all_sizes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jelica-doc-generator-") as temporary:
            docs_root = self.fixture_docs_root(Path(temporary), locale="ru")
            generated_styles = []
            for text_size in ("small", "standard", "large"):
                artifact_root = self.generate(docs_root, "ru", text_size)
                manifest = read_json(artifact_root / "documentation-manifest.json")
                search = read_json(artifact_root / "search-index.json")
                self.assertEqual(manifest["locale"], "ru")
                self.assertEqual(manifest["textSize"], text_size)
                self.assertEqual(search["locale"], "ru")
                self.assertEqual(search["documents"][0]["headingAnchors"], ["#setup"])
                self.assertIn("Русский текст", search["documents"][0]["content"])
                generated_styles.append((artifact_root / "html" / "jelica-doc.css").read_text(encoding="utf-8"))
            self.assertEqual(len(set(generated_styles)), 3)

    def test_source_locale_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jelica-doc-locale-") as temporary:
            docs_root = self.fixture_docs_root(Path(temporary), locale="sr-Cyrl")
            result = self.run_generator(docs_root, "sr-Cyrl", "standard")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match the selected locale", result.stderr)

    def test_validator_rejects_script_and_unsafe_navigation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jelica-doc-security-") as temporary:
            docs_root = self.fixture_docs_root(Path(temporary), locale="ru")
            artifact_root = self.generate(docs_root, "ru", "standard")
            index = artifact_root / "html" / "index.html"
            index.write_text(
                index.read_text(encoding="utf-8").replace(
                    "</body>", '<script src="payload.js"></script><a href="javascript:alert(1)">bad</a></body>',
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    os.environ.get("PYTHON", "python3"),
                    str(docs_root / "tooling" / "validate-artifacts.py"),
                    "--docs-root",
                    str(docs_root),
                    "--artifact-root",
                    str(artifact_root),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("script elements are not allowed", result.stderr)
            self.assertIn("unsafe URL scheme", result.stderr)

    def test_release_packaging_is_repeatable_and_excludes_work_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jelica-doc-release-") as temporary:
            docs_root = self.fixture_docs_root(Path(temporary), locale="ru")
            artifact_root = self.generate(docs_root, "ru", "standard")
            (artifact_root / "work").mkdir()
            (artifact_root / "work" / "stale.aux").write_text("stale", encoding="utf-8")
            environment = {**os.environ, "SOURCE_DATE_EPOCH": "1767225600"}
            command = [
                os.environ.get("PYTHON", "python3"),
                str(docs_root / "tooling" / "package-release.py"),
                "--docs-root",
                str(docs_root),
                "--artifact-root",
                str(artifact_root),
                "--locale",
                "ru",
                "--profile",
                "screen",
                "--size",
                "standard",
            ]
            subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
            archive = next((docs_root / "releases").rglob("*.tar.gz"))
            first_digest = file_digest(archive)
            subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
            self.assertEqual(file_digest(archive), first_digest)
            release_root = docs_root / "releases" / "0.1" / "format-v1" / "ru" / "screen-standard"
            self.assertFalse((release_root / "work").exists())
            checksums = read_json(release_root / "checksums.json")
            self.assertNotIn("checksums.json", {item["path"] for item in checksums["files"]})

    def test_size_profiles_have_distinct_body_typography(self) -> None:
        values = {
            text_size: (DOCS_ROOT / "template" / "latex" / "profiles" / f"size-{text_size}.tex").read_text(encoding="utf-8")
            for text_size in ("small", "standard", "large")
        }
        self.assertEqual(len(set(values.values())), 3)
        self.assertTrue(all("\\fontsize" in value and "\\JelicaApplyBodySize" in value for value in values.values()))

    def fixture_docs_root(self, temporary: Path, locale: str) -> Path:
        docs_root = temporary / "documentation"
        shutil.copytree(DOCS_ROOT / "schema", docs_root / "schema")
        shutil.copytree(DOCS_ROOT / "tooling", docs_root / "tooling")
        (docs_root / "template" / "html").mkdir(parents=True)
        shutil.copyfile(DOCS_ROOT / "template" / "html" / "jelica-doc.css", docs_root / "template" / "html" / "jelica-doc.css")
        shutil.copyfile(DOCS_ROOT / "version.json", docs_root / "version.json")
        source = docs_root / "source" / locale
        (source / "chapters").mkdir(parents=True)
        (source / "main.tex").write_text(
            "\\JelicaSetLocale{ru}\n"
            "\\JelicaSetTitle{Русская документация}\n"
            "\\JelicaSetSubtitle{Тест}\n"
            "\\JelicaSetVersion{0.1}\n"
            "\\JelicaSetYear{2026}\n"
            "\\input{chapters/intro}\n",
            encoding="utf-8",
        )
        (source / "chapters" / "intro.tex").write_text(
            "\\JelicaChapter{intro}{Введение}{поиск}\n"
            "\\JelicaSection{setup}{Настройка}\n"
            "Русский текст для поиска.\n",
            encoding="utf-8",
        )
        return docs_root

    def generate(self, docs_root: Path, locale: str, text_size: str) -> Path:
        result = self.run_generator(docs_root, locale, text_size)
        if result.returncode != 0:
            self.fail(result.stderr)
        return docs_root / "build" / locale / f"screen-{text_size}"

    def run_generator(self, docs_root: Path, locale: str, text_size: str) -> subprocess.CompletedProcess[str]:
        artifact_root = docs_root / "build" / locale / f"screen-{text_size}"
        html_root = artifact_root / "html"
        pdf_root = artifact_root / "pdf"
        html_root.mkdir(parents=True, exist_ok=True)
        pdf_root.mkdir(parents=True, exist_ok=True)
        (html_root / "fixture-job.html").write_text(
            "<html><head><title></title></head><body><h1 id='intro'>Введение</h1><h2 id='setup'>Настройка</h2></body></html>",
            encoding="utf-8",
        )
        pdf_name = f"jelica-documentation-{locale}-screen-{text_size}.pdf"
        (pdf_root / pdf_name).write_bytes(b"%PDF-1.4\n%%EOF\n")
        return subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(docs_root / "tooling" / "generate-artifacts.py"),
                "--docs-root",
                str(docs_root),
                "--locale",
                locale,
                "--profile",
                "screen",
                "--size",
                text_size,
                "--artifact-root",
                str(artifact_root),
                "--job-name",
                "fixture-job",
                "--pdf-name",
                pdf_name,
            ],
            text=True,
            capture_output=True,
            check=False,
        )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
