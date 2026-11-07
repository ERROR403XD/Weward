# Weward

微信群消息采集与中转项目，采集端基于 [netcat-ai/webox](https://github.com/netcat-ai/webox/tree/8ca042cd79221d05f6a74cb48064c7ba14220dc9) 的真实 Linux 微信客户端。包含采集转发端和消息中转器两个独立服务，无大模型和自动回复。

```text
微信群 → Linux 微信本地数据库 → WeBox getupdates
                                      ↓
                         collector 持久队列（含图片快照）
                                      ↓ HTTP POST JSON
                         receiver 中转器 → 数据库 / WebUI
                                      ↓ 定时或手动整理
                         messages.json + images/ → ZIP
                                      ↓ HTTP POST / 有效回执
                         外部接收端 ← 已发送标记与独立游标
```

`collector` 容器同时运行固定版本 WeBox、微信桌面和转发程序。`receiver` 是消息中转器，保留原服务名及入站地址以兼容采集端。它没有 WeBox 配置、凭据或共享目录，仅保存实际收到的 POST。图片字节通过入站 POST 的 Base64 字段传输，中转器独立保存、展示并打包输出。

## 存储与使用

### 存储目录与 NFS

默认数据已在宿主机的 `data/`，不写入容器可写层。`.env` 可设置 `WEBOX_STATE_DIR`、`COLLECTOR_DATA_DIR`、`RECEIVER_DATA_DIR` 改变三个持久目录；默认值分别为 `./data/webox`、`./data/collector`、`./data/receiver`。后两个目录包含使用 WAL 的 SQLite 数据库，应放在本机磁盘。目录映射解决系统盘占用，不会自动删除历史消息或限制总容量。

要将**完整微信数据**移到另一块本地盘，先停 collector，复制原 `data/webox/home/xwechat_files/` 的内容到已存在的目标目录（保留 UID/GID），在 `.env` 配置 `WECHAT_FILES_DIR=/mnt/storage/weward/xwechat_files`，然后运行：

```bash
docker compose -f compose.yaml -f compose.wechat-storage.yaml up -d --build collector
```

登录身份、WeBox 配置仍在 `WEBOX_STATE_DIR`。完整微信目录也包含微信自己的数据库及锁文件；虽可将已有 NFS 目录作为 bind 来源，**不把整套数据库迁到 NFS 作为稳定运行的推荐方案**。尤其不要让两个客户端同时使用同一份微信数据。

NFS 推荐只承载较大的媒体文件。使用 `compose.nfs.yaml` 将指定账号的 `msg`（图片附件、文件、视频）、`resource` 与 WeBox 解密后的 `shared` 映射出去，账号数据库、登录资料和两个 Weward 数据库留在本地：

1. 在宿主机挂载 NFS，确认挂载已生效；不要仅创建一个同名的普通本地目录。
2. 找到 `data/webox/home/xwechat_files/` 内完整的账号目录名，例如 `wxid_example_suffix`。在 `.env` 写入 `WECHAT_ACCOUNT_DIR=wxid_example_suffix` 和 `WECHAT_MEDIA_DIR=/mnt/nfs/weward/media`。
3. 停止 collector，将账号目录内的 `msg/`、`resource/` 分别复制到 `${WECHAT_MEDIA_DIR}/msg/`、`${WECHAT_MEDIA_DIR}/resource/`，将 `data/webox/shared/` 复制到 `${WECHAT_MEDIA_DIR}/shared/`。目标需包含原目录的内容，避免多套一层目录；保留本地副本用于回滚。不要在线移动正在使用的数据库。
4. 在存储端准备好 UID/GID `1000:1000` 的读写权限。NFS 开启 root_squash 时应在存储端处理权限，不需要关闭 root_squash。目标目录及已有文件均应可由该 UID 读写。
5. 使用以下命令启动。更换账号后需要修改账号目录变量，多个账号可按同一结构增加 Compose 挂载。两个可选覆盖文件选一种使用。

```bash
docker compose -f compose.yaml -f compose.nfs.yaml up -d --build collector
```

可选挂载禁用自动创建来源目录，避免路径写错时静默使用空目录。采集镜像跳过对微信目录和 shared 的递归 chown，以微信进程身份试写映射目录；NFS 不允许 chown 但 UID 1000 可写时仍可启动。缺失或不可写会在启动时明确失败。宿主机应先挂载 NFS 再启动 Docker 服务，并在重启后核实挂载；NFS 断开时的可用性取决于挂载设置，当前没有真实 NFS 故障演练结果。

迁移后检查桌面登录、采集 ready、已有图片可读和新图片可采集，再回收原媒体副本。重建 collector 可能需要在手机确认登录。回滚时停 collector，将迁移后新增的媒体同步回原目录，去掉覆盖文件和对应变量后启动；不要直接删除仍可能包含新增文件的目标目录。

### POST 事件通知

WebUI「配置」下可配置通知地址、可选请求头、JSON 请求体、每种事件的开关及通知文案。默认关闭；通知保存在 receiver 的独立持久队列，由独立线程发出。receiver 只查询 collector 控制 API 的状态，不访问 WeBox 或微信目录。

| 事件 | 触发时机 |
| --- | --- |
| `collector_offline` | 控制接口不可达、采集未就绪，或超过 120 秒没有完成采集轮询；连续 3 次观察异常 |
| `collector_online` | 连续 2 次观察采集就绪，包含已登录但没有监听群的 idle 状态 |
| `delivery_started` | 每个文件包实际开始 HTTP POST，包含自动重试；空任务没有文件包，不触发 |
| `delivery_succeeded` | 外部 HTTP 2xx 且有效投递回执已保存，游标已原子推进 |
| `delivery_failed` | HTTP 非 2xx、超时、JSON 无效、`ok` 非 true、batch_id 不匹配、缺少 receipt_id；构建文件包失败也通知 |

采集状态每次观察结束后等待 10 秒；初次启用也会在连续确认后报告当前状态，之后只有变化才通知。稳定状态持久保存，重启不会重复报告同一状态。离线不能直接等同于微信退出登录，文案中的 `reason` 提供采集异常或连接失败原因。receiver 自己停止运行时无法发送通知，需依靠外部监控覆盖。

请求体默认 `{"text":"{{message}}"}`，也可使用例如：

```json
{"msgtype":"text","text":{"content":"{{message}}"},"event":"{{event}}"}
```

`{{message}}` 是该事件文案展开后的结果，仅用于请求体。事件文案和请求体可用：`{{event}}`、`{{event_name}}`、`{{event_id}}`、`{{occurred_at}}`、`{{reason}}`、`{{account_id}}`、`{{job_id}}`、`{{job_name}}`、`{{batch_id}}`、`{{message_count}}`、`{{attempt}}`、`{{http_status}}`、`{{error}}`、`{{receipt_id}}`。无对应值的字段为空字符串；时间使用 UTC ISO 格式。将占位符写在 JSON 字符串值内，换行、引号会正确转义，替换值不会被再次当模板执行。例如投递失败文案：`「{{job_name}}」失败：{{error}}，HTTP {{http_status}}`。

通知本身默认以 HTTP 2xx 为成功。如果通知平台使用 HTTP 200 加业务状态码，可以配置成功响应字段，例如 `code` 和预期 JSON 值 `0`，或 `data.ok` 和预期值 `true`。预期值区分布尔、数字、字符串类型。此设置仅作用于通知 POST，不改变 ZIP 投递的严格回执协议。

每条通知固定请求体、目标、事件 ID 作为 `Idempotency-Key`，失败按 30 秒起的指数退避，最多尝试 8 次；最近 30 条状态和错误可在页面查看。重试可能导致对端收到重复请求，对端可按此键去重。配置修改仅作用于新通知，已入队通知保留快照；关闭总开关或某种事件会取消对应待发通知，正在网络传输的请求可能已到达。通知失败不会推进或阻塞消息投递游标，也不会递归产生通知失败事件。

### 消息排序与首次范围

消息页及左侧群列表按微信消息时间倒序，最新发言在最上方；相同时间以入库序号倒序稳定排序。翻页沿用这一顺序，补采的旧消息不会跑到顶部。投递任务仍使用独立的入库序号游标，不受显示顺序影响。

「首次范围」只决定**新建任务的起点**。「包含已有消息」包含所选群在接收端已保存的历史；「仅创建后收到的消息」跳过创建任务前已入库的记录。之后每次都只发送该任务尚未成功发送的消息。判断依据是接收端入库先后，所以晚到的旧微信消息仍可能属于“新收到”；暂停后再启用也会保留创建时起点。它不会让微信重新拉取历史，也不修改已有任务的游标。

自定义表情目前仍显示 `[表情]`。固定 WeBox 版本只给出 `emotion` 类型，未提供表情文件、下载地址或可复用的媒体处理接口；本轮按稳定性优先暂不实现。文本内 Unicode emoji 原样保留。

## 启动

需要 Linux、Docker Engine、Docker Compose、Docker Buildx、Git、curl、dpkg-deb。已在 amd64 无头 Linux 上开展验证，arm64 尚未验证。首次本地构建会下载微信安装包（amd64 约 212 MB）、基础镜像和依赖；已有符合固定哈希的 4.1.1.8 安装包可先放入 `vendor/webox/docker/wechat/`，文件名使用上游的 `WeChatLinux_x86_64.deb` / `WeChatLinux_arm64.deb`。

```bash
cp .env.example .env
bash scripts/prepare-webox.sh
docker compose up -d --build
docker compose ps
```

默认使用固定 WeBox 源码 `8ca042cd79221d05f6a74cb48064c7ba14220dc9` 本地构建。上游镜像仓库在本次环境返回匿名 401、认证后 403，因此默认流程不依赖它。

构建脚本在临时源码副本应用 `patches/webox/`，生成 `weward-webox:8ca042c-wx4.1.1.8-compat2`；原始 `vendor/webox` 保持不变。兼容补丁处理图片密钥推导与解码错误，并支持按群名搜索、按群 ID 监听。构建时运行相关上游和新增 Go 测试。

微信固定为 4.1.1.8，使用 [trouter-ai 微信安装包归档](https://github.com/trouter-ai/wechat-linux-versions/releases/tag/v4.1.1.8) 保存的官方客户端包，下载后核对固定 SHA256（amd64：`c9765e87ee5133bf4bb50d585c1814fafd995e3fb0da62c5ed07259b43dada7b`；arm64：`c3ed1a481247e6a1b166e87a66cccdee898c3ae0b76613b39bb6e9795e50929f`）。这是第三方历史归档，哈希对应 GitHub Release 资产，不代表腾讯发布签名。当前官方滚动下载包 4.1.13.26 在本环境一直停留于二维码加载，原版 WeBox 也可复现；[上游问题 #4](https://github.com/netcat-ai/webox/issues/4) 有相同报告及使用 4.1.1.8 的反馈，因此不随滚动下载地址自动升级。已有安装包哈希不符时脚本停止，请先移走该包再重新准备。

需要代理时仅为构建命令设置 `HTTP_PROXY` / `HTTPS_PROXY`；脚本把它们传入 host 网络的构建步骤。Docker daemon 的镜像拉取代理另行使用本机配置。应用的 WeBox 请求与转发请求均直接访问目标地址，不使用构建代理。

默认只监听本机。需要从其他设备访问，在 `.env` 把 `BIND_HOST` 设为这台机器的 Tailscale 地址或可信内网地址，再执行 `docker compose up -d`。测试界面及桌面没有额外登录门禁。

| 入口 | 默认地址 | 用途 |
| --- | --- | --- |
| 采集转发端 | `http://127.0.0.1:18080` | 状态、目标群备注、POST 地址、重试记录 |
| 微信桌面 | `http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale` | 扫码登录、确认已保存账号 |
| 消息中转器 | `http://127.0.0.1:18081` | 分群消息、投递任务与历史、群监听配置 |

中转器顶部的「微信扫码 ↗」按需在新标签页打开微信桌面。打开 WebUI、切换页面或自动刷新消息都不会加载 noVNC 或建立桌面连接。扫码或确认登录完成后关闭桌面标签页即可断开浏览器连接，微信和采集继续运行；重新打开 WebUI 不会自动恢复桌面连接。

默认入口使用浏览器当前访问的主机名和 `.env` 中的 `DESKTOP_PORT`（默认 6080），以 HTTP 打开现有 noVNC 服务，并关闭断线自动重连。桌面位于不同主机或使用 HTTPS 反向代理时，可在 `.env` 设置 `DESKTOP_PUBLIC_URL` 为浏览器可访问的完整 noVNC 地址，含所需路径和参数，例如 `https://desktop.example.com/vnc.html?autoconnect=1&resize=scale&reconnect=0`；完整地址优先，原样保留参数。修改后仅需执行 `docker compose up -d --build --no-deps receiver`。入口不依赖微信登录状态或采集控制 API 可用性。

WeBox 原生 API 仅绑定采集容器内 `127.0.0.1:8080`，不向宿主机发布。采集程序只调用身份、联系人、群搜索和 getupdates 接口，没有发送消息调用。中转器通过 collector 的控制 API 管理监听群，不直接调用 WeBox。

## 扫码后验证

1. 点击中转器顶部「微信扫码 ↗」，在新标签页的微信桌面中扫码，并在手机确认登录。完成后关闭桌面标签页。登录失效时也从这里重新确认。
2. 打开中转器「配置」，输入群名搜索并点击「监视」。同名群通过群 ID 区分，不需要修改微信备注。旧环境中已按 `webox.weward` 备注监听的群会保留。
3. 等待采集页显示「正在监听」及正确的账号、群。初次会读取最近 5 分钟；通过 `.env` 的 `INITIAL_LOOKBACK_SECONDS` 可调整新账号/群的初始回溯范围。已有账号/群始终从持久游标续采。
4. 让其他群成员和登录账号本人分别发一条不带 @ 的文本、一张图片、一个普通分享链接。手机发出的本人消息也需要微信客户端同步落库，实际支持情况以真机验证为准。
5. 接收页应出现这 6 条消息；核对发言者 ID、本人标识、群、微信消息时间、接收时间、内容和图片。普通文本中的 URL 也会显示为链接。
6. 可执行 `docker compose stop receiver`，再向群发消息，观察采集端待发送数和失败记录；随后 `docker compose start receiver`，等待重试或点击「立即重试」。接收页应补齐记录且不重复。

采集容器重启会重新启动微信。本次实测会保留账号和聊天数据，但仍需在桌面点击「登录」、在手机确认；不能把数据持久化视为免确认自动登录。

上游接口提供稳定的发言者 ID，但没有其他成员昵称查询。本人使用可用昵称，其他人显示 ID，不把猜测的昵称作为身份。群名使用 WeBox 联系人记录的 nickname，缺失时显示备注。图片未下载、无法解密、路径无效或超过当前单张 32 MiB 上限时，会转发明确的不可用原因并在页面显示。

## 配置与持久化

`.env` 中的群备注和 POST URL 只用于首次初始化。之后以采集页保存的配置为准；配置位于 `data/collector/collector.sqlite3`。默认目标 `http://receiver:8080/api/messages` 是 Docker 网络地址；改为外部地址时填写采集容器能访问的完整 HTTP(S) URL。

群监听列表在中转器「配置」管理，支持搜索、添加、暂停、恢复和移除，按登录账号分别持久保存。第一次手动修改前沿用备注发现模式；首次修改自动导入原来匹配的群，再切换为显式列表，此后修改微信备注不会改变监听范围。暂停或移除不删除历史与游标，恢复后从原进度续采；全部暂停时采集处于空闲状态。

配置保存后最迟在当前长轮询结束时生效，约 45 秒。已入队消息保留原目标地址，新配置只作用于之后入队的消息。每个账号/群独立保存游标，新增群不改变已有群的进度，移除后重新添加会续采。相同游标的群共用一次上游轮询，不同游标并行追赶。首次监听更早的历史需在初始化前设置回溯时间。

| 目录 | 内容 |
| --- | --- |
| `data/webox/` | 微信登录状态、本地数据库、WeBox token、解码媒体 |
| `data/collector/` | 账号/群游标、监听群列表、目标设置、转发队列、图片快照 |
| `data/receiver/` | HTTP 实际收到的消息、图片 BLOB、投递任务、固定 ZIP 包及回执 |

`docker compose down` 不删除这些 bind mount 数据。要备份完整测试环境，先 `docker compose stop`，复制 `data/` 和 `.env`，再 `docker compose start`。不要仅复制正在写入的 SQLite 主文件而遗漏 WAL。测试记录不会自动清理，长期运行需关注磁盘使用。

消息入队与游标推进在同一个 SQLite 事务内完成。重复上游批次按账号、群和消息 ID 去重。转发按至少一次语义工作：成功 2xx 后才标为送达，其他结果指数退避重试，最大约 5 分钟；重定向视为失败，防止 POST 被改成 GET 后误记成功。接收端按 `event_id` 去重并保留首次接收时间。展示按微信时间倒序，同一时间按入库序号倒序；投递仍以入库序号判断范围。

## 使用中转器

中转器 WebUI 分为「消息」「投递任务」「配置」三个选项卡。「消息」左侧选择群聊，右侧用紧凑行展示该群记录，支持关键词、类型、收起状态筛选及前后翻页。长消息可展开；原始数据、备注和收起操作放在消息详情中。收起只影响页面展示，仍会进入投递包。

在「投递任务」新建任务，填写外部 HTTP(S) 接收地址、五字段 cron、时区和群范围。群范围按账号与群 ID 精确匹配，空范围代表所有群，包括以后接入的群。首次发送可选包含历史消息或仅发送保存任务后收到的记录。任务默认暂停，启用后按计划投递，也可点击「立即发送」。

每个任务独立保存发送进度。每轮固定入库截止序号，将上次确认之后的记录整理为 ZIP，含 `messages.json` 和 `images/`。JSON 提供文字、URL 字符串数组、图片相对路径、原消息和群来源。超过 500 条或约 32 MiB 图片时会分包，逐包取得回执；单张超限图片独占一包。迟到消息按入库序号进入下一轮，不依赖微信时间判定是否已发送。

外部系统须接收 `application/zip` 请求体，按请求头 `Idempotency-Key` 去重，在可靠保存文件后返回 HTTP 2xx 和以下 JSON：

```json
{"ok": true, "batch_id": "X-Weward-Batch-Id 请求头的值", "receipt_id": "外部系统回执编号"}
```

中转器在同一事务内保存有效回执、标记已发送并推进该任务游标。超时、HTTP 错误、空回执、错误批次 ID 均保持未发送，自动退避重试；文件包字节及批次 ID 不变。「投递任务 → 投递历史」集中显示成功、失败和重试，打开详情可查看回执或错误并下载实际文件包。暂停会停止新请求，正在进行的请求仍可完成。

目前提供同步 HTTP 回执协议；尚未配置外部任务时不会向任何外部地址发送。具体 JSON 示例、接口和调度边界见 [中转器协议与实现约定](docs/relay-protocol.md)。

升级时先用 SQLite 在线备份或停止相应服务后完整保存数据库。仅更新中转器可执行 `docker compose up -d --build --no-deps receiver`，保留原 `event_id`、入库序号、首次接收时间和图片。启用群名监听需要先执行准备脚本构建 compat2，并更新 `.env` 中已有的 `WEBOX_IMAGE` 覆盖值，再构建并重建两个服务；collector 重建后需在微信桌面和手机确认登录。需要回退时暂停投递并等待请求结束，使用原镜像和备份数据库；已被外部接收的包无法撤回，按 `batch_id` 核对回执。

## HTTP 入站协议

接收接口：`POST /api/messages`，`Content-Type: application/json`，请求头 `Idempotency-Key` 等于 `event_id`，`X-Weward-Attempt` 为本次投递序号。外部接收端应在可靠保存后返回 2xx。

```json
{
  "schema_version": 1,
  "event_id": "64位SHA256，基于账号、群、消息ID生成",
  "source": "webox",
  "account_id": "登录账号稳定ID",
  "message_id": "微信消息ID",
  "room_id": "群ID@chatroom",
  "room_name": "测试群",
  "room_remark": "webox.weward",
  "sender_id": "实际发言者ID",
  "sender_name": "可用昵称或ID",
  "is_self": false,
  "message_time_ms": 1793923200000,
  "collected_at": "2026-11-06T00:00:00.000+00:00",
  "type": "text",
  "raw": {"msgtype": "text", "text": {"content": "原始WeBox消息保留于此"}}
}
```

图片额外带 `image: {status: "available", content_type, sha256, size, base64}`；不可用时为 `image: {status: "unavailable", error}`。接收端校验哈希/大小并事务保存图片。GET 返回的图片元数据包含接收端 `/media/<event_id>` 地址，不返回 Base64。

- `GET /api/messages?limit=50&before=<sequence>`：按微信时间倒序分页，before 是上一页最后一条的入库序号，用于定位时间及序号复合游标，limit 最大 100。
- `GET /api/notifications`：通知配置、事件名称、占位符及最近 30 条发送记录。
- `POST /api/notifications`：保存通知总开关、地址、请求头、JSON 模板、逐事件文案及可选响应条件。
- `GET /media/<event_id>`：从接收端数据库读取实际收到的图片。
- 两端 `GET /healthz`：进程存活；采集端额外有 `ready`。Docker 的 healthy 只代表进程可用，不代表已登录。
- 采集端 `GET /api/status`：账号、群列表 `rooms`、各群游标 `cursors`、队列计数及最近投递错误。每条转发的 `room_name`、`room_id` 和 `room_remark` 标明来源群，中转器按账号/群分别展示。

## 验证及边界

```bash
python3 -m unittest discover -s tests -v
python3 scripts/docker_smoke.py
docker compose logs --tail=100 collector receiver
```

集成测试启动真实 HTTP 服务器模拟 WeBox 协议，验证全员/本人文本图片链接、群过滤、媒体字节传输、失败重试、重启持久化、响应丢失去重、批次失败不推进游标、异常图片、配置持久化、分页和重定向失败。模拟协议测试不等于真实微信验证。

`docker_smoke.py` 在两个应用镜像已构建后运行：创建独立临时网络、容器和数据目录，以模拟 WeBox 驱动实际采集进程，验证跨容器 POST、接收端暂不可达后的重试、两端重启和图片字节；结束后清理其临时容器，不使用实际 `data/`。

2026-11-06 已在实际登录后验证目标群的两位其他成员文字、图片及独立接收页展示，包括一位企业微信成员；普通网页分享也已有可点击的真实样本。图片的 HTTP 字节哈希与接收记录一致，接收端重启后记录和图片保留。本人消息仍待真实样本，不能以模拟测试代替。后续重新评估自定义表情（emotion）后，因上游缺少可复用的文件接口，继续保留其元数据而不提供表情预览。

用户已安排进入试运行，重点观察微信掉线、消息遗漏或重复、消息与接收时间差、队列积压、转发错误及数据增长。用采集页状态和两端日志检查；Docker healthy 只表示进程健康，还须核对采集页 ready 与 last_poll_at。首次补采和人工回溯的旧消息不计入实时延迟统计。发现异常时记录群消息的时间、类型和发送账号，并保留日志，方便与接收记录对照。

上游依赖 Linux 微信数据库格式，仅读取客户端已落库消息；未同步的消息无法由本项目补造。没有 JPEG 缩略图样本时，兼容补丁会恢复当前账号的图片密钥；首次可能产生短时 CPU 开销，本次约 25 秒，随后在账号目录以权限 0600 缓存 `.weward-image-key.json`，读取缓存时验证其与图片匹配。缓存属于登录数据，随 `data/webox` 保存。采集器兼容空闲时 `messages:null` 的上游响应。

图片仍可能因未下载或格式不支持而不可用；兼容补丁保留约 1 分钟等待窗口，超过窗口会转发图片错误原因并继续后续消息。密钥首次恢复的计算时间可能额外延长该等待。视频号分享保留 `sphfeed` 数据，不能保证存在普通网页 URL；文件、语音、视频保留原消息元数据，暂不提供这些额外媒体的转发预览。

源码依据：上游 `README.md`、`docs/architecture.md`、`internal/wechatapi/server.go`、`internal/wechatdb/wechatdb.go`、`wecom/types.go`（均为上述固定 commit），可在 `vendor/webox/` 本地查看。上游独立保留其 MIT 及第三方许可。
