"""Parse only human p2p text and file events; never log raw event contents."""

from dataclasses import dataclass
import json
from typing import Optional


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    open_id: str
    message_type: str
    text: Optional[str] = None
    file_key: Optional[str] = None
    file_name: Optional[str] = None


def required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing or invalid {name}')
    return value


def parse_event(payload: dict) -> Optional[IncomingMessage]:
    if not isinstance(payload, dict):
        raise ValueError('Event must be an object')
    header = payload.get('header')
    if not isinstance(header, dict):
        raise ValueError('Missing event header')
    if header.get('event_type') != 'im.message.receive_v1':
        return None
    event = payload.get('event')
    if not isinstance(event, dict):
        raise ValueError('Missing event body')
    message, sender = event.get('message'), event.get('sender')
    if not isinstance(message, dict) or not isinstance(sender, dict):
        raise ValueError('Missing message or sender')
    if message.get('chat_type') != 'p2p' or sender.get('sender_type') != 'user':
        return None
    message_type = message.get('message_type')
    if message_type not in ('text', 'file'):
        return None
    sender_id = sender.get('sender_id')
    if not isinstance(sender_id, dict):
        raise ValueError('Missing sender identity')
    message_id = required_text(message.get('message_id'), 'message_id')
    open_id = required_text(sender_id.get('open_id'), 'open_id')
    try:
        content = json.loads(required_text(message.get('content'), 'content'))
    except json.JSONDecodeError as exc:
        raise ValueError('Malformed message JSON') from exc
    if not isinstance(content, dict):
        raise ValueError('Message content must be an object')
    if message_type == 'text':
        return IncomingMessage(message_id, open_id, message_type,
                               text=required_text(content.get('text'), 'text').strip())
    return IncomingMessage(
        message_id, open_id, message_type,
        file_key=required_text(content.get('file_key'), 'file_key'),
        file_name=required_text(content.get('file_name'), 'file_name'),
    )
