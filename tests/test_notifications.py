import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_relay import Destination, event
from weward.common import Handler, database, make_server, request_json, utc_now
from weward.notifications import Notifications, defaults
from weward.receiver import Receiver, ReceiverHandler


class NotificationEndpoint(Handler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((dict(self.headers), body))
        if self.server.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "/redirected")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.server.mode == "fail":
            self.send_json({"ok": False}, 503)
        elif self.server.mode == "empty":
            self.send_bytes(b"", "text/plain", 204)
        else:
            self.send_json({"data": {"ok": self.server.mode == "ok"}, "code": 0})


class NotificationsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Receiver(self.temporary.name)
        self.notifications = self.app.notifications
        self.endpoint = make_server(("127.0.0.1", 0), NotificationEndpoint, None)
        self.endpoint.requests, self.endpoint.mode = [], "ok"
        self.servers = []
        self.start(self.endpoint)
        self.url = f"http://127.0.0.1:{self.endpoint.server_port}/notify"
        self.config = defaults()
        self.config.update(enabled=True, target_url=self.url)
        self.notifications.save(self.config)

    def start(self, server):
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        self.servers.append((server, worker))

    def tearDown(self):
        for server, worker in self.servers:
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.temporary.cleanup()

    def emit(self, event_name="delivery_failed", **values):
        with database(self.app.db_path) as connection:
            return self.notifications.enqueue(connection, event_name, **values)

    def test_templates_escape_json_without_reexpanding_values(self):
        self.config["body"] = {"nested": [{"text": "{{message}}", "count": 1, "missing": "{{receipt_id}}"}]}
        self.config["events"]["delivery_failed"]["message"] = "{{job_name}}\n{{error}}"
        self.notifications.save(self.config)
        self.emit(job_name='群聊 "A"', error='换行\n\\{{job_id}}')
        self.notifications.deliver_once()
        self.assertEqual(json.loads(self.endpoint.requests[0][1])["nested"][0],
                         {"text": '群聊 "A"\n换行\n\\{{job_id}}', "count": 1, "missing": ""})

    def test_validation(self):
        for change in ({"body": {"text": "{{typo}}"}}, {"headers": {"Host": "elsewhere"}},
                       {"headers": {"Authorization": "bad\r\nvalue"}}, {"events": []},
                       {"target_url": "file:///tmp/test"}, {"response_field": "result..ok"}, {"body": "{{message}}"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.notifications.save({**self.config, **change})
        self.config["events"]["delivery_failed"]["message"] = "{{message}}"
        with self.assertRaises(ValueError):
            self.notifications.save(self.config)

    def test_response_predicate_and_retry_identity_survive_restart(self):
        self.config.update(response_field="data.ok", response_value=True)
        self.notifications.save(self.config)
        identifier = self.emit()
        self.endpoint.mode = "reject"
        self.notifications.deliver_once()
        failed = self.notifications.history()[0]
        self.assertEqual((failed["status"], failed["attempts"]), ("pending", 1))
        restarted = Notifications(self.app.db_path, "http://unused")
        self.endpoint.mode = "ok"
        self.assertFalse(restarted.deliver_once())
        restarted.deliver_once(now=failed["next_attempt"] + 1)
        self.assertEqual(restarted.history()[0]["status"], "sent")
        self.assertEqual(self.endpoint.requests[0], self.endpoint.requests[1])
        self.assertEqual(self.endpoint.requests[0][0]["Idempotency-Key"], identifier)

    def test_redirect_does_not_forward_and_stops_after_eight_attempts(self):
        self.emit()
        self.endpoint.mode = "redirect"
        for attempt in range(8):
            self.assertTrue(self.notifications.deliver_once(now=time.time() + attempt * 4000))
        history = self.notifications.history()
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["status"], history[0]["attempts"]), ("failed", 8))
        self.assertEqual(len(self.endpoint.requests), 8)
        self.assertFalse(self.notifications.deliver_once(now=time.time() + 999999))
        self.assertIn("307", history[0]["last_error"])

    def test_empty_2xx_and_type_strict_json_predicate(self):
        self.endpoint.mode = "empty"
        self.emit()
        self.notifications.deliver_once()
        self.assertEqual(self.notifications.history()[0]["status"], "sent")
        self.config.update(response_field="code", response_value=False)
        self.notifications.save(self.config)
        self.endpoint.mode = "ok"
        self.emit()
        self.notifications.deliver_once()
        self.assertEqual(self.notifications.history()[0]["status"], "pending")

    def test_config_persistence_and_queued_body_snapshot(self):
        self.emit(job_name="original")
        self.config["body"] = {"changed": True}
        self.notifications.save(self.config)
        restarted = Notifications(self.app.db_path, "http://unused")
        self.assertEqual(restarted.config()["body"], {"changed": True})
        restarted.deliver_once()
        self.assertIn("original", json.loads(self.endpoint.requests[0][1])["text"])

    def test_disable_cancels_pending_and_reenable_does_not_resurrect(self):
        self.emit()
        self.config["enabled"] = False
        self.notifications.save(self.config)
        self.assertEqual(self.notifications.history()[0]["status"], "cancelled")
        self.assertIsNone(self.emit())
        self.config["enabled"] = True
        self.config["events"]["delivery_failed"]["enabled"] = False
        self.notifications.save(self.config)
        self.assertIsNone(self.emit())
        self.assertFalse(self.notifications.deliver_once())
        self.assertIsNotNone(self.emit("delivery_started"))

    def test_observation_debounce_deduplication_and_restart(self):
        for online in (True, False, True):
            self.notifications.observe(online)
        self.assertEqual(self.notifications.history(), [])
        self.notifications.observe(True)
        restarted = Notifications(self.app.db_path, "http://unused")
        for unused in range(4):
            restarted.observe(True)
        self.assertEqual(len(self.notifications.history()), 1)
        for unused in range(5):
            restarted.observe(False, "微信未登录")
        self.assertEqual([item["event"] for item in self.notifications.history()], ["collector_offline", "collector_online"])
        restarted.observe(True)
        restarted.observe(True)
        self.assertEqual(len(self.notifications.history()), 3)

    def test_monitor_unreachable_unready_stale_and_recovered(self):
        for response in ({"ready": False, "phase": "waiting"}, {"ready": True, "last_poll_at": "2020-01-01T00:00:00Z"}):
            self.notifications.candidate = None
            with database(self.app.db_path) as connection:
                connection.execute("UPDATE notification_state SET online=1")
            with patch("weward.notifications.request_json", return_value=response):
                for unused in range(3):
                    self.notifications.check_collector()
            self.assertEqual(self.notifications.history()[0]["event"], "collector_offline")
        with patch("weward.notifications.request_json", return_value={"ready": True, "last_poll_at": utc_now()}):
            self.notifications.check_collector()
            self.notifications.check_collector()
        with patch("weward.notifications.request_json", side_effect=OSError("unreachable")):
            for unused in range(3):
                self.notifications.check_collector()
        self.assertEqual(self.notifications.history()[0]["event"], "collector_offline")

    def test_enqueue_rolls_back_with_domain_transaction(self):
        with self.assertRaises(RuntimeError):
            with database(self.app.db_path) as connection:
                self.notifications.enqueue(connection, "delivery_failed")
                raise RuntimeError("rollback")
        self.assertEqual(self.notifications.history(), [])

    def test_concurrent_claim(self):
        self.emit()
        workers = [threading.Thread(target=self.notifications.deliver_once) for unused in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        self.assertEqual(len(self.endpoint.requests), 1)

    def test_api(self):
        server = make_server(("127.0.0.1", 0), ReceiverHandler, self.app)
        self.start(server)
        url = f"http://127.0.0.1:{server.server_port}/api/notifications"
        self.assertEqual(request_json(url)["config"], self.config)
        self.config["enabled"] = False
        self.assertFalse(request_json(url, self.config)["config"]["enabled"])

    def test_relay_failure_and_success_do_not_wait_for_notifications(self):
        destination = make_server(("127.0.0.1", 0), Destination, None)
        destination.requests, destination.receipts, destination.mode = [], {}, "wrong-id"
        destination.paths = []
        destination.entered, destination.release = threading.Event(), threading.Event()
        destination.release.set()
        self.start(destination)
        self.app.receive(event("one"))
        relay = self.app.relay
        job = relay.save_job({"name": "job", "target_url": f"http://127.0.0.1:{destination.server_port}/", "enabled": True})
        relay.trigger(job["job_id"])
        relay.tick()
        self.assertEqual([item["event"] for item in self.notifications.history()], ["delivery_failed", "delivery_started"])
        self.assertEqual(relay.job(job["job_id"])["cursor"], 0)
        self.assertEqual(self.endpoint.requests, [])
        self.endpoint.mode = "fail"
        self.notifications.deliver_once()
        destination.mode = "ok"
        relay.trigger(job["job_id"])
        relay.tick()
        self.assertEqual(relay.job(job["job_id"])["cursor"], 1)
        self.assertEqual(self.notifications.history()[0]["event"], "delivery_succeeded")
        with database(self.app.db_path) as connection:
            body = connection.execute("SELECT body FROM notification_outbox WHERE event='delivery_failed'").fetchone()["body"]
        self.assertIn("HTTP 200", json.loads(body)["text"])

    def test_http_failure_status_placeholder(self):
        self.app.receive(event("one"))
        job = self.app.relay.save_job({"name": "job", "target_url": self.url, "enabled": True})
        self.endpoint.mode = "fail"
        self.app.relay.trigger(job["job_id"])
        self.app.relay.tick()
        with database(self.app.db_path) as connection:
            body = connection.execute("SELECT body FROM notification_outbox WHERE event='delivery_failed'").fetchone()["body"]
        self.assertIn("HTTP 503", json.loads(body)["text"])


class TimeOrderingTest(unittest.TestCase):
    def test_delayed_messages_and_equal_timestamps_paginate_without_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Receiver(directory)
            for identifier, timestamp in (("new", 3000), ("old", 1000), ("middle", 2000), ("tie", 3000)):
                app.receive(event(identifier, message_time_ms=timestamp))
            first = app.messages(limit=2)
            second = app.messages(before=first["messages"][-1]["sequence"], limit=2)
            self.assertEqual([item["message_id"] for item in first["messages"] + second["messages"]], ["tie", "new", "middle", "old"])
            self.assertEqual((first["total"], second["total"]), (4, 4))
            app.receive(event("other", room="room-b", message_time_ms=2500))
            self.assertEqual([room["room_id"] for room in app.rooms()], ["room-a", "room-b"])
            self.assertEqual(app.rooms()[0]["last_message_time_ms"], 3000)
            job = app.relay.save_job({"name": "cursor", "target_url": "http://unused", "start": "now"})
            self.assertEqual(job["cursor"], 5)
            app.receive(event("late-arrival", message_time_ms=500))
            self.assertEqual(app.relay.job(job["job_id"])["unsent_count"], 1)
