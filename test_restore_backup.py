import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("restore_backup.py")
SPEC = importlib.util.spec_from_file_location("restore_backup", SCRIPT)
restore_backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore_backup)

class RestoreBackupTests(unittest.TestCase):
    def fixture(self, root):
        backup = root / "backup.json"
        payload = {
            "format": "tenderscout-database-backup",
            "format_version": 1,
            "exported_at": "2026-10-27T00:00:00+00:00",
            "tables": {
                "tenders": [{"id": 17, "source": "Find a Tender", "title": "Test tender", "buyer": "Test Council", "notice_url": "https://example.test/tender/17", "region_match": "LOCAL", "score": 5}],
                "private_leads": [{"id": 4, "title": "Roof repair", "trade_category": "Roofing", "location_text": "Liverpool L1", "source_name": "Direct enquiry", "created_at": "2026-10-27T00:00:00+00:00"}],
                "external_projects": [{"id": 8, "title": "School refurbishment", "trade_category": "Construction", "location_text": "Knowsley", "source_name": "Public project feed", "source_url": "https://example.test/project/8", "imported_at": "2026-10-27T00:00:00+00:00"}]
            }
        }
        backup.write_text(json.dumps(payload), encoding="utf-8")
        return backup

    def invoke(self, backup, destination):
        output, errors = StringIO(), StringIO()
        with patch("sys.argv", ["restore_backup.py", str(backup), str(destination)]), redirect_stdout(output), redirect_stderr(errors):
            try:
                code = restore_backup.main()
            except SystemExit as exc:
                code = int(exc.code)
        return code, output.getvalue(), errors.getvalue()

    def test_restores_all_three_tables_and_preserves_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = self.fixture(root)
            destination = root / "restored.db"
            code, output, errors = self.invoke(backup, destination)
            self.assertEqual(code, 0, errors)
            with sqlite3.connect(destination) as db:
                self.assertEqual(db.execute("SELECT id, title FROM tenders").fetchone(), (17, "Test tender"))
                self.assertEqual(db.execute("SELECT id, title FROM private_leads").fetchone(), (4, "Roof repair"))
                self.assertEqual(db.execute("SELECT id, title FROM external_projects").fetchone(), (8, "School refurbishment"))
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIn("tenders: 1 rows", output)
            self.assertIn("private_leads: 1 rows", output)
            self.assertIn("external_projects: 1 rows", output)

    def test_refuses_to_overwrite_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = self.fixture(root)
            destination = root / "existing.db"
            destination.write_text("keep me", encoding="utf-8")
            code, _, _ = self.invoke(backup, destination)
            self.assertNotEqual(code, 0)
            self.assertEqual(destination.read_text(encoding="utf-8"), "keep me")

    def test_rejects_wrong_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / "bad.json"
            backup.write_text(json.dumps({"format": "wrong", "format_version": 1, "tables": {}}), encoding="utf-8")
            destination = root / "restored.db"
            code, _, _ = self.invoke(backup, destination)
            self.assertNotEqual(code, 0)
            self.assertFalse(destination.exists())

if __name__ == "__main__":
    unittest.main()
