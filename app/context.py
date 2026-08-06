"""Сборка того, что видит модель: переписка, факты об аккаунте, скриншоты.

Ссылку на подписку (`subscription_url`, `subscription_crypto_link`) сюда
намеренно не кладём: это фактически ключ доступа клиента, а получить её он
может кнопкой в боте. Модель не должна иметь возможности вставить её в текст.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime

from .bedolaga import Bedolaga
from .gate import READABLE_MEDIA
from .timeutil import parse_dt

log = logging.getLogger(__name__)

# Сколько последних сообщений показываем модели.
CONVERSATION_TAIL = 20
# Больше одного скриншота за раз не отдаём: дорого и почти всегда лишнее.
MAX_IMAGES = 1


def render_conversation(messages: list[dict]) -> str:
    """Переписка в виде, пригодном для чтения моделью."""
    lines = []
    for message in messages[-CONVERSATION_TAIL:]:
        who = 'Поддержка' if message.get('is_from_admin') else 'Клиент'
        text = (message.get('message_text') or '').strip()
        if message.get('has_media'):
            kind = message.get('media_type') or 'вложение'
            marker = '[скриншот]' if kind == READABLE_MEDIA else f'[вложение: {kind}]'
            text = f'{marker} {text}'.strip()
        lines.append(f'{who}: {text}')
    return '\n'.join(lines)


def render_account(user: dict | None, *, now: datetime) -> str:
    """Факты об аккаунте, на которые модель может опираться в ответе."""
    if not user:
        return 'Аккаунт клиента не найден в базе.'

    parts = [f'Язык клиента: {user.get("language") or "не указан"}']

    subscription = user.get('subscription')
    if not subscription:
        parts.append('Подписки нет.')
        if user.get('has_had_paid_subscription'):
            parts.append('Раньше платная подписка была.')
        return '\n'.join(parts)

    status = subscription.get('actual_status') or subscription.get('status')
    parts.append(f'Подписка: {status}{" (триал)" if subscription.get("is_trial") else ""}')

    if subscription.get('tariff_name'):
        parts.append(f'Тариф: {subscription["tariff_name"]}')

    end_date = parse_dt(subscription.get('end_date'))
    if end_date:
        days_left = (end_date - now).days
        when = end_date.strftime('%d.%m.%Y')
        parts.append(f'Действует до: {when} ({days_left} дн.)' if days_left >= 0 else f'Истекла {when}')

    limit_gb = subscription.get('traffic_limit_gb')
    used_gb = subscription.get('traffic_used_gb')
    if limit_gb is not None and used_gb is not None:
        limit_text = 'безлимит' if not limit_gb else f'{limit_gb} ГБ'
        parts.append(f'Трафик: использовано {used_gb:.1f} ГБ из {limit_text}')

    if subscription.get('device_limit'):
        parts.append(f'Лимит устройств: {subscription["device_limit"]}')

    if subscription.get('connected_squads'):
        parts.append(f'Подключено локаций: {len(subscription["connected_squads"])}')

    parts.append(f'Автоплатёж: {"включён" if subscription.get("autopay_enabled") else "выключен"}')
    return '\n'.join(parts)


def collect_images(api: Bedolaga, ticket: dict) -> list[str]:
    """Скриншоты из последних сообщений клиента как data-URL для модели."""
    images: list[str] = []
    for message in reversed(ticket.get('messages') or []):
        if len(images) >= MAX_IMAGES:
            break
        if message.get('is_from_admin') or not message.get('has_media'):
            continue
        if message.get('media_type') != READABLE_MEDIA:
            continue

        media = api.message_media(ticket['id'], message['id'])
        if not media or not media.get('media_file_id'):
            continue

        blob = api.download_media(media['media_file_id'])
        if not blob:
            continue

        encoded = base64.b64encode(blob).decode('ascii')
        images.append(f'data:image/jpeg;base64,{encoded}')
    return images


def build(api: Bedolaga, ticket: dict, *, now: datetime | None = None) -> dict:
    """Всё, что нужно модели по одному тикету."""
    now = now or datetime.now(UTC)
    messages = ticket.get('messages') or []

    user = None
    try:
        user = api.user(ticket['user_id'])
    except Exception:
        # Без снимка аккаунта модель ответит хуже, но это не повод молчать:
        # часть вопросов (как подключить, где взять ссылку) от него не зависит.
        log.warning('Не удалось получить аккаунт для тикета %s', ticket.get('id'), exc_info=True)

    last_client_message = next(
        (m for m in reversed(messages) if not m.get('is_from_admin')),
        None,
    )

    return {
        'ticket_id': ticket.get('id'),
        'title': ticket.get('title') or '',
        'question': (last_client_message or {}).get('message_text') or '',
        'conversation': render_conversation(messages),
        'account': render_account(user, now=now),
        'images': collect_images(api, ticket),
    }
