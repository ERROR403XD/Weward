"""Delivery configuration, immutable HTTP payloads and response validation."""

import json
import re

HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
RESERVED_HEADERS = {
    "host", "content-length", "content-type", "content-disposition", "transfer-encoding",
    "connection", "trailer", "upgrade", "expect", "idempotency-key",
}
SENSITIVE_NAME = re.compile(r"authorization|cookie|token|secret|password|api[-_]?key", re.I)


def defaults():
    return {"delivery_mode": "zip", "headers": {}, "text": None, "success_response": {"mode": "receipt"}}


def load_config(serialized):
    return {**defaults(), **json.loads(serialized)}


def normalize_config(values, previous=None):
    config = {**defaults(), **(previous or {})}
    config.update({key: values[key] for key in config if key in values})
    if config["delivery_mode"] not in ("zip", "multipart"):
        raise ValueError("delivery_mode 必须是 zip 或 multipart")
    if config["text"] is not None and not isinstance(config["text"], str):
        raise ValueError("text 必须是字符串或 null")
    headers = config["headers"]
    if not isinstance(headers, dict):
        raise ValueError("headers 必须是请求头对象")
    old_headers = {key.lower(): value for key, value in (previous or {}).get("headers", {}).items()}
    normalized, seen = {}, set()
    for key, value in headers.items():
        if (not isinstance(key, str) or not HEADER_NAME.fullmatch(key) or key.lower() in seen
                or key.lower() in RESERVED_HEADERS or key.lower().startswith("x-weward-")):
            raise ValueError("请求头名称无效、重复或属于系统管理的协议头")
        seen.add(key.lower())
        if value is None:
            if key.lower() not in old_headers:
                raise ValueError("null 只能保留已保存的同名请求头")
            value = old_headers[key.lower()]
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) >= 127 for char in value):
            raise ValueError("请求头值必须是无换行或控制字符的 ASCII 字符串")
        normalized[key] = value
    config["headers"] = normalized
    rule = config["success_response"]
    if not isinstance(rule, dict) or rule.get("mode") not in ("receipt", "custom"):
        raise ValueError("success_response.mode 必须是 receipt 或 custom")
    if rule["mode"] == "receipt":
        config["success_response"] = {"mode": "receipt"}
    else:
        codes = rule.get("status_codes", "2xx")
        if codes != "2xx" and (not isinstance(codes, list) or not codes or any(
                type(code) is not int or not 200 <= code < 300 for code in codes)):
            raise ValueError("成功状态码必须是 2xx 或非空的 200–299 整数数组")
        field = rule.get("response_field", "")
        if not isinstance(field, str) or (field and not re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", field)):
            raise ValueError("响应字段请使用 outcome 或 data.outcome 形式的对象路径")
        if field and "response_value" not in rule:
            raise ValueError("填写响应字段时必须提供 response_value")
        config["success_response"] = {
            "mode": "custom", "status_codes": codes, "response_field": field,
            "response_value": rule.get("response_value"),
        }
    try:
        serialized = json.dumps(config, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("投递配置必须是有效的 JSON 数据") from None
    if len(serialized) > 65536:
        raise ValueError("投递配置不能超过 64 KiB")
    return config


def redact(value, headers):
    secrets = set()
    for name, secret in headers.items():
        if secret:
            secrets.add(secret)
            if "authorization" in name.lower() and " " in secret:
                secrets.add(secret.split(" ", 1)[1])
    secrets.discard("")

    def clean(item):
        if isinstance(item, str):
            for secret in sorted(secrets, key=len, reverse=True):
                item = item.replace(secret, "[hidden]")
            return item
        if isinstance(item, dict):
            return {clean(key): "[hidden]" if SENSITIVE_NAME.search(key) else clean(value)
                    for key, value in item.items()}
        if isinstance(item, list):
            return [clean(value) for value in item]
        return item

    return clean(value)


def public_config(config):
    return {**config, "headers": {key: None for key in config["headers"]}}


def request_payload(batch_id, job_id, sha256, archive, config):
    filename = f"weward-{batch_id}.zip"
    headers = {
        **config["headers"], "Idempotency-Key": batch_id, "X-Weward-Batch-Id": batch_id,
        "X-Weward-SHA256": sha256, "X-Weward-Job-Id": job_id,
    }
    body = archive
    if config["delivery_mode"] == "zip":
        headers.update({"Content-Type": "application/zip",
                        "Content-Disposition": f'attachment; filename="{filename}"'})
    else:
        text = config["text"].encode("utf-8") if config["text"] is not None else None
        boundary = "weward-" + batch_id
        while boundary.encode("ascii") in archive or (text is not None and boundary.encode("ascii") in text):
            boundary += "x"
        parts = []
        if text is not None:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n'
                         'Content-Type: text/plain; charset=utf-8\r\n\r\n'.encode("ascii") + text + b"\r\n")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                      'Content-Type: application/zip\r\n\r\n').encode("ascii") + archive + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    headers["Content-Length"] = str(len(body))
    return headers, body


def json_equal(actual, expected):
    # JSON booleans are distinct from numbers, including inside arrays and objects.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def validate_response(status, body, batch_id, rule):
    codes = rule.get("status_codes", "2xx")
    if not 200 <= status < 300 or (rule["mode"] == "custom" and codes != "2xx" and status not in codes):
        raise ValueError(f"外部接口返回 HTTP {status}，不符合成功条件")
    if len(body) > 65536:
        raise ValueError("回执超过 64 KiB")
    try:
        def reject_constant(value):
            raise ValueError("非 JSON 常量")
        result = json.loads(body, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        if rule["mode"] == "receipt" or rule.get("response_field"):
            raise ValueError("回执不是有效的 JSON") from None
        result = body.decode("utf-8", errors="replace")
    if rule["mode"] == "receipt":
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("batch_id") != batch_id
                or not isinstance(result.get("receipt_id"), str) or not result["receipt_id"].strip()):
            raise ValueError("回执需包含 ok=true、匹配的 batch_id 和非空 receipt_id")
        return result
    if rule["response_field"]:
        actual = result
        for field in rule["response_field"].split("."):
            if not isinstance(actual, dict) or field not in actual:
                raise ValueError("回执缺少指定 JSON 字段")
            actual = actual[field]
        if not json_equal(actual, rule["response_value"]):
            raise ValueError("回执 JSON 字段与预期值不匹配")
    return {"http_status": status, "body": result}
