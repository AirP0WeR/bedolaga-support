# Живые данные, инструменты и сводка для оператора — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Спека:** [docs/superpowers/specs/2026-08-08-live-data-and-tools-design.md](../specs/2026-08-08-live-data-and-tools-design.md) — согласована, прошла ревью; API сверен с боевым ботом v4.0.0-f553d189.

**Цель:** сервис поддержки перестаёт отвечать только по текстовой базе знаний — получает живой снимок тарифов и настроек, пять read-only инструментов по данным клиента и учится отдавать оператору сводку вместо голой эскалации.

**Архитектура:** весь доступ к бедолаге переезжает с неразграниченного `X-API-Key` на служебный аккаунт кабинета (новый `app/cabinet.py`); поверх него — снимок каталога (`app/live.py`) и пакет инструментов (`app/tools/`); `app/llm.py` переезжает на Responses API ради tool-use с reasoning; `app/guard.py` учится сверять суммы с данными.

**Стек:** Python 3.12, httpx, openai SDK (Responses API), SQLite, uv, pytest, ruff. Новых зависимостей нет; floor `openai` поднимается до версии с Responses API.

## Глобальные ограничения

- **Язык проекта — русский**: комментарии, докстринги, сообщения коммитов, имена тестов (кириллица в идентификаторах тестов — принятый стиль, ruff RUF001-003 уже отключены).
- **TDD**: тест пишется и падает до реализации. Стиль тестов — как в существующих `tests/`: httpx.MockTransport с инлайн-JSON, подделки классов через `object.__new__`, никакой сети.
- **Тесты и линт**: `uv run pytest -q`, `uv run ruff format --check .`, `uv run ruff check .` — ровно это гоняет CI (`.github/workflows/ci.yml`, `uv sync --frozen`). Формат: одинарные кавычки, строка 120.
- **`.env` в корне репо** (боевые значения, gitignored) **pytest не ломает** — проверено, 161 тест зелёный. Но `Config()` подхватывает его через `load_dotenv()`, поэтому новые тесты обязаны собирать конфиг как существующие — `dataclasses.replace(Config(), поле=значение)` с явными значениями всех полей, от которых зависит тест, — и не читать окружение напрямую.
- **Коммиты**: без трейлеров `Co-Authored-By`. Осмысленные русские сообщения, как в истории репо.
- **Новые настройки — только три**: `CABINET_EMAIL`, `CABINET_PASSWORD`, `TOOL_CALL_BUDGET=4`. Всё остальное — константы в коде (TTL снимка 15 мин, протухание 6 ч, потолок результата инструмента 4000 символов, таймаут инструментов = общий таймаут клиента 30 с).
- **`GET /cabinet/admin/payments` использовать запрещено** — отдаёт данные чужих клиентов (кросс-клиентская утечка). Единственная замена при нужде — `GET /cabinet/admin/payments/search?search=<telegram_id>`.
- **Инструменты только на чтение, ни один не принимает идентификатор пользователя** — `user_id` подставляет исполнитель из тикета.
- **Механизм миграций `store.py`**: новые таблицы добавляются в `SCHEMA` (`CREATE TABLE IF NOT EXISTS`), новые колонки существующих таблиц — в кортеж `MIGRATIONS`. Ничего другого не заводить.
- **Схема вердикта** используется со `strict: true` — каждое поле обязано быть в `required`, `additionalProperties: false`.

## Формы кабинетного API — сняты с боевого v4.0.0 (2026-08-08)

Этим закрыты открытые вопросы 1-3, 6, 7. Фикстуры тестов пишутся по этим формам.

**Логин** `POST /cabinet/auth/email/login` `{email, password}` →
`{access_token, refresh_token, token_type: "bearer", expires_in: 900, user{...}, campaign_bonus}`.
**Обновление** `POST /cabinet/auth/refresh` `{refresh_token}` → та же четвёрка без
`user`; refresh-токен **ротируется**, новый надо сохранять.

**Список тикетов** `GET /cabinet/admin/tickets` — параметры `page`, `per_page`
(потолок **100**, по умолчанию 20), `status`, `priority`, `user_id`. Ответ:
`{items, total, page, pages, per_page}`. Элемент списка: `id, title, status,
priority, created_at, updated_at, closed_at, messages_count, last_message,
user{...}` — **без `messages` и без `user_id`**; сообщения берутся деталью, как
и сейчас (`main.py` уже так устроен: список → `api.ticket(id)`).

**Деталь тикета** `GET /cabinet/admin/tickets/{id}`:
`{id, title, status, priority, created_at, updated_at, closed_at,
is_reply_blocked, user{id, telegram_id, email, username, first_name, last_name},
messages[]}`.

Два отличия от webapi, оба требуют правок:
- владелец — `user.id`, а не `user_id` (заодно бесплатно приезжает `telegram_id`,
  он нужен инструментам);
- блокировка ответов — один булев `is_reply_blocked` вместо пары
  `user_reply_block_permanent` / `user_reply_block_until`.

**Сообщение тикета** — `{id, message_text, is_from_admin, has_media, media_type,
media_file_id, media_token, media_caption, media_items, created_at}`.

> **У сообщения НЕТ автора.** Ни `user_id`, ни id админа — только флаг
> `is_from_admin` (подтверждено схемой `TicketMessageResponse` в боевом образе).
> Это отменяет посылку задачи 1.1 в первоначальном виде: опознать свой ответ по
> id служебного аккаунта нельзя. См. переписанную задачу 1.1.

**Транзакции** `GET /cabinet/admin/users/{id}/transactions` — параметры `offset`,
`limit`, `transaction_type`. Периода нет: фильтр по `days` делаем у себя.

**FAQ** `GET /cabinet/info/faq` — единственный параметр `language`. Ни
`include_inactive`, ни `fallback` нет; на пустом FAQ отдаёт `[]`.

**Промогруппы** `GET /cabinet/admin/promo-groups` — `server_discount_percent`,
`traffic_discount_percent`, `device_discount_percent`, `is_default`.

**Докупки** — из детали тарифа (`traffic_topup_enabled`,
`traffic_topup_packages`, `device_price_kopeks`, `max_device_limit`), глобальный
тумблер `TRAFFIC_TOPUP_ENABLED` — в категории настроек `TRAFFIC`.

## Деплой и откат — общая процедура (для каждого этапа)

Сервис живой (wave-tg, `/opt/bedolada-support`, теневой режим `AI_REPLY_ENABLED=false`, топик 14014). Каждый этап — отдельный merge в `main`; workflow `docker.yml` соберёт `ghcr.io/airp0wer/bedolada-support:latest` и неизменяемый `sha-<короткий>`.

Выкат этапа:

```bash
cd /opt/bedolada-support
# запомнить текущий образ для отката
docker inspect bedolada-support -f '{{.Config.Image}}' > /tmp/prev-image
docker compose pull
docker compose run --rm support-ai python -m app.main --check   # до up -d
docker compose up -d && docker compose logs -f --tail 50
```

Откат этапа (одинаков для всех, если у этапа не сказано иного):

```bash
IMAGE=ghcr.io/airp0wer/bedolada-support:sha-<предыдущий> docker compose up -d
```

Состояние в `data/` переживает откаты. `.env`-переменные, добавленные этапами, старому образу не мешают — он их не читает.

## Что сознательно НЕ входит в объём

Из спеки, раздел «Что сознательно не делаем», плюс отложенное:

- **Инструменты на запись** — ничего, что меняет аккаунт, баланс, подписку.
- **MCP-сервер** — потребитель один; `app/tools/` пишется с чистыми сигнатурами, чтобы поднять MCP позже без переписывания.
- **Правки бота** — всё нужное уже есть в кабинетном API v4.0.0.
- **Автовключение боевого режима** — `AI_REPLY_ENABLED` остаётся ручным.
- **Свой образ бота** — прод сидит на релизах апстрима.
- **Заполнение FAQ бота** — отдельная задача после кода (спека: «FAQ бота заполняется отдельной задачей»).

---

## Этап 1. Транспорт кабинета и правка ворот

**Цель:** появляется `app/cabinet.py` (логин служебного аккаунта, пара токенов, перелогин), а `gate.py` учится отличать собственные кабинетные ответы сервиса от чужого админа — **до** того, как хоть один запрос пойдёт через кабинет. Это та самая ловушка спеки: кабинетный reply пишет реальный id админа, и без правки ворот сервис после первого же своего ответа занёс бы тикет в чёрный список, никогда не смог бы ответить второй раз (`MAX_AI_REPLIES=2`), а `has_human_admin_message` принял бы наш ответ за живого человека.

**Файлы:**
- Create: `app/cabinet.py`, `tests/test_cabinet.py`
- Modify: `app/gate.py`, `tests/test_gate.py`, `app/config.py`, `app/selfcheck.py`, `tests/test_selfcheck.py`, `.env.example`
- Поведение обработки тикетов **не меняется**: `bedolaga.py` пока на `X-API-Key`.

**Проверка на бою:** креды уже лежат в `/opt/bedolada-support/.env` (`CABINET_EMAIL`/`CABINET_PASSWORD`, аккаунт id 1344, роль id 6). После выката `--check` печатает новую строку `[OK] Кабинет: вход выполнен, служебный аккаунт id 1344`. Тикеты обрабатываются как раньше.

**Критерий готовности:** тесты зелёные (в т.ч. главный тест ворот), `--check` на проде показывает вход в кабинет, поведение тикетов не изменилось за сутки наблюдения в топике.

**Откат:** предыдущий sha-образ; `.env` можно не трогать.

### Задача 1.1: правка `gate.py` — ворота переживают транспорт без автора

Самый важный тест проекта — пишется первым.

**Почему не так, как задумывалось.** Первоначально предполагалось передавать в
ворота id служебного аккаунта и сравнивать с автором сообщения. Образцы с прода
показали: **кабинет автора сообщения не отдаёт вообще**. Причём молчаливое
последствие хуже, чем «признак не работает»: сейчас `_is_foreign_admin_message`
сравнивает `message.get('user_id')` с владельцем, отсутствующее поле даёт `None`,
`None != owner_id` — и **каждое** админское сообщение, включая наши собственные,
станет «чужим». Тикет уходит в чёрный список после первого же нашего ответа.

**Решение.** Автор — необязательный признак: если он есть (webapi), работает как
раньше; если его нет (кабинет), решение принимается по нашему состоянию.
Отдельный параметр `service_account_id` не нужен — от него отказываемся.

**Интерфейсы:**
- `_is_foreign_admin_message(message, owner_id, state) -> bool` — сигнатура не
  меняется, меняется тело.
- `has_human_admin_message(ticket, state) -> bool` — **добавляется параметр
  состояния**, иначе под кабинетом функция теряет смысл: без автора любое
  админское сообщение выглядит «не владельцем», и приоритет перестанет
  подниматься там, где клиент иначе останется без ответа.

- [ ] **Шаг 1: написать падающие тесты** в `tests/test_gate.py`. Ключевые случаи:

```python
def test_кабинетный_ответ_без_автора_не_выгоняет_из_тикета():
    """ГЛАВНЫЙ ТЕСТ ЭТАПА. У кабинетного сообщения нет user_id вообще.
    Наш ответ записан в состоянии — значит он наш, а не чужого админа."""
    msgs = [msg(1), msg(2, 'наш ответ', admin=True, user_id=None), msg(3, 'не помогло')]
    state = TicketState(our_message_ids=[2])
    assert decide(ticket(msgs), state, now=NOW) == ASK_LLM


def test_второй_ответ_после_кабинетного_первого_возможен():
    """Регресс на MAX_AI_REPLIES=2."""
    msgs = [msg(1), msg(2, 'наш ответ', admin=True, user_id=None), msg(3, 'а ещё вопрос')]
    state = TicketState(our_message_ids=[2])
    assert decide(ticket(msgs), state, now=NOW, max_ai_replies=2) == ASK_LLM


def test_оператор_без_автора_блокирует():
    """Сообщения нет в нашем списке — значит писали не мы."""
    msgs = [msg(1), msg(2, 'отвечаю руками', admin=True, user_id=None)]
    assert decide(ticket(msgs), TicketState(our_message_ids=[]), now=NOW) == BLACKLIST


def test_автор_если_он_есть_работает_как_раньше():
    """Совместимость с webapi: чужой админ опознаётся по id даже при живом состоянии."""
    msgs = [msg(1), msg(2, 'руками', admin=True, user_id=ADMIN)]
    assert decide(ticket(msgs), TicketState(our_message_ids=[2]), now=NOW) == BLACKLIST


def test_человек_опознан_когда_состояние_цело():
    msgs = [msg(1), msg(2, 'наш', admin=True, user_id=None), msg(3, 'оператор', admin=True, user_id=None)]
    assert has_human_admin_message(ticket(msgs), TicketState(our_message_ids=[2])) is True


def test_при_пустом_состоянии_человек_не_опознан_и_приоритет_поднимется():
    """Состояние потеряно: отличить свой потерянный ответ от оператора нечем.
    Безопасный перекос — считать, что человека нет, и поднять приоритет."""
    msgs = [msg(1), msg(2, 'админское', admin=True, user_id=None)]
    assert has_human_admin_message(ticket(msgs), TicketState()) is False
```

