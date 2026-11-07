import base64
import hashlib
import json
import logging
import os
import re
import signal
import threading
import urllib.parse
import urllib.error
from pathlib import Path

from .common import Handler, database, encode_json, init_database, make_server, request_json, utc_now
from .relay import Relay, message_text, room_condition
from .notifications import Notifications, EVENTS, FIELDS


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL, payload TEXT NOT NULL, image BLOB, content_type TEXT,
    delivery_attempt TEXT
);
"""


class Receiver:
    def __init__(self, data_dir, collector_url=None):
        self.db_path = Path(data_dir) / "receiver.sqlite3"
        self.collector_url = collector_url or os.environ.get("COLLECTOR_CONTROL_URL", "http://collector:8081")
        init_database(self.db_path, SCHEMA)
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
            additions = {"account_id": "TEXT", "room_id": "TEXT", "room_name": "TEXT", "room_remark": "TEXT",
                         "message_type": "TEXT", "message_time_ms": "INTEGER", "search_text": "TEXT",
                         "note": "TEXT NOT NULL DEFAULT ''", "archived": "INTEGER NOT NULL DEFAULT 0"}
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            for row in connection.execute("SELECT sequence, payload FROM messages WHERE account_id IS NULL").fetchall():
                event = json.loads(row["payload"])
                connection.execute("""UPDATE messages SET account_id=?, room_id=?, room_name=?, room_remark=?,
                    message_type=?, message_time_ms=?, search_text=? WHERE sequence=?""", (*self.metadata(event), row["sequence"]))
            connection.execute("CREATE INDEX IF NOT EXISTS messages_room ON messages(account_id, room_id, sequence)")
            connection.execute("CREATE INDEX IF NOT EXISTS messages_kind ON messages(message_type, sequence)")
            connection.execute("CREATE INDEX IF NOT EXISTS messages_time ON messages(message_time_ms DESC, sequence DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS messages_room_time ON messages(account_id, room_id, message_time_ms DESC, sequence DESC)")
        self.notifications = Notifications(self.db_path, self.collector_url)
        self.relay = Relay(self.db_path, self.notifications)

    def monitoring(self, query=None, values=None):
        url = self.collector_url.rstrip("/") + "/api/monitoring"
        if query is not None:
            url += "?" + urllib.parse.urlencode({"query": query})
        try:
            return request_json(url, values, timeout=10)
        except urllib.error.HTTPError as error:
            try:
                message = json.load(error).get("error", "采集端暂不可用")
            except (ValueError, AttributeError):
                message = "采集端暂不可用"
            if error.code == 400:
                raise ValueError(message) from None
            raise ConnectionError(message) from None
        except (OSError, ValueError):
            raise ConnectionError("无法连接采集端") from None

    @staticmethod
    def metadata(event):
        return (event["account_id"], event["room_id"], event.get("room_name", ""), event.get("room_remark", ""),
                event["type"], event["message_time_ms"], message_text(event))

    def receive(self, event, attempt="1"):
        if event.get("schema_version") != 1 or event.get("source") != "webox":
            raise ValueError("需要 Weward schema_version=1、source=webox 的消息")
        if not re.fullmatch(r"[0-9a-f]{64}", str(event.get("event_id", ""))):
            raise ValueError("event_id 必须为 64 位十六进制字符串")
        for field in ("account_id", "message_id", "room_id", "sender_id", "type", "collected_at"):
            if not isinstance(event.get(field), str) or not event[field]:
                raise ValueError(f"缺少字段 {field}")
        for field in ("room_name", "room_remark", "sender_name"):
            if field in event and not isinstance(event[field], str):
                raise ValueError(f"{field} 必须是字符串")
        if not isinstance(event.get("message_time_ms"), int) or event["message_time_ms"] <= 0:
            raise ValueError("message_time_ms 必须为正整数毫秒时间戳")
        if not isinstance(event.get("raw"), dict):
            raise ValueError("缺少 WeBox raw 消息对象")
        image_data, content_type = None, None
        image = event.get("image")
        if event["type"] == "image" and not isinstance(image, dict):
            raise ValueError("图片消息必须带 image 状态")
        if image is not None:
            if not isinstance(image, dict):
                raise ValueError("image 必须是对象")
            if image.get("status") == "available":
                image_data = base64.b64decode(image.get("base64", ""), validate=True)
                content_type = image.get("content_type")
                if not image_data or content_type not in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"):
                    raise ValueError("图片数据为空或格式不支持")
                if image.get("sha256") != hashlib.sha256(image_data).hexdigest() or image.get("size") != len(image_data):
                    raise ValueError("图片哈希或大小不一致")
                image = {key: value for key, value in image.items() if key != "base64"}
                image["url"] = "/media/" + event["event_id"]
            elif image.get("status") != "unavailable" or not image.get("error"):
                raise ValueError("无效的图片状态")
        stored = {key: value for key, value in event.items() if key != "image"}
        if image:
            stored["image"] = image
        received_at = utc_now()
        with database(self.db_path) as connection:
            inserted = connection.execute("""INSERT OR IGNORE INTO messages
                (event_id, received_at, payload, image, content_type, delivery_attempt,
                 account_id, room_id, room_name, room_remark, message_type, message_time_ms, search_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event["event_id"], received_at, encode_json(stored).decode(), image_data, content_type, attempt,
                 *self.metadata(stored))).rowcount
            row = connection.execute("SELECT sequence, received_at FROM messages WHERE event_id=?", (event["event_id"],)).fetchone()
        logging.info("http_received event=%s duplicate=%s attempt=%s", event["event_id"], not inserted, attempt)
        return {"ok": True, "event_id": event["event_id"], "duplicate": not bool(inserted), **dict(row)}

    def messages(self, before=None, limit=50, filters=None):
        filters = filters or {}
        clauses, parameters = ["1=1"], []
        for key, column in (("account_id", "account_id"), ("room_id", "room_id"), ("type", "message_type")):
            if filters.get(key):
                clauses.append(f"messages.{column}=?")
                parameters.append(filters[key])
        if filters.get("q"):
            clauses.append("(INSTR(LOWER(search_text),LOWER(?))>0 OR INSTR(LOWER(note),LOWER(?))>0)")
            parameters.extend((filters["q"], filters["q"]))
        if filters.get("archived", "all") != "all":
            if filters["archived"] not in ("0", "1"):
                raise ValueError("archived 必须是 all、0 或 1")
            clauses.append("archived=?")
            parameters.append(int(filters["archived"]))
        state = filters.get("state", "all")
        if state not in ("all", "pending", "sent"):
            raise ValueError("state 必须是 all、pending 或 sent")
        job_id = filters.get("job_id") or None
        with database(self.db_path) as connection:
            if before is not None:
                anchor = connection.execute("SELECT message_time_ms FROM messages WHERE sequence=?", (before,)).fetchone()
                if not anchor:
                    raise ValueError("分页位置不存在，请返回最新消息")
                before_time = anchor["message_time_ms"]
            else:
                before_time = None
            if job_id:
                job = connection.execute("SELECT rooms, cursor FROM relay_jobs WHERE job_id=?", (job_id,)).fetchone()
                if not job:
                    raise ValueError("任务不存在")
                clause, room_parameters = room_condition(json.loads(job["rooms"]), "messages.")
                clauses.append(clause)
                parameters.extend(room_parameters)
                if state == "pending":
                    clauses.append("sequence>?")
                    parameters.append(job["cursor"])
            if state != "all":
                clauses.append(("NOT " if state == "pending" else "") + """EXISTS (SELECT 1 FROM relay_batch_messages members
                    JOIN relay_batches batches ON batches.batch_id=members.batch_id WHERE members.sequence=messages.sequence
                    AND batches.status='sent' AND (? IS NULL OR batches.job_id=?))""")
                parameters.extend((job_id, job_id))
            where = " AND ".join(clauses)
            rows = connection.execute(f"""SELECT sequence, received_at, payload, delivery_attempt, note, archived FROM messages
                WHERE {where} AND (? IS NULL OR (message_time_ms, sequence)<(?, ?))
                ORDER BY message_time_ms DESC, sequence DESC LIMIT ?""",
                (*parameters, before, before_time, before, limit)).fetchall()
            count = connection.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", parameters).fetchone()[0]
            messages = []
            for row in rows:
                deliveries = connection.execute("""SELECT batches.batch_id, batches.job_id, jobs.name, batches.status, batches.sent_at
                    FROM relay_batch_messages members JOIN relay_batches batches ON batches.batch_id=members.batch_id
                    JOIN relay_jobs jobs ON jobs.job_id=batches.job_id WHERE members.sequence=? ORDER BY batches.created_at""",
                    (row["sequence"],)).fetchall()
                messages.append({**json.loads(row["payload"]), "sequence": row["sequence"], "received_at": row["received_at"],
                                 "delivery_attempt": row["delivery_attempt"], "note": row["note"], "archived": bool(row["archived"]),
                                 "deliveries": [dict(delivery) for delivery in deliveries]})
        return {"total": count, "messages": messages}

    def manage_message(self, event_id, values):
        note, archived = values.get("note"), values.get("archived")
        if note is not None and (not isinstance(note, str) or len(note) > 10000):
            raise ValueError("备注必须是不超过 10000 字的字符串")
        if archived is not None and not isinstance(archived, bool):
            raise ValueError("archived 必须是布尔值")
        with database(self.db_path) as connection:
            changed = connection.execute("UPDATE messages SET note=COALESCE(?,note), archived=COALESCE(?,archived) WHERE event_id=?",
                                         (note, archived, event_id)).rowcount
        if not changed:
            raise ValueError("消息不存在")
        return {"ok": True}

    def rooms(self):
        with database(self.db_path) as connection:
            rows = connection.execute("""WITH ranked AS (
                SELECT account_id, room_id, room_name, room_remark, received_at, message_time_ms, sequence,
                    COUNT(*) OVER (PARTITION BY account_id, room_id) AS message_count,
                    ROW_NUMBER() OVER (PARTITION BY account_id, room_id ORDER BY message_time_ms DESC, sequence DESC) AS position
                FROM messages)
                SELECT account_id, room_id, room_name, room_remark, message_count, received_at AS last_received_at,
                    message_time_ms AS last_message_time_ms FROM ranked WHERE position=1
                ORDER BY message_time_ms DESC, sequence DESC""").fetchall()
        return [dict(row) for row in rows]

    def status(self):
        with database(self.db_path) as connection:
            counts = dict(connection.execute("""SELECT COUNT(*) AS messages, COALESCE(SUM(archived),0) AS archived,
                COALESCE(SUM(LENGTH(image)),0) AS image_bytes, MAX(received_at) AS last_received_at FROM messages""").fetchone())
            counts["sent_messages"] = connection.execute("""SELECT COUNT(DISTINCT members.sequence) FROM relay_batch_messages members
                JOIN relay_batches batches ON batches.batch_id=members.batch_id WHERE batches.status='sent'""").fetchone()[0]
        return {"ok": True, **counts}


