"""Клиент webapi на поддельном транспорте: сеть не нужна, поведение проверяемо."""

from __future__ import annotations

import httpx

from app.bedolaga import MAX_PAGES, Bedolaga


def client(handler) -> Bedolaga:
    api = Bedolaga('http://bot', 'token')
    api.close()
    api._http = httpx.Client(base_url='http://bot', transport=httpx.MockTransport(handler))
    api._http_once = api._http
    return api


def paged_handler(counts: dict[str, int], page_size: int, log: list[dict] | None = None):
    """Отдаёт по `counts[status]` тикетов на статус, нарезая их страницами."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if log is not None:
            log.append(dict(params))
        status = params['status']
        offset = int(params['offset'])
        limit = int(params['limit'])
        base = 0 if status == 'open' else 1000
        ids = range(base + offset, min(base + counts[status], base + offset + limit))
        return httpx.Response(200, json=[{'id': i, 'status': status} for i in ids])

    return handler


def test_одна_неполная_страница_читается_одним_запросом():
    calls: list[dict] = []
    api = client(paged_handler({'open': 3, 'pending': 0}, page_size=200, log=calls))

    tickets = api.active_tickets()

    assert len(tickets) == 3
    assert len(calls) == 2  # по одному запросу на статус


def test_бэклог_больше_страницы_дочитывается_до_конца():
    """Раньше `limit=200` без offset молча терял всё, что не влезло."""
    api = client(paged_handler({'open': 250, 'pending': 30}, page_size=100))

    tickets = api.active_tickets(page_size=100)

    assert len(tickets) == 280


def test_ровно_целая_страница_требует_ещё_одного_запроса():
    calls: list[dict] = []
    api = client(paged_handler({'open': 100, 'pending': 0}, page_size=100, log=calls))

    tickets = api.active_tickets(page_size=100)

    assert len(tickets) == 100
    assert [int(c['offset']) for c in calls if c['status'] == 'open'] == [0, 100]


def test_сломанная_пагинация_не_вешает_цикл(caplog):
    """Эндпоинт игнорирует offset — листаем не бесконечно, а до предохранителя."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=[{'id': i, 'status': 'x'} for i in range(10)])

    api = client(handler)
    with caplog.at_level('WARNING', logger='app.bedolaga'):
        api.active_tickets(page_size=10)

    assert len(calls) == MAX_PAGES * 2
    assert any('не обработана' in record.message for record in caplog.records)


def test_страница_больше_потолка_урезается():
    calls: list[dict] = []
    api = client(paged_handler({'open': 1, 'pending': 0}, page_size=200, log=calls))

    api.active_tickets(page_size=10_000)

    assert all(int(c['limit']) == 200 for c in calls)
