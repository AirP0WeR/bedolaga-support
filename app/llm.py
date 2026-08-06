"""Обращение к модели и разбор её вердикта.

Провайдер спрятан за одним методом: сменить OpenAI на другого поставщика —
значит написать второй класс с тем же `decide`, ничего больше не трогая.

Любая неоднозначность трактуется в пользу человека: не разобрали ответ,
не хватило уверенности, пустой или неправдоподобно длинный текст — тикет
уходит оператору. Отдельно от этого стоит ошибка связи: она возвращает
`ERROR`, и тикет не трогают вообще, чтобы попробовать в следующем цикле.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace

from .guard import Guard, build_guard
from .prompt import VERDICT_SCHEMA, system_prompt, user_prompt

log = logging.getLogger(__name__)

ANSWER = 'answer'
ESCALATE = 'escalate'
ERROR = 'error'


@dataclass(frozen=True)
class Usage:
    """Токены одного обращения. Кешированные считаются и в prompt_tokens."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class Verdict:
    action: str
    reply_text: str = ''
    reason: str = ''
    topic: str = ''
    confidence: float = 0.0
    usage: Usage = field(default_factory=Usage)

    @property
    def is_answer(self) -> bool:
        return self.action == ANSWER


def _sanitize(raw: dict, *, confidence_threshold: float, max_reply_chars: int, guard: Guard | None = None) -> Verdict:
    """Привести ответ модели к вердикту, отбраковав всё сомнительное."""
    action = raw.get('action')
    reply = (raw.get('reply_text') or '').strip()
    reason = (raw.get('reason') or '').strip()
    topic = (raw.get('topic') or '').strip()
    try:
        confidence = float(raw.get('confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if action != ANSWER:
        return Verdict(ESCALATE, reason=reason or 'модель передала оператору', topic=topic, confidence=confidence)

    if not reply:
        return Verdict(ESCALATE, reason='пустой ответ модели', topic=topic, confidence=confidence)

    if len(reply) > max_reply_chars:
        return Verdict(
            ESCALATE,
            reason=f'ответ длиннее допустимого ({len(reply)} символов)',
            topic=topic,
            confidence=confidence,
        )

    if confidence < confidence_threshold:
        return Verdict(
            ESCALATE,
            reason=f'низкая уверенность {confidence:.2f} (порог {confidence_threshold})',
            topic=topic,
            confidence=confidence,
        )

    rejected = guard.reject(reply) if guard else None
    if rejected:
        return Verdict(ESCALATE, reason=f'пост-фильтр: {rejected}', topic=topic, confidence=confidence)

    return Verdict(ANSWER, reply_text=reply, reason=reason, topic=topic, confidence=confidence)


def _usage(response) -> Usage:
    """Расход токенов из ответа провайдера. Его может не быть — это не ошибка."""
    raw = getattr(response, 'usage', None)
    if raw is None:
        return Usage()
    details = getattr(raw, 'prompt_tokens_details', None)
    return Usage(
        prompt_tokens=getattr(raw, 'prompt_tokens', 0) or 0,
        completion_tokens=getattr(raw, 'completion_tokens', 0) or 0,
        cached_tokens=getattr(details, 'cached_tokens', 0) or 0,
    )


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        brand: str = '',
        guard: Guard | None = None,
        timeout: float = 120.0,
    ):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)
        self.model = model
        self.brand = brand
        self.guard = guard

    def decide(
        self,
        *,
        knowledge: str,
        context: dict,
        confidence_threshold: float,
        max_reply_chars: int,
    ) -> Verdict:
        content: list[dict] = [{'type': 'text', 'text': user_prompt(context)}]
        for image in context.get('images') or []:
            content.append({'type': 'image_url', 'image_url': {'url': image}})

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {'role': 'system', 'content': system_prompt(knowledge, self.brand)},
                    {'role': 'user', 'content': content},
                ],
                response_format={
                    'type': 'json_schema',
                    'json_schema': {'name': 'verdict', 'strict': True, 'schema': VERDICT_SCHEMA},
                },
            )
        except Exception:
            log.warning('Обращение к модели не удалось', exc_info=True)
            return Verdict(ERROR, reason='модель недоступна')

        usage = _usage(response)
        text = (response.choices[0].message.content or '').strip()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            log.warning('Модель вернула неразбираемый ответ: %.200s', text)
            return Verdict(ESCALATE, reason='неразбираемый ответ модели', usage=usage)

        verdict = _sanitize(
            raw,
            confidence_threshold=confidence_threshold,
            max_reply_chars=max_reply_chars,
            guard=self.guard,
        )
        # Токены потрачены независимо от того, чем кончился разбор.
        return replace(verdict, usage=usage)


def build_provider(cfg) -> OpenAIProvider:
    if cfg.llm_provider != 'openai':
        raise RuntimeError(f'Провайдер {cfg.llm_provider!r} не поддерживается')
    return OpenAIProvider(
        api_key=cfg.openai_api_key,
        model=cfg.llm_model,
        base_url=cfg.openai_base_url,
        brand=cfg.brand_name,
        guard=build_guard(cfg),
    )
