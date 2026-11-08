# WeBox 兼容补丁

基线为 `8ca042cd79221d05f6a74cb48064c7ba14220dc9`。`scripts/prepare-webox.sh` 将基线导出到临时目录，独立初始化 Git 后依次应用 `compat.patch`、`groups.patch`、`files.patch`，复制本目录 Go 文件并构建 compat3；不修改 vendor 检出。

真实群图片的 V2 缩略图不是 JPEG，上游只凭 JPEG 结尾推导 XOR 字节，因而报错。`image_key.go` 在原路径失败时结合账号目录后缀、账号 ID 和密文块恢复密钥，验证解密头后使用；计算最多使用 24 个 worker。同一进程串行初始化，缓存写入账号目录的 `.weward-image-key.json`（0600），再次加载需通过密文模板校验。缓存不能写入源码、日志或报告。

`compat.patch` 同时将图片解码失败纳入上游已有的有限等待流程，过期后返回空 `sdkfileid` 和具体 `image.error`，避免后续文字被持续阻塞。采集端将错误转发给独立接收端。

`image_key_test.go` 验证没有 JPEG 样本时的恢复、错误账号拒绝、缓存权限与失效；`media_error_test.go` 验证新图片仍等待、过期图片错误可见且后续文字继续。构建同时运行上游 `internal/wechatdb` 和 `internal/wechatapi` 全部测试。

`files.patch` / `files_api.go` 为 getupdates 增加每请求独立的 `file_policy: {enabled, extensions, max_bytes}`。策略在本地文件读取及 shared 复制之前执行，按消息 XML 的 `appattach/totallen` 和实际文件大小核对；读取本身也受 max_bytes 限制。支持全部后缀、大小写无关的末尾匹配及复合后缀。不合规则返回 `file.skip_reason`，不复制文件；失败在消息时间起约一分钟后返回 `file.error`，继续后续消息，避免永久阻塞。缺少策略的旧调用保留原文件读取行为。

补丁不触发微信远端下载，不发送微信消息。已经由微信下载到本地的原文件不受策略删除或清理。`files_api_test.go` 覆盖策略从 HTTP 传入、在读取前过滤、实际字节大小、部分下载、有限等待和原消息元数据隔离；`files_db_test.go` 验证消息声明大小提取。

`groups.patch` 增加经过原 token 认证的 `GET /ilink/bot/groups?query=...`，按群名子串或精确群 ID 搜索未删除群联系人，不要求 `webox.*` 备注。getupdates 可带 `room_ids` 显式选群，通过 `RoomSessionsFor` 读取相应会话；省略或 null 保留原备注过滤，空列表表示不选任何群。只接受群 ID，最多 1000 个；不提供发消息或修改联系人能力。

`groups_db_test.go` 和 `groups_api_test.go` 验证普通群搜索、同名群、删除联系人排除、显式会话选择、兼容旧请求、空选择与无效输入。

重新创建采集容器需要在微信桌面及手机确认登录。独立接收端与既有 HTTP 消息格式无需迁移。回退 compat1 或原始基线时须同时恢复匹配的 collector 代码与配置备份；这些旧镜像不支持显式群监听。原始基线还可能重新出现图片缺陷。
# 存储启动兼容

collector 派生镜像还通过 `docker/patch-storage.py` 对固定版本 entrypoint 做一次精确替换：初次初始化不递归 chown 微信目录和 shared。修改只发生在派生镜像内，vendor 原始检出不变；若上游片段改变，构建直接失败以提示重新审查。`docker/prepare-storage.sh` 在启动前以微信进程 UID 检查映射目录可写性，兼容 NFS root_squash 拒绝 chown 但用户可写的情况。完整目录及媒体目录的挂载方式见项目 README。

