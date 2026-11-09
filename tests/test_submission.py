import tempfile
import unittest
from pathlib import Path

from cloudcrew.submission import simple_pdf


class SubmissionTests(unittest.TestCase):
    def test_pdf_has_valid_xref_offsets_and_does_not_claim_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.pdf"
            simple_pdf(path, "Unit fixture report", ["Student fixture"], [("Security verification", ["PENDING: no live evidence."])])
            data = path.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertTrue(data.endswith(b"%%EOF\n"))
            offset = int(data.split(b"startxref\n")[1].splitlines()[0])
            self.assertEqual(data[offset:offset + 4], b"xref")
            self.assertIn(b"PENDING: no live evidence", data)


if __name__ == "__main__":
    unittest.main()
