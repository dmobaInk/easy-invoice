# SQLite 数据模型（第 2 步）

初始化：`python3 -m scripts.init_db --database data/app.db`

验证：`python3 -m unittest discover -s tests -v`

SQL 位于 `sql/001_init.sql`；使用 Python 标准库 sqlite3，无第三方依赖。
SQLite 需要支持 JSON 函数。连接统一使用 `app.db.connection.connect`，开启外键约束和忙等待；初始化启用 WAL。脚本支持重复执行，不删除已有数据。未来结构变更必须使用新的迁移脚本，不能靠重复执行本脚本迁移旧表。

第 6 步为 FastAPI 的请求依赖与处理线程切换启用 check_same_thread=False；每个请求独占一条连接，不在多个请求之间共享连接。

## 已确认的业务规则

- 完成全部 Web 页面及管理员功能；普通用户自行注册。
- users 增加 username、password_hash、role、is_active。只保存密码哈希；普通注册的角色必须由服务端固定为 user，不接受客户端指定管理员。
- 飞书绑定以用户消息中的用户名匹配已有账号，替代原文档首次收到消息自动创建用户的规则；不存在或已绑定时回复提示。Web 提供解绑入口。数据库以 NULL 表示未绑定，并保证 open_id 唯一。具体消息解析和绑定处理属于后续步骤。
- invoices 增加 title_id、invoice_type、ocr_error。通过复合外键保证抬头属于同一用户。
- 发票代码和号码全局唯一，同用户和跨用户重复均禁止入库；后续服务分别回复对应提示。报销或排除不释放去重键。
- 支持数电票；无发票代码时仅按发票号码全局去重。OCR 失败可保存 pending 记录及原始文件路径，缺失的代码、号码和金额使用 NULL；active 和 reimbursed 必须具备号码和金额，代码允许为空。
- reimbursement_plans 增加 allow_exceed；允许超过时不设上限，目标是尽可能接近目标金额。
- print_jobs 增加 auto_reimburse，默认开启。后续打印服务在成功生成 PDF 后，以事务记录任务并根据选项更新所选发票状态；生成失败不能标为已报销。发票列表支持手动修改报销状态。
- 复合外键保护打印任务与报销方案的用户归属；触发器验证方案 JSON 中每个 ID 都是当前用户的真实发票。

## 数据约定与后续实现边界

- 保留文档的 REAL 金额字段。计算组合、比较和合计时必须先转整数分，避免二进制浮点误差。
- 时间默认采用 UTC ISO 8601 文本。更新记录时由后续数据访问层显式维护 updated_at。
- 模型是数据库记录，不能直接作为 API 响应返回，尤其禁止暴露 password_hash。
- 删除有关联的数据采用 RESTRICT；发票作废通过 excluded 状态表达。方案 JSON 为历史选择记录，未来服务不得随意变更发票所属用户或硬删除被引用发票。
- SQL 外键不能代替 API 权限控制；后续所有普通用户查询、更新和下载仍必须带当前 user_id。
- 管理员系统日志使用应用日志文件，不新增第六张业务表；可选去重日志表暂不创建。
- 多页 PDF 已确认仅识别和打印第一页，完整原文件保留。
- OCR 使用用户提供的 RecognizeInvoice API：
  https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-recognizeinvoice
  官方接口支持 PDF 二进制 body 或 URL；URL 参数不支持 Base64，后续采用官方 SDK 支持的方式接入。

数据库结构现为 v2。启动时自动将 v1 原子升级至 v2，保留所有记录和 ID，并校验外键；升级失败则回滚。无代码发票号码在所有用户及状态中唯一。有代码发票继续按代码与号码联合去重。