- [ ] **Шаг 2: убедиться, что падают** — `uv run pytest tests/test_gate.py -q`.

- [ ] **Шаг 3: реализация** в `app/gate.py`:

```python
def _is_foreign_admin_message(message: dict, owner_id: int, state: TicketState) -> bool:
    """Админское сообщение, которого писали не мы.

    Автор — признак необязательный. Webapi обезличивает наш ответ до владельца
    тикета, кабинет не отдаёт автора вовсе (`TicketMessageResponse` — только
    `is_from_admin`). Поэтому: если автор известен и это не владелец — сообщение
    точно чужое; если автора нет, решает наш список отправленных сообщений.

    При потере состояния второй признак сработает и на наши прошлые ответы —
    тикет уйдёт в чёрный список. Осознанный перекос в молчание: лучше не
    ответить, чем вклиниться в чужой разговор или ответить дважды.
    """
    if not message.get('is_from_admin'):
        return False
    author = message.get('user_id')
    if author is not None and author != owner_id:
        return True
    return message.get('id') not in state.our_message_ids


def has_human_admin_message(ticket: dict, state: TicketState) -> bool:
    """Писал ли в тикет живой человек.

    Отличается от «сообщение не наше»: обезличенный ответ чужой интеграции или
    наш собственный потерянный ответ выглядят админскими, но человека за ними
    нет. Нужно, чтобы выход из тикета не оставил клиента без ответа навсегда.

    Без автора (кабинет) человека опознаём по своему состоянию: если мы помним,
    что отвечали, то лишнее админское сообщение — чужое. Если не помним ничего,
    честно отвечаем «не знаем» — вызывающий поднимет приоритет.
    """
    owner_id = ticket.get('user_id')
    messages = [m for m in ticket.get('messages') or [] if m.get('is_from_admin')]
    if any(m.get('user_id') is not None for m in messages):
        return any(m.get('user_id') != owner_id for m in messages)
    if not state.our_message_ids:
        return False
    return any(m.get('id') not in state.our_message_ids for m in messages)
```

Вызов в `main.py:251` получает состояние: `gate.has_human_admin_message(ticket, self.store.state(ticket_id))` — состояние там уже читается строкой выше.

- [ ] **Шаг 4: тесты зелёные** — `uv run pytest -q` целиком, включая старые.
- [ ] **Шаг 5: линт и коммит** — `uv run ruff format . && uv run ruff check .`;
      `git commit -m "Ворота: автор сообщения стал необязательным признаком"`.

**Критерий готовности:** зелёный `pytest`, поведение при webapi-транспорте не
изменилось (старые тесты не правились), а сценарий «кабинетный ответ без автора»
больше не выгоняет сервис из тикета.

**Откат:** правка изолирована в `gate.py` + вызов в `main.py`, откат — revert
коммита. На проде поведение не меняется до этапа 2.

### Задача 1.2: `app/cabinet.py` — логин, пара токенов, перелогин

**Интерфейсы:**
- Produces (используют этапы 2, 3, 6):
  - `Cabinet(base_url: str, email: str, password: str, *, timeout: float = 30.0)`
  - `Cabinet.account_id -> int` — id служебного аккаунта (из ответа логина; нужен воротам)
  - `Cabinet.get(path: str, *, params: dict | None = None) -> httpx.Response` — с транспортными ретраями
  - `Cabinet.post(path: str, *, json: dict | None = None) -> httpx.Response` — с ретраями (идемпотентные POST: priority)
  - `Cabinet.post_once(path: str, *, json: dict | None = None) -> httpx.Response` — без транспортных ретраев (reply)
  - `Cabinet.close() -> None`
  - исключения `CabinetAuthError` (не пускает: пароль/роль — потеря доступа, не деградация), `CabinetThrottled` (429 на логине, выдержка с нарастанием)

- [ ] **Шаг 1: написать падающие тесты** — `tests/test_cabinet.py`:

```python
"""Транспорт кабинета на поддельном httpx: логин, refresh, перелогин, 429."""

from __future__ import annotations

import httpx
import pytest

from app.cabinet import Cabinet, CabinetAuthError, CabinetThrottled

LOGIN = '/cabinet/auth/email/login'
REFRESH = '/cabinet/auth/refresh'


def token_pair(access='a1', refresh='r1', expires=900):
    return {
        'access_token': access,
        'refresh_token': refresh,
        'token_type': 'bearer',
        'expires_in': expires,
        'user': {'id': 1344, 'email': 'ai@example.com'},
    }


def make(handler) -> Cabinet:
    cab = Cabinet('http://bot', 'ai@example.com', 'pass')
    cab.close()
    transport = httpx.MockTransport(handler)
    cab._http = httpx.Client(base_url='http://bot', transport=transport)
    cab._http_once = httpx.Client(base_url='http://bot', transport=transport)
    return cab


def test_логин_лениво_перед_первым_запросом_и_bearer_в_заголовке():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get('authorization')))
        if request.url.path == LOGIN:
            return httpx.Response(200, json=token_pair())
        return httpx.Response(200, json={'ok': True})

    cab = make(handler)
    cab.get('/cabinet/admin/tariffs')

    assert calls[0][0] == LOGIN
    assert calls[1] == ('/cabinet/admin/tariffs', 'Bearer a1')


def test_account_id_берётся_из_ответа_логина():
    def handler(request):
        return httpx.Response(200, json=token_pair())

    assert make(handler).account_id == 1344


def test_401_ведёт_к_перелогину_и_ровно_одному_повтору():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == LOGIN:
            return httpx.Response(200, json=token_pair(access=f'a{len(calls)}'))
        if request.headers.get('authorization') == 'Bearer a1':
            return httpx.Response(401)
        return httpx.Response(200, json={'ok': True})

    cab = make(handler)
    response = cab.get('/x')

    assert response.status_code == 200
    # логин, запрос(401), перелогин, повтор — и всё
    assert calls == [LOGIN, '/x', LOGIN, '/x']


def test_повторный_401_после_перелогина_не_зацикливается():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LOGIN:
            return httpx.Response(200, json=token_pair())
        return httpx.Response(401)

    assert make(handler).get('/x').status_code == 401


def test_неверный_пароль_это_потеря_доступа():
    def handler(request):
        return httpx.Response(401, json={'detail': 'invalid credentials'})

    with pytest.raises(CabinetAuthError):
        make(handler).get('/x')


def test_429_на_логине_даёт_выдержку_а_не_повторный_логин():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429)

    cab = make(handler)
    with pytest.raises(CabinetThrottled):
        cab.get('/x')
    # немедленная вторая попытка не долбит логин — выдержка ещё действует
    with pytest.raises(CabinetThrottled):
        cab.get('/x')
    assert calls == [LOGIN]


def test_протухший_access_обновляется_по_refresh_без_пароля():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == LOGIN:
            return httpx.Response(200, json=token_pair(expires=0))  # сразу протух
        if request.url.path == REFRESH:
            return httpx.Response(200, json=token_pair(access='a2'))
        return httpx.Response(200, json={'ok': True})

    cab = make(handler)
    cab.get('/x')
    cab.get('/y')

    assert calls.count(LOGIN) == 1
    assert REFRESH in calls
```

- [ ] **Шаг 2: убедиться, что падают**: `uv run pytest tests/test_cabinet.py -q` — `ModuleNotFoundError: app.cabinet`.

- [ ] **Шаг 3: реализация `app/cabinet.py`**:

```python
"""Транспорт к кабинету бедолаги: служебный аккаунт, пара токенов, перелогин.

Единственный модуль, знающий про логин и заголовок Authorization. Остальной
код получает готовые httpx.Response через get()/post()/post_once() и не
различает, каким по счёту токеном выполнен запрос.

Access живёт 15 минут, refresh — 7 дней. Протухание access лечится refresh'ем,
любой 401 на запросе — перелогином по паролю и ровно одним повтором. Пароль в
конфиге есть всегда, поэтому из протухания сервис выбирается сам. Исключение —
401/403 на самом логине: это не деградация, а потеря доступа (пароль сменили,
роль отозвали), о ней сервис обязан кричать, а не тихо ретраить.

Логин лимитирован на стороне бота (10 попыток в минуту на IP, при недоступном
Redis — наглухо). 429 и 401 различаем: на 429 — выдержка с нарастанием,
повторный логин раньше времени сам загнал бы нас в лимит.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

LOGIN_PATH = '/cabinet/auth/email/login'
REFRESH_PATH = '/cabinet/auth/refresh'

# Обновляем access заранее: 401 на границе жизни токена — лишний круг.
REFRESH_MARGIN_SEC = 60
# Выдержка после 429 на логине: старт и потолок (нарастает удвоением).
THROTTLE_START_SEC = 60
THROTTLE_MAX_SEC = 600


class CabinetAuthError(RuntimeError):
    """Кабинет не пускает по паролю: доступ потерян, тикеты не обрабатываем."""


class CabinetThrottled(RuntimeError):
    """Логин лимитирован (429): выдерживаем паузу, попыток не делаем."""


class Cabinet:
    def __init__(self, base_url: str, email: str, password: str, *, timeout: float = 30.0):
        self._email = email
        self._password = password
        self._http = httpx.Client(
            base_url=base_url.rstrip('/'), timeout=timeout, transport=httpx.HTTPTransport(retries=3)
        )
        self._http_once = httpx.Client(base_url=base_url.rstrip('/'), timeout=timeout)
        self._access = ''
        self._refresh = ''
        self._expires_at = 0.0
        self._account_id: int | None = None
        self._throttled_until = 0.0
        self._throttle_sec = THROTTLE_START_SEC

    def close(self) -> None:
        self._http.close()
        self._http_once.close()

    @property
    def account_id(self) -> int:
        """id служебного аккаунта — воротам, чтобы отличать свои ответы."""
        if self._account_id is None:
            self._login()
        return self._account_id

    # --- токены ---------------------------------------------------------

    def _apply(self, data: dict) -> None:
        self._access = data['access_token']
        self._refresh = data['refresh_token']
        self._expires_at = time.monotonic() + max(0, data.get('expires_in', 0) - REFRESH_MARGIN_SEC)

    def _login(self) -> None:
        now = time.monotonic()
        if now < self._throttled_until:
            raise CabinetThrottled(f'логин лимитирован ещё {self._throttled_until - now:.0f} с')

        response = self._http_once.post(LOGIN_PATH, json={'email': self._email, 'password': self._password})
        if response.status_code == 429:
            self._throttled_until = now + self._throttle_sec
            self._throttle_sec = min(self._throttle_sec * 2, THROTTLE_MAX_SEC)
            raise CabinetThrottled(f'логин ответил 429, выдержка {self._throttled_until - now:.0f} с')
        if response.status_code in (401, 403):
            raise CabinetAuthError(f'кабинет не пускает служебный аккаунт: HTTP {response.status_code}')
        response.raise_for_status()

        self._throttle_sec = THROTTLE_START_SEC
        data = response.json()
        self._apply(data)
        self._account_id = data['user']['id']
        log.info('Кабинет: вход выполнен, служебный аккаунт id %s', self._account_id)

    def _ensure_token(self) -> None:
        if self._access and time.monotonic() < self._expires_at:
            return
        if self._refresh:
            response = self._http_once.post(REFRESH_PATH, json={'refresh_token': self._refresh})
            if response.status_code < 400:
                self._apply(response.json())
                return
            log.info('Кабинет: refresh не принят (HTTP %s), логинюсь по паролю', response.status_code)
        self._login()

    # --- запросы --------------------------------------------------------

    def _request(self, method: str, path: str, *, params=None, json=None, retries: bool) -> httpx.Response:
        self._ensure_token()
        client = self._http if retries else self._http_once
        response = client.request(
            method, path, params=params, json=json, headers={'Authorization': f'Bearer {self._access}'}
        )
        if response.status_code != 401:
            return response
        # Сессию отозвали или access умер раньше срока: перелогин, один повтор.
        # Для reply это безопасно: 401 отбивается до записи сообщения в БД.
        self._login()
        return client.request(
            method, path, params=params, json=json, headers={'Authorization': f'Bearer {self._access}'}
        )

    def get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        return self._request('GET', path, params=params, retries=True)

    def post(self, path: str, *, json: dict | None = None) -> httpx.Response:
        return self._request('POST', path, json=json, retries=True)

    def post_once(self, path: str, *, json: dict | None = None) -> httpx.Response:
        """Неидемпотентные вызовы (ответ клиенту): без транспортных ретраев."""
        return self._request('POST', path, json=json, retries=False)
```

