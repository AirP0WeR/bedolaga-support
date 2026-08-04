"""Тесты разбора вердикта: любая неоднозначность должна уводить к человеку."""

from __future__ import annotations

from app.llm import ANSWER, ESCALATE, _sanitize

OK = {
    'action': 'answer',
    'reply_text': 'Откройте бот и нажмите «Подписка».',
    'reason': 'по FAQ',
    'topic': 'подключение',
    'confidence': 0.9,
}


def sanitize(raw, *, threshold=0.7, max_chars=1500):
    return _sanitize(raw, confidence_threshold=threshold, max_reply_chars=max_chars)


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
