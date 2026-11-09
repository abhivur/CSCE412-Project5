import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from cloudcrew import site


class SiteTests(unittest.TestCase):
    def test_generated_site_has_connected_pages_and_no_broken_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site.build(root)
            result = site.check(root)
            self.assertEqual(result["status"], "passed", result["errors"])
            self.assertEqual(result["pages_checked"], 9)

    def test_checker_finds_missing_assets_and_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site.build(root)
            (root / "assets/cloud-architecture.svg").unlink()
            page = root / "index.html"
            page.write_text(page.read_text().replace("resources.html#architecture", "resources.html#missing"))
            errors = site.check(root)["errors"]
            self.assertTrue(any("cloud-architecture.svg" in item for item in errors))
            self.assertTrue(any("broken fragment" in item for item in errors))

    def test_text_is_escaped_and_unknown_pages_rejected(self):
        self.assertEqual(site.esc('<script>"'), "&lt;script&gt;&quot;")
        with self.assertRaises(ValueError):
            site.update_intro("../../.env", "A perfectly ordinary page description.")

    def test_every_page_has_current_brand_and_legacy_links_render_current_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site.build(root)
            for name in list(site.PAGES.values()) + list(site.LEGACY_PAGES) + ["404.html"]:
                content = (root / name).read_text()
                self.assertIn("| GBM Cloud</title>", content)
                self.assertNotIn("Field Notes", content)
                self.assertNotIn("CloudCrew", content)
                self.assertIn('content="GBM Cloud"', content)
            for filename, page in site.LEGACY_PAGES.items():
                self.assertIn(f'<link rel="canonical" href="{site.PAGES[page]}">', (root / filename).read_text())
            self.assertIn('data-error-page="404"', (root / "404.html").read_text())

    def test_rebuild_removes_retired_assets_and_svg_assets_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "retired.svg").write_text("old")
            site.build(root)
            self.assertFalse((root / "assets" / "retired.svg").exists())
            for asset in (root / "assets").glob("*.svg"):
                ET.parse(asset)


if __name__ == "__main__":
    unittest.main()
