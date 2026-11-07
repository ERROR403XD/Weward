import base64
import hashlib
import json
import logging
import os
import random
import signal
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import Handler, database, encode_json, init_database, make_server, request_json, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors (scope TEXT PRIMARY KEY, cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, target_url TEXT NOT NULL,
    created_at TEXT NOT NULL, delivered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(delivered_at, next_attempt);
CREATE TABLE IF NOT EXISTS watch_accounts (account_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS watched_rooms (
    account_id TEXT NOT NULL, room_id TEXT NOT NULL, room_name TEXT NOT NULL, room_remark TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(account_id, room_id)
);
"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def validate_config(config):
    remark = str(config.get("room_remark", "")).strip()
    if not remark.startswith("webox.") or len(remark) <= 6:
        raise ValueError("群备注必须以 webox. 开头，例如 webox.weward")
    target = str(config.get("target_url", "")).strip()
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment:
        raise ValueError("转发目标必须是完整的 HTTP(S) 地址，不能含片段")
    return {"room_remark": remark, "target_url": target}


def image_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"BM"):
        return "image/bmp"
    raise ValueError("图片格式无法识别，可能尚未解密或文件不完整")


def normalize_message(message, account, room, shared_root):
    for field in ("msgid", "from", "roomid", "msgtype"):
        if not isinstance(message.get(field), str) or not message[field]:
            raise ValueError(f"WeBox 消息缺少 {field}，保留游标等待修复")
    if not isinstance(message.get("msgtime"), int) or message["msgtime"] <= 0:
        raise ValueError("WeBox 消息时间无效，保留游标等待修复")
    event_id = hashlib.sha256(encode_json([account["account_id"], message["roomid"], message["msgid"]])).hexdigest()
    is_self = message["from"] == account["account_id"]
    event = {
        "schema_version": 1,
        "event_id": event_id,
        "source": "webox",
        "account_id": account["account_id"],
        "message_id": message["msgid"],
        "room_id": message["roomid"],
        "room_name": room.get("nickname") or room["remark"],
        "room_remark": room["remark"],
        "sender_id": message["from"],
        "sender_name": (account.get("nickname") or message["from"]) if is_self else message["from"],
        "is_self": is_self,
        "message_time_ms": message["msgtime"],
        "collected_at": utc_now(),
        "type": message["msgtype"],
        "raw": message,
    }
    if message["msgtype"] == "image":
        sdkfileid = (message.get("image") or {}).get("sdkfileid", "")
        try:
            if not sdkfileid:
                raise ValueError((message.get("image") or {}).get("error") or "WeBox 未提供图片文件；可能未下载、解密失败或等待超时")
            rooms_root = (Path(shared_root) / "rooms").resolve()
            room_root = (rooms_root / message["roomid"]).resolve()
            if room_root.parent != rooms_root:
                raise ValueError("WeBox 群目录无效")
            image_path = (room_root / sdkfileid).resolve()
            if not image_path.is_relative_to(room_root):
                raise ValueError("WeBox 图片路径超出当前群目录")
            if image_path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("图片超过当前转发单张 32 MiB 上限")
            content = image_path.read_bytes()
            event["image"] = {
                "status": "available",
                "content_type": image_type(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "base64": base64.b64encode(content).decode("ascii"),
            }
        except (OSError, ValueError) as error:
            event["image"] = {"status": "unavailable", "error": str(error)}
            logging.warning("image_unavailable event=%s error=%s", event_id, error)
    return event


class Collector:
    def __init__(self, data_dir, webox_url, token_file, shared_root, defaults):
        self.db_path = Path(data_dir) / "collector.sqlite3"
        self.webox_url = webox_url.rstrip("/")
        self.token_file = Path(token_file)
        self.shared_root = Path(shared_root)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.runtime = {"ready": False, "phase": "starting", "last_error": None}
        self.lookback = int(os.environ.get("INITIAL_LOOKBACK_SECONDS", "300"))
        if self.lookback < 0:
            raise ValueError("INITIAL_LOOKBACK_SECONDS 必须非负")
        init_database(self.db_path, SCHEMA)
        with database(self.db_path) as connection:
            connection.execute("INSERT OR IGNORE INTO settings VALUES ('config', ?)", (encode_json(validate_config(defaults)).decode(),))

    def config(self):
        with database(self.db_path) as connection:
            return json.loads(connection.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])

    def configure(self, value):
        config = validate_config(value)
        with database(self.db_path) as connection:
            connection.execute("UPDATE settings SET value=? WHERE key='config'", (encode_json(config).decode(),))
        self.set_runtime(ready=False, phase="configuring", last_error=None)
        return config

    def set_runtime(self, **values):
        with self.lock:
            self.runtime.update(values)

    def status(self):
        with self.lock:
            result = dict(self.runtime)
        with database(self.db_path) as connection:
            result["queue"] = dict(connection.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(delivered_at IS NULL), 0) AS pending,
                COALESCE(SUM(delivered_at IS NOT NULL), 0) AS delivered FROM outbox""").fetchone())
            result["recent_deliveries"] = [dict(row) for row in connection.execute("""SELECT event_id, created_at,
                delivered_at, attempts, last_error, next_attempt, target_url FROM outbox ORDER BY rowid DESC LIMIT 10""")]
        result["config"] = self.config()
        result["desktop_port"] = int(os.environ.get("DESKTOP_PUBLIC_PORT", "6080"))
        result["receiver_port"] = int(os.environ.get("RECEIVER_PUBLIC_PORT", "18081"))
        return result

    def webox(self, path, body=None):
        token = self.token_file.read_text().strip()
        result = request_json(self.webox_url + path, body, {
            "AuthorizationType": "ilink_bot_token",
            "Authorization": "Bearer " + token,
            "X-WECHAT-UIN": "weward-collector",
        })
        if result.get("ret") != 0:
            raise RuntimeError(f"WeBox 尚未就绪或读取失败 (ret={result.get('ret')})；请检查扫码登录及采集端日志")
        return result

    def watch_state(self, account_id, connection=None):
        if connection is None:
            with database(self.db_path) as connection:
                return self.watch_state(account_id, connection)
        row = connection.execute("SELECT revision FROM watch_accounts WHERE account_id=?", (account_id,)).fetchone()
        return row["revision"] if row else None

    def available_groups(self, query):
        response = self.webox("/ilink/bot/groups?" + urllib.parse.urlencode({"query": query}))
        return [room for room in response.get("contacts", []) if str(room.get("roomid", "")).endswith("@chatroom")]

    def monitored_groups(self, account_id):
        if self.watch_state(account_id) is None:
            response = self.webox("/ilink/bot/contacts?" + urllib.parse.urlencode({"remark": self.config()["room_remark"]}))
            return [{"account_id": account_id, "room_id": room["roomid"], "room_name": room.get("nickname", ""),
                     "room_remark": room.get("remark", ""), "enabled": True} for room in response.get("contacts", [])
                    if str(room.get("roomid", "")).endswith("@chatroom")]
        with database(self.db_path) as connection:
            return [{**dict(row), "enabled": bool(row["enabled"])} for row in connection.execute(
                "SELECT * FROM watched_rooms WHERE account_id=? ORDER BY room_name, room_id", (account_id,))]

    def monitoring(self, query=None):
        account = self.webox("/ilink/bot/userinfo")
        account_id = account.get("account_id")
        if not account_id:
            raise ValueError("请先登录微信")
        groups = self.monitored_groups(account_id)
        result = {"account_id": account_id, "account_name": account.get("nickname", account_id), "rooms": groups}
        if query is not None:
            watched = {room["room_id"] for room in groups}
            result["matches"] = [{"account_id": account_id, "room_id": room["roomid"],
                                  "room_name": room.get("nickname", ""), "monitored": room["roomid"] in watched}
                                 for room in self.available_groups(query.strip())]
        return result

    def configure_monitoring(self, values):
        action = values.get("action")
        if action not in ("add", "set", "remove"):
            raise ValueError("无效的群监听操作")
        account = self.webox("/ilink/bot/userinfo")
        account_id = account.get("account_id")
        if not account_id or values.get("account_id") != account_id:
            raise ValueError("登录账号已变化，请刷新后重试")
        room_id = values.get("room_id")
        if not isinstance(room_id, str) or not room_id.endswith("@chatroom"):
            raise ValueError("请选择有效的群聊")
        enabled = values.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        original = self.monitored_groups(account_id)
        target = None
        if action == "add":
            target = next((room for room in self.available_groups(room_id) if room["roomid"] == room_id), None)
            if target is None:
                raise ValueError("当前账号中没有找到这个群")
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self.watch_state(account_id, connection) is None:
                connection.execute("INSERT INTO watch_accounts VALUES (?,0)", (account_id,))
                connection.executemany("INSERT OR IGNORE INTO watched_rooms VALUES (?,?,?,?,1)",
                                       ((account_id, room["room_id"], room["room_name"], room["room_remark"]) for room in original))
            if action == "add":
                connection.execute("""INSERT INTO watched_rooms VALUES (?,?,?,?,1)
                    ON CONFLICT(account_id, room_id) DO UPDATE SET enabled=1, room_name=excluded.room_name,
                    room_remark=excluded.room_remark""", (account_id, room_id, target.get("nickname", ""), target.get("remark", "")))
            elif action == "set":
                if not connection.execute("UPDATE watched_rooms SET enabled=? WHERE account_id=? AND room_id=?", (enabled, account_id, room_id)).rowcount:
                    raise ValueError("此群未在监听列表中")
            else:
                connection.execute("DELETE FROM watched_rooms WHERE account_id=? AND room_id=?", (account_id, room_id))
            connection.execute("UPDATE watch_accounts SET revision=revision+1 WHERE account_id=?", (account_id,))
        self.set_runtime(phase="configuring")
        return self.monitoring()

    def poll_once(self):
        config = self.config()
        account = self.webox("/ilink/bot/userinfo")
        if not account.get("account_id"):
            raise ValueError("WeBox 未返回登录账号身份")
        revision = self.watch_state(account["account_id"])
        if revision is None:
            response = self.webox("/ilink/bot/contacts?" + urllib.parse.urlencode({"remark": config["room_remark"]}))
            contacts = response.get("contacts", [])
        else:
            selected = {room["room_id"] for room in self.monitored_groups(account["account_id"]) if room["enabled"]}
            contacts = [room for room in self.available_groups("") if room["roomid"] in selected] if selected else []
            if not contacts:
                self.set_runtime(ready=True, phase="idle", last_error=None, account=account, rooms=[], cursors={}, last_poll_at=utc_now())
                return
        rooms = {contact.get("roomid", ""): contact for contact in contacts}
        if not rooms or len(rooms) != len(contacts) or any(not room_id.endswith("@chatroom") for room_id in rooms):
            raise RuntimeError(f"群备注 {config['room_remark']} 必须匹配一个或多个不同的群，当前匹配 {len(contacts)} 个联系人；请在微信桌面检查群备注")
        scopes, cursors, batches = {}, {}, {}
        with database(self.db_path) as connection:
            initial_cursor = str(int((time.time() - self.lookback) * 1000) - 1)
            for room_id in rooms:
                scope = encode_json([account["account_id"], room_id]).decode()
                connection.execute("INSERT OR IGNORE INTO cursors VALUES (?, ?)", (scope, initial_cursor))
                cursor = connection.execute("SELECT cursor FROM cursors WHERE scope=?", (scope,)).fetchone()[0]
                scopes[room_id], cursors[room_id] = scope, cursor
                batches.setdefault(cursor, []).append(room_id)
        self.set_runtime(account={key: account[key] for key in ("account_id", "nickname") if key in account},
                         room=contacts[0], rooms=contacts, cursors=cursors)
        with ThreadPoolExecutor(max_workers=min(len(batches), 4)) as executor:
            responses = list(executor.map(lambda cursor: self.poll_updates(cursor, batches[cursor] if revision is not None else None), batches))
        if self.config() != config:
            return
        events, next_cursors = [], {}
        for room_ids, (new_cursor, messages) in zip(batches.values(), responses):
            events.extend(normalize_message(message, account, rooms[message["roomid"]], self.shared_root)
                          for message in messages if message.get("roomid") in room_ids)
            next_cursors.update({room_id: new_cursor for room_id in room_ids})
        inserted = 0
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self.watch_state(account["account_id"], connection) != revision:
                return
            for event in events:
                inserted += connection.execute("""INSERT OR IGNORE INTO outbox
                    (event_id, payload, target_url, created_at) VALUES (?, ?, ?, ?)""",
                    (event["event_id"], encode_json(event).decode(), config["target_url"], event["collected_at"])).rowcount
            for room_id, new_cursor in next_cursors.items():
                connection.execute("UPDATE cursors SET cursor=? WHERE scope=?", (new_cursor, scopes[room_id]))
        if inserted:
            logging.info("queued count=%s rooms=%s", inserted, list(rooms))
        self.set_runtime(ready=True, phase="listening", last_error=None, last_poll_at=utc_now(),
                         cursor=min(next_cursors.values(), key=int), cursors=next_cursors)

    def poll_updates(self, cursor, room_ids=None):
        response = self.webox("/ilink/bot/getupdates", {"cursor": cursor, **({"room_ids": room_ids} if room_ids is not None else {})})
        new_cursor = response.get("cursor")
        if not isinstance(new_cursor, str) or not new_cursor.isdecimal() or int(new_cursor) < int(cursor):
            raise ValueError("WeBox 返回无效或倒退的游标")
        messages = response.get("messages")
        if messages is None and "messages" in response:
            messages = []
        if not isinstance(messages, list):
            raise ValueError("WeBox 未返回消息数组")
        return new_cursor, messages

    def deliver_once(self):
        with database(self.db_path) as connection:
            row = connection.execute("""SELECT * FROM outbox WHERE delivered_at IS NULL AND next_attempt<=?
                ORDER BY rowid LIMIT 1""", (time.time(),)).fetchone()
        if row is None:
            return False
        attempt = row["attempts"] + 1
        try:
            request = urllib.request.Request(row["target_url"], data=row["payload"].encode(), headers={
                "Content-Type": "application/json", "Idempotency-Key": row["event_id"],
                "User-Agent": "Weward/1", "X-Weward-Attempt": str(attempt),
            })
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=20) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"HTTP {response.status}")
            with database(self.db_path) as connection:
                connection.execute("UPDATE outbox SET delivered_at=?, attempts=?, last_error=NULL WHERE event_id=?",
                                   (utc_now(), attempt, row["event_id"]))
            logging.info("delivered event=%s attempt=%s", row["event_id"], attempt)
        except Exception as error:
            delay = min(300, 2 ** min(attempt, 9)) + random.uniform(0, 1)
            with database(self.db_path) as connection:
                connection.execute("UPDATE outbox SET attempts=?, last_error=?, next_attempt=? WHERE event_id=?",
                                   (attempt, str(error), time.time() + delay, row["event_id"]))
            logging.warning("delivery_retry event=%s attempt=%s delay=%.1f error=%s", row["event_id"], attempt, delay, error)
        return True

    def poll_loop(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
                self.stop.wait(0.2)
            except Exception as error:
                self.set_runtime(ready=False, phase="waiting", last_error=str(error))
                logging.warning("capture_wait error=%s", error)
                self.stop.wait(5)

    def delivery_loop(self):
        while not self.stop.is_set():
            try:
                if not self.deliver_once():
                    self.stop.wait(1)
            except Exception:
                logging.exception("delivery_worker_error")
                self.stop.wait(5)

    def retry_pending(self):
        with database(self.db_path) as connection:
            return connection.execute("UPDATE outbox SET next_attempt=0 WHERE delivered_at IS NULL").rowcount


class CollectorHandler(Handler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/monitoring":
            try:
                query = urllib.parse.parse_qs(parsed.query)
                self.send_json(self.server.app.monitoring(query.get("query", [None])[0]))
            except (ValueError, TypeError) as error:
                self.send_json({"error": str(error)}, 400)
            except Exception:
                self.send_json({"error": "采集端尚未就绪，请检查微信登录"}, 503)
        elif self.path == "/":
            self.static("collector.html")
        elif self.path == "/style.css":
            self.static("style.css")
        elif self.path == "/healthz":
            self.send_json({"ok": True, "ready": self.server.app.status()["ready"]})
        elif self.path == "/api/status":
            self.send_json(self.server.app.status())
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path == "/api/config":
                self.send_json(self.server.app.configure(self.read_json()))
            elif self.path == "/api/retry":
                self.send_json({"pending": self.server.app.retry_pending()})
            elif self.path == "/api/monitoring":
                self.send_json(self.server.app.configure_monitoring(self.read_json()))
            else:
                self.send_json({"error": "not found"}, 404)
        except (ValueError, TypeError) as error:
            self.send_json({"error": str(error)}, 400)
        except Exception:
            self.send_json({"error": "采集端尚未就绪，请稍后重试"}, 503)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = Collector(
        os.environ.get("COLLECTOR_DATA_DIR", "/data"),
        os.environ.get("WEBOX_URL", "http://127.0.0.1:8080"),
        os.environ.get("WEBOX_TOKEN_FILE", "/webox/state/weagent/api-token"),
        os.environ.get("WEBOX_SHARED_DIR", "/webox/state/shared"),
        {"room_remark": os.environ.get("WATCH_ROOM_REMARK", "webox.weward"),
         "target_url": os.environ.get("FORWARD_URL", "http://receiver:8080/api/messages")},
    )
    server = make_server(("0.0.0.0", int(os.environ.get("PORT", "8081"))), CollectorHandler, app)
    for worker in (app.poll_loop, app.delivery_loop):
        threading.Thread(target=worker, daemon=True).start()
    def terminate(signum, frame):
        app.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    logging.info("collector_listening port=%s", server.server_port)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
