"""Typed database records; SQL is the source of truth for persistence constraints.

Amounts retain the document's SQLite REAL representation. Services must convert
amounts to integer cents before financial calculations. Timestamps are UTC text.
"""

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str
    feishu_open_id: Optional[str]
    name: str
    role: Literal['user', 'admin']
    is_active: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class InvoiceTitle:
    id: int
    user_id: int
    title_name: str
    tax_number: Optional[str]
    created_at: str


@dataclass(frozen=True)
class Invoice:
    id: int
    user_id: int
    title_id: Optional[int]
    invoice_code: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str]
    buyer_name: Optional[str]
    seller_name: Optional[str]
    invoice_type: Optional[str]
    amount: Optional[float]
    currency: str
    status: Literal['pending', 'active', 'reimbursed', 'excluded']
    file_path: str
    feishu_message_id: Optional[str]
    ocr_error: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReimbursementPlan:
    id: int
    user_id: int
    title_id: int
    target_amount: float
    selected_invoice_ids: str  # JSON array, as stored in SQLite
    total_amount: float
    allow_exceed: int
    created_at: str


@dataclass(frozen=True)
class PrintJob:
    id: int
    user_id: int
    reimbursement_plan_id: int
    pdf_path: str
    auto_reimburse: int
    created_at: str
