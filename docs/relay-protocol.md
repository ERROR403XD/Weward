# 中转器协议与实现约定

本文件随源码维护。`receiver` 是中转器服务名；入站地址、数据库文件名与原接收端兼容。仅使用 Python 标准库和 SQLite，不依赖系统 crond。

## 数据与管理

原始消息、收到的图片、备注和整理状态保存在 `messages`。入站重复按 `event_id` 去重，首次入库序号、接收时间、图片、备注和投递状态均不被覆盖。管理操作不删除消息。查询列由原 payload 回填，旧字段保留。

`relay_jobs` 保存任务配置、确认游标、当前轮截止序号和下次执行时间。`relay_batches` 原子保存固定 ZIP、SHA256、消息范围、投递配置快照、HTTP 请求头、尝试次数和回执；multipart 批次额外保存完整请求正文（包含 ZIP 副本），直传复用 archive 字节。`relay_batch_messages` 记录文件包实际包含的消息。文件包和图片均在独立中转器数据库中，打包期间不读取采集端目录或调用 WeBox。

新任务默认暂停，必须明确设置接收地址并启用。每个任务一个独立游标，可选择所有群或具体 `{account_id, room_id}`。同一个消息可投递给多个任务，其“已发送”状态分别计算。消息查询 API 未指定任务时，“已发送”表示至少一个任务已获回执；指定任务后仅计算该任务。选择“仅新记录”时，创建前记录不会显示为该任务待发送。

WebUI 的「消息」按账号/群切换，紧凑显示内容；元数据和管理操作在详情中。「投递任务」包含任务表与投递历史，历史详情集中展示回执、错误和文件下载。「配置」管理采集端监听群。

## 群监听控制

receiver 通过 `COLLECTOR_CONTROL_URL`（默认 `http://collector:8081`）代理 collector 的 `/api/monitoring`，不持有 WeBox token，不读微信文件或拉取消息。查询和修改只针对当前登录账号；上游不可用返回 502，输入无效返回 400，不影响既有记录的阅读和投递。

- `GET /api/monitoring` 返回账号和监听列表 `rooms`；加 `query=<群名>` 返回额外的 `matches`。同名群按 `room_id` 区分，可使用完整群 ID 搜索。
- `POST /api/monitoring` 接收 `{ "action": "add|set|remove", "account_id": "...", "room_id": "...@chatroom", "enabled": true }`。`add` 从实际联系人解析群，`set` 暂停或恢复，`remove` 移除监听。账号必须与当前登录账号一致。
- collector 的 `watch_accounts` / `watched_rooms` 持久保存显式选择。在首次修改前仍按原备注发现；首次修改自动导入原群。各账号独立，不改微信备注。
- 暂停、移除保留消息与游标；恢复从原进度续采，首次新增使用原有回溯配置。全部暂停时进入 `idle`。配置修改最迟在当前长轮询结束后生效。
- WeBox compat2 的群搜索接口读取群联系人，getupdates 的 `room_ids` 显式选择群。collector 提交消息与游标时，在同一事务内核对监听版本；轮询期间配置变化则丢弃旧结果，按新配置重新读取。

## 增量与批次边界

1. 定时触发或点击立即发送时，固定数据库当前最大入库序号为本轮截止值。
2. 选取 `(上次已确认游标, 本轮截止值]` 内符合群范围的消息，按入库序号升序打包。
3. 每包最多 500 条，图片字节总量目标上限 32 MiB；超限单图允许单独成包。此限制不是整个 ZIP 或 JSON 的硬大小限制。
4. 创建包、保存字节及成员关系在同一 SQLite 事务内完成。之后接收的新消息不改变该包。
5. 回执验证成功后，以一个事务保存回执、标记该包已发送、推进游标。本轮分包会持续发送直到固定截止值，不需等待下一次 cron。
6. 未选中的群不进入包。空范围不生成包，也不推进已确认游标。后续入库的迟到消息仍可进入下一轮，即使其微信时间更早。

