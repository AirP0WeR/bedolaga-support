"""Очистка FAQ от разметки: всё, что здесь потеряется, модель не увидит."""

from __future__ import annotations

from app.kb import _render_faq, _render_local, _strip_html


def test_теги_выкидываются_текст_остаётся():
    assert _strip_html('<b>тег</b>') == 'тег'


def test_голый_знак_меньше_не_съедает_хвост():
    """Ради этого случая и переписывали: раньше от строки оставалось «трафик »."""
    assert _strip_html('трафик < 1 ГБ в сутки') == 'трафик < 1 ГБ в сутки'


def test_голый_знак_меньше_перед_тегом():
    assert _strip_html('a < b, а <b>это</b> тег') == 'a < b, а это тег'


def test_сущности_разворачиваются():
    assert _strip_html('Ключ &amp; значение') == 'Ключ & значение'
    assert _strip_html('&quot;Обновить&quot;') == '"Обновить"'


def test_неразрывный_пробел_становится_обычным():
    assert _strip_html('10&nbsp;ГБ') == '10 ГБ'


def test_абзацы_разъезжаются_по_строкам():
    assert _strip_html('<p>абзац</p><p>второй</p>') == 'абзац\n\nвторой'


def test_перенос_строки_тегом_br():
    assert _strip_html('строка<br>вторая') == 'строка\nвторая'


def test_пункты_списка_не_слипаются():
    assert _strip_html('<ul><li>раз</li><li>два</li></ul>') == 'раз\n\nдва'


def test_лишние_пустые_строки_схлопываются():
    assert _strip_html('<div><p>раз</p><br><br><br><p>два</p></div>') == 'раз\n\nдва'


def test_содержимое_script_не_попадает_в_текст():
    assert _strip_html('<p>текст</p><script>alert(1)</script>') == 'текст'


def test_незакрытый_тег_не_роняет_разбор():
    assert _strip_html('<p>текст') == 'текст'


def test_пустая_строка():
    assert _strip_html('') == ''


def test_faq_собирается_заголовками():
    pages = [
        {'title': 'Как подключить', 'content': '<p>Откройте бота.</p>'},
        {'title': 'Пустая', 'content': ''},
    ]
    assert _render_faq(pages, 'ru') == '# FAQ (ru)\n\n## Как подключить\n\nОткройте бота.'


def test_faq_без_страниц_пуст():
    assert _render_faq([], 'ru') == ''


def test_локальные_гайды_собираются_по_алфавиту(tmp_path):
    (tmp_path / 'b.md').write_text('вторая тема', encoding='utf-8')
    (tmp_path / 'a.md').write_text('первая тема', encoding='utf-8')
    assert _render_local(str(tmp_path)) == 'первая тема\n\nвторая тема'


def test_readme_каталога_не_попадает_в_промпт(tmp_path):
    """В README лежат инструкции для людей и выдуманные примеры."""
    (tmp_path / 'README.md').write_text('# Локальная база знаний\nЧего сюда не класть...', encoding='utf-8')
    (tmp_path / 'guide.md').write_text('Не подключается: обновить подписку.', encoding='utf-8')

    assert _render_local(str(tmp_path)) == 'Не подключается: обновить подписку.'


def test_отсутствующий_каталог_не_ломает_сборку():
    assert _render_local('/нет/такого/каталога') == ''
