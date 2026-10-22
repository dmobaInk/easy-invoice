# 部署与运维说明

## 1. 部署边界

应用由 FastAPI Web 主进程和一个飞书长连接子进程组成，SQLite 是唯一数据库，PDF 和事件记录存放在本地磁盘。当前支持单实例、单 Uvicorn worker；不要同时启动第二个接收器，也不要在服务器部署中使用 `--reload`。

飞书事件使用出站 WebSocket，不需要公网 HTTP 回调地址、域名或证书。这仅指机器人接收模式：本机 Web 可用 HTTP，若通过公网访问包含登录信息的 Web 工作台，应在反向代理处提供 HTTPS，并设置 `COOKIE_SECURE=true`。

以下部署命令是交付说明，未自动执行；其中 `start`/`restart` 会按 `.env` 创建初始管理员并可能开始处理真实飞书消息。

## 2. 环境与依赖

- Python 3.9+；当前测试环境为 Python 3.9.6。新服务器可使用带 OpenSSL 的 Python 3.11/3.12，并在目标环境重跑测试。
- SQLite 需支持 JSON 函数，由配置检查脚本验证。
- 可写的本地磁盘；不需要 MySQL、PostgreSQL、Redis、Celery 或 OSS。
- 出站网络需要访问飞书开放平台、其返回的 WebSocket 地址和阿里云 OCR。
- Linux 守护进程模板使用 systemd；macOS 本地开发直接使用 Uvicorn 命令。

保留源码中的 `app/`、`scripts/`、`sql/`、`pyproject.toml` 等目录，以 editable 模式安装后从源码项目根目录运行。当前部署方式不是仅复制一个 wheel 到新机器。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
```

`requirements.lock.txt` 固定已验证的运行依赖版本；原生依赖由 pip 为目标平台选择对应构建。它不是跨平台二进制归档，目标平台若缺少预编译包，应先解决该包安装问题，再运行测试。

## 3. 配置项

首次复制 `.env.example` 为项目根目录 `.env`。不要覆盖已有文件；文件权限设置为 0600。`.env` 由应用读取，进程环境变量优先；引号由 python-dotenv 处理，禁用 `${...}` 插值。不要执行 `source .env`。

| 配置项 | 默认/要求 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/app.db` | 相对项目根目录或绝对路径 |
| `FEISHU_ENABLED` | `false` | true/false/1/0；为 true 时启动机器人 |
| `FEISHU_APP_ID` | 启用飞书时必填 | 企业自建应用 App ID |
| `FEISHU_APP_SECRET` | 启用飞书时必填 | 企业自建应用 App Secret |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | 启用 OCR 时填写 | 阿里云 AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 与 ID 同时填写 | 阿里云 AccessKey Secret |
| `ALIYUN_OCR_ENDPOINT` | `ocr-api.cn-hangzhou.aliyuncs.com` | 只填主机名，不加协议或路径 |
| `INITIAL_ADMIN_USERNAME` | 首次创建管理员时填写 | 不与普通账号重名 |
| `INITIAL_ADMIN_PASSWORD` | 至少 8 位 | 与用户名同时填写；只用于首次创建 |
| `COOKIE_SECURE` | `false` | 浏览器经 HTTPS 访问时设 true |

初始管理员已存在时不会重置密码；已有同名普通账号时拒绝自动提权。首次创建后可同时清空两项 INITIAL_ADMIN 配置，后续在 Web 中管理账号。只清空其中一项会导致配置检查失败。

缺少阿里云凭据时，有效 PDF 仍会保留为 pending，不会伪装为识别成功。真实密钥不写入示例、日志或仓库。

只读检查：

```bash
.venv/bin/python -m scripts.check_config
```

该命令不会初始化数据库、创建管理员、验证远端权限或发送网络请求。它只验证配置格式、凭据非空状态、依赖和 SQLite JSON 支持。

## 4. 飞书与阿里云

### 飞书

