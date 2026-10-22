# 第 6 步：Web 工作台与后端 API

## 打开工作台

前后端由同一个 FastAPI 进程提供，不需要 Node.js 构建或额外前端服务器。在项目根目录安装当前依赖后运行：

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 `http://127.0.0.1:8000`。应用自动读取项目根目录 `.env`；进程环境变量优先。飞书启用时仍由应用生命周期启动独立长连接子进程。

## 首个管理员

按已确认的方式，在 `.env` 填写：

```dotenv
INITIAL_ADMIN_USERNAME=填写初始管理员用户名
INITIAL_ADMIN_PASSWORD=填写至少8位的初始密码
```

启动时只在该用户名不存在时创建管理员。已有管理员不被覆盖，不因修改 `.env` 而重置密码；已有同名普通用户不会自动提升权限，而会阻止初始化，请更换初始管理员用户名。两项都留空时不创建管理员；只填写其中一项时拒绝启动。

普通用户通过注册页自行注册，不能提交或设置管理员角色。管理员可在后台停用、启用用户、调整角色、重置密码、修改显示名称和解绑飞书。系统至少保留一名启用的管理员。账户不做硬删除，保留其历史发票和报销记录。

## 页面

| 地址 | 功能 |
| --- | --- |
| `/login` | 注册与登录 |
| `/invoices` | 发票统计、分页、按抬头/金额/日期/状态筛选、修正字段、手动调整报销状态、查看原件和手动上传 |
| `/titles` | 抬头新增、编辑、删除，可用发票数量和金额 |
| `/reimbursements` | 选择抬头和目标金额，推荐组合，勾选导出、选择自动报销 |
| `/prints` | 历史打印任务、重新下载 |
| `/settings` | 飞书绑定说明、解绑、密码修改及退出登录 |
| `/admin` | 管理用户、查看全部发票和原件、查看 Web/飞书日志 |

Web 上传仅接受有效、未加密且不超过 10 MiB 的 PDF，并调用阿里云 OCR 识别第一页（可能计费）。识别出的购买方抬头按当前用户匹配，不存在则自动新增并归类；已有抬头直接复用。全局重复发票拒绝入库。识别失败保留原件为 pending，可直接补录字段。激活或标为已报销前，必须补全号码、日期、买卖方、类型与金额（无代码发票可留空代码）。支持数电票，发票代码可为空。

修改购买方名称时，若未指定归类抬头，按新名称查找或创建当前用户的抬头。已关联发票或报销方案的抬头不能删除。排除发票使用 excluded 状态，不释放发票全局去重键。更新发票同样执行同用户和跨用户去重。

## 报销组合与打印

- 仅使用当前用户、所选抬头下 active 的发票。
- 金额转换为整数分，通过稀疏动态规划精确求解，而非仅使用贪心。
- 不允许超额时只比较不超过目标的非空组合；允许超额时，不设业务超额上限，优先在达到目标的组合中寻找最近解。
- 默认选择第一个抬头并允许超额。优先完全等于目标，其次选择达到目标且超额最少的组合。同金额优先张数较少，最后按 ID 稳定排序。无法满足时列出不足额清单并提示原因和差额。
- 没有符合条件的组合时返回空结果，不保存空方案。
- 为控制单次请求资源，最多处理 200,000 个金额状态、10,000,000 次状态扩展。超出时明确提示缩小可用发票范围，不返回冒充最优解的近似结果。
- 推荐生成后可取消勾选部分发票。导出时重新校验用户、抬头和可用状态，并创建最终选择的独立方案快照，保留历史。
- 每个 A4 页面上下放两张发票，保持比例，只渲染各原 PDF 的第一页；奇数张时最后一页只放上半张。
- 渲染使用 PyMuPDF，合成使用 ReportLab。通常以 200 DPI 渲染，对异常大页面限制最长边 4096 像素以控制内存。
- 默认勾选自动报销。PDF 完整生成并验证后，在短事务中同时保存任务、方案快照和报销状态。渲染失败或期间发票被修改时，拒绝提交，不会误标报销。
- PDF 生成过程不持有数据库写锁。提交前重新比对所选发票快照，发现冲突要求刷新重试。
- 单次最多选择 200 张发票。历史打印任务可直接重新下载，不重复改变当前发票状态。
- “导出成功”指服务端生成成功并保存打印任务；无法通过普通下载接口判断用户是否最终保存到本机。

## API

