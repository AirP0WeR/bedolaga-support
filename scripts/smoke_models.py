"""Прогон кандидатов на реальном коде сервиса: держат ли стоп-правила и JSON."""

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import OpenAIProvider

# Дата в фактах аккаунта — от текущего момента, а не зашита в прошлое: иначе
# полгода спустя кейс молча превращается в «просроченная подписка».
EXPIRY_DAYS = 40
EXPIRY = (datetime.now(UTC) + timedelta(days=EXPIRY_DAYS)).strftime('%d.%m.%Y')
ACCOUNT_ACTIVE = f'Подписка: active\nТариф: Год\nДействует до: {EXPIRY} ({EXPIRY_DAYS} дн.)'

KB = """\
# FAQ

## Как подключить VPN
Откройте бота, нажмите «Моя подписка» → «Подключиться». Скопируйте ссылку и
добавьте её в приложение Happ. Для iPhone приложение ставится из App Store.

## Сколько устройств можно подключить
Количество устройств зависит от тарифа и показано в разделе «Моя подписка».
Лишние устройства нужно отвязать, чтобы подключить новое.

## Не работает подключение
Переподключите VPN в приложении, обновите подписку кнопкой «Обновить»
и попробуйте другую локацию из списка.
"""

CASES = [
    (
        'обычный вопрос',
        {
            'title': 'Подключение',
            'question': 'Как подключить на айфоне?',
            'conversation': 'Клиент: Как подключить на айфоне?',
            'account': ACCOUNT_ACTIVE,
            'images': [],
        },
        'answer',
    ),
    (
        'стоп-тема: деньги',
        {
            'title': 'Списание',
            'question': 'С меня дважды списали деньги, верните!',
            'conversation': 'Клиент: С меня дважды списали деньги, верните!',
            'account': 'Подписка: active\nТариф: Год',
            'images': [],
        },
        'escalate',
    ),
    (
        'просьба оператора',
        {
            'title': 'Оператор',
            'question': 'Дайте живого человека',
            'conversation': 'Клиент: Дайте живого человека',
            'account': 'Подписка: active',
            'images': [],
        },
        'escalate',
    ),
    (
        'нет в базе',
        {
            'title': 'Роутер',
            'question': 'Как настроить ваш VPN на роутере Keenetic по WireGuard?',
            'conversation': 'Клиент: Как настроить ваш VPN на роутере Keenetic по WireGuard?',
            'account': 'Подписка: active',
            'images': [],
        },
        'escalate',
    ),
    (
        'действие в аккаунте',
        {
            'title': 'Устройства',
            'question': 'Сбросьте мне все устройства пожалуйста',
            'conversation': 'Клиент: Сбросьте мне все устройства пожалуйста',
            'account': 'Подписка: active\nЛимит устройств: 3',
            'images': [],
        },
        'escalate',
    ),
    (
        'вопрос по аккаунту',
        {
            'title': 'Срок',
            'question': 'До какого числа у меня оплачено?',
            'conversation': 'Клиент: До какого числа у меня оплачено?',
            'account': ACCOUNT_ACTIVE,
            'images': [],
        },
        'answer',
    ),
]


def run(model: str) -> None:
    v = dotenv_values(ROOT / '.env')
    provider = OpenAIProvider(
        api_key=v['OPENAI_API_KEY'],
        model=model,
        base_url=v.get('OPENAI_BASE_URL') or None,
        brand='VPN-сервиса',
    )
    print(f'\n=== {model} ===')
    ok = 0
    started = time.monotonic()
    for name, ctx, expected in CASES:
        t0 = time.monotonic()
        verdict = provider.decide(knowledge=KB, context=ctx, confidence_threshold=0.7, max_reply_chars=1500)
        took = time.monotonic() - t0
        got = 'answer' if verdict.is_answer else verdict.action
        mark = '✓' if got == expected else '✗'
        if got == expected:
            ok += 1
        print(f'{mark} {name}: {got} ({took:.1f}с, conf {verdict.confidence:.2f})')
        if verdict.is_answer:
            print(f'    → {verdict.reply_text[:160]}')
        elif verdict.action == 'escalate':
            print(f'    причина: {verdict.reason[:100]}')
    print(f'итог: {ok}/{len(CASES)} за {time.monotonic() - started:.0f}с')


if __name__ == '__main__':
    for model in sys.argv[1:]:
        run(model)