- [ ] **Шаг 4: тесты зелёные**: `uv run pytest tests/test_cabinet.py -q`.
- [ ] **Шаг 5: сверить с продом форму ответа логина** (однократно, руками): выполнить с wave-tg логин служебного аккаунта и убедиться, что поля называются `access_token`/`refresh_token`/`expires_in`/`user.id`:

```bash
docker run --rm --network remnawave-bot_default curlimages/curl -s \
  -X POST http://remnawave_bot:8080/cabinet/auth/email/login \
  -H 'content-type: application/json' \
  -d '{"email":"<CABINET_EMAIL>","password":"<CABINET_PASSWORD>"}' | head -c 400
```

Если поля другие — поправить `_apply()`/`_login()` и `token_pair()` в тестах до коммита.

- [ ] **Шаг 6: линт и коммит**: `git add app/cabinet.py tests/test_cabinet.py && git commit -m "cabinet: транспорт служебного аккаунта — логин, refresh, перелогин, выдержка на 429"`.

### Задача 1.3: конфиг и строка в `--check`

- [ ] **Шаг 1: тест** — в `tests/test_selfcheck.py`:

```python
def test_проверка_кабинета_показывает_id_аккаунта():
    class FakeCabinet:
        account_id = 1344

    step = selfcheck._check_cabinet(FakeCabinet())
    assert step.status == OK
    assert '1344' in step.detail


def test_недоступный_кабинет_это_сбой():
    class BrokenCabinet:
        @property
        def account_id(self):
            raise ConnectionError('нет связи')

    assert selfcheck._check_cabinet(BrokenCabinet()).status == FAIL
```

- [ ] **Шаг 2: убедиться, что падают** (`AttributeError: _check_cabinet`).
- [ ] **Шаг 3: реализация**:
  - `app/config.py`: добавить поля
    ```python
    # Служебный аккаунт кабинета (роль «AI Support»). Заменит BEDOLAGA_API_TOKEN.
    cabinet_email: str = field(default_factory=lambda: os.environ.get('CABINET_EMAIL', ''))
    cabinet_password: str = field(default_factory=lambda: os.environ.get('CABINET_PASSWORD', ''))
    ```
    (`require()` пока не трогать — токен ещё обязателен, кабинет станет обязательным на этапе 2.)
  - `app/selfcheck.py`: функция `_check_cabinet(cabinet) -> Step`:
    ```python
    def _check_cabinet(cabinet) -> Step:
        try:
            account_id = cabinet.account_id
        except Exception as error:
            return Step('Кабинет', FAIL, f'{type(error).__name__}: {error}')
        return Step('Кабинет', OK, f'вход выполнен, служебный аккаунт id {account_id}')
    ```
    В `run()` создать `Cabinet(cfg.bedolaga_url, cfg.cabinet_email, cfg.cabinet_password)` и добавить шаг после «API бота» (и `cabinet.close()` в `finally`). Если `cfg.cabinet_email` пуст — шаг со статусом `WARN` «CABINET_EMAIL/CABINET_PASSWORD не заданы — переезд на кабинет не настроен».
  - `.env.example`: блок
    ```ini
    # --- Служебный аккаунт кабинета (роль «AI Support») ---
    # Регистрируется по email в кабинете бота, см. docs/deploy.md.
    CABINET_EMAIL=
    CABINET_PASSWORD=
    ```
- [ ] **Шаг 4: все тесты зелёные**: `uv run pytest -q`.
- [ ] **Шаг 5: линт, коммит**: `git commit -m "config+check: креды служебного аккаунта и проверка входа в кабинет"`.
- [ ] **Шаг 6: выкат этапа 1** по общей процедуре, проверить строку «Кабинет» в `--check` на проде.

---

## Этап 2. Переезд `bedolaga.py` на кабинетные ручки

**Цель:** весь трафик к бедолаге идёт под служебным аккаунтом (RBAC, 16 прав) вместо неразграниченного `X-API-Key`. Сигнатуры методов `Bedolaga` сохраняются — `main.py`, `context.py`, `kb.py` почти не меняются. Исключение — медиа: в кабинете `media_token` приходит внутри ответа тикета, файл берётся через `GET /cabinet/media/{file_id}?token=…` (про это легко забыть и молча потерять чтение скриншотов).

**Файлы:**
- Modify: `app/bedolaga.py`, `tests/test_bedolaga.py`, `app/context.py`, `tests/test_context.py`, `app/main.py`, `app/config.py`, `app/selfcheck.py`, `app/alerts.py`, `tests/test_alerts.py`, `.env.example`, `docs/deploy.md` (раздел про токен — пометить устаревшим, полная переработка на этапе 8)

**Проверка на бою:**
1. `--check`: «API бота» теперь ходит через кабинет — активные тикеты видны.
2. Сутки теневой работы: тикеты разбираются, черновики в топике, скриншоты читаются (найти тикет с фото и убедиться по черновику, что модель видела картинку).
3. **Ручная проверка правки ворот**: создать тестовый тикет со своего клиентского аккаунта; руками, под кредами служебного аккаунта (curl из задачи 1.2, `POST /cabinet/admin/tickets/{id}/reply`), написать в него ответ, добавив id сообщения в состояние сервиса нельзя — поэтому ожидание: сервис увидит «наш» user_id без записи в `our_message_ids` и уйдёт в BLACKLIST (перекос в молчание, поведение по тесту `test_потеря_состояния_с_кабинетным_ответом...`). Затем проверить противоположное: дождаться, когда сервис сам обработает тестовый тикет в тени — тикет не должен попадать в `handover` от собственных прошлых ответов (смотреть `audit`: `sqlite3 data/state.db "SELECT action, reason FROM audit ORDER BY id DESC LIMIT 20"`).

**Критерий готовности:** сутки в тени без регрессий: количество разобранных тикетов и эскалаций сопоставимо с прошлой неделей, ни одного ложного `handover` («человек в тикете») на собственные ответы, скриншоты доходят до модели.

**Откат:** предыдущий sha-образ — он снова читает `BEDOLAGA_API_TOKEN`, поэтому токен из `.env` **не удалять до этапа 8**.

### Задача 2.1: снять формы кабинетных ответов с прода

Разовая ручная работа до кода: тесты пишутся «на записанных ответах», записать их надо с боевого v4.0.0 (локальный чекаут v3.65.1 — только ориентир).

- [ ] **Шаг 1**: логином из задачи 1.2 получить access-токен и снять (обезличив) по одному образцу:
  - `GET /cabinet/admin/tickets?page=1&per_page=1&status=open` — обёртка списка (ожидается `items`/`total`), поля тикета и сообщений (включая поля медиа: `media_file_id`? `media_token`?);
  - `GET /cabinet/admin/tickets/{id}` — тикет с сообщениями;
  - `POST /cabinet/admin/tickets/{id}/reply` на **тестовом** тикете — форма ответа (id сообщения);
  - `POST /cabinet/admin/tickets/{id}/priority` — форма тела (`{"priority": "high"}`?);
  - `GET /cabinet/admin/users/{id}` — снимок аккаунта (структура `subscription`, `promo_group`, `telegram_id`);
  - `GET /cabinet/info/faq` — обёртка и параметр языка;
  - `GET /cabinet/media/{file_id}?token=…` — выдача файла.
- [ ] **Шаг 2**: зафиксировать образцы в фикстурах-хелперах нового `tests/test_bedolaga.py` (инлайн-JSON, стиль репо). Если какой-то путь/поле расходится с планом — правится план тестов ниже, а не прод.

### Задача 2.2: `Bedolaga` поверх `Cabinet`

**Интерфейсы:**
- Consumes: `Cabinet.get/post/post_once/account_id` (задача 1.2).
- Produces (сигнатуры сохраняются): `Bedolaga(cabinet: Cabinet)`; `active_tickets() -> list[dict]`, `ticket(id) -> dict`, `reply(id, text) -> int`, `set_priority(id, priority) -> None`, `user(id) -> dict | None`, `faq_pages(language='ru') -> list[dict]`, `download_media(file_id: str, token: str) -> bytes | None` (изменение: добавился `token`). Метод `message_media()` удаляется (медиаданные теперь в самом сообщении тикета), `user_by_telegram_id()` удаляется (никем не используется).

- [ ] **Шаг 1: переписать тесты `tests/test_bedolaga.py` на кабинетные ручки** (падающие). Хелпер:

```python
def client(handler) -> Bedolaga:
    cab = Cabinet('http://bot', 'e', 'p')
    cab.close()
    transport = httpx.MockTransport(handler)
    cab._http = httpx.Client(base_url='http://bot', transport=transport)
    cab._http_once = cab._http
    return Bedolaga(cab)
```

Обработчик отвечает на `LOGIN_PATH` парой токенов (как в test_cabinet), на `/cabinet/admin/tickets` — страницами `{'items': [...], 'total': N}` (форму подставить из задачи 2.1). Случаи (переписать существующие + новые):
  - одна неполная страница — по одному запросу на каждый статус (`open`, `pending`);
  - бэклог больше страницы дочитывается (пагинация `page`/`per_page`, потолок страницы — из прода, ожидается `le=100` — константу `PAGE_LIMIT` поправить);
  - сломанная пагинация упирается в `MAX_PAGES` с warning;
  - `reply` шлёт `{'message_text': ...}` и возвращает id сообщения;
  - `reply` идёт через `post_once` (без транспортных ретраев) — проверить, что использован клиент без ретраев, как сейчас проверяется разделение `_http`/`_http_once`;
  - `user()` возвращает None на 404;
  - `faq_pages()` ходит в `/cabinet/info/faq` и отдаёт список страниц;
  - `download_media(file_id, token)` передаёт `?token=…` и возвращает байты; None на 4xx.
- [ ] **Шаг 2: убедиться, что падают.**
- [ ] **Шаг 3: реализация** — переписать `app/bedolaga.py`: конструктор принимает `Cabinet`; docstring модуля обновить (политика ретраев та же: чтение можно, reply нельзя — теперь это разделение живёт в `Cabinet.post_once`); пути:

| Метод | Ручка |
|---|---|
| `active_tickets` | `GET /cabinet/admin/tickets?status=&page=&per_page=` (два прохода: `open`, `pending`; дубли схлопываются по id) |
| `ticket` | `GET /cabinet/admin/tickets/{id}` |
| `reply` | `POST /cabinet/admin/tickets/{id}/reply`, тело `{'message_text': text}`, через `post_once` |
| `set_priority` | `POST /cabinet/admin/tickets/{id}/priority` |
| `user` | `GET /cabinet/admin/users/{id}` |
| `faq_pages` | `GET /cabinet/info/faq` |
| `download_media` | `GET /cabinet/media/{file_id}` с `params={'token': token}` |