未完成一轮投递时不允许变更群范围，避免跳过记录。批次生成时固定接收地址、请求头、投递方式、text、成功条件以及请求正文；编辑任务只作用于之后生成的批次（含当前轮尚未生成的后续分包），不改写待重试批次。正在进行的请求不能编辑配置，可暂停后等其完成。此规则取代旧版“修正地址后原批次改用新地址”的行为，保证同一 Idempotency-Key 对应同一请求。若必须使用新地址或新凭据重投，先暂停旧任务并核对外部接收记录，再创建新任务；新任务具有独立游标和批次身份，可能重复发送历史。

完成一轮后修改群范围或接收地址继续沿用现有游标；要向新范围或新系统补发全部历史，请创建新任务。收起记录和添加备注不改变待发送范围；备注属于管理信息，不改原始消息或已生成包。

## ZIP 内容

```text
messages.json
images/<event_id>.png
images/<event_id>.jpg
```

图片按收到的 MIME 类型使用 png/jpg/gif/webp/bmp 后缀，文件名来自 event_id。图片完整字节来自中转器数据库，生成包时复核 SHA256；可用图片如果丢失或损坏，整个批次整理失败，并在任务页显示错误，不标记已发送。

JSON 的 UTF-8 示例（标识为示意值）：

```json
{
  "schema_version": 1,
  "batch_id": "32位十六进制批次ID",
  "job_id": "32位十六进制任务ID",
  "created_at": "2026-11-06T12:00:00.000+00:00",
  "range": {"after_sequence": 120, "through_sequence": 140},
  "rooms": [{"account_id": "account-a", "room_id": "room-a@chatroom"}],
  "message_count": 1,
  "messages": [{
    "sequence": 133,
    "event_id": "64位十六进制事件ID",
    "account_id": "account-a",
    "message_id": "source-message-id",
    "message_time_ms": 1793966400000,
    "room_id": "room-a@chatroom",
    "room_name": "示例群",
    "room_remark": "webox.weward",
    "sender_id": "sender-a",
    "sender_name": "本人",
    "is_self": true,
    "type": "image",
    "collected_at": "2026-11-06T11:59:59.000+00:00",
    "received_at": "2026-11-06T11:59:59.500+00:00",
    "text": "",
    "links": [],
    "image": {"status": "available", "path": "images/64位十六进制事件ID.png", "content_type": "image/png", "sha256": "图片SHA256", "size": 1024},
    "raw": {"msgtype": "image"}
  }]
}
```

`links` 是 URL 字符串数组，提取普通文字、引用标题、链接分享标题/描述/地址和视频号文字中的 HTTP(S) URL，去重并解码 HTML 实体。不访问网页，不抓取链接附件。链接卡片标题和描述同时保留在 `text` 和 `raw`；`raw` 保留实际上游消息，供无法规范化的类型使用。

可用图片包含相对 ZIP 根目录的 `image.path`；不可用图片保留 `status=unavailable` 与原因，不产生伪造文件。JSON 不携带接收端媒体 URL 或图片 Base64。自定义表情不作为媒体支持范围。

## 外部投递与回执

默认请求为直接 ZIP 请求体，旧任务和旧批次升级后保持此行为：

```http
POST /configured-path HTTP/1.1
Content-Type: application/zip
Content-Disposition: attachment; filename="weward-<batch_id>.zip"
Idempotency-Key: <batch_id>
X-Weward-Batch-Id: <batch_id>
X-Weward-Job-Id: <job_id>
X-Weward-SHA256: <ZIP文件SHA256>
```

外部应先校验、可靠保存包并生成持久回执，再返回 HTTP 2xx 与不超过 64 KiB 的 JSON 对象：

```json
{"ok": true, "batch_id": "与请求相同的批次ID", "receipt_id": "外部持久回执编号"}
```

默认 `success_response: {"mode":"receipt"}`：`ok` 必须是布尔 true，`batch_id` 必须与当前包完全一致，`receipt_id` 必须是非空字符串。额外字段随原回执一起保存。仅有 HTTP 200、HTTP 202/204 空正文、HTML、错误批次 ID 或负回执均不代表成功。重定向不跟随。没有异步回调或轮询回执接口；外部若需要长时间处理，应先可靠收包并回执，后续处理由外部负责。

### 自定义请求头与 multipart

