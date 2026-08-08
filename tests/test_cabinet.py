"""Транспорт кабинета на поддельном httpx: логин, refresh, перелогин, 429.

Сети нет, время подменяется монотонным счётчиком — иначе тест на выдержку
после 429 пришлось бы ждать минуту по-настоящему.
"""

from __future__ import annotations

import httpx
import pytest

from app.cabinet import Cabinet, CabinetAuthError, CabinetThrottled

EMAIL = 'support-ai@example.com'
PASSWORD = 'секрет'


def tokens(access: str, refresh: str = 'r1', expires_in: int = 900, account_id: int = 1344) -> dict:
    """Ответ логина в том виде, в каком его отдаёт кабинет."""
    return {
        'access_token': access,
        'refresh_token': refresh,
        'token_type': 'bearer',
        'expires_in': expires_in,
        'user': {'id': account_id, 'email': EMAIL},
    }


def cabinet(handler, *, clock=None) -> Cabinet:
    api = Cabinet('http://bot', EMAIL, PASSWORD)
    api.close()
    api._http = httpx.Client(base_url='http://bot', transport=httpx.MockTransport(handler))
    api._http_once = api._http
    if clock is not None:
        api._now = clock
    return api


class Clock:
    """Монотонное время под управлением теста."""

    def __init__(self):
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def recorder(routes):
    """Обработчик по путям; пишет каждый запрос в журнал."""
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get('Authorization')
        calls.append((request.method, request.url.path, auth))
        route = routes[request.url.path]
        return route(request) if callable(route) else route

    return handler, calls


# --- логин ----------------------------------------------------------------


def test_первый_запрос_логинится_и_подставляет_токен():
    handler, calls = recorder(
        {
            '/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1')),
            '/cabinet/admin/tickets': httpx.Response(200, json={'items': []}),
        }
    )
    api = cabinet(handler)

    response = api.get('/cabinet/admin/tickets')

    assert response.status_code == 200
    assert calls[0][:2] == ('POST', '/cabinet/auth/email/login')
    assert calls[1] == ('GET', '/cabinet/admin/tickets', 'Bearer a1')


def test_живой_токен_переиспользуется():
    handler, calls = recorder(
        {
            '/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1')),
            '/cabinet/admin/tickets': httpx.Response(200, json={'items': []}),
        }
    )
    api = cabinet(handler)

    api.get('/cabinet/admin/tickets')
    api.get('/cabinet/admin/tickets')

    logins = [c for c in calls if c[1].endswith('/login')]
    assert len(logins) == 1


def test_id_служебного_аккаунта_берётся_из_ответа_логина():
    handler, _ = recorder({'/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1', account_id=1344))})
    api = cabinet(handler)

    assert api.account_id == 1344


def test_неверный_пароль_это_потеря_доступа_а_не_деградация():
    handler, _ = recorder({'/cabinet/auth/email/login': httpx.Response(401, json={'detail': 'bad'})})
    api = cabinet(handler)

    with pytest.raises(CabinetAuthError):
        api.get('/cabinet/admin/tickets')


def test_отозванная_роль_тоже_потеря_доступа():
    handler, _ = recorder({'/cabinet/auth/email/login': httpx.Response(403, json={'detail': 'forbidden'})})
    api = cabinet(handler)

    with pytest.raises(CabinetAuthError):
        api.get('/cabinet/admin/tickets')


# --- обновление токена ----------------------------------------------------


def test_протухший_access_обновляется_рефрешем_без_пароля():
    handler, calls = recorder(
        {
            '/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1', 'r1')),
            '/cabinet/auth/refresh': httpx.Response(200, json=tokens('a2', 'r2')),
            '/cabinet/admin/tickets': httpx.Response(200, json={'items': []}),
        }
    )
    clock = Clock()
    api = cabinet(handler, clock=clock)

    api.get('/cabinet/admin/tickets')
    clock.advance(900)  # access протух
    api.get('/cabinet/admin/tickets')

    paths = [c[1] for c in calls]
    assert paths.count('/cabinet/auth/email/login') == 1
    assert '/cabinet/auth/refresh' in paths
    assert calls[-1][2] == 'Bearer a2'


def test_рефреш_токен_ротируется():
    """Кабинет отдаёт новый refresh при каждом обновлении — старый больше не годен."""
    bodies: list[dict] = []

    def refresh(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        bodies.append(jsonlib.loads(request.content))
        return httpx.Response(200, json=tokens('a3', 'r3'))

    handler, _ = recorder(
        {
            '/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1', 'r1')),
            '/cabinet/auth/refresh': refresh,
            '/cabinet/admin/tickets': httpx.Response(200, json={'items': []}),
        }
    )
    clock = Clock()
    api = cabinet(handler, clock=clock)

    api.get('/cabinet/admin/tickets')
    clock.advance(900)
    api.get('/cabinet/admin/tickets')
    clock.advance(900)
    api.get('/cabinet/admin/tickets')

    assert bodies[0] == {'refresh_token': 'r1'}
    assert bodies[1] == {'refresh_token': 'r3'}


def test_непринятый_рефреш_приводит_к_логину_по_паролю():
    handler, calls = recorder(
        {
            '/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1', 'r1')),
            '/cabinet/auth/refresh': httpx.Response(401, json={'detail': 'expired'}),
            '/cabinet/admin/tickets': httpx.Response(200, json={'items': []}),
        }
    )
    clock = Clock()
    api = cabinet(handler, clock=clock)

    api.get('/cabinet/admin/tickets')
    clock.advance(900)
    api.get('/cabinet/admin/tickets')

    assert [c[1] for c in calls].count('/cabinet/auth/email/login') == 2


# --- 401 на самом запросе -------------------------------------------------


