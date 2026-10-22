"""Official SDK listener; runs in a dedicated process with its own asyncio loop."""

import asyncio
import json
import logging

from app.config import Settings
from app.db.connection import initialize
from app.integrations.feishu.dispatcher import EventDispatcher
from app.integrations.feishu.handler import MessageHandler
from app.integrations.feishu.client import build_api_client
from app.integrations.feishu.messages import FeishuMessageSender
from app.services.feishu_binding import BindingService
from app.services.invoice_ingestion import build_ingestion
from app.logging_config import add_file_logging

logger = logging.getLogger(__name__)


def build_event_handler(dispatcher: EventDispatcher):
    import lark_oapi as lark

    def on_message(data):
        dispatcher.submit(json.loads(lark.JSON.marshal(data)))

    return (lark.EventDispatcherHandler.builder('', '')
            .register_p2_im_message_receive_v1(on_message).build())


def build_client(settings: Settings, dispatcher: EventDispatcher):
    import lark_oapi as lark

    event_handler = build_event_handler(dispatcher)
    return lark.ws.Client(settings.feishu_app_id, settings.feishu_app_secret,
                          event_handler=event_handler, log_level=lark.LogLevel.WARNING,
                          auto_reconnect=True)


def run_listener(settings: Settings):
    settings.require_feishu_credentials()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    log_handler = add_file_logging(settings, 'feishu')
    # Importing the SDK after setting the loop avoids sharing Uvicorn's loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    initialize(settings.database_path)
    dispatcher = EventDispatcher(
        MessageHandler(BindingService(settings.database_path), build_ingestion(settings)),
        FeishuMessageSender(build_api_client(settings)),
        spool_dir=settings.database_path.parent / 'feishu-events',
    )
    try:
        dispatcher.replay_pending()
        client = build_client(settings, dispatcher)
        logger.info('Starting Feishu long connection; ingestion and message replies enabled')
        client.start()
    finally:
        dispatcher.close()
        loop.close()
        logging.getLogger('app').removeHandler(log_handler)
        sdk_logger = logging.getLogger('Lark')
        sdk_logger.removeHandler(log_handler)
        sdk_logger.removeFilter(log_handler.filters[0])
        log_handler.close()


if __name__ == '__main__':
    run_listener(Settings.from_env())
