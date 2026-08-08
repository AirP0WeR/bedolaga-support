"""Транспорт к кабинету бедолаги: служебный аккаунт, пара токенов, перелогин.

Единственный модуль, знающий про логин и заголовок `Authorization`. Остальной
код получает готовые `httpx.Response` через `get()`/`post()`/`post_once()` и не
различает, каким по счёту токеном выполнен запрос.

Access живёт 15 минут, refresh — 7 дней, причём refresh при обновлении
**ротируется**: новый надо сохранять, старый больше не годен. Протухание access
лечится обновлением, любой 401 на запросе — перелогином по паролю и ровно одним
повтором. Пароль в конфиге есть всегда, поэтому из протухания сервис выбирается
сам.

Исключение — 401/403 на самом логине: это не деградация, а потеря доступа
(сменили пароль, отозвали роль). О ней сервис обязан кричать, а не тихо
ретраить.

Логин лимитирован на стороне бота: 10 попыток в минуту на IP, а при недоступном
Redis лимитер закрывается наглухо. Поэтому 429 и 401 различаются: на 429 —
выдержка с нарастанием, повторный вход раньше времени сам загнал бы нас в лимит.
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
# Выдержка после 429 на логине: старт и потолок, между ними — удвоение.
THROTTLE_START_SEC = 60
THROTTLE_MAX_SEC = 600


class CabinetAuthError(RuntimeError):
    """Кабинет не пускает по паролю: доступ потерян, тикеты не обрабатываем."""


class CabinetThrottled(RuntimeError):
    """Вход лимитирован (429): выдерживаем паузу, попыток не делаем."""


class Cabinet:
    def __init__(self, base_url: str, email: str, password: str, *, timeout: float = 30.0):
        self._email = email
        self._password = password
        self._http = httpx.Client(
            base_url=base_url.rstrip('/'),
            timeout=timeout,
            transport=httpx.HTTPTransport(retries=3),
        )
        # Отдельный клиент без транспортных ретраев для неидемпотентных вызовов.
        self._http_once = httpx.Client(base_url=base_url.rstrip('/'), timeout=timeout)
        # Часы вынесены в атрибут, чтобы тест на выдержку не ждал минуту всерьёз.
        self._now = time.monotonic
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
        """id служебного аккаунта — для логов и самопроверки: видно, кем вошли."""
        if self._account_id is None:
            self._login()
        assert self._account_id is not None
        return self._account_id

    # --- токены ---------------------------------------------------------

    def _apply(self, data: dict) -> None:
        self._access = data['access_token']
        self._refresh = data['refresh_token']
        self._expires_at = self._now() + max(0, data.get('expires_in', 0) - REFRESH_MARGIN_SEC)

    def _login(self) -> None:
        now = self._now()
        if now < self._throttled_until:
            raise CabinetThrottled(f'вход лимитирован ещё {self._throttled_until - now:.0f} с')

        response = self._http_once.post(LOGIN_PATH, json={'email': self._email, 'password': self._password})

        if response.status_code == 429:
            self._throttled_until = now + self._throttle_sec
            waiting = self._throttle_sec
            self._throttle_sec = min(self._throttle_sec * 2, THROTTLE_MAX_SEC)
            raise CabinetThrottled(f'вход ответил 429, выдержка {waiting:.0f} с')
        if response.status_code in (401, 403):
            raise CabinetAuthError(f'кабинет не пускает служебный аккаунт: HTTP {response.status_code}')
        response.raise_for_status()

        self._throttle_sec = THROTTLE_START_SEC
        data = response.json()
        self._apply(data)
        self._account_id = data['user']['id']
        log.info('Кабинет: вход выполнен, служебный аккаунт id %s', self._account_id)

    def _ensure_token(self) -> None:
        if self._access and self._now() < self._expires_at:
            return
        if self._refresh:
            response = self._http_once.post(REFRESH_PATH, json={'refresh_token': self._refresh})
            if response.status_code < 400:
                self._apply(response.json())
                return
            log.info('Кабинет: обновление не принято (HTTP %s), вхожу по паролю', response.status_code)
        self._login()

    # --- запросы --------------------------------------------------------

    def _send(self, client: httpx.Client, method: str, path: str, params, json) -> httpx.Response:
        return client.request(
            method,
            path,
            params=params,
            json=json,
            headers={'Authorization': f'Bearer {self._access}'},
        )

    def _request(self, method: str, path: str, *, params=None, json=None, retries: bool) -> httpx.Response:
        self._ensure_token()
        client = self._http if retries else self._http_once

        response = self._send(client, method, path, params, json)
        if response.status_code != 401:
            return response

        # Сессию отозвали или access умер раньше срока: перелогин и один повтор.
        # Для ответа клиенту это безопасно — 401 отбивается авторизацией до
        # того, как сообщение попадёт в БД бота.
        log.info('Кабинет: %s %s ответил 401, перелогин и один повтор', method, path)
        self._login()
        return self._send(client, method, path, params, json)

    def get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        return self._request('GET', path, params=params, retries=True)

    def post(self, path: str, *, json: dict | None = None) -> httpx.Response:
        return self._request('POST', path, json=json, retries=True)

    def post_once(self, path: str, *, json: dict | None = None) -> httpx.Response:
        """Неидемпотентные вызовы (ответ клиенту): без транспортных ретраев."""
        return self._request('POST', path, json=json, retries=False)
