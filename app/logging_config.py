import logging
from logging.handlers import RotatingFileHandler
import re


class RedactSecrets(logging.Filter):
    def __init__(self, settings):
        super().__init__()
        self.values = [value for value in (settings.feishu_app_secret, settings.aliyun_access_key_secret,
                                           settings.aliyun_access_key_id, settings.bootstrap_admin_password) if value]

    def filter(self, record):
        message = record.getMessage()
        for value in self.values:
            message = message.replace(value, '[redacted]')
        record.msg = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [redacted]', message)
        record.msg = re.sub(r'wss?://[^\s]+', '[websocket-endpoint]', record.msg)
        record.args = ()
        return True


def add_file_logging(settings, source):
    directory = settings.database_path.parent / 'logs'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(directory / (source + '.log'), maxBytes=2 * 1024 * 1024,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    handler.addFilter(RedactSecrets(settings))
    handler.setLevel(logging.INFO)
    logger = logging.getLogger('app')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    if source == 'feishu':
        sdk_logger = logging.getLogger('Lark')
        sdk_logger.addFilter(handler.filters[0])
        sdk_logger.addHandler(handler)
    return handler