`headers` 是名称到字符串值的对象。名称按 HTTP token 校验且不区分大小写，不允许重复；值须为不含控制字符的 ASCII 字符串。Host、Content-Type、Content-Disposition、Content-Length、Transfer-Encoding、Connection、Trailer、Upgrade、Expect、Idempotency-Key 及所有 X-Weward-* 头由系统保留，不能覆盖。配置总大小不超过 64 KiB。

所有自定义请求头值均按敏感值处理：写入配置和批次快照供实际投递使用，管理 API 只返回 `{ "Authorization": null }` 等名称映射；界面使用密码框。编辑时同名 `null` 保留原值（不区分大小写），字符串替换值，缺少整个 `headers` 字段保留全部，显式 `{}` 清空。已保存值不提供读回接口。响应回显的请求头敏感值在历史 API 中脱敏，日志和失败通知不包含这些值；成功回执原文仍保存在本地数据库内。

`delivery_mode` 支持 `zip`（默认）或 `multipart`。multipart 使用固定 boundary 和 CRLF，字段为：

| 字段 | 内容 |
| --- | --- |
| `files` | 单个 ZIP，filename=`weward-<batch_id>.zip`，Content-Type=`application/zip` |
| `text` | 可选 UTF-8 文本；`null` 不发送，空字符串发送空字段，界面留空按不发送处理 |

multipart 仍携带 Idempotency-Key、X-Weward-Batch-Id、X-Weward-Job-Id、X-Weward-SHA256；SHA256 针对原 ZIP，下载接口仍下载 ZIP。请求正文与 Content-Type 在批次生成时一起持久保存，重启后复用，不重新选择 boundary 或编码表单。

### 自定义成功条件

`success_response.mode="custom"` 时支持以下配置：

```json
{
  "mode": "custom",
  "status_codes": "2xx",
  "response_field": "outcome",
  "response_value": "submitted"
}
```

`status_codes` 默认为 `"2xx"`，也可设为非空的 200–299 整数数组，例如 `[200,202]`。不接受其他范围或重定向。`response_field` 使用对象点路径，例如 `data.outcome`，不支持数组下标；填写字段后必须提供 JSON `response_value`，可为字符串、数值、布尔、null、数组或对象。字段必须存在且值相等；缺失不等于 null，布尔不等于数字，复合值内部也按此规则比较。状态码和 JSON 条件同时满足才成功，不要求额外提供旧协议的 ok、batch_id、receipt_id。

字段为空时只判断状态码，允许 204 空正文和非 JSON 文本。任意成功模式均限制响应正文不超过 64 KiB。custom 回执以 `{"http_status":202,"body":<JSON值或原始文本>}` 保存；默认 receipt 模式仍保存原 JSON 对象，保持兼容。仅成功时同事务保存回执、标记 sent、推进游标并排队成功通知；HTTP 错误、非法 JSON、字段缺失/不匹配、超时均保留原批次等待重试。自定义回执无 receipt_id 时成功通知中的该占位符为空。

HTTP 超时为 30 秒，失败按 2、4、8…秒指数退避，最多 1 小时。任务列表「重试」或历史详情「立即重试」可清除退避时间，但不会绕过暂停或正在进行的请求租约。所有重试沿用同一地址、包字节、表单参数、请求头、成功条件、SHA256 和批次 ID。外部须按 `Idempotency-Key` 幂等，重复 POST 返回原回执；本端提供至少一次投递，无法单方保证跨系统恰好一次。

请求使用数据库 120 秒租约避免同时投递同一批次，进程退出后的未完成请求在租约过期后可重试。准备包时短事务锁定写入，HTTP 请求期间不持有数据库写锁。一个后台线程顺序处理启用任务，单个慢目标可能延后其他任务。暂停阻止新请求，正在进行的请求仍可完成并记录回执。receiver 设置 40 秒停止宽限期，退出时等待当前请求收尾。

## 定时规则

五字段顺序为“分 时 日 月 周”，支持数字、`*`、逗号列表、`-` 范围和 `/` 步长。周为 0–7，0/7 均为星期日。不支持秒字段、英文月份/星期或 `@daily`。日和周都显式限制时采用“满足任一”，有星号字段时按对应匹配交集处理。

时区使用 IANA 名称，默认 `Asia/Shanghai`。例如：

