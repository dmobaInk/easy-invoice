-- v1 升级到 v2：支持无代码发票。由 initialize 在关闭外键后于事务中执行。
DROP TRIGGER IF EXISTS validate_plan_invoices_insert;
DROP TRIGGER IF EXISTS validate_plan_invoices_update;
CREATE TEMP TABLE invoice_sequence_backup AS SELECT seq FROM sqlite_sequence WHERE name='invoices';
CREATE TEMP TABLE invoice_rows_backup AS SELECT * FROM invoices;
DROP TABLE invoices;
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


INSERT INTO invoices SELECT * FROM invoice_rows_backup;
UPDATE sqlite_sequence SET seq=max(seq,coalesce((SELECT seq FROM invoice_sequence_backup),0)) WHERE name='invoices';
DROP TABLE invoice_rows_backup;
DROP TABLE invoice_sequence_backup;