1. 在飞书开放平台创建企业自建应用，启用机器人能力。
2. 在「凭证与基础信息」取得 App ID、App Secret，填入 `.env`。
3. 按开发文档配置 `im:message`、`im:message.p2p_msg:readonly`、`im:message:send_as_bot`、`im:resource` 等对应权限；以控制台当前权限名称及审核结果为准。
4. 事件订阅选择「长连接」，添加 `im.message.receive_v1`。
5. 设置应用可用范围并发布版本，让测试用户可与机器人单聊；外部用户还需按所在组织控制台支持的范围完成外部可用设置。不要把“启用长连接”当作外部用户自动获得访问权限。
6. 将 `FEISHU_ENABLED` 改为 true，准备好实际接收后启动应用。

用户先在 Web 注册，然后在机器人单聊发送完整用户名，例如 `alice`。系统去除首尾空格后精确匹配，区分大小写；绑定完成后发送 PDF。Web「账户设置」可以解绑。群聊、机器人自身消息不会触发收录。

### 阿里云

开通 OCR 的增值税发票识别服务，为所用 AccessKey 授予 `ocr:RecognizeInvoice` 对应权限，并填写环境变量。使用的是 [RecognizeInvoice（2021-07-07）](https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-recognizeinvoice)，不是其他云市场供应商的 AppCode 接口。

服务通过官方 SDK 上传 PDF 二进制，固定 `PageNo=1`。接口调用会消耗相应额度或产生按量费用；网络/服务失败最多重试两次。支持无代码数电票，按发票号码全局去重。原件限制为 10 MiB，完整原文件保留。

## 5. 本地启动与验收

```bash
.venv/bin/python -m scripts.check_config
.venv/bin/python -m scripts.init_db --database data/app.db
.venv/bin/python -m unittest discover -s tests -v
```

`--database` 按实际 `DATABASE_PATH` 调整。初始化 SQL 可重复执行，不删除已有数据，但不是通用旧版本迁移工具。

准备启用实际服务时：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

浏览器访问 `http://127.0.0.1:8000`。第一次启动创建已配置的管理员。验证顺序：登录、普通用户注册、飞书用户名绑定、发送真实 PDF、查看收录与重复提示、Web 修正、推荐组合、生成双拼 PDF、检查自动报销与用户隔离。

```bash
curl --fail http://127.0.0.1:8000/health
```

健康检查中的 `process_running` 只表示飞书子进程存活，不证明远端连接和业务权限有效。飞书子进程退出时返回 503；SDK 负责可恢复的网络断线重连。主进程仍存活而子进程退出时，需检查日志并重启应用，当前 systemd 模板不会只因 HTTP 503 自动重启。

## 6. Linux systemd

模板：[easy-invoice.service](../deploy/easy-invoice.service)，默认安装目录 `/opt/easy-invoice`、系统用户 `easy-invoice`，Web 仅监听本机 8000。

先将完整源码放到 `/opt/easy-invoice`，不复制本地 `.venv`、`.env` 或 `data`。以下以具备 sudo 权限的安装账号操作；系统用户只读源码，仅拥有数据目录和 `.env` 的访问权限：

```bash
sudo useradd --system --home-dir /opt/easy-invoice --shell /usr/sbin/nologin easy-invoice
cd /opt/easy-invoice
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade pip setuptools wheel
sudo .venv/bin/python -m pip install -r requirements.lock.txt
sudo .venv/bin/python -m pip install --no-deps --no-build-isolation -e .
sudo install -d -m 700 -o easy-invoice -g easy-invoice data
sudo cp -n .env.example .env
sudo chown easy-invoice:easy-invoice .env
sudo chmod 600 .env
sudoedit /opt/easy-invoice/.env
sudo -u easy-invoice .venv/bin/python -m scripts.check_config
sudo -u easy-invoice .venv/bin/python -m scripts.init_db --database /opt/easy-invoice/data/app.db
sudo install -m 644 deploy/easy-invoice.service /etc/systemd/system/easy-invoice.service
sudo systemctl daemon-reload
```

