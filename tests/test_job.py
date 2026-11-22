import io
import json
import unittest
import uuid
import zipfile
from unittest.mock import Mock, patch
from worker import job


class ParcelTests(unittest.TestCase):
    message = {"message_id": "q-123", "attributes": {"SentTimestamp": "1760000000000"},
               "body": json.dumps({"name": "отчёт.txt", "url": "https://example.org/report"})}

    def test_archive_content(self):
        with zipfile.ZipFile(io.BytesIO(job.pack("отчёт.txt", b"receipt"))) as z:
            self.assertEqual(z.namelist(), ["отчёт.txt"])
            self.assertEqual(z.read("отчёт.txt"), b"receipt")

    def test_name_without_directory(self):
        with self.assertRaises(ValueError):
            job.pack("../report", b"")

    def test_uuid_version_and_timestamp(self):
        k = uuid.UUID(job.object_key(self.message)[:-4])
        self.assertEqual(k.version, 7)
        self.assertEqual(k.int >> 80, 1760000000000)

    def test_duplicate_delivery(self):
        storage, db = Mock(), Mock()
        db.get_item.return_value = {"Item": {"key": {"S": job.object_key(self.message)}}}
        job.process(self.message, storage, db, "b")
        storage.put_object.assert_not_called()
        db.put_item.assert_not_called()

    def test_storage_failure_leaves_catalog_empty(self):
        storage, db = Mock(), Mock()
        db.get_item.return_value = {}
        storage.put_object.side_effect = RuntimeError("Storage unavailable")
        with patch.object(job, "fetch", return_value=b"demo"), self.assertRaises(RuntimeError):
            job.process(self.message, storage, db, "b")
        db.put_item.assert_not_called()

    def test_demo_two_members(self):
        with zipfile.ZipFile(io.BytesIO(job.pack("one.txt", b"1"))) as z:
            self.assertEqual(len(z.infolist()), 2)
