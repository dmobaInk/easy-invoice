from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    @field_validator('username')
    @classmethod
    def username_valid(cls, value):
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError('用户名不能为空或包含空白字符')
        return value


class Registration(Login):
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(default='', max_length=100)


class PasswordChange(Input):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class TitleInput(Input):
    title_name: str = Field(min_length=1, max_length=200)
    tax_number: Optional[str] = Field(default=None, max_length=64)

    @field_validator('title_name')
    @classmethod
    def title_valid(cls, value):
        if not value.strip():
            raise ValueError('抬头不能为空')
        return value.strip()


InvoiceStatus = Literal['pending', 'active', 'reimbursed', 'excluded']


class InvoiceTag(Input):
    names: list[str]

    @field_validator('names')
    @classmethod
    def clean_names(cls, values):
        names = list(dict.fromkeys(value.strip() for value in values))
        if any(len(name) > 64 for name in names):
            raise ValueError('每个标签最多 64 个字符')
        return [name for name in names if name and name != '未分类']


class InvoiceUpdate(Input):
    title_id: Optional[int] = Field(default=None, gt=0)
    invoice_code: Optional[str] = Field(default=None, max_length=32, pattern=r'^[0-9]+$')
    invoice_number: Optional[str] = Field(default=None, max_length=32, pattern=r'^[0-9]+$')
    invoice_date: Optional[date] = None
    buyer_name: Optional[str] = Field(default=None, max_length=200)
    seller_name: Optional[str] = Field(default=None, max_length=200)
    invoice_type: Optional[str] = Field(default=None, max_length=100)
    amount: Optional[Decimal] = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    status: Optional[InvoiceStatus] = None


class Optimize(Input):
    title_id: int = Field(gt=0)
    target_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    allow_exceed: bool = True
    fixed_invoice_ids: list[int] = Field(default_factory=list, max_length=200)


class PrintGenerate(Input):
    reimbursement_plan_id: Optional[int] = Field(default=None, gt=0)
    title_id: Optional[int] = Field(default=None, gt=0)
    merge_pdf: bool = True
    invoice_ids: list[int] = Field(min_length=1, max_length=200)
    auto_reimburse: bool = True

    @field_validator('invoice_ids')
    @classmethod
    def valid_ids(cls, value):
        if any(type(item) is not int or item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError('发票 ID 必须为正整数，且不能重复')
        return value


class AdminUserUpdate(Input):
    name: Optional[str] = Field(default=None, max_length=100)
    role: Optional[Literal['user', 'admin']] = None
    is_active: Optional[bool] = None
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=128)
    unbind_feishu: bool = False
