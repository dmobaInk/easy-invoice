-- SQLite 数据库结构 v2；每个数据库连接都必须开启 foreign_keys。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 用户表：账号、角色与飞书绑定
CREATE TABLE IF NOT EXISTS users (
    -- 主键，自增编号
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 登录用户名，不能为空且全局唯一
    username TEXT NOT NULL UNIQUE CHECK (length(trim(username)) > 0),
    -- 密码哈希，不保存明文密码
    password_hash TEXT NOT NULL CHECK (length(password_hash) > 0),
    -- 绑定的飞书用户 open_id，全局唯一；NULL 表示未绑定
    feishu_open_id TEXT UNIQUE CHECK (feishu_open_id IS NULL OR length(trim(feishu_open_id)) > 0),
    -- 用户显示名称
    name TEXT NOT NULL DEFAULT '',
    -- 用户角色：user 普通用户，admin 管理员
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    -- 账号是否启用：1 启用，0 停用
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    -- 创建时间，UTC ISO 8601 格式
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- 最后更新时间，UTC ISO 8601 格式，由数据访问层维护
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 发票抬头表：保存用户常用抬头
CREATE TABLE IF NOT EXISTS invoice_titles (
    -- 主键，自增编号
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 所属用户 ID，关联 users.id
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- 发票抬头名称，同一用户下唯一
    title_name TEXT NOT NULL CHECK (length(trim(title_name)) > 0),
    -- 抬头对应的纳税人识别号，可为空
    tax_number TEXT,
    -- 创建时间，UTC ISO 8601 格式
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, title_name),
    UNIQUE (user_id, id)
);

-- 发票表：保存识别结果、文件路径及报销状态
CREATE TABLE IF NOT EXISTS invoices (
    -- 主键，自增编号
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 所属用户 ID，关联 users.id
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- 关联的发票抬头 ID，必须属于当前用户
    title_id INTEGER,
    -- 发票代码；数电发票无代码时为 NULL；待补录时也可为空
    invoice_code TEXT CHECK (invoice_code IS NULL OR length(trim(invoice_code)) > 0),
    -- 发票号码；有代码时与代码共同去重，无代码时仅凭号码去重
    invoice_number TEXT CHECK (invoice_number IS NULL OR length(trim(invoice_number)) > 0),
    -- 开票日期
    invoice_date TEXT,
    -- 购买方名称，即发票购买方抬头
    buyer_name TEXT,
    -- 销售方名称
    seller_name TEXT,
    -- 发票类型，如增值税普通发票、增值税专用发票
    invoice_type TEXT,
    -- 价税合计金额，单位为元；待补录时可为空，计算时转换为整数分
    amount REAL CHECK (amount IS NULL OR (typeof(amount) IN ('integer', 'real') AND amount >= 0)),
    -- 币种，当前仅支持 CNY（人民币）
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    -- 发票状态：pending 待确认，active 可用，reimbursed 已报销，excluded 已排除
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'reimbursed', 'excluded')),
    -- 原始发票 PDF 的本地存储路径
    file_path TEXT NOT NULL CHECK (length(trim(file_path)) > 0),
    -- 来源飞书消息 ID，唯一约束防止同一消息重复入库，可为空
    feishu_message_id TEXT UNIQUE,
    -- OCR 识别失败的错误信息，可为空
    ocr_error TEXT,
    -- 创建时间，UTC ISO 8601 格式
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- 最后更新时间，UTC ISO 8601 格式，由数据访问层维护
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (user_id, title_id) REFERENCES invoice_titles(user_id, id) ON DELETE RESTRICT,
    CHECK (status IN ('pending', 'excluded') OR
           (invoice_number IS NOT NULL AND amount IS NOT NULL)),
    UNIQUE (user_id, invoice_code, invoice_number)
);
-- 全局去重同样适用于已排除、已报销的发票。
-- 标识字段为 NULL 时，允许保留多条 OCR 失败的待补录记录。
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_global_identity
    ON invoices(invoice_code, invoice_number)
    WHERE invoice_code IS NOT NULL AND invoice_number IS NOT NULL;
