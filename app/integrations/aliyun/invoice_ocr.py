"""RecognizeInvoice (2021-07-07): original PDF body and explicit first page."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Optional

from app.config import Settings

logger = logging.getLogger(__name__)


class OcrError(Exception):
    """Messages are internal reason codes, never raw provider responses."""


@dataclass(frozen=True)
class RecognizedInvoice:
    invoice_code: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    buyer_name: Optional[str] = None
    seller_name: Optional[str] = None
    invoice_type: Optional[str] = None
    amount: Optional[Decimal] = None
    buyer_tax_number: Optional[str] = None
    validation_error: Optional[str] = None
    item_names: tuple = ()
    remarks: Optional[str] = None


def text(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None


def digits(value):
    value = text(value)
    return value if value and re.fullmatch(r'[0-9]+', value) else None


def normalize_date(value):
    value = text(value)
    if not value:
        return None
    match = re.fullmatch(r'(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})日?', value)
    if match is None:
        match = re.fullmatch(r'(\d{4})(\d{2})(\d{2})', value)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return None


def normalize_amount(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        raw = re.sub(r'^[¥￥]', '', str(value).strip()).replace(',', '')
        amount = Decimal(raw)
        if not amount.is_finite() or amount < 0 or not math.isfinite(float(amount)):
            return None
        rounded = amount.quantize(Decimal('0.01'))
        return rounded if rounded == amount else None
    except (ValueError, InvalidOperation, OverflowError):
        return None


def parse_invoice_data(raw: str) -> RecognizedInvoice:
    try:
        payload = json.loads(raw)
        data = payload['data']
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise OcrError('invalid_ocr_response') from None
    code = digits(data.get('invoiceCode'))
    number = digits(data.get('invoiceNumber'))
    kind = text(data.get('invoiceType')) or text(data.get('title'))
    details = data.get('invoiceDetails')
    names = tuple(name for detail in details if isinstance(detail, dict)
                  if (name := text(detail.get('itemName')))) if isinstance(details, list) else ()
    values = {
        'item_names': names, 'remarks': text(data.get('remarks')),
        'invoice_code': code, 'invoice_number': number,
        'invoice_date': normalize_date(data.get('invoiceDate')),
        'buyer_name': text(data.get('purchaserName')), 'seller_name': text(data.get('sellerName')),
        'invoice_type': kind, 'amount': normalize_amount(data.get('totalAmount')),
        'buyer_tax_number': text(data.get('purchaserTaxNumber')),
    }
    missing = [key for key in ('invoice_number', 'invoice_date', 'buyer_name',
                               'seller_name', 'invoice_type', 'amount') if values[key] is None]
    return RecognizedInvoice(**values, validation_error=('missing_or_invalid:' + ','.join(missing)) if missing else None)


class AliyunInvoiceOcr:
    def __init__(self, settings: Settings, *, client=None, sleep=time.sleep):
        self.settings, self.client, self.sleep = settings, client, sleep

    def _client(self):
        from alibabacloud_ocr_api20210707.client import Client
        from alibabacloud_tea_openapi.models import Config

        if self.client is not None:
            return self.client
        if not self.settings.aliyun_access_key_id or not self.settings.aliyun_access_key_secret:
            raise OcrError('aliyun_credentials_missing')
        # Construct per call instead of sharing mutable SDK state across workers.
        return Client(Config(
            access_key_id=self.settings.aliyun_access_key_id,
            access_key_secret=self.settings.aliyun_access_key_secret,
            endpoint=self.settings.aliyun_ocr_endpoint,
            connect_timeout=10000, read_timeout=30000,
        ))

    def recognize(self, path: Path) -> RecognizedInvoice:
        from alibabacloud_ocr_api20210707.models import RecognizeInvoiceRequest
        from alibabacloud_tea_util.models import RuntimeOptions

        client = self._client()
        for attempt in range(3):
            try:
                with path.open('rb') as stream:
                    request = RecognizeInvoiceRequest(body=stream, page_no=1)
                    response = client.recognize_invoice_with_options(
                        request, RuntimeOptions(autoretry=False, connect_timeout=10000, read_timeout=30000),
                    )
                if response.body is None or response.body.code not in (None, '', 'Success', 'success'):
                    raise OcrError('ocr_provider_rejected')
                return parse_invoice_data(response.body.data)
            except Exception:
                logger.warning('Aliyun OCR failed: attempt=%d', attempt + 1)
                if attempt == 2:
                    raise OcrError('ocr_recognition_failed') from None
                self.sleep(2 ** attempt)
        raise AssertionError('unreachable')
