import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aU1cAAAAASUVORK5CYII=")


def worker(target):
    from weward.collector import main
    from weward.common import Handler, make_server

    room_id = "docker-smoke@chatroom"
    messages = []
    for sender in ("smoke-self", "smoke-member"):
        for kind in ("text", "image", "link"):
            messages.append({
                "msgid": sender + "-" + kind, "action": "send", "from": sender, "tolist": [],
                "roomid": room_id, "msgtime": int(time.time() * 1000), "msgtype": kind,
                kind: {"text": {"content": "Docker 模拟消息，无需 @"},
                       "image": {"sdkfileid": "inbox/sample.png"},
                       "link": {"title": "Docker 测试链接", "link_url": "https://example.com"}}[kind],
            })
    messages.append({**messages[1], "msgid": "missing-image", "image": {"sdkfileid": ""}})
    shared = Path("/tmp/weward-smoke-media")
    media = shared / "rooms" / room_id / "inbox" / "sample.png"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(PNG)
    token = Path("/tmp/weward-smoke-token")
    token.write_text("smoke-token")

    class FakeHandler(Handler):
        def do_GET(self):
            if self.path == "/ilink/bot/userinfo":
                self.send_json({"ret": 0, "account_id": "smoke-self", "wechat_id": "smoke-self", "nickname": "本人"})
            elif self.path.startswith("/ilink/bot/contacts?"):
                self.send_json({"ret": 0, "contacts": [{"roomid": room_id, "remark": "webox.smoke", "nickname": "Docker 模拟群"}]})
            else:
                self.send_json({"error": "unknown"}, 404)

        def do_POST(self):
            if self.path != "/ilink/bot/getupdates":
                self.send_json({"error": "sending forbidden"}, 405)
                return
            self.read_json()
            self.send_json({"ret": 0, "cursor": str(int(time.time() * 1000)), "messages": messages})

    server = make_server(("127.0.0.1", 0), FakeHandler, None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update({"WEBOX_URL": f"http://127.0.0.1:{server.server_port}", "WEBOX_TOKEN_FILE": str(token),
                       "WEBOX_SHARED_DIR": str(shared), "WATCH_ROOM_REMARK": "webox.smoke", "FORWARD_URL": target})
    main()


def docker(*arguments):
    return subprocess.check_output(["docker", *arguments], text=True).strip()


def request(url, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode()
    with opener.open(urllib.request.Request(url, data, {"Content-Type": "application/json"}), timeout=5) as response:
        return json.load(response)


def wait_for(condition):
    deadline = time.monotonic() + 60
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = condition()
            if result:
                return result
        except Exception as error:
            last_error = error
        time.sleep(0.3)
    raise RuntimeError(f"Docker smoke condition timed out: {last_error}")


def address(container, port):
    info = json.loads(docker("inspect", container))[0]
    published = info["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"]
    return "http://127.0.0.1:" + published


def smoke():
    identity = "weward-smoke-" + uuid.uuid4().hex[:10]
    collector_name, receiver_name = identity + "-collector", identity + "-receiver"
    with tempfile.TemporaryDirectory(prefix="weward-docker-smoke-") as directory:
        root = Path(directory)
        for role in ("collector", "receiver"):
            (root / role).mkdir()
        docker("network", "create", identity)
        try:
            docker("run", "-d", "--name", collector_name, "--network", identity,
                   "-p", "127.0.0.1::8081", "-v", f"{root / 'collector'}:/data",
                   "-v", f"{Path(__file__).resolve()}:/app/docker_smoke.py:ro",
                   "--entrypoint", "python3", "weward-collector", "-u", "/app/docker_smoke.py",
                   "--worker", f"http://{receiver_name}:8080/api/messages")
            collector_url = address(collector_name, 8081)
            wait_for(lambda: request(collector_url + "/api/status")["queue"]["pending"] == 7)
            wait_for(lambda: any(record["attempts"] for record in request(collector_url + "/api/status")["recent_deliveries"]))
            docker("run", "-d", "--name", receiver_name, "--network", identity,
                   "-p", "127.0.0.1::8080", "-v", f"{root / 'receiver'}:/data", "weward-receiver")
            receiver_url = address(receiver_name, 8080)
            wait_for(lambda: request(receiver_url + "/healthz")["ok"])
            request(collector_url + "/api/retry", {})
            wait_for(lambda: request(collector_url + "/api/status")["queue"]["delivered"] == 7)
            first = request(receiver_url + "/api/messages")
            assert first["total"] == 7, first
            assert sum(event["is_self"] for event in first["messages"]) == 4
            original_times = {event["event_id"]: event["received_at"] for event in first["messages"]}
            docker("restart", receiver_name, collector_name)
            collector_url, receiver_url = address(collector_name, 8081), address(receiver_name, 8080)
            wait_for(lambda: request(collector_url + "/api/status")["ready"])
            second = request(receiver_url + "/api/messages")
            assert second["total"] == 7
            assert {event["event_id"]: event["received_at"] for event in second["messages"]} == original_times
            image_count = 0
            for event in second["messages"]:
                if event.get("image", {}).get("status") == "available":
                    with urllib.request.urlopen(receiver_url + event["image"]["url"], timeout=5) as response:
                        assert response.read() == PNG
                    image_count += 1
            assert image_count == 2
            assert any(event.get("image", {}).get("status") == "unavailable" for event in second["messages"])
            print(json.dumps({"result": "passed", "messages": 7, "images": 2, "initial_outage_retried": True,
                              "both_containers_restarted": True, "receiver_first_receipt_preserved": True,
                              "real_wechat": False}))
        except Exception:
            for container in (collector_name, receiver_name):
                subprocess.run(["docker", "logs", "--tail", "25", container], check=False)
            raise
        finally:
            subprocess.run(["docker", "rm", "-f", collector_name, receiver_name], check=False, stdout=subprocess.DEVNULL)
            subprocess.run(["docker", "network", "rm", identity], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
    else:
        smoke()
