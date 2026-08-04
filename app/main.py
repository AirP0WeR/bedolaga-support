"""Главный цикл: опрос тикетов, решение, действие.

Опрос выбран вместо вебхуков осознанно: кабинет бедолаги событий не эмитит
вовсе (в app/cabinet/ нет ни одного вызова event_emitter), поэтому обойтись
вебхуками нельзя. Побочная выгода — один тикет обрабатывается строго
последовательно, а сообщения, которые клиент шлёт очередью, к моменту опроса
успевают сложиться в законченную мысль.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime

from . import context as context_builder
from . import gate, llm
from .bedolaga import Bedolaga
from .config import Config
from .kb import KnowledgeBase
from .notify import Notifier
from .store import Store

log = logging.getLogger('support')


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api = Bedolaga(cfg.bedolaga_url, cfg.bedolaga_token)
        self.store = Store(cfg.state_path)
        self.notifier = Notifier(bot_token=cfg.tg_bot_token, chat_id=cfg.tg_chat_id, topic_id=cfg.tg_topic_id)
        self.kb = KnowledgeBase(
            self.api,
            kb_dir=cfg.kb_dir,
            languages=list(cfg.kb_languages),
            cache_path=cfg.kb_cache_path,
        )
        self.provider = llm.build_provider(cfg)
        self._stopping = False

    def stop(self, *_args) -> None:
        log.info('Останавливаюсь после текущего тикета')
        self._stopping = True

    def close(self) -> None:
        self.api.close()
        self.store.close()
        self.notifier.close()

    # --- обработка одного тикета ----------------------------------------

    def handle(self, ticket_id: int) -> None:
        # Всегда перечитываем тикет целиком: списочный эндпоинт отдаёт
        # тикеты без сообщений, а решать по ним нельзя.
        ticket = self.api.ticket(ticket_id)
        state = self.store.state(ticket_id)
        now = datetime.now(UTC)

        decision = gate.decide(
            ticket,
            state,
            now=now,
            max_ai_replies=self.cfg.max_ai_replies,
            debounce_sec=self.cfg.debounce_sec,
        )

        if decision == gate.BLACKLIST:
            self._hand_over(ticket)
            return
        if decision in (gate.SKIP, gate.DEFER):
            return

        messages = ticket.get('messages') or []
        last_message_id = messages[-1]['id'] if messages else None

        # Одно и то же сообщение обрабатываем один раз: иначе в теневом
        # режиме, где мы не отвечаем, каждый цикл заново дёргал бы модель.
        if last_message_id is not None and self.store.last_decided_message_id(ticket_id) == last_message_id:
            return

        question = next(
            (m.get('message_text') or '' for m in reversed(messages) if not m.get('is_from_admin')),
            '',
        )

        if decision == gate.ESCALATE:
            self._escalate(ticket_id, question=question, reason='правило ворот: вложение или лимит ответов')
            self.store.set_last_decided(ticket_id, last_message_id)
            return

        # Незакрытое намерение означает, что прошлая попытка ответа оборвалась
        # неизвестно на каком шаге. Второй ответ клиенту хуже, чем молчание.
        if self.store.pending_reply(ticket_id):
            log.warning('Тикет %s: прошлый ответ оборвался, отдаю человеку', ticket_id)
            self.store.clear_reply_intent(ticket_id)
            self._escalate(ticket_id, question=question, reason='прерванная отправка ответа')
            self.store.set_last_decided(ticket_id, last_message_id)
            return

        self._answer_or_escalate(ticket, question=question, last_message_id=last_message_id)

    def _answer_or_escalate(self, ticket: dict, *, question: str, last_message_id: int | None) -> None:
        ticket_id = ticket['id']
        self.kb.refresh()
        payload = context_builder.build(self.api, ticket)

        verdict = self.provider.decide(
            knowledge=self.kb.text(),
            context=payload,
            confidence_threshold=self.cfg.confidence_threshold,
            max_reply_chars=self.cfg.max_reply_chars,
        )

        if verdict.action == llm.ERROR:
            # Провайдер недоступен — тикет не трогаем совсем. Поведение
            # деградирует ровно до «как без ИИ»: ждёт человека.
            log.warning('Тикет %s: модель недоступна, отложил', ticket_id)
            return

        if not verdict.is_answer:
            self._escalate(ticket_id, question=question, reason=verdict.reason, topic=verdict.topic)
            self.store.set_last_decided(ticket_id, last_message_id)
            return

        if not self.cfg.reply_enabled:
            self.store.audit(
                ticket_id,
                'answer',
                question=question,
                reply=verdict.reply_text,
                reason=verdict.reason,
                confidence=verdict.confidence,
                topic=verdict.topic,
                model=self.provider.model,
                shadow=True,
            )
            self.notifier.answered(
                ticket_id,
                question=question,
                reply=verdict.reply_text,
                confidence=verdict.confidence,
                topic=verdict.topic,
                shadow=True,
            )
            self.store.set_last_decided(ticket_id, last_message_id)
            return

        # Перечитываем прямо перед отправкой: пока думала модель, в тикет мог
        # прийти живой админ или новое сообщение клиента.
        fresh = self.api.ticket(ticket_id)
        if gate.decide(fresh, self.store.state(ticket_id), now=datetime.now(UTC)) != gate.ASK_LLM:
            log.info('Тикет %s изменился, пока думала модель — ответ отменён', ticket_id)
            return

        self.store.begin_reply(ticket_id)
        message_id = self.api.reply(ticket_id, verdict.reply_text)
        self.store.finish_reply(ticket_id, message_id)
        self.store.set_last_decided(ticket_id, last_message_id)
        self.store.audit(
            ticket_id,
            'answer',
            question=question,
            reply=verdict.reply_text,
            reason=verdict.reason,
            confidence=verdict.confidence,
            topic=verdict.topic,
            model=self.provider.model,
        )
        self.notifier.answered(
            ticket_id,
            question=question,
            reply=verdict.reply_text,
            confidence=verdict.confidence,
            topic=verdict.topic,
            shadow=False,
        )
        log.info('Тикет %s: ответили (сообщение %s)', ticket_id, message_id)

    def _escalate(self, ticket_id: int, *, question: str, reason: str, topic: str = '') -> None:
        self.api.set_priority(ticket_id, 'high')
        self.store.mark_escalated(ticket_id)
        self.store.audit(ticket_id, 'escalate', question=question, reason=reason, topic=topic)
        self.notifier.escalated(ticket_id, question=question, reason=reason)
        log.info('Тикет %s: передан оператору (%s)', ticket_id, reason)

    def _hand_over(self, ticket: dict) -> None:
        """Выход из тикета, в котором появилось чужое админское сообщение.

        Если за ним стоит живой человек — просто уходим. Если сообщение
        обезличено (чужая интеграция или наш собственный потерянный ответ),
        человека может не быть вовсе, и молчаливый выход оставил бы клиента
        без ответа навсегда — поэтому поднимаем приоритет.
        """
        ticket_id = ticket['id']
        if self.store.state(ticket_id).human_seen:
            return

        human = gate.has_human_admin_message(ticket)
        self.store.mark_human(ticket_id)

        if not human:
            self.api.set_priority(ticket_id, 'high')
            self.store.mark_escalated(ticket_id)

        self.store.audit(ticket_id, 'handover', reason='человек в тикете' if human else 'неопознанный ответ админа')
        self.notifier.handed_over(ticket_id, escalated=not human)

    # --- цикл -------------------------------------------------------------

    def run_once(self) -> None:
        for ticket in self.api.active_tickets():
            if self._stopping:
                return
            try:
                self.handle(ticket['id'])
            except Exception:
                log.exception('Тикет %s: ошибка обработки', ticket.get('id'))

    def run(self) -> None:
        self.kb.refresh(force=True)
        if self.kb.is_empty:
            raise RuntimeError('База знаний пуста: проверьте FAQ бота и каталог kb/')

        mode = 'боевой' if self.cfg.reply_enabled else 'теневой (клиенту не отвечаем)'
        log.info('Старт, режим %s, модель %s, опрос раз в %s с', mode, self.provider.model, self.cfg.poll_interval)

        while not self._stopping:
            try:
                self.run_once()
            except Exception:
                log.exception('Цикл упал, жду следующего')
            for _ in range(self.cfg.poll_interval):
                if self._stopping:
                    break
                time.sleep(1)


def main() -> None:
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    cfg.require()

    service = Service(cfg)
    signal.signal(signal.SIGTERM, service.stop)
    signal.signal(signal.SIGINT, service.stop)
    try:
        if '--once' in sys.argv:
            service.kb.refresh(force=True)
            service.run_once()
        else:
            service.run()
    finally:
        service.close()


if __name__ == '__main__':
    main()