def test_401_на_запросе_лечится_перелогином_и_одним_повтором():
    state = {'access': 'a1', 'first': True}

    def login(request: httpx.Request) -> httpx.Response:
        state['access'] = 'a2'
        return httpx.Response(200, json=tokens('a2'))

    def tickets(request: httpx.Request) -> httpx.Response:
        if state['first']:
            state['first'] = False
            return httpx.Response(401, json={'detail': 'session revoked'})
        return httpx.Response(200, json={'items': []})

    handler, calls = recorder({'/cabinet/auth/email/login': login, '/cabinet/admin/tickets': tickets})
    api = cabinet(handler)

    response = api.get('/cabinet/admin/tickets')

    assert response.status_code == 200
    assert [c[1] for c in calls].count('/cabinet/auth/email/login') == 2
    assert calls[-1][2] == 'Bearer a2'


def test_повтор_после_401_ровно_один():
    """Если и повтор получил 401 — отдаём ответ как есть, а не крутимся в цикле."""
    handler, calls = recorder(
        {
            '/cabinet/auth/email/login': httpx.Response(200, json=tokens('a1')),
            '/cabinet/admin/tickets': httpx.Response(401, json={'detail': 'nope'}),
        }
    )
    api = cabinet(handler)

    response = api.get('/cabinet/admin/tickets')

    assert response.status_code == 401
    assert [c[1] for c in calls].count('/cabinet/admin/tickets') == 2


# --- лимит попыток входа --------------------------------------------------


def test_429_на_логине_даёт_выдержку_а_не_новую_попытку():
    handler, calls = recorder({'/cabinet/auth/email/login': httpx.Response(429, json={'detail': 'slow down'})})
    api = cabinet(handler, clock=Clock())

    with pytest.raises(CabinetThrottled):
        api.get('/cabinet/admin/tickets')
    with pytest.raises(CabinetThrottled):
        api.get('/cabinet/admin/tickets')

    # Вторая попытка входа не пошла в сеть: сами себя в лимит не загоняем.
    assert [c[1] for c in calls].count('/cabinet/auth/email/login') == 1


def test_выдержка_нарастает_и_после_неё_попытка_повторяется():
    handler, calls = recorder({'/cabinet/auth/email/login': httpx.Response(429, json={'detail': 'slow down'})})
    clock = Clock()
    api = cabinet(handler, clock=clock)

    with pytest.raises(CabinetThrottled):
        api.get('/cabinet/admin/tickets')

    clock.advance(60)  # первая выдержка истекла
    with pytest.raises(CabinetThrottled):
        api.get('/cabinet/admin/tickets')

    clock.advance(60)  # вторая выдержка вдвое длиннее — попытки ещё нет
    with pytest.raises(CabinetThrottled):
        api.get('/cabinet/admin/tickets')

    assert [c[1] for c in calls].count('/cabinet/auth/email/login') == 2


def test_успешный_вход_сбрасывает_выдержку():
    answers = [httpx.Response(429, json={'detail': 'slow down'}), httpx.Response(200, json=tokens('a1'))]

    def login(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    handler, _ = recorder({'/cabinet/auth/email/login': login, '/cabinet/admin/tickets': httpx.Response(200, json={})})
    clock = Clock()
    api = cabinet(handler, clock=clock)

    with pytest.raises(CabinetThrottled):
        api.get('/cabinet/admin/tickets')
    clock.advance(60)
    api.get('/cabinet/admin/tickets')

    assert api._throttle_sec == 60  # стартовое значение вернулось


# --- ретраи: ответ клиенту идёт другим клиентом ---------------------------


def two_client_cabinet(handler_retry, handler_once) -> Cabinet:
    """Кабинет, у которого клиенты с ретраями и без разведены по транспортам.

    Общий хелпер `cabinet()` подменяет оба клиента одним и тем же — на нём
    подмена `post_once` на `post` осталась бы незамеченной.
    """
    api = Cabinet('http://bot', EMAIL, PASSWORD)
    api.close()
    api._http = httpx.Client(base_url='http://bot', transport=httpx.MockTransport(handler_retry))
    api._http_once = httpx.Client(base_url='http://bot', transport=httpx.MockTransport(handler_once))
    return api


def test_ответ_клиенту_идёт_без_транспортных_ретраев():
    """post_once обязан ходить клиентом без ретраев: повтор — второе сообщение."""
    retried: list[str] = []
    once: list[str] = []

    def with_retries(request: httpx.Request) -> httpx.Response:
        retried.append(request.url.path)
        return httpx.Response(200, json={})

    def without_retries(request: httpx.Request) -> httpx.Response:
        once.append(request.url.path)
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json=tokens('a1'))
        return httpx.Response(201, json={'message': {'id': 5}})

    api = two_client_cabinet(with_retries, without_retries)

    api.post_once('/cabinet/admin/tickets/7/reply', json={'message': 'привет'})

    assert '/cabinet/admin/tickets/7/reply' in once
    assert retried == []


def test_идемпотентный_post_идёт_клиентом_с_ретраями():
    retried: list[str] = []
    once: list[str] = []

    def with_retries(request: httpx.Request) -> httpx.Response:
        retried.append(request.url.path)
        return httpx.Response(200, json={})

    def without_retries(request: httpx.Request) -> httpx.Response:
        once.append(request.url.path)
        return httpx.Response(200, json=tokens('a1'))

    api = two_client_cabinet(with_retries, without_retries)

    api.post('/cabinet/admin/tickets/7/priority', json={'priority': 'high'})

    assert retried == ['/cabinet/admin/tickets/7/priority']
    # Вход и обновление токена всегда идут клиентом без ретраев.
    assert once == ['/cabinet/auth/email/login']