若用户已存在，跳过 useradd。若自定义安装目录、服务用户或数据目录，请同时修改模板中的 User/Group/WorkingDirectory/ExecStartPre/ExecStart/ReadWritePaths；`ProtectHome=true` 的模板不适用于放在 `/home` 下的项目目录。

准备开始实际服务后才执行：

```bash
sudo systemctl enable --now easy-invoice
sudo systemctl status easy-invoice
```

停止、重启和查看控制台日志：

```bash
sudo systemctl stop easy-invoice
sudo systemctl restart easy-invoice
sudo journalctl -u easy-invoice -n 100 --no-pager
```

模板包含主进程异常退出自动重启、停机清理子进程、0077 文件掩码和数据目录写权限限制。当前开发机器是 macOS，模板未在真实 Linux systemd 主机上执行安装验收。

通过已有 HTTPS 反向代理访问时，将代理后端设为 `127.0.0.1:8000`，传递 Host、X-Forwarded-For 和 X-Forwarded-Proto，并为 multipart 上传预留大于 10 MiB 的请求体上限。只信任本机代理转发头；WebSocket 是后端到飞书的出站连接，不需要由反向代理转发到应用。反向代理证书、域名和公网开放不由本模板自动配置。

## 7. 数据布局

以数据库所在目录为基准：

```text
data/
├── app.db                  # 五张业务表
├── app.db-wal / app.db-shm  # SQLite 运行期间可能出现
├── session.key             # Web 签名密钥
├── invoices/<user_id>/     # 完整 PDF 原件
├── prints/<user_id>/       # 已生成 A4 PDF
├── feishu-events/          # 未完成事件 .json 与回复收据 .result
└── logs/                   # web.log、feishu.log 及轮转文件
```

不要只备份 `.db` 而遗漏原件、打印文件和回复收据。不要把运行中的 `.db` 单独复制后当作完整 WAL 快照；本项目备份脚本通过 SQLite backup API 生成独立数据库快照。

## 8. 完整备份与恢复

### 备份

完整快照涉及 SQLite 与磁盘文件，需要先停 Web 和所有独立飞书接收进程。`--service-stopped` 是操作者对停服状态的确认，脚本不会替你停止进程，也不能证明其他实例不存在。

```bash
sudo systemctl stop easy-invoice
cd /opt/easy-invoice
sudo .venv/bin/python -m scripts.backup --database /opt/easy-invoice/data/app.db --output /srv/easy-invoice-backups/backup-001 --service-stopped
```

每次更换一个不存在的备份目录，且不要放到运行数据目录内部。工具不覆盖已有目录，会校验 SQLite 完整性、外键、数据库引用的文件，以及文件 SHA-256 和大小。备份目录仅操作者可访问，文件权限为 0600。`manifest.json` 最后写入；缺少它或校验失败的快照不能恢复。SHA-256 用于检测文件损坏，不是备份真实性签名。

`.env` 不包含在快照中，应单独受保护或加密保存；不要将快照、SQLite、PDF 或密钥提交仓库。确认备份成功后，根据实际启用意图重新启动服务：

```bash
sudo systemctl start easy-invoice
```

本机开发先在原终端 Ctrl+C，确认接收器也结束后，使用相同 Python 模块命令和本机路径，无需 systemd。

### 恢复

恢复只写入**不存在的新目录**，不覆盖现有运行数据。恢复数据库文件名固定为 `app.db`，会重写数据库中原件、打印文件的绝对路径，支持迁移到新位置。

同一服务器恢复示例：停服后，先将当前数据目录移到一个未使用的保留目录，再恢复到原 `data` 路径。

```bash
sudo systemctl stop easy-invoice
sudo mv -n /opt/easy-invoice/data /opt/easy-invoice/data-before-restore
cd /opt/easy-invoice
sudo .venv/bin/python -m scripts.restore --snapshot /srv/easy-invoice-backups/backup-001 --output /opt/easy-invoice/data --service-stopped
sudo chown -R easy-invoice:easy-invoice /opt/easy-invoice/data
sudo -u easy-invoice .venv/bin/python -m scripts.check_config
```