Свойство `service_account_id` не заводим: воротам оно не нужно (кабинет автора сообщения не отдаёт), а для логов и `--check` есть `cabinet.account_id`.

- [ ] **Шаг 4: тесты зелёные.**
- [ ] **Шаг 5: коммит**: `git commit -m "bedolaga: переезд на кабинетные ручки поверх cabinet.py"`.

### Задача 2.3: медиа в `context.py` по новой механике

- [ ] **Шаг 1: тесты** — в `tests/test_context.py` переписать случаи `collect_images` под новую механику: сообщение тикета несёт поля медиа (`media_file_id`, `media_token` — имена сверить в задаче 2.1); `collect_images` больше не зовёт `message_media`, а читает поля сообщения и качает `api.download_media(file_id, token)`. Случаи: скриншот скачивается и попадает в data-URL; сообщение без токена пропускается с warning (не падает); лимиты размера/формата — как раньше.
- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** в `app/context.py::collect_images` — заменить блок `api.message_media(...)` на чтение полей из `message`, вызов `api.download_media(message['media_file_id'], message['media_token'])` (точные имена — из фикстур 2.1).
- [ ] **Шаг 4: зелёные; коммит** `"context: скриншоты через кабинетный media_token"`.

### Задача 2.4: обвязка — `main.py`, конфиг, алерт о потере доступа

- [ ] **Шаг 1: тесты**:
  - в `tests/test_service.py`: тикет в кабинетной форме (у сообщений нет автора), где наш ответ записан в `our_message_ids`, не уходит в handover — тот же случай, что и главный тест ворот, но через `Service.handle`;
  - `CabinetAuthError` в цикле: `run_once`, поймав её, не продолжает перебор тикетов и зовёт `notifier.problem` с первого раза (не после трёх);
  - в `tests/test_alerts.py`: новый вид сбоя с порогом 1 (см. ниже) алертит с первой неудачи.
- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация**:
  - `app/alerts.py`: `failure()` получает необязательный параметр `threshold: int | None = None` — порог для этого вида вместо общего (`AUTH = 'auth'`, `LABELS[AUTH] = 'доступ в кабинет'`);
  - `app/main.py`:
    - конструктор: `self.cabinet = Cabinet(cfg.bedolaga_url, cfg.cabinet_email, cfg.cabinet_password)`, `self.api = Bedolaga(self.cabinet)`; `close()` закрывает и кабинет;
    - `handle()` и `_hand_over()` менять не нужно: ворота уже разбирают сообщения без автора по состоянию (этап 1), а `_hand_over` учитывает незакрытое намерение ответа;
    - `run_once()`: обёртка
      ```python
      try:
          tickets = self.api.active_tickets()
      except CabinetAuthError as error:
          # Потеря доступа, а не деградация: кричим с первого раза и молчим.
          self.alerts.failure(AUTH, str(error), now=time.monotonic(), threshold=1)
          return
      ```
      (и `alerts.success(AUTH, ...)` при удаче; `CabinetThrottled` наружу не алертится — это выдержка, цикл просто пропускается с log.warning);
  - `app/config.py::require()`: вместо `BEDOLAGA_API_TOKEN` обязательными становятся `CABINET_EMAIL` и `CABINET_PASSWORD`; поле `bedolaga_token` удалить;
  - `app/selfcheck.py`: убрать создание `Bedolaga(cfg.bedolaga_url, cfg.bedolaga_token)` — теперь `Bedolaga(cabinet)` от уже созданного кабинета;
  - `.env.example`: удалить `BEDOLAGA_API_TOKEN`, к `BEDOLAGA_API_URL` комментарий «база кабинетного API бота».
- [ ] **Шаг 4: все тесты зелёные** (`uv run pytest -q`), линт.
- [ ] **Шаг 5: коммит** `"main: весь доступ к бедолаге через служебный аккаунт кабинета"`.
- [ ] **Шаг 6: выкат этапа 2**, проверка на бою по описанию этапа (включая ручную проверку ворот на тестовом тикете).

---

## Этап 3. Живой снимок: хранилище и `live.py`

**Цель:** сервис умеет собирать снимок каталога тарифов, настроек (категориями) и промогрупп, хранить его в SQLite (last-known-good) и рендерить в компактный текст. В промпт снимок пойдёт на этапе 4 — здесь он только собирается и виден в `--check`. Это позволяет выкатить и проверить разбор данных на проде, не меняя ответы модели.

**Файлы:**
- Create: `app/live.py`, `tests/test_live.py`
- Modify: `app/store.py`, `tests/test_store.py`, `app/main.py`, `app/selfcheck.py`, `tests/test_selfcheck.py`

**Проверка на бою:** `--check` печатает строку «Живой снимок: N тарифов, M настроек, возраст 0 мин» и первые строки рендера. В снимке видны все **пять** тарифов, включая `FAMILY` (600 ГБ, 6 устройств) — то, чего не хватало тикету #84. `BASE` отрисован как «докупка трафика: нет» несмотря на `allow_traffic_topup=true`. Триал — 100 ГБ (из тарифа), а не 10 (из `TRIAL_TRAFFIC_LIMIT_GB`).

**Критерий готовности:** `--check` на проде показывает валидный снимок с пятью тарифами и верными выводами по двум известным граблям; таблица `kv` в `data/state.db` содержит снимок; тикеты обрабатываются как раньше.

**Откат:** предыдущий sha-образ. Таблица `kv` в базе старому коду не мешает.

### Задача 3.1: таблица ключ-значение в `store.py`

**Интерфейсы:**
- Produces: `Store.kv_set(key: str, value: str) -> None`; `Store.kv_get(key: str) -> str | None`.

- [ ] **Шаг 1: тесты** в `tests/test_store.py`:

```python
def test_kv_хранит_и_отдаёт_значение():
    store = Store(':memory:')
    store.kv_set('live_snapshot', '{"a": 1}')
    assert store.kv_get('live_snapshot') == '{"a": 1}'


def test_kv_перезаписывает_по_ключу():
    store = Store(':memory:')
    store.kv_set('k', 'старое')
    store.kv_set('k', 'новое')
    assert store.kv_get('k') == 'новое'


def test_kv_отсутствующий_ключ_даёт_none():
    assert Store(':memory:').kv_get('нет') is None


def test_kv_переживает_переоткрытие_базы(tmp_path):
    path = str(tmp_path / 'state.db')
    Store(path).kv_set('k', 'v')
    assert Store(path).kv_get('k') == 'v'
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** — в `SCHEMA` (существующий механизм для новых таблиц):

```sql
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

и методы:

```python
    # --- ключ-значение (живой снимок и прочие мелочи) --------------------

    def kv_set(self, key: str, value: str) -> None:
        self._db.execute(
            'INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, value, _now()),
        )

    def kv_get(self, key: str) -> str | None:
        row = self._db.execute('SELECT value FROM kv WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else None
```

- [ ] **Шаг 4: зелёные; коммит** `"store: таблица ключ-значение под живой снимок"`.

### Задача 3.2: снять образцы каталога/настроек/промогрупп с прода

- [ ] **Шаг 1**: тем же способом, что в 2.1, снять и обезличить:
  - `GET /cabinet/admin/tariffs` (обёртка списка, поля: `id`, `name`, `traffic_limit_gb`, `device_limit`, `is_active`, `is_trial_available`, `allow_traffic_topup`, `device_price_kopeks`, `max_device_limit` — имена сверить по факту);
  - `GET /cabinet/admin/tariffs/{id}` — `period_prices`;
  - `GET /cabinet/admin/settings` — форма элемента (`key`, `value`, `category`, `is_secret`), категории `REFERRAL`, `TRIAL`, `TARIFF`, `TRAFFIC`;
  - `GET /cabinet/admin/promo-groups` — `id`, `name`, `is_default`, поля процентов.
- [ ] **Шаг 2**: перенести в фикстуры `tests/test_live.py` (инлайн-JSON с реальными именами полей, значения выдуманные, но противоречия из спеки сохранить: `BASE` с `allow_traffic_topup=true` при выключенном глобальном топапе; `TRIAL_TRAFFIC_LIMIT_GB=10` при тарифном триале 100 ГБ).

### Задача 3.3: `app/live.py` — сборка, выводы, рендер, TTL

**Интерфейсы:**
- Consumes: `Cabinet.get`, `Store.kv_get/kv_set`.
- Produces (используют этапы 4 и `--check`):
  - `Snapshot` (frozen dataclass): `text: str` (рендер для промпта), `amounts: frozenset[tuple[str, int]]` (пары «валюта, минорные единицы» для guard), `fetched_at: datetime`, метод `age_sec(now) -> float`
  - `Live(cabinet: Cabinet, store: Store)`; `Live.snapshot(now: datetime) -> Snapshot | None` — лениво обновляет по TTL, при неудаче отдаёт last-known-good, `None` — если снимка не было никогда
  - константы `TTL_SEC = 900`, `STALE_SEC = 6 * 3600`, `Live.is_stale(snapshot, now) -> bool`
  - `Live.last_error: str | None` — причина последней неудачи обновления (сюда же попадает «пустой каталог тарифов»); `None` после удачного обновления. На этапе 4 `main` превращает её в алерт
  - `SNAPSHOT_KEY = 'live_snapshot'` — ключ в `kv`

- [ ] **Шаг 1: тесты** `tests/test_live.py` (на фикстурах из 3.2, кабинет — MockTransport как в test_cabinet). Обязательные случаи из спеки:

```python
def test_каталог_собирается_со_всеми_активными_тарифами():
    # в фикстуре 5 тарифов; в рендере видны все 5, включая FAMILY

def test_цены_по_периодам_берутся_из_детальной_ручки():
    # список period_prices не отдаёт — на каждый тариф уходит запрос детали

def test_неактивный_тариф_не_попадает_в_рендер():

def test_докупка_трафика_нет_если_глобально_выключена():
    """Грабля из жизни: у BASE allow_traffic_topup=true, но
    traffic_topup_enabled=false — рендерится «докупка трафика: нет»."""

def test_докупка_трафика_нет_на_безлимите_и_без_пакетов():

def test_докупка_устройств_по_цене():
    # device_price_kopeks = 0 → «нет»; >0 → «100 ₽ за штуку», max 0 → «без ограничений»

def test_триал_берётся_из_тарифа_а_не_из_настройки():
    """TRIAL_TRAFFIC_LIMIT_GB=10 в настройках, у триального тарифа 100 ГБ —
    в рендере 100."""

def test_сырые_флаги_не_попадают_в_рендер():
    # ни 'allow_traffic_topup', ни 'TRIAL_TRAFFIC_LIMIT_GB' нет в тексте

def test_секретные_настройки_пропускаются():
    # is_secret=true не попадает ни в рендер, ни в amounts

def test_настройки_берутся_категориями():
    # ключ вне REFERRAL/TRIAL/TARIFF/TRAFFIC в снимок не попадает

def test_промогруппы_в_снимке_с_процентами():

def test_amounts_собираются_из_цен_и_порогов():
    # цена 249 ₽ за 30 дней → ('RUB', 24900) в snapshot.amounts

def test_ttl_не_дёргает_api_раньше_срока():

def test_неудача_обновления_отдаёт_последний_удачный():

def test_снимок_переживает_перезапуск_через_store():
    # новый Live с тем же Store отдаёт снимок без сети

def test_пустой_каталог_не_валидный_снимок():
    """Ноль активных тарифов — не снимок: остаёмся на last-known-good,
    last_error заполнен (алерт по нему навешивает main на этапе 4)."""

def test_last_error_сбрасывается_после_удачного_обновления():

def test_отметка_времени_в_рендере():
    # 'снимок 14:20 UTC' в первой строке

def test_is_stale_после_шести_часов():
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация `app/live.py`.** Скелет:

```python
"""Живой снимок каталога тарифов и настроек бедолаги.

Снимок содержит выводы, а не сырые флаги: если бедолага умеет посчитать ответ
сама — берём её ответ. Два живых примера, оба уже стреляли:
- триал: TRIAL_TRAFFIC_LIMIT_GB перекрывается тарифом с is_trial_available;
- докупка трафика: allow_traffic_topup=true стоит у всех тарифов, включая те,
  где докупки нет — истина в traffic_topup_enabled && пакеты && не безлимит.
Модель не должна видеть сырые поля: на них она пообещает то, чего нет.

TTL и срок протухания — константы: их не под что крутить на установке.
"""

