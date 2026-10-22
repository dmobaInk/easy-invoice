"""Shared authenticated HTTP SDK client; no credentials or payloads in logs."""

from app.config import Settings


def build_api_client(settings: Settings):
    import lark_oapi as lark

    settings.require_feishu_credentials()
    return (lark.Client.builder().app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret).timeout(20)
            .log_level(lark.LogLevel.WARNING).build())