class ReceiverHandler(Handler):
    def desktop_url(self):
        configured = os.environ.get("DESKTOP_PUBLIC_URL", "").strip()
        if configured:
            parsed = urllib.parse.urlsplit(configured)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or any(ord(char) < 32 for char in configured):
                raise ValueError("DESKTOP_PUBLIC_URL 必须是完整的 HTTP(S) 桌面地址")
            return configured
        host = urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname
        port = int(os.environ.get("DESKTOP_PUBLIC_PORT", "6080"))
        if not host or not 1 <= port <= 65535:
            raise ValueError("无法确定桌面地址，请检查 DESKTOP_PUBLIC_PORT 或设置 DESKTOP_PUBLIC_URL")
        if ":" in host:
            host = "[" + host + "]"
        return f"http://{host}:{port}/vnc.html?autoconnect=1&resize=scale&reconnect=0"

    def do_GET(self):
        try:
            self.get()
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
        except ConnectionError as error:
            self.send_json({"error": str(error)}, 502)

    def get(self):
        parsed = urllib.parse.urlsplit(self.path)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        app = self.server.app
        if parsed.path == "/":
            self.static("receiver.html")
        elif parsed.path == "/desktop":
            target = self.desktop_url()
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif parsed.path == "/style.css":
            self.static("style.css")
        elif parsed.path == "/receiver.js":
            self.static("receiver.js")
        elif parsed.path == "/receiver.css":
            self.static("receiver.css")
        elif parsed.path == "/healthz":
            self.send_json({"ok": True})
        elif parsed.path == "/api/status":
            self.send_json(app.status())
        elif parsed.path == "/api/rooms":
            self.send_json({"rooms": app.rooms()})
        elif parsed.path == "/api/jobs":
            self.send_json({"jobs": app.relay.jobs()})
        elif parsed.path == "/api/monitoring":
            self.send_json(app.monitoring(query.get("query")))
        elif parsed.path == "/api/notifications":
            self.send_json({"config": app.notifications.config(), "events": EVENTS,
                            "placeholders": sorted(FIELDS), "history": app.notifications.history()})
        elif parsed.path == "/api/batches":
            before = int(query["before"]) if "before" in query else None
            limit = max(1, min(100, int(query.get("limit", "30"))))
            self.send_json({"batches": app.relay.batches(query.get("job_id"), before, limit)})
        elif re.fullmatch(r"/api/batches/[0-9a-f]{32}/download", parsed.path):
            batch_id = parsed.path.split("/")[3]
            archive = app.relay.archive(batch_id)
            if archive:
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(archive)))
                self.send_header("Content-Disposition", f'attachment; filename="weward-{batch_id}.zip"')
                self.end_headers()
                try:
                    self.wfile.write(archive)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_json({"error": "文件包不存在"}, 404)
        elif parsed.path == "/api/messages":
            before = int(query["before"]) if "before" in query else None
            limit = max(1, min(100, int(query.get("limit", "50"))))
            self.send_json(app.messages(before, limit, query))
        elif parsed.path.startswith("/media/"):
            event_id = parsed.path.removeprefix("/media/")
            with database(self.server.app.db_path) as connection:
                row = connection.execute("SELECT image, content_type FROM messages WHERE event_id=?", (event_id,)).fetchone()
            if row and row["image"]:
                self.send_bytes(row["image"], row["content_type"])
            else:
                self.send_json({"error": "接收端未收到此图片字节"}, 404)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            app = self.server.app
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/messages":
                event = self.read_json()
                key = self.headers.get("Idempotency-Key")
                if key and key != event.get("event_id"):
                    raise ValueError("Idempotency-Key 与 event_id 不匹配")
                result = app.receive(event, self.headers.get("X-Weward-Attempt", "1"))
                self.send_json(result, 200 if result["duplicate"] else 201)
            elif re.fullmatch(r"/api/messages/[0-9a-f]{64}/manage", path):
                self.send_json(app.manage_message(path.split("/")[3], self.read_json()))
            elif path == "/api/jobs":
                self.send_json(app.relay.save_job(self.read_json()), 201)
            elif path == "/api/monitoring":
                self.send_json(app.monitoring(values=self.read_json()))
            elif path == "/api/notifications":
                self.send_json({"config": app.notifications.save(self.read_json())})
            elif re.fullmatch(r"/api/jobs/[0-9a-f]{32}(/enabled|/run)?", path):
                parts = path.split("/")
                values = self.read_json()
                if len(parts) == 4:
                    self.send_json(app.relay.save_job(values, parts[3]))
                elif parts[4] == "enabled":
                    self.send_json(app.relay.set_enabled(parts[3], values.get("enabled")))
                else:
                    self.send_json(app.relay.trigger(parts[3]), 202)
            else:
                self.send_json({"error": "not found"}, 404)
        except (ValueError, TypeError) as error:
            self.send_json({"error": str(error)}, 400)
        except ConnectionError as error:
            self.send_json({"error": str(error)}, 502)
        except Exception:
            logging.exception("receiver_write_failed")
            self.send_json({"error": "接收端保存失败，请重试"}, 500)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = Receiver(os.environ.get("RECEIVER_DATA_DIR", "/data"))
    server = make_server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), ReceiverHandler, app)
    workers = [threading.Thread(target=target, daemon=True)
               for target in (app.relay.run, app.notifications.run, app.notifications.monitor)]
    for worker in workers:
        worker.start()
    def terminate(signum, frame):
        app.relay.stop.set()
        app.notifications.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    logging.info("receiver_listening port=%s", server.server_port)
    server.serve_forever()
    server.server_close()
    app.relay.stop.set()
    app.notifications.stop.set()
    for worker in workers:
        worker.join(timeout=35)


if __name__ == "__main__":
    main()