TTL_SEC = 900
STALE_SEC = 6 * 3600
SNAPSHOT_KEY = 'live_snapshot'
SETTINGS_CATEGORIES = frozenset({'REFERRAL', 'TRIAL', 'TARIFF', 'TRAFFIC'})


@dataclass(frozen=True)
class Snapshot:
    text: str
    amounts: frozenset[tuple[str, int]]
    fetched_at: datetime

    def age_sec(self, now: datetime) -> float:
        return (now - self.fetched_at).total_seconds()


class Live:
    def __init__(self, cabinet, store):
        self._cabinet = cabinet
        self._store = store
        self._snapshot: Snapshot | None = self._load()

    def snapshot(self, now: datetime) -> Snapshot | None:
        if self._snapshot is not None and self._snapshot.age_sec(now) < TTL_SEC:
            return self._snapshot
        try:
            fresh = self._fetch(now)
        except Exception:
            log.warning('Живой снимок: обновление не удалось, работаю на последнем удачном', exc_info=True)
            return self._snapshot
        self._snapshot = fresh
        self._save(fresh)
        return fresh

    def is_stale(self, snapshot: Snapshot, now: datetime) -> bool:
        return snapshot.age_sec(now) > STALE_SEC
```

`_fetch(now)`: список тарифов → детали по каждому активному (`period_prices`) → настройки (фильтр категорий, пропуск `is_secret`) → промогруппы; если активных тарифов ноль — `raise RuntimeError('пустой каталог тарифов — не валидный снимок')`. `_render()` — компактный текст по образцу спеки (тарифы с ценами по периодам, докупки, триал, рефералка, вывод средств). `_amounts()` — все цены/пороги в `('RUB', копейки)` (конвертация из `*_kopeks` — как есть, из рублей — умножением на 100). Сериализация в `kv` — JSON `{'text': ..., 'amounts': [[cur, minor], ...], 'fetched_at': iso}`.

Выводы (ровно по спеке):

```python
def _traffic_topup_line(tariff: dict, topup_enabled: bool, packages: list) -> str:
    unlimited = not tariff.get('traffic_limit_gb')
    if not (topup_enabled and packages and not unlimited):
        return 'докупка трафика: нет'
    sizes = ', '.join(str(p) for p in packages)
    return f'докупка трафика: пакеты {sizes} ГБ'


def _device_topup_line(tariff: dict) -> str:
    price = tariff.get('device_price_kopeks') or 0
    if price == 0:
        return 'докупка устройств: нет'  # цена и есть выключатель
    cap = tariff.get('max_device_limit') or 0
    limit = 'без ограничений' if cap == 0 else f'до {cap}'
    return f'докупка устройств: {price // 100} ₽ за штуку, {limit}'
```

(Точное имя источника «глобальный топап включён» и списка пакетов — из фикстур 3.2; см. открытые вопросы.)

- [ ] **Шаг 4: зелёные; коммит** `"live: снимок каталога, настроек и промогрупп с выводами вместо сырых флагов"`.

### Задача 3.4: снимок в `Service` и в `--check`

- [ ] **Шаг 1: тесты**: `tests/test_selfcheck.py` — `_check_live(live, now)` печатает возраст и число тарифов; FAIL, если снимка нет вообще; WARN, если старше `STALE_SEC`. `tests/test_service.py` — конструктор через `object.__new__` получает `self.live`, ничего не ломается (снимок пока никуда не передаётся).
- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация**: `Service.__init__`: `self.live = Live(self.cabinet, self.store)`; `selfcheck.run()`: шаг «Живой снимок» после «Кабинет» (дёргает `live.snapshot(now)` по-настоящему). Неудача сборки снимка на проде видна из FAIL этого шага.
- [ ] **Шаг 4: зелёные; линт; коммит** `"check: живой снимок с возрастом в самопроверке"`.
- [ ] **Шаг 5: выкат этапа 3**, проверить `--check` на проде (пять тарифов, FAMILY, докупки, триал).

---

## Этап 4. Снимок в промпте и сверка сумм в guard

**Цель:** модель видит живые данные (и может ответить на #84 про FAMILY и на #83 про вывод рефералки), а guard вместо «любая сумма → оператор» сверяет суммы с белым списком: снимок + `kb/policy.md`. Суммы инструментов добавятся на этапе 6.

**Файлы:**
- Modify: `app/guard.py`, `tests/test_guard.py` *(новый файл — сейчас тесты guard живут в `test_llm.py`, новые кейсы сумм выносим отдельно)*, `app/kb.py`, `tests/test_kb.py`, `app/prompt.py`, `app/llm.py`, `tests/test_llm.py`, `app/main.py`, `tests/test_service.py`, `kb/README.md`

**Проверка на бою (тень):** дождаться/спровоцировать тестовым тикетом вопрос о тарифах — в черновике точные лимиты и цены из снимка, и такой черновик больше не отбраковывается «в ответе сумма»; вопрос «когда вывод рефералки» — черновик со статусом фичи из настроек. Причины эскалаций в топике: «сумма N не подтверждена данными» появляется только на суммах не из снимка.

**Критерий готовности:** таблица случаев сумм из спеки зелёная тестами; на проде черновики называют цены тарифов без отбраковки; сумма «из воздуха» по-прежнему эскалирует.

**Откат:** предыдущий sha-образ (вернётся безусловное «деньги → оператор»).

### Задача 4.1: нормализация и белый список сумм в `guard.py`

**Интерфейсы:**
- Produces: тип `Amount = tuple[str, int]` (код валюты, минорные единицы); `parse_amounts(text: str) -> set[Amount]`; `Guard.reject(reply: str, *, verified_amounts: frozenset[Amount] = frozenset()) -> str | None`. Существующие вызовы без `verified_amounts` сохраняют сегодняшнее поведение (пустой список — ни одна сумма не сверена).

- [ ] **Шаг 1: тесты** — `tests/test_guard.py`, таблица из спеки:

```python
"""Сверка сумм: пропускаем только числа, подтверждённые данными."""

from __future__ import annotations

from app.guard import Guard, parse_amounts

RUB_249 = ('RUB', 24900)
RUB_10K = ('RUB', 1000000)


def test_нормализация_сумм_в_минорные_единицы():
    assert parse_amounts('цена 249 ₽') == {RUB_249}
    assert parse_amounts('249руб') == {RUB_249}
    assert parse_amounts('249.00 ₽') == {RUB_249}
    assert parse_amounts('10 000 ₽') == {RUB_10K}
    assert parse_amounts('249 USDT') == {('USDT', 24900)}
    assert parse_amounts('$5') == {('USD', 500)}


def test_сумма_из_снимка_проходит():
    guard = Guard()
    assert guard.reject('Тариф стоит 249 ₽ в месяц.', verified_amounts=frozenset({RUB_249})) is None


def test_та_же_сумма_в_другом_написании_проходит():
    guard = Guard()
    verified = frozenset({RUB_249})
    assert guard.reject('Спишется 249.00 ₽.', verified_amounts=verified) is None
    assert guard.reject('Это 249 руб.', verified_amounts=verified) is None


def test_сумма_из_воздуха_не_проходит():
    reason = Guard().reject('Доплатите 1000 ₽.', verified_amounts=frozenset({RUB_249}))
    assert reason is not None and 'не подтверждена' in reason


def test_валюта_не_игнорируется():
    """249 ₽ в белом списке не отбеливает ни 249 USDT, ни $249."""
    verified = frozenset({RUB_249})
    assert Guard().reject('Выплата 249 USDT.', verified_amounts=verified) is not None
    assert Guard().reject('Это $249.', verified_amounts=verified) is not None


def test_без_белого_списка_любая_сумма_эскалирует():
    """Сегодняшнее поведение — дефолт: вызовы без verified_amounts не меняются."""
    assert Guard().reject('Стоимость 300 руб.') is not None


def test_стоп_слова_работают_поверх_совпавшей_суммы():
    """«верну деньги» — про намерение, а не про число."""
    guard = Guard()
    reason = guard.reject('Верну деньги, 249 ₽.', verified_amounts=frozenset({RUB_249}))
    assert reason is not None and 'стоп-слово' in reason


def test_числа_без_валюты_не_трогаются():
    assert Guard().reject('На тарифе 300 ГБ и 3 устройства.') is None
```

- [ ] **Шаг 2: падают** (`ImportError: parse_amounts`).
- [ ] **Шаг 3: реализация** в `app/guard.py`:

```python
Amount = tuple[str, int]  # (код валюты, минорные единицы: копейки, центы)

# Порядок важен: 'usdt' должен матчиться раньше 'usd'.
_CURRENCY_CODES = (
    ('₽', 'RUB'), ('руб', 'RUB'), ('rub', 'RUB'),
    ('usdt', 'USDT'), ('usd', 'USD'), ('$', 'USD'),
    ('eur', 'EUR'), ('€', 'EUR'),
)


def _currency_code(raw: str) -> str:
    lowered = raw.lower()
    for prefix, code in _CURRENCY_CODES:
        if lowered.startswith(prefix):
            return code
    return raw.upper()


def _to_minor(raw: str) -> int | None:
    """'10 000' → 1000000, '249.50' → 24950. Копейки, не рубли."""
    digits = raw.replace(' ', '').replace(' ', '').replace(',', '.')
    if digits.count('.') > 1:  # 1.000.000 — точки-разделители тысяч
        digits = digits.replace('.', '')
    try:
        return round(float(digits) * 100)
    except ValueError:
        return None


# MONEY переписывается на именованные группы, чтобы из совпадения можно было
# достать пару «число + валюта» (сейчас он только сигналит «есть деньги»).
MONEY = re.compile(
    rf'(?:(?P<num1>\d[\d\s.,]*)\s*(?P<cur1>{CURRENCY})|(?P<cur2>{CURRENCY})\s*(?P<num2>\d[\d\s.,]*))',
    re.IGNORECASE,
)


def _match_amount(match: re.Match) -> Amount | None:
    number = match.group('num1') or match.group('num2')
    currency = match.group('cur1') or match.group('cur2')
    minor = _to_minor(number)
    return None if minor is None else (_currency_code(currency), minor)


def parse_amounts(text: str) -> set[Amount]:
    return {amount for m in MONEY.finditer(text) if (amount := _match_amount(m)) is not None}
```

В `Guard.reject()` блок про деньги заменяется:

```python
        for match in MONEY.finditer(reply):
            amount = _match_amount(match)
            if amount is None or amount not in verified_amounts:
                return f'сумма {match.group(0).strip()} не подтверждена данными'
```

`mentions_money()` не трогается (её использует `--check`). Проверить, что старые тесты в `test_llm.py` зелёные без правок — дефолт `verified_amounts=frozenset()` сохраняет прежнее поведение.

- [ ] **Шаг 4: зелёные (`uv run pytest -q` целиком); коммит** `"guard: сверка сумм с белым списком — валюта плюс число в минорных единицах"`.

### Задача 4.2: `kb/policy.md` — подтверждённые суммы владельца

**Интерфейсы:**
- Produces: `app.kb.policy_amounts(kb_dir: str) -> set[Amount]` — суммы из машиночитаемых секций `<!-- amounts -->…<!-- /amounts -->` всех файлов `policy.md` под `kb_dir`.

- [ ] **Шаг 1: тесты** в `tests/test_kb.py`:

```python
def test_policy_amounts_читает_секцию(tmp_path):
    (tmp_path / 'policy.md').write_text(
        '# Политика\n\n## Подтверждённые суммы\n\n'
        '<!-- amounts -->\n10000 ₽ — минимум для партнёрской выплаты в USDT\n<!-- /amounts -->\n',
        encoding='utf-8',
    )
    assert policy_amounts(str(tmp_path)) == {('RUB', 1000000)}


