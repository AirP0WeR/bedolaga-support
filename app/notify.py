"""Наблюдение: что сервис сделал с каждым тикетом — в топик админ-форума.

Это единственный интерфейс сервиса к человеку, поэтому сообщение должно
читаться с телефона без открытия чего-либо ещё: видно вопрос, видно ответ,
видно причину. Сбой отправки уведомления не должен ронять обработку тикета —
поэтому здесь всё гасится и логируется.
"""

from __future__ import annotations

import html
import logging

import httpx

log = logging.getLogger(__name__)

# Телеграм режет сообщения длиннее 4096 символов.
MAX_FIELD = 700


def _clip(text: str, limit: int = MAX_FIELD) -> str:
    text = (text or '').strip()
    return text if len(text) <= limit else text[: limit - 1] + '…'


class Notifier:
    def __init__(self, *, bot_token: str, chat_id: str, topic_id: str = '', timeout: float = 15.0):
        self._token = bot_token
        self._chat_id = chat_id
        self._topic_id = topic_id
        self._http = httpx.Client(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def close(self) -> None:
        self._http.close()

    def _send(self, text: str) -> None:
        if not self.enabled:
            return
        payload = {
            'chat_id': self._chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if self._topic_id:
            payload['message_thread_id'] = self._topic_id
        try:
            response = self._http.post(f'https://api.telegram.org/bot{self._token}/sendMessage', json=payload)
            if response.status_code >= 400:
                log.warning('Уведомление не ушло: HTTP %s %s', response.status_code, response.text[:200])
        except Exception:
            log.warning('Уведомление не ушло', exc_info=True)

    def answered(
        self, ticket_id: int, *, question: str, reply: str, confidence: float, topic: str, shadow: bool
    ) -> None:
        head = '📝 <b>Черновик</b> (клиенту не отправлен)' if shadow else '✅ <b>Ответ отправлен</b>'
        self._send(
            f'{head}\n'
            f'Тикет #{ticket_id} · {html.escape(topic or "без темы")} · уверенность {confidence:.2f}\n\n'
            f'<b>Вопрос:</b>\n{html.escape(_clip(question))}\n\n'
            f'<b>Ответ:</b>\n{html.escape(_clip(reply))}'
        )

    def escalated(self, ticket_id: int, *, question: str, reason: str) -> None:
        self._send(
            f'🔺 <b>Передан оператору</b>\n'
            f'Тикет #{ticket_id} · приоритет high\n\n'
            f'<b>Вопрос:</b>\n{html.escape(_clip(question))}\n\n'
            f'<b>Причина:</b> {html.escape(_clip(reason, 200))}'
        )

    def handed_over(self, ticket_id: int, *, escalated: bool) -> None:
        tail = ' Приоритет поднят — тикет мог остаться без внимания.' if escalated else ''
        self._send(f'👤 <b>Тикет #{ticket_id} ведёт человек</b> — сервис из него вышел.{tail}')

    def problem(self, text: str) -> None:
        self._send(f'⚠️ <b>Сбой сервиса поддержки</b>\n{html.escape(_clip(text, 500))}')

    def digest(self, digest) -> None:
        """Сводка за сутки: одно сообщение, по которому видно, чем занят сервис."""
        lines = [
            '📊 <b>Сводка за сутки</b>',
            f'с {digest.since.strftime("%d.%m %H:%M")} UTC',
            '',
            f'Ответов клиентам: {digest.answered}',
        ]
        if digest.answered_shadow:
            lines.append(f'Черновиков (клиенту не ушли): {digest.answered_shadow}')
        lines.append(f'Передано операторам: {digest.escalated}')
        if digest.handover:
            lines.append(f'Тикетов забрали люди: {digest.handover}')
        if digest.llm_errors:
            lines.append(f'Ошибок модели: {digest.llm_errors}')
        if digest.avg_confidence is not None:
            lines.append(f'Средняя уверенность: {digest.avg_confidence:.2f}')
        if digest.prompt_tokens or digest.completion_tokens:
            cached = f' (из них {digest.cached_tokens:,} из кеша)'.replace(',', ' ') if digest.cached_tokens else ''
            lines.append(f'Токенов на вход: {digest.prompt_tokens:,}{cached}'.replace(',', ' '))
            lines.append(f'Токенов на выход: {digest.completion_tokens:,}'.replace(',', ' '))
        if digest.topics:
            lines.append('')
            lines.append('<b>Темы:</b>')
            lines += [f'· {html.escape(topic)} — {count}' for topic, count in digest.topics]

        self._send('\n'.join(lines))

    def recovered(self, text: str) -> None:
        self._send(f'🟢 <b>Сервис поддержки в норме</b>\n{html.escape(_clip(text, 500))}')
