"""Самопроверка: по её выводу человек решает, можно ли запускать сервис."""

from __future__ import annotations

from dataclasses import replace

from app import llm, selfcheck
from app.config import Config
from app.selfcheck import FAIL, OK, WARN


class FakeKb:
    def __init__(self, text: str = 'ф' * 1000, error: Exception | None = None):
        self._text = text
        self._error = error

    def refresh(self, *, force: bool = False) -> None:
        if self._error:
            raise self._error

    def text(self) -> str:
        return self._text


class FakeApi:
    def __init__(self, tickets: list | None = None, error: Exception | None = None):
        self._tickets = tickets or []
        self._error = error

    def active_tickets(self):
        if self._error:
            raise self._error
        return self._tickets


class FakeProvider:
    def __init__(self, verdict: llm.Verdict):
        self._verdict = verdict

    def decide(self, **kwargs) -> llm.Verdict:
        return self._verdict


def config(**overrides) -> Config:
    base = replace(
        Config(),
        bedolaga_url='http://bot',
        bedolaga_token='t',
        openai_api_key='k',
        llm_model='test-model',
    )
    return replace(base, **overrides)


def test_недостающие_переменные_названы_поимённо():
    step = selfcheck._check_config(replace(config(), openai_api_key=''))
    assert step.status == FAIL
    assert 'OPENAI_API_KEY' in step.detail


def test_режим_виден_в_отчёте():
    assert 'теневой' in selfcheck._check_config(config(reply_enabled=False)).detail
    assert 'боевой' in selfcheck._check_config(config(reply_enabled=True)).detail


def test_недоступный_api_объясняет_причину():
    step = selfcheck._check_api(FakeApi(error=ConnectionError('соединение отклонено')))
    assert step.status == FAIL
    assert 'соединение отклонено' in step.detail


def test_доступный_api_считает_тикеты():
    step = selfcheck._check_api(FakeApi(tickets=[{'id': 1}, {'id': 2}]))
    assert step.status == OK
    assert '2' in step.detail


def test_пустая_база_знаний_это_сбой():
    step = selfcheck._check_kb(FakeKb(text='   '))
    assert step.status == FAIL
    assert 'kb/' in step.detail


def test_короткая_база_знаний_это_предупреждение():
    """Сервис запустится, но будет звать оператора почти всегда."""
    assert selfcheck._check_kb(FakeKb(text='коротко')).status == WARN


def test_полная_база_знаний_проходит():
    assert selfcheck._check_kb(FakeKb()).status == OK


def test_недоступная_модель_это_сбой():
    provider = FakeProvider(llm.Verdict(llm.ERROR, reason='модель недоступна'))
    step = selfcheck._check_model(provider, 'база', config())
    assert step.status == FAIL
    assert 'test-model' in step.detail


def test_эскалация_на_проверочном_вопросе_не_считается_сбоем():
    """Проверяем связность, а не то, что модель ответила именно этот вопрос."""
    provider = FakeProvider(llm.Verdict(llm.ESCALATE, reason='нет в базе'))
    assert selfcheck._check_model(provider, 'база', config()).status == OK


def test_расход_токенов_виден():
    provider = FakeProvider(
        llm.Verdict(llm.ANSWER, reply_text='вот так', confidence=0.9, usage=llm.Usage(1000, 50, 900))
    )
    assert '1050' in selfcheck._check_model(provider, 'база', config()).detail


class FakeNotifier:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.sent: list[str] = []

    def problem(self, text: str) -> None:
        self.sent.append(text)


def test_ненастроенное_наблюдение_предупреждает():
    step = selfcheck._check_notifier(FakeNotifier(enabled=False))
    assert step.status == WARN
    assert 'вслепую' in step.detail


def test_настроенное_наблюдение_шлёт_тестовое_сообщение():
    notifier = FakeNotifier(enabled=True)
    assert selfcheck._check_notifier(notifier).status == OK
    assert len(notifier.sent) == 1


def test_отчёт_читается_строкой():
    line = selfcheck.Step('API бота', OK, 'доступен').line()
    assert 'API бота' in line and OK in line
