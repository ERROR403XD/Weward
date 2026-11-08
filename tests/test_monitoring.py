import unittest
from unittest.mock import patch
import urllib.error

import test_pipeline
from test_pipeline import ROOM, message
from weward.collector import CollectorHandler
from weward.common import database, make_server, request_json


class MonitoringTest(unittest.TestCase):
    setUp = test_pipeline.PipelineTest.setUp
    tearDown = test_pipeline.PipelineTest.tearDown
    start_server = test_pipeline.PipelineTest.start_server
    new_collector = test_pipeline.PipelineTest.new_collector
    deliver_all = test_pipeline.PipelineTest.deliver_all

    def add_group(self, room_id="new@chatroom", name="新的群"):
        self.fake.groups = [*self.fake.contacts, {"roomid": room_id, "nickname": name, "remark": "普通备注"}]
        return {"action": "add", "account_id": "self-account", "room_id": room_id}

    def test_search_add_plain_name_and_preserve_original_groups(self):
        values = self.add_group()
        result = self.collector.monitoring("新的")
        self.assertEqual(result["matches"][0]["room_id"], "new@chatroom")
        self.assertFalse(result["matches"][0]["monitored"])
        configured = self.collector.configure_monitoring(values)
        self.assertEqual({room["room_id"] for room in configured["rooms"]}, {ROOM, "new@chatroom"})
        self.fake.selections = []
        self.fake.messages = [message("original"), message("new", room="new@chatroom"), message("excluded", room="other@chatroom")]
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["total"], 2)
        self.assertEqual(set(self.fake.selections[0]), {ROOM, "new@chatroom"})
        self.assertEqual(self.new_collector().monitoring()["rooms"], configured["rooms"])

    def test_pause_remove_and_resume_keeps_cursor_and_records(self):
        values = self.add_group()
        self.collector.configure_monitoring(values)
        self.fake.messages = [message("new-one", room="new@chatroom")]
        self.collector.poll_once()
        self.deliver_all()
        with database(self.collector.db_path) as connection:
            before = dict(connection.execute("SELECT scope,cursor FROM cursors"))
        self.collector.configure_monitoring({**values, "action": "set", "enabled": False})
        self.fake.messages.append(message("new-two", room="new@chatroom"))
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["total"], 1)
        self.collector.configure_monitoring({**values, "action": "remove"})
        self.assertEqual(len(self.collector.monitoring()["rooms"]), 1)
        self.collector.configure_monitoring(values)
        with database(self.collector.db_path) as connection:
            after = dict(connection.execute("SELECT scope,cursor FROM cursors"))
        for scope, cursor in before.items():
            if "new@chatroom" in scope:
                self.assertEqual(after[scope], cursor)
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["total"], 2)

    def test_pause_all_is_idle_and_does_not_poll_messages(self):
        self.collector.configure_monitoring({"action": "set", "account_id": "self-account", "room_id": ROOM, "enabled": False})
        self.collector.poll_once()
        self.assertTrue(self.collector.status()["ready"])
        self.assertEqual(self.collector.status()["phase"], "idle")
        self.assertEqual(self.fake.request_cursors, [])

    def test_monitor_change_during_poll_discards_old_snapshot(self):
        values = self.add_group()
        self.collector.configure_monitoring(values)
        self.fake.messages = [message("racing", room="new@chatroom")]
        poll = self.collector.poll_updates
        def changed(cursor, room_ids=None, file_config=None):
            result = poll(cursor, room_ids, file_config)
            self.collector.configure_monitoring({**values, "action": "set", "enabled": False})
            return result
        with patch.object(self.collector, "poll_updates", side_effect=changed):
            self.collector.poll_once()
        self.assertEqual(self.collector.status()["queue"]["total"], 0)

    def test_duplicate_names_choose_ids_and_validate_account_and_group(self):
        values = self.add_group(name="测试群")
        self.assertEqual(len(self.collector.monitoring("测试群")["matches"]), 2)
        with self.assertRaises(ValueError):
            self.collector.configure_monitoring({**values, "account_id": "wrong-account"})
        with self.assertRaises(ValueError):
            self.collector.configure_monitoring({**values, "room_id": "private"})
        with self.assertRaises(ValueError):
            self.collector.configure_monitoring({**values, "room_id": "unknown@chatroom"})
        self.collector.configure_monitoring(values)
        self.assertEqual(len(self.collector.monitoring()["rooms"]), 2)

    def test_receiver_proxies_only_collector_control_and_preserves_errors(self):
        values = self.add_group()
        server = make_server(("127.0.0.1", 0), CollectorHandler, self.collector)
        self.start_server(server)
        self.receiver.collector_url = f"http://127.0.0.1:{server.server_port}"
        base = self.target.removesuffix("/api/messages")
        result = request_json(base + "/api/monitoring?query=%E6%96%B0")
        self.assertEqual(len(result["matches"]), 1)
        configured = request_json(base + "/api/monitoring", values)
        self.assertEqual(len(configured["rooms"]), 2)
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            request_json(base + "/api/monitoring", {**values, "account_id": "wrong"})
        self.assertEqual(invalid.exception.code, 400)
        self.receiver.collector_url = "http://127.0.0.1:1"
        with self.assertRaises(urllib.error.HTTPError) as offline:
            request_json(base + "/api/monitoring")
        self.assertEqual(offline.exception.code, 502)
        self.assertEqual(request_json(base + "/api/messages")["total"], 0)
