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
from .cabinet import Cabinet, CabinetAuthError, CabinetThrottled
from .config import Config
from .guard import Guard, build_guard
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


def _check_reply_filter(guard: Guard, kb_text: str) -> Step:
    """Что пост-фильтр отбракует на вашей же базе знаний.

    Самая неочевидная настройка сервиса: ссылка есть во FAQ, модель её честно
    пересказывает, а фильтр отбраковывает ответ. Без этой проверки человек
    узнаёт о запрете из эскалации вместо ответа — и не понимает, почему.
    """
    hosts = guard.unlisted_hosts(kb_text)
    money = guard.mentions_money(kb_text)

    if not hosts and not money:
        return Step('Пост-фильтр', OK, 'в базе знаний нет ни ссылок, ни сумм — отбраковывать нечего')

    notes = []
    if hosts:
        notes.append(
            f'в базе знаний есть ссылки ({", ".join(hosts[:5])}) — ответы с ними уйдут оператору. '
            f'Разрешить: ALLOWED_DOMAINS={",".join(hosts[:5])}'
        )
    if money:
        notes.append('в базе знаний есть суммы — ответы с ценами всегда уходят оператору, это правило не отключается')
    return Step('Пост-фильтр', WARN, '; '.join(notes))


def _check_cabinet_config(cfg: Config) -> Step:
    if not (cfg.cabinet_email and cfg.cabinet_password):
        return Step(
            'Кабинет',
            WARN,
            'CABINET_EMAIL или CABINET_PASSWORD не заданы — сервис работает по webapi-токену, '
            'каталог тарифов и список устройств будут недоступны',
        )
    return Step('Кабинет', OK, 'учётные данные заданы')


def _check_cabinet(cabinet) -> Step:
    """Пускает ли кабинет и хватает ли роли.

    Проверяем не только вход: роль без `tariffs:read` пустит в кабинет, но
    каталог не отдаст — а именно за ним сервис туда и ходит.
    """
    try:
        account_id = cabinet.account_id
        response = cabinet.get('/cabinet/admin/tariffs')
        if response.status_code == 403:
            return Step('Кабинет', FAIL, f'вошли (id {account_id}), но роли не хватает права tariffs:read')
        response.raise_for_status()
        tariffs = response.json().get('tariffs', [])
    except CabinetThrottled as error:
        return Step('Кабинет', WARN, f'вход временно лимитирован: {error}')
    except CabinetAuthError as error:
        return Step('Кабинет', FAIL, f'не пускает — проверьте пароль и роль служебного аккаунта: {error}')
    except Exception as error:
        return Step('Кабинет', FAIL, f'{type(error).__name__}: {error}')
    return Step('Кабинет', OK, f'вошли служебным аккаунтом id {account_id}, роль видит тарифов: {len(tariffs)}')


def _check_notifier(notifier: Notifier) -> Step:
    if not notifier.enabled:
        return Step('Наблюдение', WARN, 'TG_BOT_TOKEN или TG_CHAT_ID не заданы — сервис будет работать вслепую')
    notifier.hello('Проверка связи: этот топик подключён, сюда будут падать черновики и эскалации.')
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
            steps.append(_check_cabinet_config(cfg))
            if steps[-1].status == OK:
                cabinet = Cabinet(cfg.bedolaga_url, cfg.cabinet_email, cfg.cabinet_password)
                try:
                    steps[-1] = _check_cabinet(cabinet)
                finally:
                    cabinet.close()
            steps.append(_check_kb(kb))
            if steps[-1].status != FAIL:
                steps.append(_check_reply_filter(build_guard(cfg), kb.text()))
                steps.append(_check_model(llm.build_provider(cfg), kb.text(), cfg))
            steps.append(_check_notifier(notifier))
        finally:
            api.close()
            notifier.close()

    print('\nПроверка bedolaga-support\n')
    for step in steps:
        print(step.line())

    failed = [step for step in steps if step.status == FAIL]
    print('')
    if failed:
        print(f'Не готово: {len(failed)} из {len(steps)} проверок не прошли.')
        return False
    print('Готово к запуску: docker compose up -d')
    return True
