# 发票小助手 · 飞书机器人版

Python FastAPI + SQLite 的个人/小团队发票工作台。通过飞书单聊发送 PDF，自动下载、调用阿里云 OCR、去重入库；在 Web 中整理发票、管理抬头、推荐报销组合并导出 A4 双拼 PDF。

## 本地安装

在项目根目录使用 Python 3.9+，保留完整源码目录运行；无需额外数据库或前端构建服务。

```bash
git clone https://github.com/dmobaInk/easy-invoice.git
cd easy-invoice
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
cp -n .env.example .env
chmod 600 .env
```

已有 `.env` 时保留它，不用示例覆盖真实配置。编辑 `.env`，先填写初始管理员用户名和至少 8 位密码；要启用机器人，再填写飞书、阿里云凭据并设置 `FEISHU_ENABLED=true`。

```bash
.venv/bin/python -m scripts.check_config
.venv/bin/python -m scripts.init_db --database data/app.db
```

`check_config` 只做离线检查，不创建账号、连接飞书或调用 OCR。数据库路径自定义时，初始化命令的 `--database` 应与 `.env` 一致；应用启动也会自动初始化。

准备好实际启用服务时运行：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 [本机工作台](http://127.0.0.1:8000)。首次启动按 `.env` 创建管理员；普通用户可以自行注册，在飞书单聊向机器人发送完整用户名完成绑定。Web「账户设置」提供解绑。

## 功能与约定

- 发票列表、抬头管理、报销助手、打印历史、账户设置和管理后台。
- 用户数据隔离，发票代码 + 号码全局去重，不同用户也不能重复收录。
- 每个 PDF 仅识别和打印第一页，完整原文件保留；支持数电票；无发票代码时仅按发票号码全局去重。
- OCR 最多重试两次，失败保留为 pending，允许 Web 补录。Web 上传同样调用 OCR，按当前用户自动新增或复用购买方抬头并归类。识别失败保留待补录。
- 报销组合按整数分计算，允许超额时尽可能接近目标，不设业务超额上限；资源过大时明确提示缩小范围。
- 导出默认自动标为已报销，也可取消勾选；发票列表可手动修改报销状态。
- 接收事件及回复结果落盘，支持重启恢复；当前按单实例、单 Uvicorn worker 部署。

## 文档

- [部署、配置、备份恢复及故障排查](docs/deployment.md)
- [Web 页面、API 和管理员说明](docs/web.md)
- [数据库设计](docs/database.md)
- [飞书长连接](docs/feishu-events.md)
- [下载、OCR 与入库](docs/invoice-ingestion.md)
- [机器人回复与恢复](docs/feishu-replies.md)
- [原始开发文档](docs/发票小助手-飞书机器人版开发文档.md)

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

测试使用临时数据库及虚构数据，不会启用真实机器人或消耗 OCR 额度。依赖版本清单来自本机已验证环境；在目标服务器安装后仍需运行测试与配置检查。启动真实服务、发布飞书应用及实际消息联调是独立操作，不由安装或检查脚本自动执行。

## 项目结构

```text
app/          FastAPI 接口、Web 页面、飞书和 OCR 集成
scripts/      配置检查、数据库初始化、备份与恢复
sql/          数据库结构及迁移
resource/     Web 使用的机器人二维码
deploy/       systemd 服务示例
docs/         部署和功能文档
tests/        自动化测试
```

## 配置与数据安全

仓库只提供空凭据的 `.env.example`，真实配置放在本机 `.env`。`.gitignore` 已排除 `.env` 及其变体（保留 `.env.example`）、运行数据 `data/`、数据库、会话密钥、日志、备份、虚拟环境和构建产物。不要将真实凭据写入源码、文档或提交记录。

发票原件、导出文件和用户数据均保存在本机数据库所在目录；备份与迁移请参阅部署文档，不要通过 Git 同步。自定义数据库路径时也应确保对应数据目录不被提交。

部署自己的飞书应用时，请将 `resource/invoice-qr.jpg` 替换为自己的机器人二维码；页面会使用该图片帮助用户添加机器人。