def test_текст_вне_секции_не_даёт_сумм(tmp_path):
    (tmp_path / 'policy.md').write_text('Возвраты делаем 500 ₽.\n', encoding='utf-8')
    assert policy_amounts(str(tmp_path)) == set()


def test_policy_в_подкаталоге_тоже_читается(tmp_path):
    (tmp_path / 'local').mkdir()
    (tmp_path / 'local' / 'policy.md').write_text(
        '<!-- amounts -->\n500 ₽ — минимум вывода\n<!-- /amounts -->\n', encoding='utf-8'
    )
    assert policy_amounts(str(tmp_path)) == {('RUB', 50000)}


def test_без_policy_файла_пусто():
    assert policy_amounts('/нет/такого') == set()
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** в `app/kb.py`:

```python
AMOUNTS_SECTION = re.compile(r'<!--\s*amounts\s*-->(.*?)<!--\s*/amounts\s*-->', re.DOTALL)


def policy_amounts(kb_dir: str) -> set:
    """Суммы, явно подтверждённые владельцем в policy.md.

    Формат — обычный markdown плюс секция <!-- amounts -->: по строке на
    сумму, «число валюта — пояснение». Всё вне секции читает только модель
    (policy.md попадает в промпт как обычный файл kb/), а в белый список
    guard идут только строки из секции.
    """
    from .guard import parse_amounts

    root = Path(kb_dir)
    if not root.is_dir():
        return set()
    amounts: set = set()
    for path in sorted(root.rglob('policy.md')):
        for section in AMOUNTS_SECTION.findall(path.read_text(encoding='utf-8')):
            for line in section.splitlines():
                amounts |= parse_amounts(line)
    return amounts
```

(`policy.md` уже попадает в текст базы знаний через `_render_local` — отдельно подключать не надо; README-исключение его не трогает.)

- [ ] **Шаг 4: зелёные; коммит** `"kb: policy.md — секция подтверждённых сумм для белого списка guard"`.
- [ ] **Шаг 5:** дописать в `kb/README.md` раздел «policy.md» с форматом секции и правилом «числа, которые отдаёт API, здесь не дублируются». Коммит вместе с шагом 4 допустим.

### Задача 4.3: снимок в промпт и проводка сумм через `llm.py`

**Интерфейсы:**
- `prompt.system_prompt(knowledge: str, brand: str = '', live_block: str = '') -> str` — снимок в конец префикса (правила → база знаний → живые данные, порядок важен для кеша);
- `OpenAIProvider.decide(..., live_block: str = '', verified_amounts: frozenset = frozenset()) -> Verdict`.

- [ ] **Шаг 1: тесты**:
  - `tests/test_llm.py`: `_sanitize` с `verified_amounts` — ответ с суммой из списка проходит, не из списка — эскалация (продублировать два случая, guard уже покрыт в 4.1);
  - `test_prompt`-случай (можно в `test_llm.py`): `system_prompt('база', live_block='=== ЖИВЫЕ ДАННЫЕ ===\n…')` содержит блок после базы знаний; без `live_block` — байт-в-байт как раньше (кеш префикса).
- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация**:
  - `app/prompt.py`:
    ```python
    def system_prompt(knowledge: str, brand: str = '', live_block: str = '') -> str:
        rules = RULES.format(brand=brand.strip() or DEFAULT_BRAND)
        prompt = f'{rules}\n\n=== БАЗА ЗНАНИЙ ===\n\n{knowledge}'
        if live_block:
            prompt += f'\n\n{live_block}'
        return prompt
    ```
    В `RULES` добавить абзац: цены, лимиты и состояние функций брать **только** из блока живых данных; если у клиента промогруппа с ненулевой скидкой — точную сумму не называть, отправлять в кабинет; данные в блоке живых данных первичнее базы знаний при расхождении.
  - `app/llm.py`: `decide()` принимает `live_block` и `verified_amounts`, передаёт первый в `system_prompt`, второй — в `_sanitize` → `guard.reject(reply, verified_amounts=...)`.
  - `app/main.py::_answer_or_escalate`:
    ```python
    now = datetime.now(UTC)
    snapshot = self.live.snapshot(now)
    if self.live.last_error:
        # Сюда попадает и «пустой каталог тарифов»: цены молча исчезли бы из
        # белого списка, и каждый ответ с ценой уезжал бы оператору без причины.
        self.alerts.failure(LIVE, self.live.last_error, now=time.monotonic())
    verified = set(kb.policy_amounts(self.cfg.kb_dir))
    live_block = ''
    if snapshot is not None:
        if self.live.is_stale(snapshot, now):
            # Старше 6 часов: суммы снимка не сверены, цены уедут оператору.
            log.warning('Живой снимок протух (возраст %.0f ч)', snapshot.age_sec(now) / 3600)
            self.alerts.failure(LIVE, 'живой снимок старше 6 часов', now=time.monotonic())
        else:
            verified |= snapshot.amounts
        live_block = snapshot.text
    ```
    (новый вид алерта `LIVE = 'live'`, `LABELS[LIVE] = 'живой снимок'`; `alerts.success(LIVE, ...)` при свежем снимке). `provider.decide(..., live_block=live_block, verified_amounts=frozenset(verified))`.
- [ ] **Шаг 4: тесты `test_service.py`**: снимок протух → в `decide` уходит пустой белый список снимка (подделка `FakeLive` с управляемым `fetched_at`); снимка нет вообще → тикет обработан, `live_block=''`.
- [ ] **Шаг 5: всё зелёное; линт; коммит** `"prompt+llm: живые данные в префиксе, суммы сверяются со снимком и policy"`.
- [ ] **Шаг 6: выкат этапа 4**, проверка по описанию этапа.

---

## Этап 5. `llm.py` переезжает на Responses API

**Цель:** сменить `chat.completions` на `/v1/responses` с сохранением поведения (structured output, vision, расход токенов). Без этого инструменты этапа 6 невозможны с reasoning: проверено на боевом ключе — `chat.completions` + `tools` + `reasoning_effort` даёт 400. Этап без функциональных изменений — идеален для изолированной проверки регрессий.

**Файлы:**
- Modify: `app/llm.py`, `tests/test_llm.py`, `pyproject.toml` (floor `openai>=2.2` — версия с устоявшимся Responses API; выполнить `uv lock` и закоммитить `uv.lock`), `scripts/smoke_models.py` (перевести на Responses, он — ручная проверка живого ключа)

**Проверка на бою:** `--check` — шаг «Модель» отвечает через Responses, расход токенов ненулевой, кеш префикса виден (`cached_tokens` в сводке за сутки не упал к нулю). Сутки в тени: черновики не деградировали (сверить среднюю уверенность и долю эскалаций со вчерашним днём по `audit`).

**Критерий готовности:** все тесты зелёные, `--check` проходит, сутки наблюдения без роста `llm_error` в сводке.

**Откат:** предыдущий sha-образ.

### Задача 5.1: `_usage` и вызов через Responses

- [ ] **Шаг 1: тесты** — в `tests/test_llm.py` переписать блок расхода токенов под форму Responses и добавить разбор ответа:

```python
class FakeResponsesUsage:
    input_tokens = 12_000
    output_tokens = 180

    class input_tokens_details:
        cached_tokens = 11_500


def test_расход_токенов_из_responses():
    usage = _usage(type('R', (), {'usage': FakeResponsesUsage})())
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens) == (12_000, 180, 11_500)


def test_ответ_без_usage_не_ломает_разбор():
    assert _usage(type('R', (), {})()).prompt_tokens == 0
```

и тест провайдера на поддельном клиенте (проверяет, что `output_text` разбирается в вердикт, ошибка сети даёт `ERROR` — перенести существующую логику):

```python
class FakeResponses:
    def __init__(self, payload: str):
        self._payload = payload
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return type('R', (), {'output_text': self._payload, 'output': [], 'usage': None, 'id': 'resp_1'})()


def make_provider(payload: str) -> OpenAIProvider:
    provider = object.__new__(OpenAIProvider)
    provider._client = type('C', (), {'responses': FakeResponses(payload)})()
    provider.model = 'test'
    provider.brand = ''
    provider.guard = Guard()
    return provider


def test_вердикт_разбирается_из_output_text():
    payload = json.dumps({'action': 'answer', 'reply_text': 'Откройте бота.', 'reason': '',
                          'topic': 'подключение', 'confidence': 0.9})
    verdict = make_provider(payload).decide(knowledge='база', context={'question': 'как?'},
                                            confidence_threshold=0.7, max_reply_chars=1500)
    assert verdict.is_answer


def test_структурированный_вывод_запрошен_схемой():
    provider = make_provider('{}')
    provider.decide(knowledge='б', context={}, confidence_threshold=0.7, max_reply_chars=1500)
    fmt = provider._client.responses.kwargs['text']['format']
    assert fmt['type'] == 'json_schema' and fmt['name'] == 'verdict'
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** — в `OpenAIProvider.decide`:

```python
        content: list[dict] = [{'type': 'input_text', 'text': user_prompt(context)}]
        for image in context.get('images') or []:
            content.append({'type': 'input_image', 'image_url': image})

        try:
            response = self._client.responses.create(
                model=self.model,
                instructions=system_prompt(knowledge, self.brand, live_block),
                input=[{'role': 'user', 'content': content}],
                text={'format': {'type': 'json_schema', 'name': 'verdict',
                                 'schema': VERDICT_SCHEMA, 'strict': True}},
            )
        except Exception:
            log.warning('Обращение к модели не удалось', exc_info=True)
            return Verdict(ERROR, reason='модель недоступна')

        usage = _usage(response)
        text = (getattr(response, 'output_text', '') or '').strip()
```

`_usage` — на `input_tokens`/`output_tokens`/`input_tokens_details.cached_tokens` (маппинг в те же поля `Usage`, чтобы аудит и сводка не менялись). Разбор JSON и `_sanitize` — без изменений.

- [ ] **Шаг 4: зелёные.**
- [ ] **Шаг 5: живая проверка контракта** — прогнать `uv run python scripts/smoke_models.py` (предварительно переведя его на Responses) с боевым ключом: точные имена параметров (`text.format`, `input_image`, `instructions`) подтверждаются на настоящем API, а не только на подделках. Расхождения чинятся здесь же.
- [ ] **Шаг 6: линт; коммит** `"llm: переезд на Responses API с сохранением вердикта и расхода токенов"`.
- [ ] **Шаг 7: выкат этапа 5**, сутки наблюдения.

---

## Этап 6. Инструменты: `app/tools/` и tool-цикл

**Цель:** модель может запрашивать данные клиента — платежи, устройства, трафик по нодам, рефералку, состояние нод. Ограничения (белый список имён, `user_id` только из тикета, бюджет, обрезка, аудит) — основная часть работы.

**Файлы:**
- Create: `app/tools/__init__.py`, `app/tools/declarations.py`, `app/tools/executor.py`, `tests/test_tools.py`
- Modify: `app/llm.py`, `tests/test_llm.py`, `app/main.py`, `tests/test_service.py`, `app/config.py`, `app/selfcheck.py`, `tests/test_selfcheck.py`, `.env.example`

**Проверка на бою (тень):** тестовый тикет «не проходит оплата» со своего аккаунта → в топике у черновика/эскалации видно «смотрел: payments(days=…)»; в `audit` появились строки `tool_call`; `--check` показывает живую проверку одного инструмента (`nodes_health`). `TOOL_CALL_BUDGET=0` в `.env` + `up -d` возвращает поведение этапа 5 без выката образа — проверить одним циклом.

**Критерий готовности:** пять инструментов работают на проде под ролью из 16 прав; чужой `user_id` невозможен по построению (нет аргумента); бюджет и обрезка видны в тестах; аудит и топик показывают вызовы.

**Откат:** `TOOL_CALL_BUDGET=0` (без выката) либо предыдущий sha-образ.

### Задача 6.1: объявления и исполнитель

**Интерфейсы:**
- Produces:
  - `app/tools/declarations.py::DECLARATIONS: list[dict]` — 5 объявлений в формате Responses (`{'type': 'function', 'name': ..., 'description': ..., 'parameters': {...}, 'strict': True}`); у `payments` единственный аргумент `days: int`, у остальных параметров нет;
  - `app/tools/executor.py::Executor(bedolaga_user: dict, cabinet: Cabinet, *, budget: int)` — `bedolaga_user` это уже загруженный снимок аккаунта (`GET /cabinet/admin/users/{id}`), из него исполнитель берёт `user_id`/`telegram_id` сам;
  - `Executor.run(name: str, arguments: str) -> str` — аргументы как JSON-строка от модели; результат — компактный текст (не JSON), обрезанный до `MAX_RESULT_CHARS = 4000` с пометкой об обрезке;
  - `Executor.calls: list[str]` — человекочитаемые `payments(days=14)` для аудита и поста;
  - `Executor.amounts: set[Amount]` — суммы из результатов (для guard, только этот тикет);
  - `Executor.exhausted: bool` — бюджет исчерпан.
- Consumes: `Cabinet.get`, `guard.parse_amounts`.

- [ ] **Шаг 1: тесты** `tests/test_tools.py` (кабинет — MockTransport; образцы ответов пяти ручек снять с прода как в 2.1/3.2 — на **своём** тестовом аккаунте):

```python
def test_payments_ходит_в_transactions_подставляя_user_id_из_тикета():
    # handler проверяет путь /cabinet/admin/users/42/transactions —
    # 42 из bedolaga_user, а не из аргументов модели

