import hashlib
import html
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .common import database, encode_json, init_database, utc_now
from .schedule import Cron


SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_jobs (
    job_id TEXT PRIMARY KEY, name TEXT NOT NULL, target_url TEXT NOT NULL,
    cron TEXT NOT NULL, timezone TEXT NOT NULL, rooms TEXT NOT NULL,
    enabled INTEGER NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
    run_until INTEGER, next_run REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS relay_batches (
    batch_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    from_sequence INTEGER NOT NULL, through_sequence INTEGER NOT NULL,
    message_count INTEGER NOT NULL, target_url TEXT NOT NULL,
    sha256 TEXT NOT NULL, archive BLOB NOT NULL, created_at TEXT NOT NULL,
    sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
    last_error TEXT, receipt TEXT, lease_until REAL NOT NULL DEFAULT 0, claim_token TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS relay_one_pending ON relay_batches(job_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS relay_batch_history ON relay_batches(created_at DESC);
CREATE TABLE IF NOT EXISTS relay_batch_messages (
    batch_id TEXT NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY(batch_id, sequence)
);
CREATE INDEX IF NOT EXISTS relay_message_batches ON relay_batch_messages(sequence);
"""

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}


def message_text(event):
    raw = event.get("raw", {})
    parts = []
    for section, keys in (("text", ("content",)), ("reply", ("title",)),
                          ("link", ("title", "description", "link_url")),
                          ("sphfeed", ("sph_name", "feed_desc"))):
        content = raw.get(section)
        if isinstance(content, dict):
            parts.extend(content[key] for key in keys if isinstance(content.get(key), str) and content[key])
    return "\n".join(parts)


def message_links(event):
    result = []
    raw_link = event.get("raw", {}).get("link", {})
    explicit = raw_link.get("link_url", "") if isinstance(raw_link, dict) else ""
    candidates = ([explicit] if isinstance(explicit, str) and explicit else [])
    candidates += re.findall(r'https?://[^\s<>"\u3000-\u303f\uff00-\uffef]+', message_text(event))
    for candidate in candidates:
        candidate = html.unescape(candidate).rstrip(".,;!?:'\"。！，；")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while candidate.endswith(closing) and candidate.count(closing) > candidate.count(opening):
                candidate = candidate[:-1]
        try:
            parsed = urllib.parse.urlsplit(candidate)
            valid = parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except ValueError:
            valid = False
        if valid and candidate not in result:
            result.append(candidate)
    return result


def room_condition(rooms, prefix=""):
    if not rooms:
        return "1=1", []
    clauses, values = [], []
    for room in rooms:
        clauses.append(f"({prefix}account_id=? AND {prefix}room_id=?)")
        values.extend((room["account_id"], room["room_id"]))
    return "(" + " OR ".join(clauses) + ")", values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class Relay:
    def __init__(self, db_path, notifications=None):
        self.db_path = db_path
        self.notifications = notifications
        self.stop = threading.Event()
        self.max_messages = 500
        self.max_image_bytes = 32 * 1024 * 1024
        init_database(db_path, SCHEMA)

    def notify(self, connection, event, batch, **values):
        if self.notifications:
            job = connection.execute("SELECT name FROM relay_jobs WHERE job_id=?", (batch["job_id"],)).fetchone()
            self.notifications.enqueue(connection, event, job_id=batch["job_id"], job_name=job["name"],
                batch_id=batch["batch_id"], message_count=batch["message_count"], attempt=batch["attempts"], **values)

    def save_job(self, values, job_id=None):
        if not isinstance(values, dict):
            raise ValueError("任务配置必须是对象")
        name = values.get("name", "")
        target_url = values.get("target_url", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("需要任务名称")
        if not isinstance(target_url, str):
            raise ValueError("需要 HTTP/HTTPS 投递地址")
        parsed = urllib.parse.urlsplit(target_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment or parsed.username:
            raise ValueError("需要 HTTP/HTTPS 投递地址，不包含用户信息或片段")
        parsed.port
        enabled = values.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        rooms = values.get("rooms", [])
        if not isinstance(rooms, list) or len(rooms) > 100:
            raise ValueError("rooms 必须是最多 100 个账号/群的数组；空数组表示所有群")
        normalized = []
        for room in rooms:
            if not isinstance(room, dict) or any(not isinstance(room.get(key), str) or not room[key] for key in ("account_id", "room_id")):
                raise ValueError("每个群需要 account_id 和 room_id")
            entry = {key: room[key] for key in ("account_id", "room_id")}
            if entry not in normalized:
                normalized.append(entry)
        cron = values.get("cron", "0 * * * *")
        timezone_name = values.get("timezone", "Asia/Shanghai")
        next_run = Cron(cron, timezone_name).next_after(time.time())
        start = values.get("start", "all")
        if start not in ("all", "now"):
            raise ValueError("start 必须是 all 或 now")
        now = utc_now()
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if job_id:
                existing = connection.execute("SELECT * FROM relay_jobs WHERE job_id=?", (job_id,)).fetchone()
                if not existing:
                    raise ValueError("任务不存在")
                if existing["run_until"] is not None and normalized != json.loads(existing["rooms"]):
                    raise ValueError("当前投递尚未完成，不能修改群范围；可修正接收地址或暂停任务")
                pending = connection.execute("SELECT lease_until FROM relay_batches WHERE job_id=? AND status='pending'", (job_id,)).fetchone()
                if pending and pending["lease_until"] > time.time():
                    raise ValueError("请求正在进行，请稍后编辑；可单独暂停任务")
                connection.execute("""UPDATE relay_jobs SET name=?, target_url=?, cron=?, timezone=?, rooms=?,
                    enabled=?, next_run=?, updated_at=? WHERE job_id=?""",
                    (name.strip(), target_url, cron, timezone_name, json.dumps(normalized), enabled, next_run, now, job_id))
                connection.execute("UPDATE relay_batches SET target_url=?, next_attempt=0 WHERE job_id=? AND status='pending'", (target_url, job_id))
            else:
                job_id = uuid.uuid4().hex
                cursor = connection.execute("SELECT COALESCE(MAX(sequence),0) FROM messages").fetchone()[0] if start == "now" else 0
                connection.execute("""INSERT INTO relay_jobs
                    (job_id, name, target_url, cron, timezone, rooms, enabled, cursor, next_run, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job_id, name.strip(), target_url, cron, timezone_name, json.dumps(normalized), enabled, cursor, next_run, now, now))
        return self.job(job_id)

    def jobs(self):
        with database(self.db_path) as connection:
            rows = connection.execute("SELECT * FROM relay_jobs ORDER BY created_at, job_id").fetchall()
            result = []
            for row in rows:
                job = dict(row)
                job["rooms"] = json.loads(job["rooms"])
                job["enabled"] = bool(job["enabled"])
                clause, parameters = room_condition(job["rooms"])
                job["unsent_count"] = connection.execute(
                    f"SELECT COUNT(*) FROM messages WHERE sequence>? AND {clause}", (job["cursor"], *parameters)).fetchone()[0]
                job["sent_count"] = connection.execute(
                    "SELECT COALESCE(SUM(message_count),0) FROM relay_batches WHERE job_id=? AND status='sent'", (job["job_id"],)).fetchone()[0]
                pending = connection.execute("""SELECT batch_id, attempts, next_attempt, last_error, message_count,
                    lease_until FROM relay_batches WHERE job_id=? AND status='pending'""", (job["job_id"],)).fetchone()
                job["pending"] = dict(pending) if pending else None
                result.append(job)
        return result

    def job(self, job_id):
        return next((job for job in self.jobs() if job["job_id"] == job_id), None)

    def set_enabled(self, job_id, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        with database(self.db_path) as connection:
            if not connection.execute("UPDATE relay_jobs SET enabled=?, updated_at=? WHERE job_id=?", (enabled, utc_now(), job_id)).rowcount:
                raise ValueError("任务不存在")
        return self.job(job_id)

    def trigger(self, job_id):
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM relay_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError("任务不存在")
            if not job["enabled"]:
                raise ValueError("请先启用任务")
            if job["run_until"] is None:
                cutoff = connection.execute("SELECT COALESCE(MAX(sequence),0) FROM messages").fetchone()[0]
                connection.execute("UPDATE relay_jobs SET run_until=? WHERE job_id=?", (cutoff, job_id))
            connection.execute("UPDATE relay_batches SET next_attempt=0 WHERE job_id=? AND status='pending'", (job_id,))
        return self.job(job_id)

    def schedule_due(self, now):
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            due = connection.execute("SELECT * FROM relay_jobs WHERE enabled=1 AND next_run<=?", (now,)).fetchall()
            for job in due:
                cutoff = connection.execute("SELECT COALESCE(MAX(sequence),0) FROM messages").fetchone()[0]
                next_run = Cron(job["cron"], job["timezone"]).next_after(now)
                connection.execute("UPDATE relay_jobs SET run_until=COALESCE(run_until,?), next_run=? WHERE job_id=?",
                                   (cutoff, next_run, job["job_id"]))

    def build_archive(self, batch_id, job, rows, cutoff, created_at):
        output = io.BytesIO()
        messages = []
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for row in rows:
                event = json.loads(row["payload"])
                message = {key: event.get(key) for key in (
                    "event_id", "account_id", "message_id", "message_time_ms", "room_id", "room_name", "room_remark",
                    "sender_id", "sender_name", "is_self", "type", "collected_at")}
                message.update(sequence=row["sequence"], received_at=row["received_at"], text=message_text(event), links=message_links(event), raw=event["raw"])
                if event["type"] == "reply":
                    message["reply"] = event["raw"].get("reply")
                if event.get("image"):
                    image = {key: value for key, value in event["image"].items() if key not in ("url", "base64")}
                    if image["status"] == "available":
                        if not row["image"] or hashlib.sha256(row["image"]).hexdigest() != image["sha256"]:
                            raise ValueError(f"图片缺失或损坏：{event['event_id']}")
                        image["path"] = f"images/{event['event_id']}.{EXTENSIONS[row['content_type']]}"
                        archive.writestr(image["path"], row["image"])
                    message["image"] = image
                messages.append(message)
            document = {"schema_version": 1, "batch_id": batch_id, "job_id": job["job_id"], "created_at": created_at,
                        "range": {"after_sequence": job["cursor"], "through_sequence": cutoff},
                        "rooms": json.loads(job["rooms"]), "message_count": len(messages), "messages": messages}
            archive.writestr("messages.json", encode_json(document))
        return output.getvalue()

    def prepare_batch(self, job_id):
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM relay_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job or not job["enabled"] or job["run_until"] is None:
                return None
            pending = connection.execute("SELECT batch_id FROM relay_batches WHERE job_id=? AND status='pending'", (job_id,)).fetchone()
            if pending:
                return pending["batch_id"]
            clause, parameters = room_condition(json.loads(job["rooms"]))
            candidates = connection.execute(f"""SELECT sequence, COALESCE(LENGTH(image),0) AS image_size FROM messages
                WHERE sequence>? AND sequence<=? AND {clause} ORDER BY sequence LIMIT ?""",
                (job["cursor"], job["run_until"], *parameters, self.max_messages + 1)).fetchall()
            if not candidates:
                connection.execute("UPDATE relay_jobs SET run_until=NULL, last_error=NULL WHERE job_id=?", (job_id,))
                return None
            selected, size = [], 0
            for candidate in candidates:
                if selected and (len(selected) >= self.max_messages or size + candidate["image_size"] > self.max_image_bytes):
                    break
                selected.append(candidate["sequence"])
                size += candidate["image_size"]
            cutoff = job["run_until"] if len(selected) == len(candidates) else selected[-1]
            rows = connection.execute(f"SELECT * FROM messages WHERE sequence IN ({','.join('?' for _ in selected)}) ORDER BY sequence", selected).fetchall()
            batch_id, created_at = uuid.uuid4().hex, utc_now()
            archive = self.build_archive(batch_id, job, rows, cutoff, created_at)
            connection.execute("""INSERT INTO relay_batches
                (batch_id, job_id, from_sequence, through_sequence, message_count, target_url, sha256, archive, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, job_id, job["cursor"], cutoff, len(rows), job["target_url"], hashlib.sha256(archive).hexdigest(), archive, created_at))
            connection.executemany("INSERT INTO relay_batch_messages (batch_id, sequence) VALUES (?, ?)",
                                   ((batch_id, row["sequence"]) for row in rows))
            connection.execute("UPDATE relay_jobs SET last_error=NULL WHERE job_id=?", (job_id,))
        return batch_id

    def deliver_batch(self, batch_id, now=None):
        now = time.time() if now is None else now
        claim_token = uuid.uuid4().hex
        with database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute("""UPDATE relay_batches SET attempts=attempts+1, lease_until=?, claim_token=?
                WHERE batch_id=? AND status='pending' AND next_attempt<=? AND lease_until<=?
                AND EXISTS (SELECT 1 FROM relay_jobs WHERE relay_jobs.job_id=relay_batches.job_id AND enabled=1)""",
                (now + 120, claim_token, batch_id, now, now)).rowcount
            if not claimed:
                return False
            batch = connection.execute("SELECT * FROM relay_batches WHERE batch_id=?", (batch_id,)).fetchone()
            self.notify(connection, "delivery_started", batch)
        http_status = ""
        try:
            request = urllib.request.Request(batch["target_url"], data=batch["archive"], headers={
                "Content-Type": "application/zip", "Content-Disposition": f'attachment; filename="weward-{batch_id}.zip"',
                "Idempotency-Key": batch_id, "X-Weward-Batch-Id": batch_id,
                "X-Weward-SHA256": batch["sha256"], "X-Weward-Job-Id": batch["job_id"],
            }, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=30) as response:
                http_status = response.status
                if not 200 <= response.status < 300:
                    raise ValueError(f"外部接口返回 HTTP {response.status}")
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError("回执超过 64 KiB")
                receipt = json.loads(body)
            if (not isinstance(receipt, dict) or receipt.get("ok") is not True or receipt.get("batch_id") != batch_id
                    or not isinstance(receipt.get("receipt_id"), str) or not receipt["receipt_id"].strip()):
                raise ValueError("回执需包含 ok=true、匹配的 batch_id 和非空 receipt_id")
            with database(self.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute("""UPDATE relay_batches SET status='sent', receipt=?, sent_at=?,
                    last_error=NULL, lease_until=0, claim_token=NULL WHERE batch_id=? AND status='pending' AND claim_token=?""",
                    (encode_json(receipt).decode(), utc_now(), batch_id, claim_token)).rowcount
                if changed:
                    connection.execute("""UPDATE relay_jobs SET cursor=?,
                        run_until=CASE WHEN run_until<=? THEN NULL ELSE run_until END WHERE job_id=?""",
                        (batch["through_sequence"], batch["through_sequence"], batch["job_id"]))
                    self.notify(connection, "delivery_succeeded", batch, http_status=http_status, receipt_id=receipt["receipt_id"])
            logging.info("relay_sent batch=%s count=%s", batch_id, batch["message_count"])
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                http_status = error.code
            delay = min(3600, 2 ** min(batch["attempts"], 12))
            with database(self.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute("""UPDATE relay_batches SET last_error=?, next_attempt=?, lease_until=0, claim_token=NULL
                    WHERE batch_id=? AND status='pending' AND claim_token=?""",
                    (str(error)[:1000], time.time() + delay, batch_id, claim_token)).rowcount
                if changed:
                    self.notify(connection, "delivery_failed", batch, http_status=http_status, error=str(error)[:1000])
            logging.warning("relay_retry batch=%s attempt=%s error=%s", batch_id, batch["attempts"], type(error).__name__)
        return True

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.schedule_due(now)
        with database(self.db_path) as connection:
            jobs = connection.execute("SELECT job_id FROM relay_jobs WHERE enabled=1 AND run_until IS NOT NULL ORDER BY created_at").fetchall()
        for job in jobs:
            if self.stop.is_set():
                break
            try:
                batch_id = self.prepare_batch(job["job_id"])
                if batch_id:
                    self.deliver_batch(batch_id)
            except Exception as error:
                with database(self.db_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    previous = connection.execute("SELECT name, last_error FROM relay_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                    connection.execute("UPDATE relay_jobs SET last_error=? WHERE job_id=?", (str(error)[:1000], job["job_id"]))
                    if self.notifications and previous["last_error"] != str(error)[:1000]:
                        self.notifications.enqueue(connection, "delivery_failed", job_id=job["job_id"],
                            job_name=previous["name"], error=str(error)[:1000])
                logging.exception("relay_job_failed job=%s", job["job_id"])

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                logging.exception("relay_worker_failed")
            self.stop.wait(1)

    def batches(self, job_id=None, before=None, limit=30):
        with database(self.db_path) as connection:
            rows = connection.execute("""SELECT batch_id, job_id, status, from_sequence, through_sequence,
                message_count, sha256, LENGTH(archive) AS size, created_at, sent_at, attempts, next_attempt,
                last_error, receipt FROM relay_batches WHERE (? IS NULL OR job_id=?)
                AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?""", (job_id, job_id, before, before, limit)).fetchall()
            result = []
            for row in rows:
                batch = dict(row)
                batch["receipt"] = json.loads(batch["receipt"]) if batch["receipt"] else None
                batch["sequence"] = connection.execute("SELECT rowid FROM relay_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0]
                result.append(batch)
        return result

    def archive(self, batch_id):
        with database(self.db_path) as connection:
            row = connection.execute("SELECT archive FROM relay_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return row["archive"] if row else None
