import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from weward.collector import Collector
from weward.common import Handler, database, make_server, request_json
from weward.receiver import Receiver, ReceiverHandler


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1cAAAAASUVORK5CYII=")
ROOM = "test-room@chatroom"


def message(identifier, kind="text", sender="member-a", room=ROOM, **body):
    return {"msgid": identifier, "action": "send", "from": sender, "tolist": [], "roomid": room,
            "msgtime": int(time.time() * 1000), "msgtype": kind,
            **({"text": {"content": "无需 @ 的普通群消息"}} if kind == "text" else {}), **body}


class FakeWeBox(Handler):
    def authorize(self):
        self.server.calls.append((self.command, self.path))
        valid = (self.headers.get("Authorization") == "Bearer test-token"
                 and self.headers.get("AuthorizationType") == "ilink_bot_token"
                 and self.headers.get("X-WECHAT-UIN"))
        if not valid:
            self.send_json({"error": "unauthorized"}, 401)
        return valid

    def do_GET(self):
        if not self.authorize():
            return
        if self.path == "/ilink/bot/userinfo":
            self.send_json({"ret": 0, "account_id": "self-account", "nickname": "本人", "wechat_id": "account"})
        elif self.path.startswith("/ilink/bot/contacts?"):
            self.send_json({"ret": 0, "contacts": self.server.contacts})
        elif self.path.startswith("/ilink/bot/groups?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("query", [""])[0]
            groups = getattr(self.server, "groups", self.server.contacts)
            self.send_json({"ret": 0, "contacts": [room for room in groups if query.lower() in room.get("nickname", "").lower() or query == room["roomid"]]})
        else:
            self.send_json({"error": "unknown route"}, 404)

    def do_POST(self):
        if not self.authorize():
            return
        if self.path != "/ilink/bot/getupdates":
            self.send_json({"error": "send forbidden"}, 405)
            return
        body = self.read_json()
        self.server.request_cursors.append(body["cursor"])
        if hasattr(self.server, "selections"):
            self.server.selections.append(body.get("room_ids"))
        self.send_json({"ret": 0, "cursor": body["cursor"] if self.server.replay else str(int(time.time() * 1000)),
                        "messages": self.server.batches.get(body["cursor"], self.server.messages)})


class FailingReceiver(ReceiverHandler):
    def do_POST(self):
        if self.server.failure == "before":
            self.read_json()
            self.send_json({"error": "temporary outage"}, 503)
        elif self.server.failure == "after":
            self.server.app.receive(self.read_json())
            self.send_json({"error": "acknowledgement lost"}, 503)
        elif self.server.failure == "redirect":
            self.read_json()
            self.send_response(302)
            self.send_header("Location", "/healthz")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            super().do_POST()


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.servers = []
        self.token = self.root / "api-token"
        self.token.write_text("test-token")
        self.shared = self.root / "shared"
        self.image_path = self.shared / "rooms" / ROOM / "inbox" / "sample.png"
        self.image_path.parent.mkdir(parents=True)
        self.image_path.write_bytes(PNG)
        self.fake = make_server(("127.0.0.1", 0), FakeWeBox, None)
        self.fake.messages, self.fake.calls, self.fake.request_cursors = [], [], []
        self.fake.replay = False
        self.fake.batches = {}
        self.fake.contacts = [{"roomid": ROOM, "remark": "webox.weward", "nickname": "测试群"}]
        self.start_server(self.fake)
        self.receiver = Receiver(self.root / "receiver")
        self.receive_server = make_server(("127.0.0.1", 0), FailingReceiver, self.receiver)
        self.receive_server.failure = None
        self.start_server(self.receive_server)
        self.target = f"http://127.0.0.1:{self.receive_server.server_port}/api/messages"
        self.collector = self.new_collector()

    def start_server(self, server):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.servers.append((server, worker))

    def new_collector(self):
        return Collector(self.root / "collector", f"http://127.0.0.1:{self.fake.server_port}", self.token,
                         self.shared, {"room_remark": "webox.weward", "target_url": self.target})

    def tearDown(self):
        for server, worker in self.servers:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.temporary.cleanup()

    def deliver_all(self):
        while self.collector.deliver_once():
            pass

    def test_all_members_text_images_links_and_missing_image_over_http(self):
        for sender in ("self-account", "member-a"):
            self.fake.messages.extend([
                message(sender + "-text", sender=sender),
                message(sender + "-image", "image", sender, image={"sdkfileid": "inbox/sample.png"}),
                message(sender + "-link", "link", sender, link={"title": "分享链接", "description": "描述", "link_url": "https://example.com/article", "image_url": ""}),
            ])
        self.fake.messages.extend([
            message("missing", "image", image={"sdkfileid": ""}),
            message("private", room="private-contact"),
            message("other-group", room="other@chatroom"),
        ])
        self.collector.poll_once()
        self.assertEqual(self.collector.status()["queue"]["pending"], 7)
        self.image_path.unlink()
        self.deliver_all()
        result = request_json(self.target)
        self.assertEqual(result["total"], 7)
        self.assertEqual(sum(event["is_self"] for event in result["messages"]), 3)
        self.assertEqual({event["room_id"] for event in result["messages"]}, {ROOM})
        self.assertEqual({event["sender_id"] for event in result["messages"]}, {"self-account", "member-a"})
        missing = next(event for event in result["messages"] if event["message_id"] == "missing")
        self.assertEqual(missing["image"]["status"], "unavailable")
        self.assertIn("未提供", missing["image"]["error"])
        for event in result["messages"]:
            self.assertTrue(event["received_at"])
            if event.get("image", {}).get("status") == "available":
                with urllib.request.urlopen(self.target.replace("/api/messages", event["image"]["url"])) as response:
                    self.assertEqual(response.read(), PNG)
                    self.assertEqual(response.headers["Content-Type"], "image/png")
                self.assertNotIn("base64", event["image"])
        self.assertTrue(all(path != "/ilink/bot/sendmessage" for method, path in self.fake.calls))

    def test_failure_retry_persists_across_restart(self):
        self.fake.messages = [message("retry")]
        self.collector.poll_once()
        self.receive_server.failure = "before"
        self.collector.deliver_once()
        self.assertEqual(self.receiver.messages()["total"], 0)
        record = self.collector.status()["recent_deliveries"][0]
        self.assertEqual(record["attempts"], 1)
        self.assertIn("503", record["last_error"])
        self.assertGreater(record["next_attempt"], time.time())
        self.collector = self.new_collector()
        self.assertEqual(self.collector.status()["queue"]["pending"], 1)
        self.receive_server.failure = None
        self.collector.retry_pending()
        self.deliver_all()
        reopened = Receiver(self.root / "receiver")
        self.assertEqual(reopened.messages()["total"], 1)
        self.assertEqual(reopened.messages()["messages"][0]["delivery_attempt"], "2")
        self.assertEqual(self.collector.status()["queue"]["delivered"], 1)

    def test_lost_acknowledgement_and_replayed_cursor_do_not_duplicate(self):
        self.fake.messages = [message("duplicate")]
        self.fake.replay = True
        self.collector.poll_once()
        self.collector.poll_once()
        self.assertEqual(self.collector.status()["queue"]["total"], 1)
        self.assertEqual(*self.fake.request_cursors)
        self.receive_server.failure = "after"
        self.collector.deliver_once()
        original_time = self.receiver.messages()["messages"][0]["received_at"]
        self.collector = self.new_collector()
        self.receive_server.failure = None
        self.collector.retry_pending()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["total"], 1)
        self.assertEqual(self.receiver.messages()["messages"][0]["received_at"], original_time)

    def test_invalid_batch_does_not_commit_cursor_or_partial_outbox(self):
        self.fake.messages = [message("good"), {**message("bad"), "from": ""}]
        with self.assertRaises(ValueError):
            self.collector.poll_once()
        self.assertEqual(self.collector.status()["queue"]["total"], 0)
        with database(self.collector.db_path) as connection:
            cursor = connection.execute("SELECT cursor FROM cursors").fetchone()[0]
        self.assertEqual(cursor, self.fake.request_cursors[0])
        self.fake.messages.pop()
        self.collector.poll_once()
        self.assertEqual(self.collector.status()["queue"]["total"], 1)

    def test_idle_null_messages_remains_ready_and_resumes_delivery(self):
        self.fake.messages = None
        self.fake.replay = True
        self.collector.poll_once()
        status = self.collector.status()
        self.assertTrue(status["ready"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["queue"]["total"], 0)
        self.assertEqual(status["cursor"], self.fake.request_cursors[0])
        self.fake.messages = [message("after-idle")]
        self.fake.replay = False
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["messages"][0]["message_id"], "after-idle")

    def test_invalid_message_collection_keeps_cursor(self):
        for messages in ({}, "", False):
            with self.subTest(messages=messages):
                self.fake.messages = messages
                with self.assertRaisesRegex(ValueError, "消息数组"):
                    self.collector.poll_once()
                self.assertEqual(self.collector.status()["queue"]["total"], 0)
                with database(self.collector.db_path) as connection:
                    cursor = connection.execute("SELECT cursor FROM cursors").fetchone()[0]
                self.assertEqual(cursor, self.fake.request_cursors[0])

    def test_empty_duplicate_or_private_contacts_never_start_capture(self):
        for contacts in ([], self.fake.contacts * 2, [{"roomid": "person", "remark": "webox.weward"}]):
            with self.subTest(contacts=contacts):
                self.fake.contacts = contacts
                with self.assertRaisesRegex(RuntimeError, "不同的群"):
                    self.collector.poll_once()
        self.assertEqual(self.fake.request_cursors, [])

    def test_multiple_rooms_keep_group_identity_images_and_deduplication(self):
        second_room = "second-room@chatroom"
        self.fake.contacts.append({"roomid": second_room, "remark": "webox.weward", "nickname": "第二个群"})
        second_image = self.shared / "rooms" / second_room / "inbox" / "sample.png"
        second_image.parent.mkdir(parents=True)
        second_image.write_bytes(PNG + b"second-room")
        for room_id in (ROOM, second_room):
            self.fake.messages.extend([message("shared-id", room=room_id),
                                       message("shared-image", "image", "self-account", room_id,
                                               image={"sdkfileid": "inbox/sample.png"})])
        self.fake.messages.append(message("unwatched", room="other@chatroom"))
        self.collector.poll_once()
        self.assertEqual(len(self.fake.request_cursors), 1)
        self.deliver_all()
        result = self.receiver.messages()
        self.assertEqual(result["total"], 4)
        self.assertEqual({event["room_id"]: event["room_name"] for event in result["messages"]},
                         {ROOM: "测试群", second_room: "第二个群"})
        self.assertEqual(len({event["event_id"] for event in result["messages"]}), 4)
        for event in result["messages"]:
            if event["type"] == "image":
                with urllib.request.urlopen(self.target.replace("/api/messages", event["image"]["url"])) as response:
                    self.assertEqual(response.read(), PNG if event["room_id"] == ROOM else PNG + b"second-room")
                self.assertTrue(event["is_self"])
        received_at = {event["event_id"]: event["received_at"] for event in result["messages"]}
        self.collector = self.new_collector()
        self.collector.poll_once()
        self.deliver_all()
        self.assertEqual({event["event_id"]: event["received_at"] for event in self.receiver.messages()["messages"]},
                         received_at)
        self.assertEqual(len(self.collector.status()["rooms"]), 2)

    def test_added_room_has_own_lookback_and_readded_room_resumes(self):
        second_room = "second-room@chatroom"
        first_scope = json.dumps(["self-account", ROOM], separators=(",", ":"))
        second_scope = json.dumps(["self-account", second_room], separators=(",", ":"))
        with database(self.collector.db_path) as connection:
            connection.execute("INSERT INTO cursors VALUES (?, ?)", (first_scope, "100000"))
        second_contact = {"roomid": second_room, "remark": "webox.weward", "nickname": "新增群"}
        self.fake.contacts.append(second_contact)
        self.fake.batches = {
            "100000": [message("original-backlog"), message("before-new-lookback", room=second_room)],
            "699999": [message("new-room", room=second_room), message("unrelated-replay")],
        }
        with patch("weward.collector.time.time", return_value=1000):
            self.collector.poll_once()
        self.assertEqual(set(self.fake.request_cursors), {"100000", "699999"})
        self.deliver_all()
        self.assertEqual({event["message_id"] for event in self.receiver.messages()["messages"]},
                         {"original-backlog", "new-room"})
        self.fake.contacts.pop()
        with patch("weward.collector.time.time", return_value=2000):
            self.collector.poll_once()
        with database(self.collector.db_path) as connection:
            self.assertEqual(connection.execute("SELECT cursor FROM cursors WHERE scope=?", (second_scope,)).fetchone()[0],
                             "1000000")
        self.collector = self.new_collector()
        self.fake.contacts.append(second_contact)
        self.fake.request_cursors.clear()
        with patch("weward.collector.time.time", return_value=3000):
            self.collector.poll_once()
        self.assertEqual(set(self.fake.request_cursors), {"1000000", "2000000"})

    def test_invalid_second_room_batch_does_not_advance_either_room(self):
        second_room = "second-room@chatroom"
        self.fake.contacts.append({"roomid": second_room, "remark": "webox.weward", "nickname": "第二个群"})
        self.fake.messages = [message("good"), {**message("bad", room=second_room), "from": ""}]
        with self.assertRaises(ValueError):
            self.collector.poll_once()
        with database(self.collector.db_path) as connection:
            cursors = [row["cursor"] for row in connection.execute("SELECT cursor FROM cursors")]
        self.assertEqual(cursors, [self.fake.request_cursors[0]] * 2)
        self.assertEqual(self.collector.status()["queue"]["total"], 0)

    def test_media_path_error_is_delivered_explicitly(self):
        self.fake.messages = [message("traversal", "image", image={"sdkfileid": "../../../api-token"}),
                              message("missing-file", "image", image={"sdkfileid": "inbox/missing.png"})]
        self.collector.poll_once()
        self.deliver_all()
        events = self.receiver.messages()["messages"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["image"]["status"] == "unavailable" for event in events))

    def test_upstream_image_error_is_preserved_and_later_text_delivered(self):
        self.fake.messages = [message("decode-error", "image", image={"sdkfileid": "", "error": "unsupported fixture image"}),
                              message("after-image")]
        self.collector.poll_once()
        self.deliver_all()
        events = self.receiver.messages()["messages"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["image"], {"status": "unavailable", "error": "unsupported fixture image"})
        self.assertEqual(events[0]["message_id"], "after-image")

    def test_config_survives_restart_and_queued_target_is_unchanged(self):
        self.fake.messages = [message("original-target")]
        self.collector.poll_once()
        self.collector.configure({"room_remark": "webox.new", "target_url": "https://example.com/webhook"})
        self.collector = self.new_collector()
        self.assertEqual(self.collector.config()["room_remark"], "webox.new")
        self.deliver_all()
        self.assertEqual(self.receiver.messages()["total"], 1)

    def test_receiver_rejects_bad_image_without_saving(self):
        self.fake.messages = [message("image", "image", image={"sdkfileid": "inbox/sample.png"})]
        self.collector.poll_once()
        with database(self.collector.db_path) as connection:
            event = json.loads(connection.execute("SELECT payload FROM outbox").fetchone()[0])
        event["image"]["sha256"] = "incorrect"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request_json(self.target, event)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.receiver.messages()["total"], 0)

    def test_receiver_pagination_and_html(self):
        self.fake.messages = [message(str(index)) for index in range(6)]
        self.collector.poll_once()
        self.deliver_all()
        page = request_json(self.target + "?limit=2")
        older = request_json(self.target + "?limit=2&before=" + str(page["messages"][-1]["sequence"]))
        self.assertEqual([event["message_id"] for event in page["messages"]], ["5", "4"])
        self.assertEqual([event["message_id"] for event in older["messages"]], ["3", "2"])
        with urllib.request.urlopen(self.target.replace("/api/messages", "/")) as response:
            self.assertIn('id="messages-panel"', response.read().decode())

    def test_redirect_to_successful_get_is_not_a_successful_post(self):
        self.fake.messages = [message("redirect")]
        self.collector.poll_once()
        self.receive_server.failure = "redirect"
        self.collector.deliver_once()
        self.assertEqual(self.receiver.messages()["total"], 0)
        status = self.collector.status()
        self.assertEqual(status["queue"]["pending"], 1)
        self.assertIn("302", status["recent_deliveries"][0]["last_error"])


if __name__ == "__main__":
    unittest.main()
