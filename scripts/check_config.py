"""Offline preflight: does not initialize users, start listeners or call providers."""

import argparse
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sqlite3

from app.config import PROJECT_ROOT, Settings
from app.schemas.requests import Registration


def check(settings: Settings) -> list[str]:
    messages = []
    if bool(settings.bootstrap_admin_username) != bool(settings.bootstrap_admin_password):
        raise ValueError('请同时填写 INITIAL_ADMIN_USERNAME 和 INITIAL_ADMIN_PASSWORD')
    if settings.bootstrap_admin_username:
        try:
            Registration(username=settings.bootstrap_admin_username, password=settings.bootstrap_admin_password)
        except ValueError:
            raise ValueError('初始管理员配置无效，请检查用户名及密码长度；不会输出配置内容') from None
        messages.append('初始管理员配置格式：通过（未创建账号）')
    else:
        messages.append('初始管理员配置：未启用首次创建')
    if settings.feishu_enabled:
        settings.require_feishu_credentials()
    messages.append('飞书配置：' + ('已启用，凭据非空（未验证远端）' if settings.feishu_enabled else '已关闭'))
    if bool(settings.aliyun_access_key_id) != bool(settings.aliyun_access_key_secret):
        raise ValueError('请同时填写阿里云 AccessKey ID 和 Secret')
    messages.append('OCR 配置：' + ('凭据非空（未验证远端）' if settings.aliyun_access_key_id else '未配置，识别将降级为待确认'))
    if (not settings.aliyun_ocr_endpoint or '/' in settings.aliyun_ocr_endpoint
            or any(char.isspace() for char in settings.aliyun_ocr_endpoint)):
        raise ValueError('ALIYUN_OCR_ENDPOINT 应为主机名，不包含协议或路径')
    for relative in ('sql/001_init.sql', 'app/web/index.html', 'app/web/app.js', 'app/web/styles.css'):
        if not (PROJECT_ROOT / relative).is_file():
            raise ValueError('项目源码不完整：缺少 ' + relative)
    with closing(sqlite3.connect(':memory:')) as db:
        if db.execute("SELECT json_valid('[]')").fetchone()[0] != 1:
            raise ValueError('SQLite JSON 函数不可用')
    messages.append('SQLite JSON 支持：通过')
    packages = ('fastapi', 'uvicorn', 'lark-oapi', 'python-dotenv', 'alibabacloud-ocr-api20210707',
                'PyMuPDF', 'reportlab', 'python-multipart', 'itsdangerous')
    for package in packages:
        try:
            version(package)
        except PackageNotFoundError:
            raise ValueError('缺少运行依赖：' + package) from None
    messages.append('运行依赖与静态资源：通过')
    messages.append('Cookie Secure：' + ('开启（需 HTTPS）' if settings.cookie_secure else '关闭（本机 HTTP 模式）'))
    return messages


def main():
    parser = argparse.ArgumentParser(description='只读检查本地配置，不启动服务或发送网络请求')
    parser.add_argument('--env-file', type=Path, default=PROJECT_ROOT / '.env')
    args = parser.parse_args()
    try:
        messages = check(Settings.from_env(args.env_file))
    except (ValueError, sqlite3.Error) as exc:
        # These errors contain controlled configuration names, never input values.
        parser.exit(1, f'配置检查失败：{exc}\n')
    for message in messages:
        print(message)
    print('检查完成：未创建数据库或管理员，未启动飞书，未调用 OCR。')


if __name__ == '__main__':
    main()
