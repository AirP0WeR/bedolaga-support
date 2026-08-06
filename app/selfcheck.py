"""Проверка связности после деплоя: `python -m app.main --check`.

Отвечает на единственный вопрос — заработает ли сервис на этом сервере,
прежде чем его увидит первый живой клиент. Каждый шаг проверяет ровно один
внешний кусок, а не «всё вместе», чтобы по выводу было видно, что чинить.

Модель здесь дёргается по-настоящему, одним коротким вопросом: без этого
неверный ключ или недоступный шлюз выяснились бы на первом тикете.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import llm
from .bedolaga import Bedolaga
from .config import Config
from .kb import KnowledgeBase
from .notify import Notifier

OK = 'OK'
FAIL = 'СБОЙ'
WARN = 'ВНИМАНИЕ'

# Вопрос, ответ на который есть в любой базе знаний. Проверяем не качество
# ответа, а то, что провайдер отвечает и вердикт разбирается.
PROBE_QUESTION = 'Как подключиться?'


@dataclass
class Step:
    name: str
    status: str
    detail: str

    def line(self) -> str:
        return f'[{self.status:^8}] {self.name}: {self.detail}'


def _check_config(cfg: Config) -> Step:
    try:
        cfg.require()
    except RuntimeError as error:
        return Step('Настройки', FAIL, str(error))
    mode = 'боевой — клиенту отвечаем' if cfg.reply_enabled else 'теневой — клиенту не пишем'
    return Step('Настройки', OK, f'всё заполнено, режим {mode}')


def _check_api(api: Bedolaga) -> Step:
    try:
        tickets = api.active_tickets()
    except Exception as error:
        return Step('API бота', FAIL, f'{type(error).__name__}: {error}')
    return Step('API бота', OK, f'доступен, активных тикетов {len(tickets)}')


def _check_kb(kb: KnowledgeBase) -> Step:
    try:
        kb.refresh(force=True)
    except Exception as error:
        return Step('База знаний', FAIL, f'{type(error).__name__}: {error}')

    text = kb.text()
    if not text.strip():
        return Step('База знаний', FAIL, 'пуста: проверьте FAQ в админке бота и каталог kb/')
    if len(text) < 500:
        return Step('База знаний', WARN, f'всего {len(text)} символов — модель будет часто звать оператора')
    return Step('База знаний', OK, f'{len(text)} символов')


def _check_model(provider, kb_text: str, cfg: Config) -> Step:
    verdict = provider.decide(
        knowledge=kb_text,
        context={
            'ticket_id': 0,
            'title': 'Проверка связи',
            'question': PROBE_QUESTION,
            'conversation': f'Клиент: {PROBE_QUESTION}',
            'account': 'Подписка: active',
            'images': [],
        },
        confidence_threshold=cfg.confidence_threshold,
        max_reply_chars=cfg.max_reply_chars,
    )
    if verdict.action == llm.ERROR:
        return Step('Модель', FAIL, f'{cfg.llm_model}: {verdict.reason} (ключ, шлюз или имя модели)')

    spent = verdict.usage.prompt_tokens + verdict.usage.completion_tokens
    decision = 'ответила' if verdict.is_answer else f'передала оператору ({verdict.reason})'
    return Step('Модель', OK, f'{cfg.llm_model} {decision}, потрачено {spent} токенов')


def _check_notifier(notifier: Notifier) -> Step:
    if not notifier.enabled:
        return Step('Наблюдение', WARN, 'TG_BOT_TOKEN или TG_CHAT_ID не заданы — сервис будет работать вслепую')
    notifier.problem('Проверка связи: сервис поддержки видит этот топик.')
    return Step('Наблюдение', OK, 'тестовое сообщение отправлено — проверьте топик')


def run(cfg: Config) -> bool:
    """Прогнать проверки, напечатать отчёт. True — можно запускать."""
    steps = [_check_config(cfg)]

    if steps[0].status == OK:
        api = Bedolaga(cfg.bedolaga_url, cfg.bedolaga_token)
        kb = KnowledgeBase(api, kb_dir=cfg.kb_dir, languages=list(cfg.kb_languages), cache_path=cfg.kb_cache_path)
        notifier = Notifier(bot_token=cfg.tg_bot_token, chat_id=cfg.tg_chat_id, topic_id=cfg.tg_topic_id)
        try:
            steps.append(_check_api(api))
            steps.append(_check_kb(kb))
            if steps[-1].status != FAIL:
                steps.append(_check_model(llm.build_provider(cfg), kb.text(), cfg))
            steps.append(_check_notifier(notifier))
        finally:
            api.close()
            notifier.close()

    print('\nПроверка bedolada-support\n')
    for step in steps:
        print(step.line())

    failed = [step for step in steps if step.status == FAIL]
    print('')
    if failed:
        print(f'Не готово: {len(failed)} из {len(steps)} проверок не прошли.')
        return False
    print('Готово к запуску: docker compose up -d')
    return True