保留目录已存在时请换一个名称，并确认原 `data` 已移走；恢复工具发现目标仍存在会拒绝操作。恢复失败时先排查，不要直接启动服务，以免自动初始化空数据库。

迁移到其他路径时，让 `.env` 的 `DATABASE_PATH` 指向新恢复目录下 `app.db`，并调整 systemd 的 ReadWritePaths。不要在恢复完成后直接移动数据目录，数据库里的绝对路径会再次失配；需要迁移时重新用恢复工具指定最终位置。

`.env` 需在目标机器另行配置，优先使用当前有效的 AccessKey。会话密钥一并恢复后原会话可能仍有效；需要让旧会话全部失效时，应在停服状态更换签名密钥。事件与收据也会恢复，启用飞书后的启动回放可能补发尚未完成的回复。确认配置和恢复结果后再实际启动服务。

## 9. 日志、升级与回滚

管理员可从 Web 后台查看最近的 Web/飞书日志。文件日志每个 2 MiB，保留 3 个轮转文件；主进程启动和退出还可从 journalctl 查看。应用日志对已配置密钥及 Bearer token 做脱敏，飞书 WebSocket URL 也会隐藏。

更新源码前先停服并创建完整快照，保留对应版本的源码与依赖清单。更新后安装依赖、执行离线配置检查和测试，再重启。初始化 SQL 只支持当前 v1 初始结构，不会迁移未来未知结构。

回滚应恢复配套源码、依赖和完整数据快照，不要只替换数据库或只恢复 Python 代码。当前备份工具只接受 schema v1；未来数据库升级需另外提供迁移/回滚说明。

## 10. 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 登录后立即退出或每次重启都退出 | 检查 session.key 是否稳定可读；HTTP 环境不能设置 COOKIE_SECURE=true；密码变更/停用会使会话失效 |
| 页面提示 CSRF 验证失败 | 刷新获取新会话后重试，不要用旧 token 调用写接口 |
| 管理员未创建 | 两项 INITIAL_ADMIN 都要填写；仅首次创建；同名普通账号不会自动提权 |
| 修改 `.env` 密码但登录密码未变 | 初始配置不是重置密码入口，请用账户设置或管理员重置 |
| 机器人没有回复 | 检查启用开关、子进程、机器人可用范围、发布版本、消息事件和发送权限；查看 feishu.log 与 journal |
| 绑定提示账号不存在 | 先 Web 注册，再发送完整用户名，不要附带“绑定”等额外文字 |
| 下载失败 | 确认 im:resource 等资源权限、原消息和附件可访问；程序总共尝试三次后会提示重发 |
| OCR 一直 pending | 检查服务开通、额度、RAM 权限和 endpoint；只识别第一页，失败后可在 Web 补录 |
| 数电票识别不完整 | 无代码可正常入库；其他必要字段缺失时保留待补录 |
| 文件过大/加密/损坏 | 提供不超过 10 MiB 的有效未加密 PDF，不仅修改扩展名 |
| 重复发票被拒绝 | 全局去重包含已报销、已排除记录；不同用户也不重复收录 |
| 推荐计算量过大 | 按抬头整理、排除不参与本次报销的发票后重试；不会静默返回近似结果 |
| 导出时提示发票已变更 | 生成期间状态或内容改变，刷新重新推荐；失败不会误标报销 |
| 恢复后原件/打印文件找不到 | 应用恢复工具重写绝对路径；不要直接搬动数据库，核对 DATABASE_PATH 和系统目录权限 |
| systemd 启动后进入 failed | 查看 journal；检查安装路径、服务用户、data 目录、ReadWritePaths 和离线检查结果 |
| macOS 出现 LibreSSL/urllib3 提示 | 使用带受支持 OpenSSL 的 Python 虚拟环境，安装依赖后重跑检查 |

参考：[systemd 服务配置](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[SQLite backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)、[飞书长连接](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)。
