"""Environment settings shared by the HTTP app and Feishu process."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_path: Path
    feishu_enabled: bool = False
    feishu_app_id: str = field(default='', repr=False)
    feishu_app_secret: str = field(default='', repr=False)
    aliyun_access_key_id: str = field(default='', repr=False)
    aliyun_access_key_secret: str = field(default='', repr=False)
    aliyun_ocr_endpoint: str = 'ocr-api.cn-hangzhou.aliyuncs.com'
    bootstrap_admin_username: str = field(default='', repr=False)
    bootstrap_admin_password: str = field(default='', repr=False)
    cookie_secure: bool = False

    @classmethod
    def from_env(cls, env_file: Optional[Path] = PROJECT_ROOT / '.env') -> 'Settings':
        # Explicit process environment takes precedence; do not mutate os.environ.
        # Disable interpolation so credentials containing ${...} remain literal.
        values = dotenv_values(env_file, interpolate=False) if env_file is not None else {}
        values.update(os.environ)
        def value(key: str, default: str = '') -> str:
            return values.get(key) or default

        enabled = value('FEISHU_ENABLED', 'false').strip().lower()
        if enabled not in ('true', 'false', '1', '0'):
            raise ValueError('FEISHU_ENABLED must be true, false, 1 or 0')
        secure = value('COOKIE_SECURE', 'false').strip().lower()
        if secure not in ('true', 'false', '1', '0'):
            raise ValueError('COOKIE_SECURE must be true, false, 1 or 0')
        path = Path(value('DATABASE_PATH', 'data/app.db')).expanduser()
        settings = cls(
            database_path=path if path.is_absolute() else PROJECT_ROOT / path,
            feishu_enabled=enabled in ('true', '1'),
            feishu_app_id=value('FEISHU_APP_ID').strip(),
            feishu_app_secret=value('FEISHU_APP_SECRET').strip(),
            aliyun_access_key_id=value('ALIBABA_CLOUD_ACCESS_KEY_ID').strip(),
            aliyun_access_key_secret=value('ALIBABA_CLOUD_ACCESS_KEY_SECRET').strip(),
            aliyun_ocr_endpoint=value('ALIYUN_OCR_ENDPOINT', 'ocr-api.cn-hangzhou.aliyuncs.com').strip(),
            bootstrap_admin_username=value('INITIAL_ADMIN_USERNAME').strip(),
            bootstrap_admin_password=value('INITIAL_ADMIN_PASSWORD'),
            cookie_secure=secure in ('true', '1'),
        )
        if settings.feishu_enabled:
            settings.require_feishu_credentials()
        return settings

    def require_feishu_credentials(self) -> None:
        if not self.feishu_app_id or not self.feishu_app_secret:
            raise ValueError('FEISHU_APP_ID and FEISHU_APP_SECRET are required')