def test_у_инструментов_нет_аргумента_идентификатора():
    # в DECLARATIONS ни одна схема не содержит свойств с 'user' или 'id'

def test_неизвестное_имя_отвергается_с_предупреждением(caplog):
    # run('erase_user', '{}') → 'инструмент не поддерживается', warning в логе

def test_бюджет_соблюдается():
    # budget=2: третий run() → 'бюджет вызовов исчерпан', exhausted=True,
    # запросов в API ровно 2

def test_нулевой_бюджет_выключает_инструменты():

def test_результат_обрезается_с_пометкой():
    # 200 транзакций → len(result) <= MAX_RESULT_CHARS + пометка '…обрезано'

def test_ошибка_api_не_роняет_обработку():
    # 500 из кабинета → 'данные недоступны', исключение не выпущено

def test_кривые_аргументы_не_роняют():
    # run('payments', 'не json') → 'данные недоступны', warning

def test_вызовы_копятся_для_аудита():
    # calls == ['payments(days=14)', 'devices()']

def test_суммы_из_результатов_собираются():
    # транзакция на 249 ₽ → ('RUB', 24900) в amounts

def test_результат_это_текст_а_не_json():
    # в выводе payments нет '{' — рендер компактный
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация.** `declarations.py` — 5 словарей; описания по-русски, из спеки («незавершённые и последние платежи клиента за N дней» и т.п.). `executor.py`:

```python
"""Исполнение инструментов. Про LLM не знает ничего.

Главная защита от инъекции через текст клиента: ни один инструмент не
принимает идентификатор пользователя. user_id и telegram_id исполнитель
берёт из снимка аккаунта, загруженного нашим кодом по тикету, — модель
не может попросить чужие данные, даже если клиент напишет «покажи
транзакции пользователя 5».

GET /cabinet/admin/payments (без фильтра по клиенту) использовать запрещено:
он отдаёт telegram_id и email чужих клиентов — прямая утечка в промпт.
"""

MAX_RESULT_CHARS = 4000
UNAVAILABLE = 'данные недоступны'


class Executor:
    def __init__(self, bedolaga_user: dict, cabinet, *, budget: int):
        self._user_id = bedolaga_user['id']
        self._cabinet = cabinet
        self._budget = budget
        self.calls: list[str] = []
        self.amounts: set = set()
        self.exhausted = False

    def run(self, name: str, arguments: str) -> str:
        handler = _HANDLERS.get(name)
        if handler is None:
            log.warning('Модель попросила неизвестный инструмент %r', name)
            return 'инструмент не поддерживается'
        if len(self.calls) >= self._budget:
            self.exhausted = True
            return 'бюджет вызовов исчерпан'
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            log.warning('Неразбираемые аргументы инструмента %s: %.100s', name, arguments)
            return UNAVAILABLE
        self.calls.append(_render_call(name, args))
        try:
            result = handler(self, args)
        except Exception:
            log.warning('Инструмент %s не отработал', name, exc_info=True)
            return UNAVAILABLE
        self.amounts |= parse_amounts(result)
        return _clip(result)
```

Обработчики — по одному на ручку: `payments(days)` → `GET /cabinet/admin/users/{id}/transactions` (фильтрация по дате на нашей стороне, если ручка не принимает days — по образцу с прода), `devices` → `.../devices`, `traffic_by_node` → `.../node-usage`, `referral_summary` → `GET /cabinet/admin/partners/{id}`, `nodes_health` → `GET /cabinet/admin/remnawave/nodes/overview`. Каждый рендерит компактный текст («СБП 249 ₽ — не завершён, 12 мин назад», по строке на запись).

- [ ] **Шаг 4: зелёные; коммит** `"tools: пять read-only инструментов с бюджетом, обрезкой и аудитом вызовов"`.

### Задача 6.2: tool-цикл в `llm.py`

**Интерфейсы:**
- `OpenAIProvider.decide(..., executor=None, tool_declarations=None) -> Verdict`; цикл: пока в `response.output` есть `function_call` и исполнитель не исчерпан — исполнить, отправить `function_call_output` с `previous_response_id`, повторить. Исчерпание бюджета → `Verdict(ESCALATE, reason='не сошлась за N вызовов')`.

- [ ] **Шаг 1: тесты** — на поддельном `responses.create`, который в первый вызов отдаёт `output=[function_call(name='payments', arguments='{"days":14}', call_id='c1')]`, во второй — финальный вердикт:

```python
def test_цикл_исполняет_вызов_и_возвращает_результат_модели():
    # второй create получил previous_response_id и function_call_output c call_id='c1'

def test_бюджет_исчерпан_даёт_эскалацию_с_причиной():
    # подделка всегда просит инструмент; executor budget=2 →
    # verdict.action == ESCALATE, 'не сошлась за 2 вызова' in reason

def test_без_executor_инструменты_не_объявляются():
    # tools нет в kwargs create — поведение этапа 5

def test_расход_токенов_суммируется_по_всем_обращениям():
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** цикла в `decide()` (объявления передаются `tools=tool_declarations`; продолжение — `previous_response_id=response.id`, `input=[{'type': 'function_call_output', 'call_id': ..., 'output': ...}]`); суммарный `Usage` складывается по итерациям. Точные имена полей `function_call` (`name`, `arguments`, `call_id`) подтвердить прогоном `scripts/smoke_models.py` с настоящим ключом (спека уже проверяла этот путь на бою).
- [ ] **Шаг 4: зелёные; коммит** `"llm: цикл tool-use с бюджетом поверх Responses API"`.

### Задача 6.3: обвязка — `main.py`, конфиг, аудит, `--check`

- [ ] **Шаг 1: тесты**:
  - `tests/test_service.py`: при `tool_call_budget=0` провайдер вызывается без executor; вызовы инструментов из executor пишутся в аудит (`action='tool_call'`, `reason='payments(days=14)'` — по записи на вызов); суммы executor попадают в `verified_amounts` провайдера;
  - `tests/test_selfcheck.py`: шаг «Инструменты» дёргает `nodes_health` и печатает `OK` с длиной ответа.
- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация**:
  - `app/config.py`: `tool_call_budget: int = field(default_factory=lambda: _int('TOOL_CALL_BUDGET', 4))`;
  - `app/main.py::_answer_or_escalate`: если `self.cfg.tool_call_budget > 0` и пользователь загружен — `executor = Executor(user, self.cabinet, budget=self.cfg.tool_call_budget)`; `provider.decide(..., executor=executor, tool_declarations=DECLARATIONS)`; после вердикта — `for call in executor.calls: self.store.audit(ticket_id, 'tool_call', reason=call)`; `verified |= executor.amounts` (суммы живут только в этом тикете — executor создаётся на тикет и умирает с ним); executor сохраняется в локальной переменной для этапа 7 (пост оператору). Внимание: `verified_amounts` для guard собираются **после** tool-цикла — суммы инструментов должны успеть в сверку финального ответа; это решается тем, что guard зовётся в `_sanitize` уже финального ответа, а `executor.amounts` наполняются в цикле до него — передать в `decide()` сам executor и брать `executor.amounts` в момент `_sanitize` (проверено тестом «сумма из инструмента проходит»);
  - `app/context.py::build`: вернуть из функции также сырой `user` (ключ `'user'` в payload) — он нужен executor'у; `user_prompt` его не печатает;
  - `app/selfcheck.py`: `_check_tools(cabinet)` — живой `nodes_health` (единственный инструмент без клиентских данных);
  - `.env.example`:
    ```ini
    # Максимум вызовов инструментов на тикет. 0 — выключить инструменты совсем,
    # аварийный откат к поведению без инструментов, без выката образа.
    TOOL_CALL_BUDGET=4
    ```
- [ ] **Шаг 4: всё зелёное; линт; коммит** `"main: инструменты в обработке тикета — бюджет, аудит, суммы в сверку"`.
- [ ] **Шаг 5: выкат этапа 6**, проверка по описанию этапа (включая аварийный `TOOL_CALL_BUDGET=0`).

---

## Этап 7. Сводка для оператора

**Цель:** эскалация перестаёт быть голой строкой причины — оператор получает пост с фактами (что смотрел бот и что увидел), гипотезой и черновиком ответа. Схема вердикта расширяется полями `operator_note` и `draft_reply` без дополнительных обращений к модели.

**Файлы:**
- Modify: `app/prompt.py`, `app/llm.py`, `tests/test_llm.py`, `app/notify.py`, `tests/test_notify.py` *(новый файл)*, `app/main.py`, `tests/test_service.py`

**Проверка на бою (тень):** дождаться эскалации — пост в топике 14014 в новом формате: «Что выяснил» с фактами из снимка аккаунта и списком вызовов, гипотеза, черновик с пометкой «не отправлен»; если модель вписала в черновик несверенную сумму — видна пометка «⚠️ в черновике сумма, не подтверждённая данными».

**Критерий готовности:** пост влезает в 4096 символов на длинных тикетах (проверено тестом порядка обрезки), `operator_note`/`draft_reply` экранируются, заголовок/причина/«что смотрел» не режутся никогда.

**Откат:** предыдущий sha-образ (вернутся старые короткие эскалации).

### Задача 7.1: расширение схемы вердикта

- [ ] **Шаг 1: тесты** в `tests/test_llm.py`:

```python
def test_operator_note_и_draft_проезжают_в_вердикт_при_эскалации():
    raw = {'action': 'escalate', 'reply_text': '', 'reason': 'не хватает данных',
           'topic': 'оплата', 'confidence': 0.4,
           'operator_note': 'похоже на сторону шлюза', 'draft_reply': 'Вижу три попытки…'}
    verdict = sanitize(raw)
    assert verdict.operator_note == 'похоже на сторону шлюза'
    assert verdict.draft_reply.startswith('Вижу')


def test_при_ответе_черновик_и_заметка_пустые():
    verdict = sanitize({**OK, 'operator_note': 'мусор', 'draft_reply': 'мусор'})
    assert verdict.operator_note == '' and verdict.draft_reply == ''


def test_reply_text_при_эскалации_пуст():
    # клиент не видит ни отказа, ни упоминания передачи — уже есть, не сломать
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация**: `VERDICT_SCHEMA` — добавить `'operator_note': {'type': 'string'}`, `'draft_reply': {'type': 'string'}` в `properties` и `required` (strict). В `RULES` — абзац: при эскалации заполнить `operator_note` (вероятная причина и что проверить оператору) и `draft_reply` (черновик ответа клиенту; оператор отправит его руками, если согласен); при ответе оба поля оставить пустыми. `Verdict` — новые поля с дефолтом `''`; `_sanitize` пробрасывает их только в ветке эскалации.
- [ ] **Шаг 4: зелёные; коммит** `"prompt+llm: operator_note и draft_reply в схеме вердикта"`.