| cron | 执行时间 |
| --- | --- |
| `*/30 * * * *` | 每小时第 0、30 分钟 |
| `0 9 * * *` | 每天 09:00 |
| `0 18 * * 1-5` | 工作日 18:00 |

按配置时区处理夏令时：不存在的本地时间跳过，重复的本地时间可分别执行。已保存的 `next_run` 过期时，重启/恢复会补触发一轮到当前入库截止值，不为每个停机时段重建空任务。运行中的同一任务不会并发启动第二轮；其新收到的消息留待后续定时/手动触发。空轮不向外部发送空包。

## 管理 API

所有管理接口只供可信内网使用。POST 请求均为 JSON 对象；字段不完整或无效返回 400。后台发送不在 HTTP 管理请求内阻塞。

| 接口 | 作用 |
| --- | --- |
| `GET /api/status` | 收到、收起、已有回执的消息计数及图片字节 |
| `GET /api/rooms` | 按账号/群聚合的群列表与最新名称 |
| `GET /api/messages` | 消息和逐任务投递状态；支持 before、limit（1–100）、account_id、room_id、type、q、archived（all/0/1）、job_id、state（all/pending/sent） |
| `POST /api/messages/<event_id>/manage` | `{ "note": "备注", "archived": true }`，两字段均可单独更新 |
| `GET /api/jobs` | 所有任务、待发送数、已确认数、当前失败信息 |
| `POST /api/jobs` | 创建任务 |
| `POST /api/jobs/<job_id>` | 更新任务配置，保留游标；新增投递字段省略时保留原值，start 字段仅创建时生效，已生成批次不变 |
| `POST /api/jobs/<job_id>/enabled` | `{ "enabled": true }` 启用/暂停 |
| `POST /api/jobs/<job_id>/run` | `{}` 启动本轮或立即重试原包；需要任务已启用，返回 202 |
| `GET /api/batches` | 投递历史及回执；支持 job_id、before、limit（1–100，默认30） |
| `GET /api/batches/<batch_id>/download` | 下载已持久保存的实际 ZIP 字节 |
| `POST /api/batches/<batch_id>/retry` | `{}` 立即重试指定 pending 批次，返回 202；已完成、不存在、任务暂停时返回 400 |

创建任务示例：

```json
{
  "name": "每天的群记录",
  "target_url": "http://external-receiver:8080/upload",
  "cron": "0 9 * * *",
  "timezone": "Asia/Shanghai",
  "rooms": [{"account_id": "account-a", "room_id": "room-a@chatroom"}],
  "enabled": false,
  "start": "all"
}
```

`start` 为 `all` 或 `now`；后者取创建时最大入库序号作为起点。所有群使用 `rooms: []`。省略新增字段时默认 `delivery_mode="zip"`、`headers={}`、`text=null`、`success_response={"mode":"receipt"}`。接入表单接口时可在创建任务对象中追加：

```json
{
  "delivery_mode": "multipart",
  "headers": {"Authorization": "Bearer example-token"},
  "text": "本次群聊记录",
  "success_response": {
    "mode": "custom",
    "status_codes": "2xx",
    "response_field": "outcome",
    "response_value": "submitted"
  }
}
```

## 运行、存储与验证

无第三方 Python 包；容器的系统时区数据供 `zoneinfo` 使用。默认保留消息、原图片、ZIP 和回执，多任务会各自保存包，磁盘使用会持续增长。当前不提供自动删除或保留期，以保留可重试原件和回执关联。

升级自动追加投递配置和请求快照列；旧任务、已完成回执和待重试 ZIP 保留原身份、字节及游标。旧待重试批次第一次尝试时持久保存默认 HTTP 请求头。回退应暂停任务、等请求结束并恢复旧版程序及升级前数据库备份；旧版程序不认识新增投递配置，不能直接处理含新配置的数据库。外部已接收的数据无法通过本地回退撤回。

执行 `python3 -m unittest discover -s tests -v` 验证迁移、原采集链路、真实本地 HTTP ZIP/multipart 投递、请求头脱敏与保留/替换/删除、自定义成功条件、错误回执、响应丢失、修改配置与重启后请求逐字节不变、手动重试、事务回滚、并发租约、分群/分账号、分包、迟到消息和 cron 边界。外部系统真实地址与回执对接需要使用其实际接口另行验收。