-- 无代码发票按号码全局去重，涵盖所有用户和状态。
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_number_only
    ON invoices(invoice_number) WHERE invoice_code IS NULL AND invoice_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_invoices_user_status_title
    ON invoices(user_id, status, title_id);
CREATE INDEX IF NOT EXISTS ix_invoices_user_date ON invoices(user_id, invoice_date);

-- 报销方案表：保存目标金额和所选发票
CREATE TABLE IF NOT EXISTS reimbursement_plans (
    -- 主键，自增编号
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 所属用户 ID，关联 users.id
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- 关联的发票抬头 ID，必须属于当前用户
    title_id INTEGER NOT NULL,
    -- 报销目标金额，单位为元，必须大于零
    target_amount REAL NOT NULL CHECK (typeof(target_amount) IN ('integer', 'real') AND target_amount > 0),
    -- 选中的发票 ID 列表，以 JSON 整数数组保存，所有发票必须属于当前用户
    selected_invoice_ids TEXT NOT NULL CHECK (
        CASE WHEN json_valid(selected_invoice_ids) THEN json_type(selected_invoice_ids) = 'array' ELSE 0 END
    ),
    -- 所选发票的合计金额，单位为元
    total_amount REAL NOT NULL CHECK (typeof(total_amount) IN ('integer', 'real') AND total_amount >= 0),
    -- 是否允许合计超出目标金额：1 允许且不设超额上限，0 不允许
    allow_exceed INTEGER NOT NULL DEFAULT 0 CHECK (allow_exceed IN (0, 1)),
    -- 创建时间，UTC ISO 8601 格式
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (user_id, title_id) REFERENCES invoice_titles(user_id, id) ON DELETE RESTRICT,
    UNIQUE (user_id, id)
);

-- 打印任务表：保存导出 PDF 路径及自动报销选项
CREATE TABLE IF NOT EXISTS print_jobs (
    -- 主键，自增编号
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 所属用户 ID，关联 users.id
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    -- 关联的报销方案 ID，必须属于当前用户
    reimbursement_plan_id INTEGER NOT NULL,
    -- 生成的 A4 双拼 PDF 的本地存储路径
    pdf_path TEXT NOT NULL CHECK (length(trim(pdf_path)) > 0),
    -- 导出成功后是否自动标为已报销：1 是，0 否，默认开启
    auto_reimburse INTEGER NOT NULL DEFAULT 1 CHECK (auto_reimburse IN (0, 1)),
    -- 创建时间，UTC ISO 8601 格式
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (user_id, reimbursement_plan_id) REFERENCES reimbursement_plans(user_id, id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_print_jobs_user_created ON print_jobs(user_id, created_at);

-- 新建报销方案时，校验发票 ID 类型、存在性和用户归属。
CREATE TRIGGER IF NOT EXISTS validate_plan_invoices_insert
BEFORE INSERT ON reimbursement_plans
BEGIN
    SELECT RAISE(ABORT, 'invalid or cross-user selected invoice')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.selected_invoice_ids) item
        WHERE item.type != 'integer' OR NOT EXISTS (
            SELECT 1 FROM invoices i WHERE i.id = item.value AND i.user_id = NEW.user_id
        )
    );
END;
-- 修改报销方案的发票列表或所属用户时，重新校验用户归属。
CREATE TRIGGER IF NOT EXISTS validate_plan_invoices_update
BEFORE UPDATE OF selected_invoice_ids, user_id ON reimbursement_plans
BEGIN
    SELECT RAISE(ABORT, 'invalid or cross-user selected invoice')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.selected_invoice_ids) item
        WHERE item.type != 'integer' OR NOT EXISTS (
            SELECT 1 FROM invoices i WHERE i.id = item.value AND i.user_id = NEW.user_id
        )
    );
END;
PRAGMA user_version = 2;
COMMIT;
