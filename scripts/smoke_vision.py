"""Проверка, что модель действительно читает скриншот из тикета."""

import base64
import io
import sys
from pathlib import Path

from dotenv import dotenv_values
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import OpenAIProvider

KB = """\
# FAQ

## Ошибка «App not supported»
Такое сообщение появляется, если приложение не передало идентификатор
устройства. Помогает переустановка приложения Happ и повторное добавление
ссылки подписки.

## Не работает подключение
Переподключите VPN и обновите подписку кнопкой «Обновить».
"""


def screenshot() -> str:
    image = Image.new('RGB', (760, 320), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 90, 720, 230], fill=(255, 255, 255), outline=(210, 210, 214), width=2)
    draw.text((70, 130), 'Happ', fill=(20, 20, 20))
    draw.text((70, 165), 'App not supported', fill=(200, 40, 40))
    draw.text((70, 195), 'code 156', fill=(90, 90, 90))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG')
    return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()


def main(model: str) -> None:
    v = dotenv_values(ROOT / '.env')
    provider = OpenAIProvider(
        api_key=v['OPENAI_API_KEY'],
        model=model,
        base_url=v.get('OPENAI_BASE_URL') or None,
        brand='VPN-сервиса',
    )
    context = {
        'title': 'Не подключается',
        'question': 'не работает, вот скрин',
        'conversation': 'Клиент: [скриншот] не работает, вот скрин',
        'account': 'Подписка: active\nЛимит устройств: 3',
        'images': [screenshot()],
    }
    verdict = provider.decide(knowledge=KB, context=context, confidence_threshold=0.7, max_reply_chars=1500)
    print(f'=== {model} ===')
    print('действие:', verdict.action, '| уверенность:', verdict.confidence)
    print('тема:', verdict.topic)
    print('ответ:', verdict.reply_text or '(пусто)')
    print('причина:', verdict.reason)


if __name__ == '__main__':
    main(sys.argv[1])
