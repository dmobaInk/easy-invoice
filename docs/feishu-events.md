# 第 3 步：飞书长连接事件接收

## 当前实现范围

- 使用官方 `lark-oapi` SDK 注册 `im.message.receive_v1`，建立长连接，断线重连由 SDK 负责。
- FastAPI 启动时初始化数据库，并在启用飞书时启动独立子进程；关闭应用时回收子进程。独立进程避免 SDK 阻塞式事件循环与 Uvicorn 冲突。
- 仅处理人类用户的单聊文本、文件消息；群聊、机器人消息及其他消息类型忽略。
- 文本消息去掉首尾空格后，按完整用户名精确匹配，区分大小写。不做包含匹配或自动创建账号。
- 支持绑定成功、账号不存在、账号已关联其他飞书、当前飞书已关联其他账号、重复绑定、账号停用等结果。
- 使用 SQLite 写事务串行执行绑定检查与更新，防止并发绑定覆盖。
- 文件事件解析 message_id、open_id、file_key、file_name；拒绝非 PDF 文件；未绑定或已停用用户不能进入文件处理。
- PDF 文件下载、内容校验、OCR 和入库已在第 4 步接入，详见 `invoice-ingestion.md`。仅识别和打印每个 PDF 的第一页，原文件完整保留。
- 第 5 步已接入实际飞书回复接口，处理结果会回复原消息；发送失败时保留业务结果供重启恢复，详见 `feishu-replies.md`。

## 本地运行

在项目根目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

export FEISHU_ENABLED=true
export FEISHU_APP_ID='填写应用 App ID'
export FEISHU_APP_SECRET='填写应用 App Secret'
export DATABASE_PATH='data/app.db'

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

默认 `FEISHU_ENABLED=false`，可先启动 Web 服务而不连接飞书。启用飞书但缺少凭据时，应用启动直接报错，不静默忽略。应用自动读取项目根目录 `.env`，进程环境变量优先于文件；密钥按原文读取，不做变量插值。已填写 `.env` 时无需重复 export。环境变量示例与完整部署说明在第 7 步提供。

也可独立运行接收进程（不要与上面的启用飞书模式同时运行）：

```bash
.venv/bin/python -m app.integrations.feishu.listener
```

飞书开放平台需启用企业自建应用机器人、选择长连接、订阅 `im.message.receive_v1` 并开通开发文档列出的权限。应用上线后再做真实消息联调。

第 6 步尚未实现注册页面；本阶段没有可用的正式用户注册入口，也不创建默认账号或管理员密码。

## 接收确认、恢复及健康检查

- 生产接收器先将事件原子写入数据库同目录的 `feishu-events/` 文件夹，再交给有容量上限的线程池，避免在 SDK 回调中等待业务处理。临时文件权限为 0600；目录包含用户事件，不应公开或提交仓库。
- 写盘失败、队列满或接收器已关闭时抛出错误，由 SDK 返回失败响应；不会确认成功后直接丢弃事件。
- 同进程内按 message_id 去重：忽略处理中事件，完成记录最多保存 4096 条、有效期一小时。持久化文件名使用 message_id 的 SHA-256，避免路径注入。
- 业务结果先保存为 `.result` 收据，回复成功后删除事件文件，保留成功收据供跨重启去重；处理失败时保留文件。启动时顺序回放未完成事件，已有业务结果时只重试回复。损坏文件保留并记录错误，供管理员检查。
- 这是至少一次处理：重启和异常可能导致重复执行，业务操作必须保持幂等。绑定本身幂等；第 4 步利用数据库的飞书消息 ID 和发票唯一约束保证不重复入库。
- 关闭时可能中断正在处理的事件，事件文件和回复收据会保留供重启恢复。文件处理事务、重试及回复失败恢复已在第 4、5 步实现。
- `/health` 返回 disabled、process_running 或 process_stopped；子进程退出时 HTTP 状态为 503。process_running 仅表示进程存活，不证明飞书已经连接成功。
- 只运行一个带长连接的应用实例、一个 Uvicorn worker；多实例共享事件目录不在当前实现范围内。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖事件过滤、错误消息、用户名精确匹配、绑定冲突、并发绑定、文件分流、重复事件、背压、持久化恢复、SDK 真实模型解析以及 FastAPI 生命周期。SDK 测试不访问飞书服务器；真实长连接握手、权限与外部用户可达性需配置凭据后验收。

官方参考：[长连接接收事件](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)；[接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)。