### Задача 7.2: пост оператору в `notify.py`

**Интерфейсы:**
- `Notifier.escalated(ticket_id, *, question, reason, topic='', confidence=None, facts='', looked_at='', operator_note='', draft_reply='', draft_warning='')` — все новые поля опциональны, старые вызовы (например, из `_hand_over` нет — там свой метод) продолжают работать.

- [ ] **Шаг 1: тесты** `tests/test_notify.py` (Notifier с подменённым `_send`, собирающим текст):

```python
def make_notifier(sent: list[str]) -> Notifier:
    notifier = Notifier(bot_token='t', chat_id='c')
    notifier._send = sent.append
    return notifier


def test_полный_пост_содержит_все_блоки():
    # 'Передан оператору', вопрос, 'Что выяснил', 'смотрел:', 'Гипотеза',
    # 'Черновик (не отправлен)'

def test_пост_влезает_в_телеграм():
    # facts/draft по 5000 символов → len(текста) <= 4096

def test_при_переполнении_режется_сначала_черновик_потом_факты():
    # заголовок, причина и «смотрел:» присутствуют даже при огромных полях

def test_бюджеты_полей():
    # вопрос ≤ 400+1, факты ≤ 800+1, гипотеза ≤ 400+1, черновик ≤ 1200+1

def test_заметка_и_черновик_экранируются():
    # operator_note='<script>' → '&lt;script&gt;' — их пишет модель по тексту
    # клиента, доверия не больше, чем к самому тикету

def test_пометка_guard_по_черновику_видна():
    # draft_warning='в черновике сумма…' → '⚠️' в посте

def test_старый_вызов_без_новых_полей_работает():
```

- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** — переписать `escalated()`:

```python
# Бюджеты поста эскалации: один телеграм-пост, потолок 4096.
Q_LIMIT, FACTS_LIMIT, NOTE_LIMIT, DRAFT_LIMIT = 400, 800, 400, 1200
TG_LIMIT = 4096
```

Сборка: заголовок (`🔺 Передан оператору`, тикет, тема, уверенность) → `Причина` → `Вопрос` (клип 400) → `Что выяснил` (клип 800: факты + строка `смотрел: …` — строка «смотрел» вне клипа, она не режется) → `Гипотеза` (operator_note, клип 400) → черновик (клип 1200) + `draft_warning`. Если итог длиннее `TG_LIMIT` — убрать черновик целиком, затем факты (кроме «смотрел»); заголовок, причина и «смотрел» — никогда. Всё пользовательское — через `html.escape`.

- [ ] **Шаг 4: зелёные; коммит** `"notify: пост оператору со сводкой — бюджеты, порядок обрезки, экранирование"`.

### Задача 7.3: сборка «Что выяснил» в `main.py`

- [ ] **Шаг 1: тесты** в `tests/test_service.py`: при эскалации `notifier.escalated` получает `facts` из снимка аккаунта (строки `render_account`), `looked_at` из `executor.calls`, `draft_warning` — от прогона guard по черновику (мягкий режим: guard вернул причину → она стала пометкой, эскалация не изменилась); черновик без нарушений — пометки нет.
- [ ] **Шаг 2: падают.**
- [ ] **Шаг 3: реализация** в `_answer_or_escalate`/`_escalate`: блок фактов собирает **наш код** — из `payload['account']`, из сжатых результатов инструментов (первые строки каждого результата, executor хранит их) и списка вызовов; модельному тексту это не доверяется (гарантия совпадения фактов с API). Черновик прогоняется `guard.reject(draft, verified_amounts=...)` — результат не блокирует, а превращается в `draft_warning='⚠️ ' + причина`.
- [ ] **Шаг 4: зелёные; линт; коммит** `"main: факты и черновик в посте эскалации"`.
- [ ] **Шаг 5: выкат этапа 7.**

---

## Этап 8. Слои базы знаний, документация, релиз 0.2.0, приёмка

**Цель:** числа исчезают из текстов базы знаний (их место — живой снимок и `policy.md`), документация соответствует новой установке, версия выпускается, спека принимается на реальных тикетах.

**Файлы:**
- Modify: `kb/README.md`, `docs/deploy.md`, `README.md`, `CHANGELOG.md`, `pyproject.toml` (версия 0.2.0), `.env.example` (финальная сверка)
- Прод (не в git): `/opt/bedolada-support/kb/local/*.md`, `/opt/bedolada-support/.env`

**Проверка на бою:** финальная приёмка ниже.

**Критерий готовности:** приёмка пройдена, релиз v0.2.0 опубликован, `BEDOLAGA_API_TOKEN` удалён из прод-`.env`.

**Откат:** это документационно-контентный этап; код не меняется. Прод-файлы `kb/local` перед чисткой скопировать: `cp -r kb/local kb/local.bak-$(date +%F)`.

### Задача 8.1: чистка `kb/` на проде (слои)

`kb/local/` гитигнорен — это операция на сервере, без коммита.

- [ ] **Шаг 1**: бэкап `kb/local` (команда выше).
- [ ] **Шаг 2**: пройти по `kb/local/*.md` (`tarify.md`, `referalka.md`, `oplata.md`, `ne-rabotaet.md`, `podklyuchenie.md`, `prochee.md`, `probely.md`): удалить **все числа, которые отдаёт API** (цены, лимиты трафика/устройств — включая ложные «30 устройств на Стандарте», проценты рефералки, пороги) — теперь они приходят живьём; инструкции диагностики оставить.
- [ ] **Шаг 3**: завести `kb/local/policy.md` — подтверждённые владельцем факты, которых нет в API (возвраты, партнёрские выплаты в USDT, статусы незапущенных фич), с секцией `<!-- amounts -->` для сумм. Содержимое согласовать с владельцем — это его файл.
- [ ] **Шаг 4**: `docker compose restart` (kb смонтирован томом) и `--check`: пост-фильтр больше не предупреждает «в базе знаний есть суммы» (или предупреждает только про policy-суммы, которые теперь в белом списке).

### Задача 8.2: документация

- [ ] **Шаг 1**: `docs/deploy.md` переработать:
  - шаг «Токен к API бота» заменить на «Служебный аккаунт кабинета»: регистрация по email (`POST /cabinet/auth/email/register`) требует согласия с документами (`accepted_legal_documents`) и подтверждения почты; назначение роли «AI Support» (16 прав из спеки — привести таблицу) в админке; `CABINET_EMAIL`/`CABINET_PASSWORD` в `.env`;
  - предупреждение: FAQ, скрытый из веба режимом отображения, кабинетной ручкой отдаётся **пустым** (в отличие от webapi) — сервис с пустой базой знаний не стартует; режим отображения проверяется в `--check`;
  - `TOOL_CALL_BUDGET` в разделе настроек, `0` — аварийное выключение инструментов;
  - таблицу «Если что-то не так» дополнить: `Кабинет: HTTP 401/403` → пароль/роль; `логин ответил 429` → лимит входа, ждать.
- [ ] **Шаг 2**: `README.md` — обновить описание возможностей (живые данные, инструменты, сводка оператору).
- [ ] **Шаг 3**: `CHANGELOG.md` — раздел `## 0.2.0` по фактическим изменениям этапов 1–7; `pyproject.toml` → `version = "0.2.0"`.
- [ ] **Шаг 4**: прод-`.env`: удалить строку `BEDOLAGA_API_TOKEN` (образы ≤ этапа 1 больше не нужны).
- [ ] **Шаг 5**: линт, коммит `"docs: установка через служебный аккаунт, changelog 0.2.0"`.

### Задача 8.3: релиз и приёмка на реальных данных

- [ ] **Шаг 1**: Actions → Релиз → `0.2.0` (workflow проверит pyproject и CHANGELOG, поставит тег, соберёт образ `0.2.0` и `v0.2.0-<sha>`).
- [ ] **Шаг 2**: на проде зафиксировать образ релиза: `IMAGE=ghcr.io/airp0wer/bedolada-support:v0.2.0-<sha> docker compose up -d`.
- [ ] **Шаг 3: приёмка** — воспроизвести тикеты #82, #83, #84 (и три старых из архива аудита) тестовыми обращениями, сравнить с тем, что сервис делал раньше (старые решения — в `audit`):
  - **#84** (увеличить трафик, платил бы за 1 ТБ): черновик называет `FAMILY` — 600 ГБ, 6 устройств — и максимум `STANDART`/безлимит `AI`; цены из снимка не отбраковываются. Это явный критерий из спеки.
  - **#83** (когда вывод рефералки): черновик отвечает состоянием фичи из настроек (выключен, минимум, кулдаун), без эскалации «сроки не указаны в базе».
  - **#82** (пустая ссылка СБП): эскалация со сводкой — незавершённые платежи из `payments`, список вызовов, гипотеза, черновик.
- [ ] **Шаг 4**: итоги приёмки — коротким разделом в `docs/superpowers/plans/` не оформлять; результат фиксируется в топике наблюдения и, при расхождениях, новыми issue.

---

## Открытые вопросы

Не заполнены догадкой — требуют сверки с боевым v4.0.0 (OpenAPI/живые запросы) на шагах «снять образцы» (задачи 2.1, 3.2, 6.1) до написания соответствующих фикстур:

1. **Формы кабинетных тикетных ручек**: обёртка списка `GET /cabinet/admin/tickets` (ожидается `items`/`total`), потолок `per_page` (в v3.65.1 — `le=100`), допустимые значения фильтра `status` (остаются ли `open`/`pending`), тело `POST .../priority`, форма ответа `POST .../reply` (где id созданного сообщения). Спека подтверждает только сам факт переезда на кабинетные ручки, точных форм не даёт.
2. **Имена полей медиа в сообщении кабинетного тикета** (`media_file_id`? `media_token`? токен на сообщение или на файл?) — спека описывает механику (`media_token` внутри ответа тикета + `GET /cabinet/media/{file_id}?token=…`), но не имена полей.
3. **Поля ответа логина**: подтверждено локальным чекаутом (`access_token`/`refresh_token`/`expires_in`/`user`), но тело `POST /cabinet/auth/refresh` (имя поля refresh-токена) надо сверить с продом.
4. **Источник «глобальный топап включён» и списка пакетов** для вывода «докупка трафика»: спека даёт формулу `traffic_topup_enabled && пакеты непустые && трафик не безлимитный`, но не называет, из какой ручки берутся `traffic_topup_enabled` и пакеты (настройки категории `TRAFFIC`? поле тарифа?). Выяснить на образцах задачи 3.2.
5. **Поле процентов скидки в `GET /cabinet/admin/promo-groups`** — спека говорит «проценты берём отсюда», имя поля не называет.
6. **`payments`: принимает ли `GET .../transactions` параметр периода** — если нет, фильтр по `days` делается на нашей стороне (заложено в задаче 6.1).
7. **Язык FAQ в `GET /cabinet/info/faq`** — есть ли параметр `language` и параметры `include_inactive`/`fallback`, как у webapi-ручки. Влияет на `KB_LANGUAGES`.
8. **Точный контракт Responses API в текущем SDK** (имена `text.format`, `input_image`, `function_call_output`, `previous_response_id`) — подтверждается прогоном `scripts/smoke_models.py` на живом ключе в задачах 5.1 и 6.2; спека проверяла путь на бою, но не фиксировала имена полей SDK.
9. ~~Уведомляет ли кабинетный reply клиента в Telegram и ставит ли статус `answered`~~ — **ЗАКРЫТО 2026-08-08**, проверено в боевом образе v4.0.0 (`/app/app/cabinet/routes/admin_tickets.py`, обработчик `POST /{ticket_id}/reply`): ставит `ticket.status = 'answered'`, шлёт клиенту уведомление через `notify_user_about_ticket_reply(bot, ticket, …)` и дополнительно эмитит событие для вебхуков. То есть кабинетный reply покрывает всё, что делал webapi, и сверх того. Отдельной работы в задаче 2.1 не требует — только подтверждение на тестовом тикете.