所有 `/api` 查询均需登录（会话探测、注册、登录除外），写操作需 `X-CSRF-Token`。浏览器先请求 `/api/auth/session` 获取 CSRF token，并保存返回的签名会话 cookie。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/auth/session` | 当前会话和 CSRF token |
| `POST /api/auth/register` | 注册，固定为普通用户 |
| `POST /api/auth/login` | 登录 |
| `POST /api/auth/logout` | 清除当前浏览器会话 |
| `GET /api/users/me` | 当前用户 |
| `PUT /api/users/me/password` | 验证旧密码并修改密码 |
| `POST /api/auth/feishu/bind` | 返回飞书用户名绑定说明；按已确认的对话绑定规则，不接受客户端自行声明 open_id |
| `DELETE /api/auth/feishu/bind` | 当前用户解绑飞书 |
| `GET /api/invoices` | 分页及筛选，参数 title_id/status/amount_min/amount_max/date_from/date_to/page/page_size |
| `GET /api/invoices/summary` | 当前用户按状态汇总 |
| `POST /api/invoices/upload` | multipart 手动上传，文件字段为 file |
| `GET /api/invoices/{id}` | 发票详情 |
| `PUT /api/invoices/{id}` | 修正字段及状态 |
| `DELETE /api/invoices/{id}` | 删除发票记录，保留原始文件及已导出的 PDF |
| `GET /api/invoices/{id}/file` | 下载当前用户的原件 |
| `GET/POST /api/titles` | 查询和创建抬头 |
| `PUT/DELETE /api/titles/{id}` | 更新和删除抬头 |
| `POST /api/reimbursements/optimize` | 输入 title_id/target_amount/allow_exceed，返回方案 ID、发票、合计与差额 |
| `GET /api/reimbursements` | 当前用户最近的方案记录 |
| `POST /api/print/generate` | 输入 reimbursement_plan_id/invoice_ids/auto_reimburse，生成打印任务 |
| `GET /api/print` | 打印历史分页 |
| `GET /api/print/{id}/download` | 下载当前用户的打印文件 |
| `GET /api/admin/users` | 管理员查看用户列表 |
| `PUT /api/admin/users/{id}` | 管理员修改 name/role/is_active/new_password/unbind_feishu |
| `GET /api/admin/invoices` | 管理员查看全部发票，可按 user_id 筛选 |
| `GET /api/admin/invoices/{id}/file` | 管理员查看原件 |
| `GET /api/admin/logs` | 固定 web/feishu 两种日志源，最多 1000 行 |

## 会话与数据隔离

密码使用带随机盐的 PBKDF2-SHA256（600,000 次）存储。签名会话最长 12 小时，cookie 为 HttpOnly、SameSite=Lax；CSRF token 由签名会话校验。每次请求重新检查账号是否启用及当前角色；密码变更后旧会话失效。登录/注册有按来源 IP 的进程内频率限制。

`COOKIE_SECURE=false` 适用于本机 HTTP 开发；实际通过 HTTPS 访问时应配置为 true。签名密钥自动生成在数据库同目录 `session.key`，权限 0600；应纳入受保护的备份。丢失或更换密钥会使既有会话失效。会话不新增第六张数据库表。

普通用户的发票、抬头、报销方案和打印任务在所有读取、更新及下载中均按 user_id 检查，跨用户访问返回 404。只有明确的管理员路由可跨用户查看。文件路径不从 API 返回，也不接受客户端修改；下载时再次验证文件位于对应用户存储目录内。

系统日志保存在 `logs/web.log` 和 `logs/feishu.log`，分文件轮转。应用日志过滤已配置的密钥及 Bearer token，管理员日志接口只允许固定文件名。Web 页面将动态文本按纯文本或 HTML 转义处理，并配置 CSP、禁止嵌入及 no-store 等响应头。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

API 测试使用独立临时数据库，覆盖权限、CSRF、密码哈希、会话失效、跨用户读写与下载、抬头统计、筛选、上传补录、全局去重、解绑、管理员权限、并行请求、打印失败事务和导出时状态变化。算法另以穷举结果交叉检查。

浏览器验收使用临时测试账号和虚构发票，已走通登录、抬头创建、推荐 60+40 元组合、PDF 导出、自动报销、打印历史及列表手动修改报销状态；测试数据未写入业务数据库。另逐页检查了三张发票生成两页 A4 的布局。

原件默认在页面内预览，支持完整 PDF 翻页；列表和预览窗口均提供独立的“原件下载”按钮。`GET /api/invoices/{id}/preview?page=1` 返回 PNG，`X-PDF-Page-Count` 为总页数；管理员对应 `/api/admin/invoices/{id}/preview`。预览遵循账号和文件访问权限，`/file` 接口仍下载完整原始 PDF。
