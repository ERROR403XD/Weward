import base64
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from weward.common import Handler, database, encode_json, init_database, make_server, request_json
from weward.receiver import Receiver, ReceiverHandler, SCHEMA
from weward.relay import Relay, message_links
from weward.schedule import Cron


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1cAAAAASUVORK5CYII=")


def event(identifier, room="room-a", account="account-a", kind="text", **values):
    return {"schema_version": 1, "source": "webox", "event_id": hashlib.sha256(identifier.encode()).hexdigest(),
            "account_id": account, "message_id": identifier, "room_id": room, "room_name": "测试群 " + room,
            "room_remark": "webox.weward", "sender_id": "myself", "sender_name": "本人", "is_self": True,
            "type": kind, "collected_at": "2026-11-06T10:00:00Z", "message_time_ms": 1793959200000,
            "raw": {"text": {"content": "测试文字 https://example.com/article?a=1&b=2。"}}, **values}


def image_event(identifier, **values):
    return event(identifier, kind="image", raw={"image": {"sdkfileid": "inbox/image.png"}}, image={
        "status": "available", "content_type": "image/png", "size": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(), "base64": base64.b64encode(PNG).decode()}, **values)


class Destination(Handler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        batch_id = self.headers["X-Weward-Batch-Id"]
        self.server.requests.append((dict(self.headers), body))
        self.server.entered.set()
        self.server.release.wait(3)
        if self.server.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "/success")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        receipt = self.server.receipts.setdefault(batch_id, {"ok": True, "batch_id": batch_id, "receipt_id": "receipt-" + batch_id})
        if self.server.mode == "lost":
            self.send_json({"error": "stored but response lost"}, 503)
        elif self.server.mode == "wrong-id":
            self.send_json({**receipt, "batch_id": "different"})
        elif self.server.mode == "no-receipt":
            self.send_json({"ok": True, "batch_id": batch_id})
        elif self.server.mode == "rejected":
            self.send_json({**receipt, "ok": False})
        elif self.server.mode == "invalid-json":
            self.send_bytes(b"accepted", "text/plain")
        elif self.server.mode == "empty":
            self.send_bytes(b"", "application/json", 204)
        elif self.server.mode == "large":
            self.send_json({**receipt, "padding": "x" * 65536})
        elif self.server.mode == "list":
            self.send_json([receipt])
        else:
            self.send_json(receipt)


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.receiver = Receiver(self.root)
        self.relay = self.receiver.relay
        self.servers = []
        self.destination = make_server(("127.0.0.1", 0), Destination, None)
        self.destination.requests, self.destination.receipts = [], {}
        self.destination.mode = "ok"
        self.destination.entered, self.destination.release = threading.Event(), threading.Event()
        self.destination.release.set()
        self.start(self.destination)
        self.url = f"http://127.0.0.1:{self.destination.server_port}/upload"

    def start(self, server):
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        self.servers.append((server, worker))

    def tearDown(self):
        self.destination.release.set()
        for server, worker in self.servers:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.temporary.cleanup()

    def job(self, **values):
        return self.relay.save_job({"name": "每小时群消息", "target_url": self.url, "enabled": True, **values})

    def run_job(self, job):
        self.relay.trigger(job["job_id"])
        self.relay.tick()
        return self.relay.batches(job["job_id"])

    def unpack(self, batch):
        archive = zipfile.ZipFile(io.BytesIO(self.relay.archive(batch["batch_id"])))
        return archive, json.loads(archive.read("messages.json"))

    def test_zip_contains_selected_rooms_self_links_and_independent_images(self):
        events = [event("text"), image_event("image"), event("share", kind="link", raw={"link": {
            "title": "分享", "description": "另见 https://example.org/备用。", "link_url": "https://example.com/share?a=1&amp;b=2"}}),
            event("missing-image", kind="image", image={"status": "unavailable", "error": "上游未提供图片"}),
            event("excluded-room", room="room-b"), event("excluded-account", account="account-b")]
        for message in events:
            self.receiver.receive(message)
        job = self.job(rooms=[{"account_id": "account-a", "room_id": "room-a"}])
        batch = self.run_job(job)[0]
        self.assertEqual(batch["status"], "sent")
        archive, document = self.unpack(batch)
        with archive:
            self.assertEqual(document["message_count"], 4)
            self.assertEqual(document["range"], {"after_sequence": 0, "through_sequence": 6})
            self.assertTrue(all(message["is_self"] for message in document["messages"]))
            picture = document["messages"][1]["image"]
            self.assertEqual(archive.read(picture["path"]), PNG)
            self.assertNotIn("url", picture)
            self.assertNotIn("base64", picture)
            self.assertEqual(document["messages"][2]["links"], ["https://example.com/share?a=1&b=2", "https://example.org/备用"])
            self.assertEqual(document["messages"][3]["image"]["status"], "unavailable")
        headers, body = self.destination.requests[0]
        self.assertEqual(headers["Idempotency-Key"], batch["batch_id"])
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(hashlib.sha256(body).hexdigest(), batch["sha256"])
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 6)
        self.assertEqual(self.receiver.messages(filters={"job_id": job["job_id"], "state": "sent"})["total"], 4)

    def test_failed_receipt_never_marks_sent_and_retry_keeps_exact_bytes(self):
        self.receiver.receive(image_event("retry"))
        job = self.job()
        for mode in ("lost", "wrong-id", "no-receipt", "rejected", "invalid-json", "empty", "large", "list", "redirect"):
            with self.subTest(mode=mode):
                self.destination.mode = mode
                batch = self.run_job(job)[0]
                self.assertEqual(batch["status"], "pending")
                self.assertIsNone(batch["receipt"])
                self.assertEqual(self.relay.job(job["job_id"])["cursor"], 0)
                self.assertEqual(self.receiver.status()["sent_messages"], 0)
                self.assertTrue(batch["last_error"])
        self.assertEqual(len(self.destination.requests), 9)
        self.receiver = Receiver(self.root)
        self.relay = self.receiver.relay
        self.destination.mode = "ok"
        completed = self.run_job(job)[0]
        self.assertEqual(completed["status"], "sent")
        self.assertEqual(completed["attempts"], 10)
        self.assertEqual(len({body for headers, body in self.destination.requests}), 1)
        self.assertEqual(len(self.destination.receipts), 1)
        self.assertEqual(self.receiver.messages()["messages"][0]["deliveries"][0]["status"], "sent")

    def test_receipt_and_cursor_commit_together(self):
        self.receiver.receive(event("transaction"))
        job = self.job()
        with database(self.receiver.db_path) as connection:
            connection.execute("""CREATE TRIGGER reject_cursor BEFORE UPDATE OF cursor ON relay_jobs
                BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END""")
        batch = self.run_job(job)[0]
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 0)
        with database(self.receiver.db_path) as connection:
            connection.execute("DROP TRIGGER reject_cursor")
        self.assertEqual(self.run_job(job)[0]["status"], "sent")
        self.assertEqual(len(self.destination.receipts), 1)

    def test_chunk_cutoff_and_late_arrival_do_not_skip_records(self):
        self.relay.max_messages = 2
        for number in range(5):
            self.receiver.receive(event(str(number)))
        job = self.job()
        self.run_job(job)
        self.receiver.receive(event("late", message_time_ms=1000))
        self.relay.tick()
        self.relay.tick()
        batches = self.relay.batches(job["job_id"])
        self.assertEqual([batch["message_count"] for batch in batches], [1, 2, 2])
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 5)
        self.assertIsNone(self.relay.job(job["job_id"])["run_until"])
        self.assertEqual(self.relay.job(job["job_id"])["unsent_count"], 1)
        self.run_job(job)
        self.assertEqual(self.receiver.status()["sent_messages"], 6)
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 6)

    def test_image_size_budget_splits_without_losing_large_single_message(self):
        self.relay.max_image_bytes = len(PNG) - 1
        for identifier in ("image-one", "image-two"):
            self.receiver.receive(image_event(identifier))
        job = self.job()
        self.run_job(job)
        self.relay.tick()
        self.assertEqual([batch["message_count"] for batch in self.relay.batches()], [1, 1])
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 2)

    def test_jobs_have_independent_cursors_and_start_now_ignores_existing(self):
        self.receiver.receive(event("old"))
        historical, future = self.job(), self.job(start="now")
        self.assertEqual(self.receiver.messages(filters={"job_id": future["job_id"], "state": "pending"})["total"], 0)
        self.run_job(historical)
        self.assertEqual(self.relay.job(future["job_id"])["sent_count"], 0)
        self.receiver.receive(event("new"))
        self.run_job(future)
        self.assertEqual(self.relay.job(historical["job_id"])["unsent_count"], 1)
        self.assertEqual(self.relay.job(future["job_id"])["sent_count"], 1)
        self.run_job(historical)
        self.assertEqual(len(self.receiver.messages()["messages"][0]["deliveries"]), 2)

    def test_scheduler_catches_overdue_after_restart_and_pause_stops_retry(self):
        self.receiver.receive(event("scheduled"))
        job = self.job()
        with database(self.receiver.db_path) as connection:
            connection.execute("UPDATE relay_jobs SET next_run=? WHERE job_id=?", (time.time() - 3600, job["job_id"]))
        self.relay = Receiver(self.root).relay
        self.destination.mode = "lost"
        self.relay.tick()
        self.assertEqual(len(self.destination.requests), 1)
        self.assertGreater(self.relay.job(job["job_id"])["next_run"], time.time())
        self.relay.tick()
        self.assertEqual(len(self.destination.requests), 1)
        self.relay.set_enabled(job["job_id"], False)
        with self.assertRaises(ValueError):
            self.relay.trigger(job["job_id"])
        self.relay.tick()
        self.assertEqual(len(self.destination.requests), 1)
        self.destination.mode = "ok"
        self.relay.set_enabled(job["job_id"], True)
        self.run_job(job)
        self.assertEqual(self.relay.job(job["job_id"])["sent_count"], 1)

    def test_concurrent_workers_claim_only_one_delivery_and_expired_lease_recovers(self):
        self.receiver.receive(event("concurrency"))
        job = self.job()
        self.relay.trigger(job["job_id"])
        batch_id = self.relay.prepare_batch(job["job_id"])
        self.destination.release.clear()
        worker = threading.Thread(target=self.relay.deliver_batch, args=(batch_id,))
        worker.start()
        try:
            self.assertTrue(self.destination.entered.wait(2))
            other = Relay(self.receiver.db_path)
            self.assertFalse(other.deliver_batch(batch_id))
            with self.assertRaises(ValueError):
                self.relay.save_job({**job, "target_url": self.url + "-new"}, job["job_id"])
        finally:
            self.destination.release.set()
            worker.join(timeout=3)
        self.assertEqual(len(self.destination.requests), 1)
        self.receiver.receive(event("recovery"))
        self.relay.trigger(job["job_id"])
        batch_id = self.relay.prepare_batch(job["job_id"])
        with database(self.receiver.db_path) as connection:
            connection.execute("UPDATE relay_batches SET lease_until=?, claim_token='dead-worker' WHERE batch_id=?", (time.time() - 1, batch_id))
        self.assertTrue(Relay(self.receiver.db_path).deliver_batch(batch_id))
        self.assertEqual(self.relay.job(job["job_id"])["sent_count"], 2)

    def test_pending_destination_can_be_corrected_without_repacking(self):
        self.receiver.receive(event("bad-url"))
        job = self.job()
        self.destination.mode = "lost"
        batch = self.run_job(job)[0]
        original = self.relay.archive(batch["batch_id"])
        with self.assertRaises(ValueError):
            self.relay.save_job({**job, "rooms": [{"account_id": "account-a", "room_id": "room-a"}]}, job["job_id"])
        self.relay.save_job({**job, "target_url": self.url + "-corrected"}, job["job_id"])
        self.destination.mode = "ok"
        self.relay.tick()
        self.assertEqual(self.relay.batches()[0]["status"], "sent")
        self.assertEqual(self.relay.archive(batch["batch_id"]), original)

    def test_empty_or_nonmatching_run_does_not_create_or_advance(self):
        self.receiver.receive(event("not-selected", room="room-b"))
        job = self.job(rooms=[{"account_id": "account-a", "room_id": "room-a"}])
        self.assertEqual(self.run_job(job), [])
        self.assertEqual(self.relay.job(job["job_id"])["cursor"], 0)
        self.assertIsNone(self.relay.job(job["job_id"])["run_until"])
        self.receiver.receive(event("selected"))
        self.assertEqual(self.run_job(job)[0]["message_count"], 1)

    def test_notes_archiving_and_duplicates_do_not_change_export_or_receipt(self):
        message = event("managed")
        received = self.receiver.receive(message)
        self.receiver.manage_message(message["event_id"], {"note": "稍后整理", "archived": True})
        self.assertEqual(self.receiver.messages(filters={"archived": "0"})["total"], 0)
        self.assertEqual(self.receiver.messages(filters={"q": "稍后"})["total"], 1)
        job = self.job()
        self.run_job(job)
        duplicate = self.receiver.receive(message)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["received_at"], received["received_at"])
        current = self.receiver.messages()["messages"][0]
        self.assertEqual(current["note"], "稍后整理")
        self.assertTrue(current["archived"])
        self.assertEqual(current["deliveries"][0]["status"], "sent")

    def test_original_database_migration_preserves_identity_timestamp_and_image(self):
        old_root = self.root / "old"
        path = old_root / "receiver.sqlite3"
        init_database(path, SCHEMA)
        message = image_event("legacy")
        del message["image"]["base64"]
        with database(path) as connection:
            connection.execute("INSERT INTO messages (sequence,event_id,received_at,payload,image,content_type) VALUES (42,?,?,?,?,?)",
                               (message["event_id"], "2026-01-01T00:00:00Z", encode_json(message).decode(), PNG, "image/png"))
        migrated = Receiver(old_root)
        self.assertEqual(migrated.messages()["messages"][0]["sequence"], 42)
        self.assertEqual(migrated.messages()["messages"][0]["received_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(migrated.rooms()[0]["room_id"], "room-a")
        self.assertEqual(Receiver(old_root).messages()["total"], 1)
        self.assertEqual(migrated.receive(image_event("legacy"))["sequence"], 42)
        with database(path) as connection:
            self.assertEqual(connection.execute("SELECT image FROM messages").fetchone()[0], PNG)

    def test_management_http_queries_validation_and_download(self):
        server = make_server(("127.0.0.1", 0), ReceiverHandler, self.receiver)
        self.start(server)
        base = f"http://127.0.0.1:{server.server_port}"
        for message in (event("http-a"), event("http-b", room="room-b")):
            request_json(base + "/api/messages", message)
        self.assertEqual(len(request_json(base + "/api/rooms")["rooms"]), 2)
        self.assertEqual(request_json(base + "/api/messages?room_id=room-a&type=text&q=example")["total"], 1)
        latest = request_json(base + "/api/messages?limit=1")["messages"][0]
        self.assertEqual(len(request_json(base + f"/api/messages?before={latest['sequence']}")["messages"]), 1)
        job = request_json(base + "/api/jobs", {"name": "HTTP 任务", "target_url": self.url, "enabled": False})
        request_json(base + f"/api/jobs/{job['job_id']}/enabled", {"enabled": True})
        request_json(base + f"/api/jobs/{job['job_id']}/run", {})
        self.relay.tick()
        batch = request_json(base + "/api/batches")["batches"][0]
        self.assertEqual(batch["status"], "sent")
        with urllib.request.urlopen(base + f"/api/batches/{batch['batch_id']}/download") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), batch["sha256"])
        request_json(base + f"/api/messages/{latest['event_id']}/manage", {"archived": True, "note": "HTTP 备注"})
        self.assertEqual(request_json(base + "/api/status")["archived"], 1)
        self.assertEqual(request_json(base + "/api/messages?state=sent")["total"], 2)
        with self.assertRaises(urllib.error.HTTPError) as error:
            request_json(base + "/api/messages?state=unknown")
        self.assertEqual(error.exception.code, 400)
        with urllib.request.urlopen(base + "/receiver.js") as response:
            self.assertIn("javascript", response.headers["Content-Type"])

    def test_invalid_job_and_management_inputs(self):
        for values in ({"cron": "bad"}, {"cron": "0 0 30 2 *"}, {"timezone": "Nowhere/Invalid"},
                       {"enabled": "false"}, {"rooms": ["room-a"]}, {"rooms": [{}]}, {"target_url": "file:///tmp/package"},
                       {"target_url": "http://example.com:bad"}, {"target_url": "http://user@example.com"}, {"start": "yesterday"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.job(**values)
        self.receiver.receive(event("validation"))
        with self.assertRaises(ValueError):
            self.receiver.manage_message(event("validation")["event_id"], {"archived": "false"})

    def test_batch_pagination_and_scope(self):
        first, second = self.job(name="one"), self.job(name="two")
        self.receiver.receive(event("one"))
        self.run_job(first)
        self.run_job(second)
        latest = self.relay.batches(limit=1)[0]
        older = self.relay.batches(before=latest["sequence"])
        self.assertEqual(len(older), 1)
        self.assertEqual(older[0]["job_id"], first["job_id"])
        self.assertEqual(self.relay.batches(second["job_id"])[0]["job_id"], second["job_id"])

    def test_corrupted_image_is_visible_and_never_advances_cursor(self):
        self.receiver.receive(image_event("corrupt"))
        with database(self.receiver.db_path) as connection:
            connection.execute("UPDATE messages SET image=?", (b"broken-image",))
        job = self.job()
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.run_job(job), [])
        current = self.relay.job(job["job_id"])
        self.assertEqual(current["cursor"], 0)
        self.assertIn("图片缺失或损坏", current["last_error"])
        with database(self.receiver.db_path) as connection:
            connection.execute("UPDATE messages SET image=?", (PNG,))
        self.relay.tick()
        self.assertIsNone(self.relay.job(job["job_id"])["last_error"])
        self.assertEqual(self.relay.batches()[0]["status"], "sent")

    def test_invalid_image_and_query_metadata_are_rejected(self):
        for values in ({"kind": "image", "image": {}}, {"image": "invalid"}, {"room_name": []}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.receiver.receive(event("invalid", **values))


class CronTest(unittest.TestCase):
    @staticmethod
    def timestamp(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()

    def assert_next(self, expression, zone, after, expected):
        self.assertEqual(Cron(expression, zone).next_after(self.timestamp(after)), self.timestamp(expected))

    def test_steps_ranges_lists_and_timezone(self):
        self.assert_next("*/15 9-17 * * 1-5", "Asia/Shanghai", "2026-11-06T01:00:00", "2026-11-06T01:15:00")
        self.assert_next("5,35 * * * *", "UTC", "2026-11-06T01:35:01", "2026-11-06T02:05:00")
        self.assert_next("0 9 * * 7", "Asia/Shanghai", "2026-11-06T00:00:00", "2026-11-08T01:00:00")

    def test_day_and_weekday_use_or_when_both_restricted(self):
        self.assert_next("0 0 10 * 1", "UTC", "2026-11-08T00:00:00", "2026-11-09T00:00:00")
        self.assert_next("0 0 10 * 1", "UTC", "2026-11-09T00:00:00", "2026-11-10T00:00:00")
        self.assert_next("0 0 * * 1", "UTC", "2026-11-09T00:00:00", "2026-11-16T00:00:00")

    def test_dst_skips_nonexistent_and_handles_both_repeated_times(self):
        self.assert_next("30 2 * * *", "America/New_York", "2026-03-08T06:00:00", "2026-03-09T06:30:00")
        self.assert_next("30 1 * * *", "America/New_York", "2026-11-01T05:00:00", "2026-11-01T05:30:00")
        self.assert_next("30 1 * * *", "America/New_York", "2026-11-01T05:30:00", "2026-11-01T06:30:00")

    def test_leap_year_and_rejected_syntax(self):
        self.assert_next("0 0 29 2 *", "UTC", "2026-11-06T00:00:00", "2028-02-29T00:00:00")
        for expression in ("* * * *", "60 * * * *", "*/0 * * * *", "0 24 * * *", "0 0 * 13 *", "0 0 * * 8", "0 0 5-1 * *", "1/2/3 * * * *", "0 0 * * MON"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                Cron(expression, "UTC")

    def test_link_extraction_preserves_query_unicode_and_balanced_parentheses(self):
        message = event("links", raw={"text": {"content": '(https://example.com/page_(test)). https://例子.com/路径?q=a&amp;b=2， https://example.com/page_(test)'}})
        self.assertEqual(message_links(message), ["https://example.com/page_(test)", "https://例子.com/路径?q=a&b=2"])
