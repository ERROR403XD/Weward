import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from .common import database, encode_json, init_database, request_json, utc_now
from .relay import NoRedirect


EVENTS = {
    "collector_offline": "采集器离线",
    "collector_online": "采集器上线",
    "delivery_started": "执行投递",
    "delivery_succeeded": "投递成功",
    "delivery_failed": "投递失败",
}
DEFAULT_MESSAGES = {
    "collector_offline": "采集器离线：{{reason}}",
    "collector_online": "采集器已上线（{{account_id}}）",
    "delivery_started": "开始投递「{{job_name}}」，{{message_count}} 条消息，第 {{attempt}} 次尝试",
    "delivery_succeeded": "「{{job_name}}」投递成功：{{message_count}} 条，回执 {{receipt_id}}",
    "delivery_failed": "「{{job_name}}」投递失败：{{error}}（HTTP {{http_status}}），将自动重试",
}
FIELDS = {"event", "event_name", "event_id", "occurred_at", "message", "reason", "account_id",
          "job_id", "job_name", "batch_id", "message_count", "attempt", "http_status", "error", "receipt_id"}
PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*}}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_state (id INTEGER PRIMARY KEY CHECK(id=1), online INTEGER);
INSERT OR IGNORE INTO notification_state (id, online) VALUES (1, NULL);
CREATE TABLE IF NOT EXISTS notification_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event TEXT NOT NULL,
    created_at TEXT NOT NULL, target_url TEXT NOT NULL, headers TEXT NOT NULL, body BLOB NOT NULL,
    response_field TEXT NOT NULL, response_value TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, claim_token TEXT, last_error TEXT, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS notification_due ON notification_outbox(status, next_attempt);
