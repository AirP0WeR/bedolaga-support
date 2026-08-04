"""Тесты сборки контекста — чистая часть, без сети."""

from __future__ import annotations

from datetime import UTC, datetime

from app.context import render_account, render_conversation

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def test_переписка_размечена_ролями():
    messages = [
        {'message_text': 'не работает', 'is_from_admin': False},
        {'message_text': 'проверьте подписку', 'is_from_admin': True},
    ]
    assert render_conversation(messages) == 'Клиент: не работает\nПоддержка: проверьте подписку'


def test_скриншот_помечен_в_переписке():
    messages = [{'message_text': 'вот ошибка', 'is_from_admin': False, 'has_media': True, 'media_type': 'photo'}]
    assert render_conversation(messages) == 'Клиент: [скриншот] вот ошибка'


def test_нефото_вложение_названо_своим_типом():
    messages = [{'message_text': '', 'is_from_admin': False, 'has_media': True, 'media_type': 'voice'}]
    assert render_conversation(messages) == 'Клиент: [вложение: voice]'


def test_переписка_обрезается_хвостом():
    messages = [{'message_text': str(i), 'is_from_admin': False} for i in range(50)]
    lines = render_conversation(messages).splitlines()
    assert len(lines) == 20
    assert lines[-1] == 'Клиент: 49'


def test_аккаунт_без_подписки():
    user = {'language': 'ru', 'subscription': None, 'has_had_paid_subscription': True}
    text = render_account(user, now=NOW)
    assert 'Подписки нет.' in text
    assert 'Раньше платная подписка была.' in text


def test_аккаунт_с_активной_подпиской():
    user = {
        'language': 'ru',
        'subscription': {
            'actual_status': 'active',
            'is_trial': False,
            'tariff_name': 'Год',
            'end_date': '2026-08-14T12:00:00+00:00',
            'traffic_limit_gb': 0,
            'traffic_used_gb': 12.34,
            'device_limit': 3,
            'connected_squads': ['a', 'b'],
            'autopay_enabled': True,
        },
    }
    text = render_account(user, now=NOW)
    assert 'Тариф: Год' in text
    assert 'Действует до: 14.08.2026 (10 дн.)' in text
    assert 'использовано 12.3 ГБ из безлимит' in text
    assert 'Лимит устройств: 3' in text
    assert 'Автоплатёж: включён' in text


def test_истёкшая_подписка_названа_истёкшей():
    user = {
        'language': 'ru',
        'subscription': {
            'actual_status': 'expired',
            'is_trial': False,
            'end_date': '2026-07-01T12:00:00+00:00',
            'traffic_limit_gb': 100,
            'traffic_used_gb': 5.0,
            'device_limit': 1,
            'autopay_enabled': False,
        },
    }
    text = render_account(user, now=NOW)
    assert 'Истекла 01.07.2026' in text


def test_ссылка_подписки_не_попадает_в_контекст():
    """Ссылка — это ключ доступа клиента, модель не должна её видеть."""
    user = {
        'language': 'ru',
        'subscription': {
            'actual_status': 'active',
            'is_trial': False,
            'end_date': '2026-09-01T12:00:00+00:00',
            'traffic_limit_gb': 0,
            'traffic_used_gb': 1.0,
            'device_limit': 1,
            'autopay_enabled': False,
            'subscription_url': 'https://sub.example.com/secret-token',
            'subscription_crypto_link': 'happ://crypt4/secret',
        },
    }
    text = render_account(user, now=NOW)
    assert 'secret' not in text
    assert 'http' not in text


def test_отсутствующий_аккаунт_не_ломает_сборку():
    assert 'не найден' in render_account(None, now=NOW)
