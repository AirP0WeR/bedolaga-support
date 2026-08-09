"""Прогон набора обращений через живую базу знаний: проверка поведения модели.

Зачем отдельно от `tests/`: там всё детерминированное и бесплатное, здесь —
реальные вызовы модели, поэтому в CI этому не место. Гонять руками перед
сменой модели, правкой промпта и после заметных изменений FAQ.

Проверяется **поведение**, а не текст: вердикт (ответить или передать
человеку) плюс инварианты — какие слова обязаны быть в ответе и каких быть не
должно. Сравнения с эталонным ответом нет намеренно: формулировка модели
меняется от прогона к прогону, и такой тест краснел бы на перефразировке.

База знаний берётся живая — тот же FAQ через API плюс `kb/`, что и в бою.
Проверка идёт с тем же `Guard`, что цензурирует ответы клиенту, поэтому
запрещённая ссылка в ответе видна как эскалация, а не как зелёный кейс.

    python scripts/eval_cases.py --cases ~/cases.json
    python scripts/eval_cases.py --cases ~/cases.json --only "Возврат"

Формат кейса:

    {
      "name": "Устройства на STANDART",
      "question": "Сколько устройств на STANDART?",
      "account": "Подписки нет.",          # факты аккаунта, как их видит модель
      "expect": "answer",                   # answer | escalate
      "must_any": [["3", "три"]],           # каждая группа: хотя бы одно вхождение
      "must_not": ["30 устройств"]
    }
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bedolaga import Bedolaga
from app.config import Config
from app.kb import KnowledgeBase
from app.llm import build_provider


def check(case: dict, verdict) -> list[str]:
    """Чем этот кейс не сошёлся. Пустой список — сошёлся."""
    failures = []

    got = verdict.action
    if got != case['expect']:
        failures.append(f'вердикт {got}, ждали {case["expect"]}')
        # Дальше проверять текст бессмысленно: у эскалации его просто нет.
        return failures

    if got != 'answer':
        return failures

    reply = verdict.reply_text.lower()

    for group in case.get('must_any', []):
        if not any(word.lower() in reply for word in group):
            failures.append(f'нет ни одного из {group}')

    for word in case.get('must_not', []):
        if word.lower() in reply:
            failures.append(f'встретилось запретное «{word}»')

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description='Прогнать набор обращений через модель')
    parser.add_argument('--cases', required=True, help='json с кейсами')
    parser.add_argument('--only', default='', help='гонять только кейсы, у которых это есть в названии')
    parser.add_argument('--verbose', action='store_true', help='печатать ответы целиком')
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).expanduser().read_text(encoding='utf-8'))
    if args.only:
        cases = [case for case in cases if args.only.lower() in case['name'].lower()]
    if not cases:
        raise SystemExit('нечего гонять')

    cfg = Config()
    kb = KnowledgeBase(
        Bedolaga(cfg.bedolaga_url, cfg.bedolaga_token),
        kb_dir=cfg.kb_dir,
        languages=list(cfg.kb_languages),
        cache_path=cfg.kb_cache_path,
    )
    kb.refresh(force=True)
    knowledge = kb.text()
    provider = build_provider(cfg)

    print(f'Модель {cfg.llm_model}, база знаний {len(knowledge)} знаков, кейсов {len(cases)}\n')

    passed = 0
    started = time.monotonic()

    for case in cases:
        verdict = provider.decide(
            knowledge=knowledge,
            context={
                'title': case['name'],
                'question': case['question'],
                'conversation': f'Клиент: {case["question"]}',
                'account': case.get('account', ''),
                'images': [],
            },
            confidence_threshold=cfg.confidence_threshold,
            max_reply_chars=cfg.max_reply_chars,
        )

        failures = check(case, verdict)
        passed += not failures
        print(f'{"✓" if not failures else "✗"} {case["name"]} — {verdict.action} (conf {verdict.confidence:.2f})')

        for failure in failures:
            print(f'    ! {failure}')
        if verdict.action == 'escalate' and verdict.reason:
            print(f'    причина: {verdict.reason[:120]}')
        if verdict.reply_text and (args.verbose or failures):
            print(f'    → {verdict.reply_text[:400]}')

    print(f'\nИтог: {passed}/{len(cases)} за {time.monotonic() - started:.0f} с')
    return 0 if passed == len(cases) else 1


if __name__ == '__main__':
    sys.exit(main())