"""


def render(value, context, allowed=FIELDS):
    if isinstance(value, str):
        def substitute(match):
            key = match.group(1)
            if key not in allowed:
                raise ValueError(f"未知或不适用的占位符：{key}")
            return str(context.get(key, ""))
        return PLACEHOLDER.sub(substitute, value)
    if isinstance(value, list):
        return [render(item, context, allowed) for item in value]
    if isinstance(value, dict):
        return {key: render(item, context, allowed) for key, item in value.items()}
    return value


def defaults():
    return {"enabled": False, "target_url": "", "headers": {}, "body": {"text": "{{message}}"},
            "response_field": "", "response_value": True,
            "events": {name: {"enabled": True, "message": message} for name, message in DEFAULT_MESSAGES.items()}}


class Notifications:
    def __init__(self, db_path, collector_url):
        self.db_path = db_path
        self.collector_url = collector_url.rstrip("/")
        self.stop = threading.Event()
        self.candidate = None
        self.observations = 0
        init_database(db_path, SCHEMA)

    def config(self, connection=None):
        if connection is None:
            with database(self.db_path) as connection:
                return self.config(connection)
        row = connection.execute("SELECT value FROM notification_config WHERE id=1").fetchone()
        return json.loads(row["value"]) if row else defaults()

    def save(self, values):
        if not isinstance(values.get("enabled"), bool):
            raise ValueError("enabled 必须是布尔值")
        config = defaults()
        config.update({key: values[key] for key in config if key in values})
        target = config["target_url"]
        if not isinstance(target, str):
            raise ValueError("通知地址必须是 HTTP(S) 地址")
        if target or config["enabled"]:
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.fragment:
                raise ValueError("通知地址必须是 HTTP(S) 地址，不包含用户信息或片段")
            parsed.port
        headers = config["headers"]
        if not isinstance(headers, dict) or any(
                not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or key.lower() in ("host", "content-length", "transfer-encoding", "idempotency-key", "content-type")
                or not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 255 for char in value)
                for key, value in headers.items()):
            raise ValueError("请求头需为 JSON 字符串对象；Content-Type 等协议头由系统设置")
        if not isinstance(config["body"], (dict, list)):
            raise ValueError("请求体模板必须是 JSON 对象或数组，占位符写在字符串值内")
        if len(encode_json(config)) > 65536:
            raise ValueError("通知配置不能超过 64 KiB")
        render(config["body"], {})
        if not isinstance(config["response_field"], str) or (config["response_field"] and not re.fullmatch(
                r"[\w-]+(?:\.[\w-]+)*", config["response_field"])):
            raise ValueError("响应字段请使用 result.ok 形式的对象路径")
        if not isinstance(config["events"], dict) or set(config["events"]) != set(EVENTS):
            raise ValueError("请配置全部五种通知事件")
        for rule in config["events"].values():
            if not isinstance(rule, dict) or not isinstance(rule.get("enabled"), bool) or not isinstance(rule.get("message"), str):
                raise ValueError("每种事件需要 enabled 和 message 模板")
            render(rule["message"], {}, FIELDS - {"message"})
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self.config(connection)
            connection.execute("INSERT OR REPLACE INTO notification_config VALUES (1, ?)", (encode_json(config).decode(),))
            for event, rule in config["events"].items():
                if not config["enabled"] or not rule["enabled"]:
                    connection.execute("UPDATE notification_outbox SET status='cancelled' WHERE status='pending' AND event=?", (event,))
            if config["enabled"] and not previous["enabled"]:
                connection.execute("UPDATE notification_state SET online=NULL WHERE id=1")
        return config

    def enqueue(self, connection, event, **context):
        config = self.config(connection)
        if not config["enabled"] or not config["events"][event]["enabled"]:
            return None
        event_id = uuid.uuid4().hex
        context = {**context, "event": event, "event_name": EVENTS[event], "event_id": event_id, "occurred_at": utc_now()}
        context["message"] = render(config["events"][event]["message"], context)
        connection.execute("""INSERT INTO notification_outbox
            (event_id, event, created_at, target_url, headers, body, response_field, response_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (event_id, event, context["occurred_at"], config["target_url"],
            encode_json(config["headers"]).decode(), encode_json(render(config["body"], context)),
            config["response_field"], encode_json(config["response_value"]).decode()))
        return event_id

    def observe(self, online, reason="", account_id=""):
        if self.candidate != online:
            self.candidate, self.observations = online, 0
        self.observations += 1
        if self.observations < (2 if online else 3):
            return
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT online FROM notification_state WHERE id=1").fetchone()["online"]
            if previous is not None and bool(previous) == online:
                return
            connection.execute("UPDATE notification_state SET online=? WHERE id=1", (online,))
            self.enqueue(connection, "collector_online" if online else "collector_offline", reason=reason, account_id=account_id)

    def check_collector(self):
        try:
            status = request_json(self.collector_url + "/api/status", timeout=5)
            online = status.get("ready") is True
            reason = "采集未就绪：" + str(status.get("last_error") or status.get("phase") or "未知状态")
            if online:
                heartbeat = datetime.fromisoformat(status.get("last_poll_at", "")).timestamp()
                if time.time() - heartbeat > 120:
                    online, reason = False, "采集器超过 120 秒未完成轮询"
            account_id = (status.get("account") or {}).get("account_id", "")
        except (OSError, ValueError, TypeError, AttributeError):
            online, reason, account_id = False, "采集器无法连接或状态响应无效", ""
        self.observe(online, reason, account_id)

    def history(self):
        with database(self.db_path) as connection:
            return [dict(row) for row in connection.execute("""SELECT event_id, event, created_at, status, attempts,
                next_attempt, last_error, sent_at FROM notification_outbox ORDER BY sequence DESC LIMIT 30""")]

    def deliver_once(self, now=None):
        now = time.time() if now is None else now
        token = uuid.uuid4().hex
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self.config(connection)["enabled"]:
                return False
            row = connection.execute("""SELECT * FROM notification_outbox WHERE status='pending'
                AND next_attempt<=? AND lease_until<=? ORDER BY sequence LIMIT 1""", (now, now)).fetchone()
            if not row:
                return False
            connection.execute("""UPDATE notification_outbox SET attempts=attempts+1, lease_until=?, claim_token=?
                WHERE event_id=?""", (now + 60, token, row["event_id"]))
        attempts = row["attempts"] + 1
        try:
            request = urllib.request.Request(row["target_url"], data=row["body"], method="POST", headers={
                **json.loads(row["headers"]), "Content-Type": "application/json", "Idempotency-Key": row["event_id"]})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise ValueError(f"HTTP {response.status}")
                if row["response_field"]:
                    body = response.read(65537)
                    if len(body) > 65536:
                        raise ValueError("通知回执超过 64 KiB")
                    result = json.loads(body)
                    for field in row["response_field"].split("."):
                        if not isinstance(result, dict) or field not in result:
                            raise ValueError("通知回执缺少指定字段")
                        result = result[field]
                    expected = json.loads(row["response_value"])
                    if type(result) is not type(expected) or result != expected:
                        raise ValueError("通知回执字段与预期值不匹配")
            with database(self.db_path) as connection:
                connection.execute("""UPDATE notification_outbox SET status='sent', sent_at=?, last_error=NULL,
                    lease_until=0, claim_token=NULL WHERE event_id=? AND claim_token=? AND status='pending'""",
                    (utc_now(), row["event_id"], token))
        except Exception as error:
            detail = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            if isinstance(error, ValueError):
                detail = str(error)[:300]
            with database(self.db_path) as connection:
                connection.execute("""UPDATE notification_outbox SET status=?, last_error=?, next_attempt=?,
                    lease_until=0, claim_token=NULL WHERE event_id=? AND claim_token=? AND status='pending'""",
                    ("failed" if attempts >= 8 else "pending", detail, now + min(3600, 30 * 2 ** (attempts - 1)), row["event_id"], token))
            logging.warning("notification_retry event=%s attempt=%s error=%s", row["event_id"], attempts, detail)
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                if self.deliver_once():
                    continue
            except Exception:
                logging.exception("notification_worker_failed")
            self.stop.wait(1)

    def monitor(self):
        while not self.stop.is_set():
            try:
                self.check_collector()
            except Exception:
                logging.exception("notification_monitor_failed")
            self.stop.wait(10)
