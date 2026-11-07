# Weward

- 这是基于 WeBox 的微信群消息采集与中转项目，部署目标为可信内网。
- 两个 Compose 服务：collector（固定版本 WeBox、桌面和持久转发队列）与 receiver（独立 HTTP 入站、数据库/WebUI、定时文件包投递）。
- 接收端只能展示实际 POST 收到的数据，不得调用 WeBox 或挂载其数据目录。
- 群监听由 collector 持久管理，receiver 仅代理控制 API；按群名查找后使用账号/群 ID 精确选择，不修改微信备注。监听变化须在入队事务内核对版本，保留既有游标。
- 采集端不得自动发送微信消息；必须保留本人消息，不以 @ 作为采集条件。
- 自定义表情（emotion）按稳定性优先评估；当前固定 WeBox 版本没有可复用的表情文件接口，本轮暂不实现，保留类型占位。
- 持久化游标必须与入队在同一事务内提交。HTTP 转发至少一次，接收端按 event_id 幂等。
- 中转器每个投递任务独立保存入库序号游标；固定 ZIP 和批次 ID 用于重试，仅在验证外部回执后原子标记已发送并推进游标。协议见 docs/relay-protocol.md。
- 同一监听备注可对应多个群；每个账号/群独立保存游标，每条转发保留对应的 room_id、room_name 和 room_remark。
- Python 仅使用标准库，验证入口为 `python3 -m unittest discover -s tests -v`。
- 消息展示按 message_time_ms、sequence 倒序分页；投递游标仍以入库 sequence 推进。
- POST 事件通知在 receiver 独立持久排队，与投递结果在同一事务保存；通知网络失败不能阻塞投递或推进游标。
- NFS 优先用于微信媒体和 WeBox shared；Weward SQLite 与微信数据库留本地。存储迁移需停采并保留文件所有权，不自动迁移现有数据。
- WeBox 源码固定版本和准备步骤见 README。vendor、data、.env、.local、AGENTS.local.md 均为本机文件，不纳入源码交付。
- WeBox 兼容修改维护在 patches/webox，仅在临时构建目录应用，保持 vendor/webox 原始检出不变。图片密钥缓存属于登录数据，不提交或写入日志。
- 本机端口、镜像构建和现场验证记录放 AGENTS.local.md；需人阅读的阶段性结论按 obsidian-handoff 归档。
