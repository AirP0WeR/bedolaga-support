"""Тесты разбора вердикта: любая неоднозначность должна уводить к человеку."""

from __future__ import annotations

from app.guard import Guard
from app.llm import ANSWER, ESCALATE, _sanitize, _usage

GUARD = Guard()
ALLOWING_GUARD = Guard(allowed_domains=frozenset({'example.com'}))

OK = {
    'action': 'answer',
    'reply_text': 'Откройте бот и нажмите «Подписка».',
    'reason': 'по FAQ',
    'topic': 'подключение',
    'confidence': 0.9,
}


def sanitize(raw, *, threshold=0.7, max_chars=1500, guard=GUARD):
    return _sanitize(raw, confidence_threshold=threshold, max_reply_chars=max_chars, guard=guard)


def answer(text: str) -> dict:
    return {**OK, 'reply_text': text}


def test_уверенный_ответ_проходит():
    verdict = sanitize(OK)
    assert verdict.action == ANSWER
    assert verdict.reply_text.startswith('Откройте бот')
    assert verdict.topic == 'подключение'


def test_низкая_уверенность_уводит_к_человеку():
    verdict = sanitize({**OK, 'confidence': 0.4})
    assert verdict.action == ESCALATE
    assert 'уверенность' in verdict.reason


def test_пустой_текст_уводит_к_человеку():
    assert sanitize({**OK, 'reply_text': '   '}).action == ESCALATE


def test_слишком_длинный_ответ_уводит_к_человеку():
    verdict = sanitize({**OK, 'reply_text': 'а' * 2000})
    assert verdict.action == ESCALATE
    assert 'длиннее' in verdict.reason


def test_явная_эскалация_не_несёт_текста_клиенту():
    verdict = sanitize(
        {
            'action': 'escalate',
            'reply_text': 'передаю оператору',
            'reason': 'деньги',
            'topic': 'оплата',
            'confidence': 0.95,
        }
    )
    assert verdict.action == ESCALATE
    assert verdict.reply_text == ''


def test_мусорное_действие_уводит_к_человеку():
    assert sanitize({**OK, 'action': 'send'}).action == ESCALATE


def test_нечисловая_уверенность_считается_нулевой():
    verdict = sanitize({**OK, 'confidence': 'высокая'})
    assert verdict.action == ESCALATE
    assert verdict.confidence == 0.0


def test_отсутствующие_поля_не_ломают_разбор():
    assert sanitize({}).action == ESCALATE


# --- пост-фильтр -------------------------------------------------------------


def test_нормальный_ответ_проходит_фильтр():
    """Без этого кейса остальные прошли бы и на фильтре, который режет всё."""
    verdict = sanitize(answer('Откройте бота, нажмите «Моя подписка» → «Подключиться».'))
    assert verdict.action == ANSWER


def test_ссылка_без_разрешения_уводит_к_человеку():
    verdict = sanitize(answer('Возьмите ссылку тут: https://sub.example.com/abc'))
    assert verdict.action == ESCALATE
    assert 'sub.example.com' in verdict.reason


def test_разрешённый_домен_проходит():
    verdict = sanitize(answer('Инструкция: https://example.com/help'), guard=ALLOWING_GUARD)
    assert verdict.action == ANSWER


def test_поддомен_разрешённого_домена_проходит():
    verdict = sanitize(answer('Смотрите https://help.example.com/vpn'), guard=ALLOWING_GUARD)
    assert verdict.action == ANSWER


def test_чужой_домен_при_непустом_списке_не_проходит():
    verdict = sanitize(answer('Смотрите https://evil.tld/vpn'), guard=ALLOWING_GUARD)
    assert verdict.action == ESCALATE


def test_домен_без_схемы_тоже_ловится():
    assert sanitize(answer('Пишите в t.me/support')).action == ESCALATE


def test_ссылка_подписки_со_своей_схемой_ловится():
    verdict = sanitize(answer('Добавьте happ://crypt4/secret в приложение'))
    assert verdict.action == ESCALATE


def test_сумма_в_ответе_уводит_к_человеку():
    verdict = sanitize(answer('Стоимость продления — 300 руб в месяц.'))
    assert verdict.action == ESCALATE
    assert 'сумма' in verdict.reason


def test_сумма_перед_числом_тоже_ловится():
    assert sanitize(answer('Спишется $5 при продлении.')).action == ESCALATE


def test_обычные_числа_не_путаются_с_деньгами():
    verdict = sanitize(answer('На тарифе 100 ГБ и 3 устройства, осталось 12 дней.'))
    assert verdict.action == ANSWER


def test_стоп_слово_уводит_к_человеку():
    verdict = sanitize(answer('Оформим возврат в течение трёх дней.'))
    assert verdict.action == ESCALATE
    assert 'стоп-слово' in verdict.reason


def test_стоп_слово_ловится_по_корню():
    assert sanitize(answer('Компенсация будет начислена автоматически.')).action == ESCALATE


def test_свой_список_стоп_слов():
    guard = Guard(stop_words=('промокод',))
    assert sanitize(answer('Введите промокод в боте.'), guard=guard).action == ESCALATE
    assert sanitize(answer('Оформим возврат.'), guard=guard).action == ANSWER


def test_причина_отбраковки_доезжает_до_наблюдения():
    """Причина уходит в аудит и в TG — иначе фильтр молча съедает ответы."""
    verdict = sanitize(answer('Держите https://sub.example.com/abc'))
    assert verdict.reason.startswith('пост-фильтр:')


# --- расход токенов -----------------------------------------------------------


class FakeUsage:
    prompt_tokens = 12_000
    completion_tokens = 180

    class prompt_tokens_details:  # имя как в ответе провайдера
        cached_tokens = 11_500


def test_расход_токенов_снимается_с_ответа():
    usage = _usage(type('R', (), {'usage': FakeUsage})())
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (12_000, 180, 11_500)


def test_ответ_без_usage_не_ломает_разбор():
    usage = _usage(type('R', (), {})())
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (0, 0, 0)


def test_провайдер_без_кеша_даёт_нули_вместо_none():
    class NoDetails:
        prompt_tokens = 100
        completion_tokens = 10
        prompt_tokens_details = None

    assert _usage(type('R', (), {'usage': NoDetails})()).cached_tokens == 0


def test_причина_отбраковки_называет_переменную():
    """Человек в топике должен понять, что делать, без чтения README."""
    verdict = sanitize(answer('Статус здесь: https://dash.example.com/'))
    assert 'ALLOWED_DOMAINS' in verdict.reason


def test_домен_с_цифрой_в_имени_разрешается_как_обычный():
    """Вроде 2ip.ru — регексп не должен спотыкаться о цифру в начале."""
    assert sanitize(answer('Проверьте IP на 2ip.ru')).action == ESCALATE
    guard = Guard(allowed_domains=frozenset({'2ip.ru'}))
    assert sanitize(answer('Проверьте IP на 2ip.ru'), guard=guard).action == ANSWER
