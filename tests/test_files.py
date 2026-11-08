import base64
import copy
import hashlib
import io
import json
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

import test_pipeline
import test_relay
from test_pipeline import ROOM, message
from test_relay import event, image_event
from weward.collector import CollectorHandler, file_snapshot, validate_file_config
from weward.common import database, encode_json, make_server, request_json
from weward.receiver import Receiver


def file_event(identifier, content=b"file bytes", filename="report.pdf", **values):
    return event(identifier, kind="file", raw={"file": {"filename": filename}}, file={
        "status": "available", "filename": filename, "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(), "base64": base64.b64encode(content).decode(),
    }, **values)


class FilePipelineTest(unittest.TestCase):
    setUp = test_pipeline.PipelineTest.setUp
    tearDown = test_pipeline.PipelineTest.tearDown
    start_server = test_pipeline.PipelineTest.start_server
    new_collector = test_pipeline.PipelineTest.new_collector
    deliver_all = test_pipeline.PipelineTest.deliver_all

    def incoming(self, identifier, filename="report.pdf", content=b"file bytes", **values):
        path = self.shared / "rooms" / ROOM / "inbox" / identifier / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return message(identifier, "file", file={"filename": filename, "filesize": len(content),
                       "sdkfileid": str(path.relative_to(self.shared / "rooms" / ROOM))}, **values), path

    def test_file_bytes_survive_source_removal_restart_lost_ack_and_download(self):
        filename = "\u4f1a\u8bae\u8bb0\u5f55.PDF"
        incoming, path = self.incoming("file", filename, b"\x00\xff file bytes", sender="self-account")
        self.fake.messages = [incoming]
        self.collector.poll_once()
        path.unlink()
        self.receive_server.failure = "after"
        self.deliver_all()
        first = self.receiver.messages()["messages"][0]
        self.collector = self.new_collector()
        self.receive_server.failure = None
        self.collector.retry_pending()
        self.deliver_all()
        reopened = Receiver(self.root / "receiver")
        saved = reopened.messages()["messages"][0]
        self.assertEqual(first["received_at"], saved["received_at"])
        self.assertEqual(reopened.messages()["total"], 1)
        self.assertTrue(saved["is_self"])
        self.assertEqual(saved["file"]["filename"], filename)
        self.assertNotIn("base64", saved["file"])
        with urllib.request.urlopen(self.target.replace("/api/messages", saved["file"]["url"])) as response:
            self.assertEqual(response.read(), b"\x00\xff file bytes")
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertIn("attachment;", response.headers["Content-Disposition"])
            self.assertIn("filename*=UTF-8''", response.headers["Content-Disposition"])
        self.assertEqual(reopened.status()["file_bytes"], len(b"\x00\xff file bytes"))

    def test_suffix_and_size_filters_keep_message_and_reason(self):
        self.collector.file_config({"extensions": [" .PDF ", "pdf", "tar.gz"], "max_size_mb": 5 / 1048576})
        fixtures = [("allowed", "REPORT.PDF", b"12345"), ("archive", "data.TAR.GZ", b"ok"),
                    ("extension", "file.exe", b"1"), ("large", "big.pdf", b"123456"),
                    ("empty", "empty.pdf", b"")]
        self.fake.messages = [self.incoming(identifier, name, content)[0] for identifier, name, content in fixtures]
        # Exercise the actual byte count when the upstream size is absent.
        self.fake.messages[3]["file"]["filesize"] = 0
        self.collector.poll_once()
        self.deliver_all()
        by_id = {item["message_id"]: item for item in self.receiver.messages()["messages"]}
        for identifier in ("allowed", "archive", "empty"):
            self.assertEqual(by_id[identifier]["file"]["status"], "available")
        self.assertEqual(by_id["extension"]["file"]["reason"], "extension")
        self.assertEqual(by_id["large"]["file"]["reason"], "size")
        self.assertEqual(self.receiver.messages(filters={"type": "file", "q": "REPORT.PDF"})["total"], 1)
        with urllib.request.urlopen(self.target.replace("/api/messages", by_id["empty"]["file"]["url"])) as response:
            self.assertEqual(response.read(), b"")

    def test_disabled_and_no_extension_allow_all(self):
        incoming, path = self.incoming("plain", "README")
        self.collector.file_config({"enabled": False})
        self.fake.messages = [incoming]
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["messages"][0]["file"]["reason"], "disabled")
        self.collector.file_config({})
        self.fake.messages = [self.incoming("plain-enabled", "README")[0]]
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["messages"][0]["file"]["status"], "available")

    def test_missing_partial_outside_path_and_upstream_error_do_not_block_text(self):
        partial, _ = self.incoming("partial", content=b"partial")
        partial["file"]["filesize"] = 30
        self.fake.messages = [partial,
            message("missing", "file", file={"filename": "report.pdf", "sdkfileid": "inbox/missing.pdf"}),
            message("outside", "file", file={"filename": "report.pdf", "sdkfileid": "../../../api-token"}),
            message("failed", "file", file={"filename": "report.pdf", "error": "fixture read failed"}),
            message("too-large-upstream", "file", file={"filename": "big.pdf", "skip_reason": "size"}),
            message("text")]
        self.collector.poll_once()
        self.deliver_all()
        result = {item["message_id"]: item for item in self.receiver.messages()["messages"]}
        for identifier in ("partial", "missing", "outside", "failed"):
            self.assertEqual(result[identifier]["file"]["status"], "unavailable")
        self.assertEqual(result["failed"]["file"]["error"], "fixture read failed")
        self.assertEqual(result["too-large-upstream"]["file"]["reason"], "size")
        self.assertIn("text", result)

    def test_config_proxy_survives_restart_and_does_not_reset_target(self):
        control = make_server(("127.0.0.1", 0), CollectorHandler, self.collector)
        self.start_server(control)
        self.receiver.collector_url = f"http://127.0.0.1:{control.server_port}"
        url = self.target.replace("/api/messages", "/api/file-settings")
        result = request_json(url, {"enabled": True, "extensions": [".PDF", "tar.gz"], "max_size_mb": 12.5})
        self.assertEqual(result["extensions"], ["pdf", "tar.gz"])
        self.assertEqual(request_json(url), result)
        self.collector.configure({"room_remark": "webox.changed", "target_url": self.target})
        restarted = self.new_collector()
        self.assertEqual(restarted.file_config(), result)
        self.assertEqual(restarted.config()["target_url"], self.target)
        self.assertEqual(restarted.config()["room_remark"], "webox.changed")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request_json(url, {"max_size_mb": 33})
        self.assertEqual(caught.exception.code, 400)

    def test_file_config_race_during_snapshot_preserves_cursor(self):
        self.fake.messages = [self.incoming("racing")[0]]
        def changed(*args, **kwargs):
            snapshot = file_snapshot(*args, **kwargs)
            self.collector.file_config({"enabled": False})
            return snapshot
        with patch("weward.collector.file_snapshot", side_effect=changed):
            self.collector.poll_once()
        self.assertEqual(self.collector.status()["queue"]["total"], 0)
        with database(self.collector.db_path) as connection:
            cursor = connection.execute("SELECT cursor FROM cursors").fetchone()[0]
        self.assertEqual(cursor, self.fake.request_cursors[0])
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["messages"][0]["file"]["reason"], "disabled")

    def test_queued_file_uses_original_policy(self):
        incoming, path = self.incoming("snapshot")
        self.fake.messages = [incoming]
        self.collector.poll_once()
        self.collector.file_config({"enabled": False})
        path.unlink()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["messages"][0]["file"]["status"], "available")

    def test_old_config_gets_defaults(self):
        with database(self.collector.db_path) as connection:
            connection.execute("UPDATE settings SET value=? WHERE key='config'",
                               (encode_json({"room_remark": "webox.legacy", "target_url": self.target}).decode(),))
        restarted = self.new_collector()
        self.assertEqual(restarted.file_config(), {"enabled": True, "extensions": [], "max_size_mb": 20})
        self.assertEqual(restarted.config()["room_remark"], "webox.legacy")

    def test_receiver_rejects_invalid_files_over_http(self):
        good = file_event("bad")
        for values in ({"sha256": "wrong"}, {"size": 999}, {"base64": "!"},
                       {"filename": "../escape.pdf"}, {"filename": "a\\b.pdf"},
                       {"size": True}, {"size": 32 * 1048576 + 1}, {"base64": []},
                       {"status": "unknown"}, {"status": "unavailable", "error": 3}):
            candidate = copy.deepcopy(good)
            candidate["file"].update(values)
            with self.subTest(values=values), self.assertRaises(urllib.error.HTTPError) as caught:
                request_json(self.target, candidate)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.receiver.messages()["total"], 0)

    def test_maximum_file_fits_existing_http_envelope(self):
        content = b"x" * (32 * 1048576)
        incoming, _ = self.incoming("maximum", content=content)
        self.collector.file_config({"max_size_mb": 32})
        self.fake.messages = [incoming]
        self.collector.poll_once()
        self.deliver_all()
        saved = self.receiver.messages()["messages"][0]["file"]
        self.assertEqual(saved["size"], len(content))
        self.assertEqual(saved["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.collector.status()["queue"]["pending"], 0)


class FileConfigTest(unittest.TestCase):
    def test_invalid_policies(self):
        for value in ([], {"enabled": "false"}, {"max_size_mb": False}, {"max_size_mb": 0},
                      {"max_size_mb": -1}, {"max_size_mb": 33}, {"max_size_mb": float("nan")},
                      {"max_size_mb": float("inf")}, {"max_size_mb": "10"},
                      {"extensions": "pdf"}, {"extensions": ["*.pdf"]}, {"extensions": ["../pdf"]},
                      {"extensions": [""]}, {"extensions": [1]}, {"extensions": ["x"] * 101}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_file_config(value)


class FileRelayTest(unittest.TestCase):
    setUp = test_relay.RelayTest.setUp
    tearDown = test_relay.RelayTest.tearDown
    start = test_relay.RelayTest.start
    job = test_relay.RelayTest.job
    run_job = test_relay.RelayTest.run_job
    unpack = test_relay.RelayTest.unpack

    def test_zip_preserves_same_name_files_images_and_skipped_metadata(self):
        filename = "\u62a5\u544a.pdf"
        for identifier, content in (("first", b"first"), ("second", b"second"), ("empty", b"")):
            self.receiver.receive(file_event(identifier, content, filename))
        self.receiver.receive(image_event("image"))
        self.receiver.receive(event("skipped", kind="file", raw={"file": {"filename": "huge.pdf"}}, file={
            "status": "skipped", "filename": "huge.pdf", "reason": "size", "error": "too large"}))
        self.receiver.receive(file_event("excluded", room="room-b"))
        job = self.job(rooms=[{"account_id": "account-a", "room_id": "room-a"}])
        batch = self.run_job(job)[0]
        self.assertEqual(batch["status"], "sent")
        archive, document = self.unpack(batch)
        self.assertEqual(document["message_count"], 5)
        for item in document["messages"]:
            if item["type"] != "file":
                continue
            attachment = item["file"]
            self.assertNotIn("base64", attachment)
            self.assertNotIn("url", attachment)
            if attachment["status"] == "available":
                self.assertEqual(attachment["path"], f'files/{item["event_id"]}/{filename}')
                content = archive.read(attachment["path"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), attachment["sha256"])
            else:
                self.assertNotIn("path", attachment)
        self.assertEqual(len(archive.namelist()), 5)

    def test_retry_keeps_fixed_file_bytes_after_restart(self):
        self.receiver.receive(file_event("retry", b"original"))
        job = self.job(delivery_mode="multipart", text="files included")
        self.destination.mode = "lost"
        self.run_job(job)
        first = self.destination.requests[-1]
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 0)
        restarted = Receiver(self.root)
        self.destination.mode = "ok"
        restarted.relay.trigger(job["job_id"])
        restarted.relay.tick()
        self.assertEqual(self.destination.requests[-1], first)
        self.assertEqual(restarted.relay.batches()[0]["status"], "sent")

    def test_files_count_towards_media_split_and_empty_file_is_valid(self):
        self.relay.max_media_bytes = 6
        for identifier, content in (("one", b"12345"), ("two", b"67890"), ("empty", b"")):
            self.receiver.receive(file_event(identifier, content))
        job = self.job()
        self.relay.trigger(job["job_id"])
        first_id = self.relay.prepare_batch(job["job_id"])
        first_doc = json.loads(zipfile.ZipFile(io.BytesIO(self.relay.archive(first_id))).read("messages.json"))
        self.assertEqual(first_doc["message_count"], 1)
        self.relay.deliver_batch(first_id)
        second_id = self.relay.prepare_batch(job["job_id"])
        archive = zipfile.ZipFile(io.BytesIO(self.relay.archive(second_id)))
        second_doc = json.loads(archive.read("messages.json"))
        self.assertEqual(second_doc["message_count"], 2)
        self.assertEqual(archive.read(second_doc["messages"][1]["file"]["path"]), b"")

    def test_missing_or_corrupt_file_aborts_batch_without_advancing_cursor(self):
        self.receiver.receive(file_event("corrupt"))
        job = self.job()
        self.relay.trigger(job["job_id"])
        for content in (None, b"wrong"):
            with database(self.receiver.db_path) as connection:
                connection.execute("UPDATE messages SET file_data=?", (content,))
            with self.assertRaises(ValueError):
                self.relay.prepare_batch(job["job_id"])
            self.assertEqual(self.relay.batches(), [])
            self.assertEqual(self.relay.job(job["job_id"])["cursor"], 0)

    def test_legacy_file_metadata_and_database_upgrade_remain_readable(self):
        self.receiver.receive(event("old-file", kind="file", raw={"file": {"filename": "legacy.pdf"}}))
        with database(self.receiver.db_path) as connection:
            original = dict(connection.execute("SELECT * FROM messages").fetchone())
            connection.execute("UPDATE messages SET search_text=''")
            connection.execute("ALTER TABLE messages DROP COLUMN file_data")
        restarted = Receiver(self.root)
        saved = restarted.messages()["messages"][0]
        self.assertEqual(saved["received_at"], original["received_at"])
        self.assertNotIn("file", saved)
        self.assertEqual(restarted.messages(filters={"q": "legacy.pdf"})["total"], 1)
        job = restarted.relay.save_job({"name": "legacy", "target_url": self.url, "enabled": True})
        restarted.relay.trigger(job["job_id"])
        restarted.relay.tick()
        self.assertEqual(restarted.relay.batches()[0]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
