# -*- coding: utf-8 -*-
"""
Автозаполнение Яндекс.Форм (v5, финальная)
Ключевое:
  • pick_type кликает ТОЛЬКО по кнопкам aria-label^='Добавить:' панели
  • Никаких ложных срабатываний на пункты dropdown активного вопроса
  • Перезапрос локатора по q_index после каждой мутации (React рвёт DOM)
  • Везде .fill() вместо .type()
  • set_required проверяет результат после клика
"""

import os
import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

INPUT_FILE = Path(os.getenv("QUESTIONS_FILE", "questions.txt"))
USER_DATA_DIR = Path(os.getenv("YANDEX_PROFILE_DIR", "./yandex_profile"))
SLOW_MO = 60
ADMIN_URL = 'https://forms.yandex.ru/admin/'
EDITOR_URL_RE = re.compile(r'/admin/[a-f0-9]+/edit')

TYPE_ARIA = {
    'text':     ['Добавить: Короткий текст', 'Добавить: Текст'],
    'long':     ['Добавить: Длинный текст', 'Добавить: Развёрнутый ответ',
                 'Добавить: Длинный ответ'],
    'radio':    ['Добавить: Один вариант', 'Добавить: Список'],
    'multi':    ['Добавить: Несколько вариантов'],
    'int':      ['Добавить: Целое число', 'Добавить: Число'],
    'scale':    ['Добавить: Оценка по шкале', 'Добавить: Таблица',
                 'Добавить: Матрица', 'Добавить: Оценка'],
    'dropdown': ['Добавить: Выпадающий список'],
    'section':  ['Добавить: Текст без вопроса'],
}

DISPLAY_TYPE_TEXT = {
    'text':     ['Короткий текст'],
    'long':     ['Длинный ответ', 'Длинный текст', 'Развёрнутый ответ'],
    'radio':    ['Один вариант', 'Список'],
    'multi':    ['Несколько вариантов'],
    'dropdown': ['Выпадающий список'],
    'scale':    ['Оценка по шкале', 'Таблица', 'Матрица'],
    'int':      ['Целое число', 'Число'],
    'section':  ['Текст без вопроса'],
}

TYPE_MAP_SHORT = {'T': 'text', 'L': 'long', 'I': 'int',
                  'R': 'radio', 'M': 'multi', 'S': 'scale'}


# ============================================================
#                       ПАРСЕР
# ============================================================
def _looks_long(label: str) -> bool:
    markers = ['опишите', 'расскажите', 'перечислите', 'что делали',
               'предпочтения', 'что может помешать', 'пробовали ли']
    if any(m in label.lower() for m in markers):
        return True
    return len(label) > 80


def _parse_question_block(lines):
    q = {'label': '', 'type': None, 'description': None,
         'options': [], 'criteria': [], 'required': True, 'widget': None}
    label_lines, desc_lines = [], []
    options_lines, criteria_lines = [], []
    section = 'label'

    for raw in lines:
        line = raw.rstrip()
        s = line.strip()

        if s == '[OPT]':
            q['required'] = False
            continue
        if s == '[DD]':
            q['type'] = 'dropdown'
            continue
        m = re.match(r'^\[([TLIRMS])\]$', s)
        if m:
            q['type'] = TYPE_MAP_SHORT[m.group(1)]
            continue
        m = re.match(r'^\[W\]\s*(\S+)\s*$', s)
        if m:
            q['widget'] = m.group(1).lower()
            continue
        if s == '[A]':
            section = 'options'
            continue
        if s == '[C]':
            section = 'criteria'
            continue
        m = re.match(r'^\[D\]\s*(.*)$', s)
        if m:
            if m.group(1):
                desc_lines.append(m.group(1))
            continue

        if section == 'label':
            label_lines.append(line)
        elif section == 'options':
            options_lines.append(line)
        elif section == 'criteria':
            criteria_lines.append(line)

    q['label'] = '\n'.join(label_lines).strip()
    q['description'] = '\n'.join(desc_lines).strip() or None
    q['options'] = [l.strip() for l in options_lines if l.strip()]
    q['criteria'] = [l.strip() for l in criteria_lines if l.strip()]

    if q['type'] is None:
        if q['options']:
            q['type'] = 'multi' if len(q['options']) > 5 else 'radio'
        else:
            q['type'] = 'long' if _looks_long(q['label']) else 'text'

    if not q['label']:
        return None
    return q


def parse_questions(file_path: Path):
    lines = file_path.read_text(encoding='utf-8').lstrip('\ufeff').splitlines()
    items = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()

        if s == '[PAGE]':
            title = lines[i + 1].strip() if i + 1 < len(lines) else ''
            items.append({'kind': 'page', 'title': title})
            i += 2
            continue

        if s == '[SECTION]':
            i += 1
            block = []
            while (i < len(lines)
                   and lines[i].strip() not in ('[Q]', '[PAGE]', '[SECTION]')):
                block.append(lines[i])
                i += 1
            text = '\n'.join(block).strip()
            if text:
                items.append({'kind': 'section', 'text': text})
            continue

        if s == '[Q]':
            block = []
            i += 1
            while (i < len(lines)
                   and lines[i].strip() not in ('[Q]', '[PAGE]', '[SECTION]')):
                block.append(lines[i])
                i += 1
            q = _parse_question_block(block)
            if q:
                items.append({'kind': 'question', **q})
            else:
                print('  ⚠️ Пустой блок пропущен')
            continue

        i += 1
    return items


# ============================================================
#                       HELPERS
# ============================================================
def _vis(page, sel) -> bool:
    try:
        loc = page.locator(sel).first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


def _force_click(loc, timeout=3000):
    """Сначала обычный клик (с actionability), потом force, потом DOM."""
    try:
        loc.first.wait_for(state='attached', timeout=timeout)
    except Exception:
        pass
    try:
        loc.first.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    # 1) обычный клик — упадёт, если элемент перекрыт
    try:
        loc.first.click(timeout=1500)
        return True
    except Exception:
        pass
    # 2) force
    try:
        loc.first.click(timeout=timeout, force=True)
        return True
    except Exception:
        pass
    # 3) DOM-событие
    try:
        handle = loc.first.element_handle()
        if handle:
            handle.evaluate("el => el.click()")
            return True
    except Exception:
        pass
    return False


def click_first(page, selectors, timeout=6000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state='visible', timeout=timeout)
            loc.scroll_into_view_if_needed()
            loc.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def is_in_editor(page) -> bool:
    return bool(EDITOR_URL_RE.search(page.url))


def count_questions(page) -> int:
    try:
        return page.locator('div.Question').count()
    except Exception:
        return 0


def wait_for_editor(page):
    print('→ Жду загрузки редактора...')
    for sel in ['button.Pages-FooterButton', 'div.AdminContent-Content',
                'text=Добавить вопрос']:
        try:
            page.wait_for_selector(sel, timeout=10000, state='visible')
            print('  ✓ Редактор готов')
            return
        except PWTimeout:
            continue
    print('  ⚠️ Редактор не подтверждён, продолжаю.')


def safe_screenshot(page, name: str):
    try:
        if not page.is_closed():
            page.screenshot(path=f'error_{name}_{int(time.time())}.png')
    except Exception:
        pass


# ============================================================
#                       PICKER
# ============================================================
def is_type_picker_open(page) -> bool:
    try:
        loc = page.locator('button[aria-label^="Добавить:"]')
        n = loc.count()
        for i in range(min(30, n)):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return _vis(page, 'text=Выберите вопрос')


def wait_for_picker(page, timeout=6000) -> bool:
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if is_type_picker_open(page):
            return True
        page.wait_for_timeout(150)
    return is_type_picker_open(page)


def wait_for_picker_closed(page, timeout=6000) -> bool:
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if not is_type_picker_open(page):
            return True
        page.wait_for_timeout(200)
    return not is_type_picker_open(page)


def expand_picker_if_needed(page):
    """Только при открытой панели."""
    if not is_type_picker_open(page):
        return False
    for sel in ['button:has-text("Показать все вопросы")',
                'a:has-text("Показать все вопросы")',
                'text=Показать все вопросы']:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                _force_click(loc)
                page.wait_for_timeout(600)
                return True
        except Exception:
            continue
    return False


def open_type_picker(page, is_first: bool) -> bool:
    try:
        page.keyboard.press('Escape')
        page.wait_for_timeout(200)
    except Exception:
        pass

    if is_type_picker_open(page):
        return True

    # 1) "+" под последним вопросом
    if not is_first:
        try:
            last_q = page.locator('div.Question').last
            if last_q.count() > 0:
                add_below = last_q.locator('button.AddQuestion_position_bottom')
                if add_below.count() > 0:
                    try:
                        add_below.first.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    _force_click(add_below)
                    page.wait_for_timeout(500)
                    if wait_for_picker(page, timeout=3000):
                        return True
                    try:
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(200)
                    except Exception:
                        pass
        except Exception:
            pass

    # 2) футер
    try:
        btn = page.locator('button.Pages-FooterButton').filter(
            has_text=re.compile(r'^Добавить вопрос$')).first
        if btn.count() > 0:
            try:
                btn.scroll_into_view_if_needed()
            except Exception:
                pass
            _force_click(btn)
            page.wait_for_timeout(500)
    except Exception as e:
        print(f'      ⚠️ футер: {e}')

    return wait_for_picker(page, timeout=4000)


def pick_type(page, qtype: str) -> bool:
    """Клик ТОЛЬКО по кнопкам панели (aria-label^='Добавить:')."""
    if not is_type_picker_open(page):
        print(f'      ⚠️ pick_type: панель закрыта, тип {qtype} не выбираю')
        return False

    arias = TYPE_ARIA.get(qtype, [])

    def try_arias() -> bool:
        for aria in arias:
            try:
                loc = page.locator(f'button[aria-label="{aria}"]')
                n = loc.count()
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:
                        continue
                    if _force_click(el):
                        page.wait_for_timeout(500)
                        closed = wait_for_picker_closed(page, timeout=5000)
                        if closed:
                            print(f'      ✓ aria: {aria}')
                            return True
                        print(f'      ⚠️ клик по aria: {aria}, панель не закрылась')
                        try:
                            page.keyboard.press('Escape')
                            page.wait_for_timeout(200)
                        except Exception:
                            pass
                        return False
            except Exception:
                continue
        return False

    if try_arias():
        return True

    expand_picker_if_needed(page)
    page.wait_for_timeout(400)
    if is_type_picker_open(page) and try_arias():
        return True

    print(f'      ⚠️ не нашёл кнопку для типа {qtype} в панели')
    safe_screenshot(page, f'pick_{qtype}')
    return False


# ============================================================
#                    ТИП: ЧТЕНИЕ / КОНВЕРТАЦИЯ
# ============================================================
def read_question_type(question_locator) -> str:
    try:
        h4 = question_locator.locator('h4[data-qa="type-select"]').first
        if h4.count() > 0:
            t = (h4.inner_text() or '').strip()
            if t:
                return t
    except Exception:
        pass
    try:
        btn = question_locator.locator('button.BaseQuestion-TypeSelect').first
        if btn.count() > 0:
            return (btn.inner_text() or '').strip()
    except Exception:
        pass
    return ''


def verify_question_type(question_locator, expected_type: str) -> bool:
    actual = read_question_type(question_locator)
    expected_options = DISPLAY_TYPE_TEXT.get(expected_type, [])
    if not actual:
        return True
    low = actual.lower()
    for exp in expected_options:
        if exp.lower() in low:
            return True
    print(f'      ⚠️ ТИП НЕ СОВПАЛ: ожидался «{expected_type}», получен «{actual}»')
    return False


def convert_question_type(page, question_locator, target_type: str) -> bool:
    target_texts = DISPLAY_TYPE_TEXT.get(target_type, [])
    if not target_texts:
        return False
    try:
        btn = question_locator.locator('button.BaseQuestion-TypeSelect').first
        if btn.count() == 0:
            return False
        _force_click(btn)
        page.wait_for_timeout(500)
        for t in target_texts:
            try:
                opt = page.locator(
                    '[role="option"], [role="listbox"] [role="option"], '
                    'div[class*="SelectOption"], div[class*="Option"]'
                ).filter(has_text=re.compile(rf'^{re.escape(t)}$')).first
                if opt.count() > 0 and opt.is_visible():
                    _force_click(opt)
                    page.wait_for_timeout(700)
                    if verify_question_type(question_locator, target_type):
                        print(f'      ✓ тип исправлен на «{t}»')
                        return True
                    break
            except Exception:
                continue
        page.keyboard.press('Escape')
        return False
    except Exception as e:
        print(f'      ⚠️ конвертация типа: {e}')
        try:
            page.keyboard.press('Escape')
        except Exception:
            pass
        return False


# ============================================================
#             ГЛАВНОЕ: перезапрос локатора по индексу
# ============================================================
def qat(page, q_index: int):
    try:
        loc = page.locator('div.Question').nth(q_index)
        loc.wait_for(state='visible', timeout=4000)
        return loc
    except Exception:
        return None


# ============================================================
#                       ПОЛЯ
# ============================================================
def fill_title(page, q_index: int, label: str) -> bool:
    for attempt in range(3):
        try:
            q = qat(page, q_index)
            if q is None:
                page.wait_for_timeout(300)
                continue
            box = q.locator(
                'textarea[placeholder="Введите текст вопроса"], '
                'textarea[placeholder*="текст вопроса" i]'
            ).first
            box.wait_for(state='visible', timeout=4000)
            box.click()
            page.keyboard.press('Control+A')
            page.keyboard.press('Delete')
            box.fill(label)
            print(f'      ✓ заголовок: {label[:50]!r}')
            return True
        except Exception as e:
            if attempt == 2:
                print(f'      ⚠️ заголовок: {e}')
                safe_screenshot(page, 'title')
                return False
            page.wait_for_timeout(400)
    return False


def _find_visible_desc(q):
    sel = ('textarea[placeholder*="пояснени" i], '
           'textarea[placeholder*="добавьте пояснени" i]')
    ta = q.locator(sel)
    for i in range(ta.count()):
        try:
            el = ta.nth(i)
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def fill_description(page, q_index: int, text: str) -> bool:
    if not text:
        return True
    for attempt in range(4):
        try:
            q = qat(page, q_index)
            if q is None:
                page.wait_for_timeout(300)
                continue

            el = _find_visible_desc(q)
            if el is None:
                clicked = False
                for bsel in [
                    'button.BaseQuestion-Action:has-text("Добавить пояснение")',
                    'button:has-text("Добавить пояснение")',
                    'span:has-text("Добавить пояснение")',
                ]:
                    try:
                        btn = q.locator(bsel).first
                        if btn.count() > 0:
                            _force_click(btn)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    print('      ⚠️ кнопка «Добавить пояснение» не найдена')
                    return False
                for _ in range(8):
                    page.wait_for_timeout(200)
                    q = qat(page, q_index)
                    if q is None:
                        continue
                    el = _find_visible_desc(q)
                    if el is not None:
                        break
                if el is None:
                    print('      ⚠️ поле пояснения не появилось')
                    return False

            q = qat(page, q_index)
            if q is None:
                page.wait_for_timeout(300)
                continue
            el = _find_visible_desc(q)
            if el is None:
                continue

            el.click()
            page.keyboard.press('Control+A')
            page.keyboard.press('Delete')
            el.fill(text)
            print(f'      ✓ пояснение: {text[:50]!r}')
            return True
        except Exception as e:
            if attempt == 3:
                print(f'      ⚠️ пояснение: {e}')
                safe_screenshot(page, 'desc')
                return False
            page.wait_for_timeout(400)
    return False


def set_required(page, q_index: int, required: bool) -> bool:
    for attempt in range(4):
        try:
            q = qat(page, q_index)
            if q is None:
                page.wait_for_timeout(200)
                continue
            inp = q.locator(
                'label.BaseQuestion-RequiredSwitch input[role="switch"]'
            ).first
            if inp.count() == 0:
                print('      ⚠️ тумблер «Обязательный» не найден')
                return True

            def is_on():
                try:
                    return bool(inp.is_checked())
                except Exception:
                    return None

            cur = is_on()
            if cur is None:
                page.wait_for_timeout(200)
                continue
            if cur == required:
                print(f'      ✓ обязательность: {"Да" if required else "Нет"}')
                return True

            for _ in range(3):
                try:
                    inp.click(timeout=1500, force=True)
                except Exception:
                    try:
                        inp.evaluate("el => el.click()")
                    except Exception:
                        pass
                page.wait_for_timeout(250)
                if is_on() == required:
                    print(f'      ✓ обязательность: {"Да" if required else "Нет"}')
                    return True
        except Exception:
            pass
        page.wait_for_timeout(300)

    print(f'      ⚠️ обязательность не переключилась (нужно {required})')
    return False


# ============================================================
#                    ВАРИАНТЫ ОТВЕТА
# ============================================================
def _option_textareas(q):
    return q.locator(
        'textarea[placeholder="Введите вариант ответа"], '
        'textarea[placeholder*="вариант ответа" i]'
    )


def _click_add_option(page, q_index: int) -> bool:
    for attempt in range(3):
        q = qat(page, q_index)
        if q is None:
            page.wait_for_timeout(200)
            continue
        before = _option_textareas(q).count()
        for sel in ['button.ChoicesQuestion-AddButton',
                    'button:has-text("Добавить вариант")',
                    'button:has-text("Добавить ответ")']:
            try:
                btn = q.locator(sel).first
                if btn.count() == 0:
                    continue
                try:
                    btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                if not _force_click(btn):
                    continue
                for _ in range(15):
                    page.wait_for_timeout(120)
                    q2 = qat(page, q_index)
                    if q2 is None:
                        continue
                    if _option_textareas(q2).count() > before:
                        return True
            except Exception:
                continue
        page.wait_for_timeout(200)
    return False


def fill_options(page, q_index: int, options) -> None:
    n_target = len(options)
    q = qat(page, q_index)
    if q is None:
        print('      ⚠️ вопрос исчез перед заполнением опций')
        return
    cur = _option_textareas(q).count()
    print(f'      → стартовых textarea-вариантов: {cur}, нужно: {n_target}')

    max_attempts = n_target + 30
    stalled = 0
    for _ in range(max_attempts):
        q = qat(page, q_index)
        if q is None:
            break
        cur = _option_textareas(q).count()
        if cur >= n_target:
            break
        if _click_add_option(page, q_index):
            stalled = 0
        else:
            stalled += 1
            if stalled >= 3:
                break

    q = qat(page, q_index)
    real = _option_textareas(q).count() if q is not None else 0
    print(f'      → фактически textarea: {real}')

    filled = 0
    for i, opt in enumerate(options):
        if i >= real:
            break
        ok = False
        for attempt in range(3):
            try:
                q = qat(page, q_index)
                if q is None:
                    page.wait_for_timeout(200)
                    continue
                ta = _option_textareas(q)
                if ta.count() <= i:
                    page.wait_for_timeout(200)
                    continue
                el = ta.nth(i)
                el.scroll_into_view_if_needed()
                el.click()
                page.keyboard.press('Control+A')
                page.keyboard.press('Delete')
                el.fill(opt)
                ok = True
                break
            except Exception:
                page.wait_for_timeout(200)
                continue
        if ok:
            filled += 1
        else:
            print(f'      ⚠️ не заполнил вариант #{i + 1}: {opt[:40]!r}')

    print(f'      → заполнено: {filled}/{n_target} (в DOM: {real})')


# ============================================================
#                       ВИДЖЕТ
# ============================================================
def is_numeric_scale(options) -> bool:
    if len(options) not in (3, 5, 7, 10):
        return False
    try:
        nums = [int(str(o).strip()) for o in options]
    except (ValueError, TypeError):
        return False
    return nums == list(range(1, len(nums) + 1))


def _switch_widget(page, q_index: int, widget_text: str) -> bool:
    value_map = {'В один ряд': 'onerow', 'Звёзды': 'stars', 'Плитка': 'radio'}
    val = value_map.get(widget_text)
    for attempt in range(3):
        q = qat(page, q_index)
        if q is None:
            page.wait_for_timeout(200)
            continue
        if val:
            try:
                inp = q.locator(
                    f'input.g-segmented-radio-group__option-control[value="{val}"]'
                ).first
                if inp.count() > 0:
                    for _ in range(2):
                        try:
                            inp.click(timeout=1500, force=True)
                        except Exception:
                            try:
                                inp.evaluate("el => el.click()")
                            except Exception:
                                pass
                        page.wait_for_timeout(250)
                        try:
                            if inp.is_checked():
                                print(f'      ✓ виджет: «{widget_text}»')
                                return True
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            opt = q.locator('label.g-segmented-radio-group__option')\
                .filter(has_text=re.compile(
                    rf'^\s*{re.escape(widget_text)}\s*$')).first
            if opt.count() > 0 and opt.is_visible():
                _force_click(opt)
                page.wait_for_timeout(300)
                q = qat(page, q_index)
                if q is not None and val:
                    try:
                        inp = q.locator(
                            f'input.g-segmented-radio-group__option-control[value="{val}"]'
                        ).first
                        if inp.count() > 0 and inp.is_checked():
                            print(f'      ✓ виджет (label): «{widget_text}»')
                            return True
                    except Exception:
                        pass
        except Exception:
            pass

    print(f'      ⚠️ виджет «{widget_text}» не удалось активировать')
    return False


def set_widget_onerow(page, q_index: int) -> bool:
    return _switch_widget(page, q_index, 'В один ряд')


def set_widget_stars(page, q_index: int) -> bool:
    return _switch_widget(page, q_index, 'Звёзды')


# ============================================================
#                       МАТРИЦА (scale)
# ============================================================
def _find_add_button_in_section(question_locator, section_titles, order_index):
    for title in section_titles:
        try:
            xpath = (
                f'.//*[normalize-space(text())="{title}"]'
                f'/following::button[.//span[normalize-space(text())="Добавить"]][1]'
            )
            btn = question_locator.locator(f'xpath={xpath}')
            if btn.count() > 0 and btn.first.is_visible():
                return btn.first
        except Exception:
            continue
    try:
        all_add = question_locator.locator('button').filter(
            has_text=re.compile(r'^\s*Добавить\s*$'))
        if all_add.count() > order_index:
            return all_add.nth(order_index)
    except Exception:
        pass
    return None


def _click_add_until_count(page, add_btn, counter_locator, target_count,
                           max_attempts=40, label='элемент'):
    if add_btn is None:
        print(f'      ⚠️ кнопка «Добавить» для «{label}» не найдена')
        return counter_locator.count()
    cur = counter_locator.count()
    attempts = 0
    while cur < target_count and attempts < max_attempts:
        attempts += 1
        try:
            add_btn.scroll_into_view_if_needed()
            add_btn.click(timeout=4000)
            page.wait_for_timeout(250)
            new_cnt = counter_locator.count()
            if new_cnt == cur:
                page.wait_for_timeout(250)
                new_cnt = counter_locator.count()
                if new_cnt == cur:
                    print(f'      ⚠️ «{label}» не добавился (осталось {cur})')
                    break
            cur = new_cnt
        except Exception as e:
            print(f'      ⚠️ клик «Добавить {label}»: {e}')
            break
    return cur


def fill_scale(page, q_index: int, criteria, options):
    try:
        q = qat(page, q_index)
        if q is None:
            raise RuntimeError('вопрос не найден')
        crit_inputs = q.locator('input[placeholder="Введите критерий"]')
        crit_inputs.first.wait_for(state='visible', timeout=10000)
        add_crit = _find_add_button_in_section(
            q, ['Критерии', 'Строки', 'Вопросы'], order_index=0)
        _click_add_until_count(page, add_crit, crit_inputs, len(criteria),
                               label='критерий')
        filled_c = 0
        for i, c in enumerate(criteria):
            for attempt in range(3):
                try:
                    q = qat(page, q_index)
                    ci = q.locator('input[placeholder="Введите критерий"]')
                    el = ci.nth(i)
                    el.click()
                    page.keyboard.press('Control+A')
                    page.keyboard.press('Delete')
                    el.fill(c)
                    filled_c += 1
                    break
                except Exception:
                    page.wait_for_timeout(200)
        print(f'      → критериев: {filled_c}/{len(criteria)}')

        q = qat(page, q_index)
        opt_inputs = q.locator('input[placeholder="Введите вариант ответа"]')
        opt_inputs.first.wait_for(state='visible', timeout=8000)
        add_opt = _find_add_button_in_section(
            q, ['Ответы', 'Варианты ответа', 'Варианты', 'Столбцы'],
            order_index=1)
        _click_add_until_count(page, add_opt, opt_inputs, len(options),
                               max_attempts=25, label='вариант ответа')
        filled_o = 0
        for i, o in enumerate(options):
            for attempt in range(3):
                try:
                    q = qat(page, q_index)
                    oi = q.locator('input[placeholder="Введите вариант ответа"]')
                    el = oi.nth(i)
                    el.click()
                    page.keyboard.press('Control+A')
                    page.keyboard.press('Delete')
                    el.fill(o)
                    filled_o += 1
                    break
                except Exception:
                    page.wait_for_timeout(200)
        print(f'      → вариантов: {filled_o}/{len(options)}')
    except Exception as e:
        print(f'      ⚠️ Ошибка матрицы: {e}')
        safe_screenshot(page, 'scale')


# ============================================================
#                       СТРАНИЦА / СЕКЦИЯ
# ============================================================
def rename_page_in_sidebar(page, title: str) -> bool:
    try:
        cands = page.locator('text=/^Страница\\s+\\d+$/')
        if cands.count() == 0:
            return False
        target = cands.last
        target.scroll_into_view_if_needed()
        target.dblclick(timeout=3000)
        page.wait_for_timeout(400)
        page.keyboard.press('Control+A')
        page.keyboard.press('Delete')
        page.keyboard.type(title, delay=2)
        page.keyboard.press('Enter')
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        print(f'      ⚠️ rename_page_in_sidebar: {e}')
        return False


def add_page(page, title: str) -> bool:
    print(f'  [PAGE] {title}')
    try:
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        page.wait_for_timeout(500)
    except Exception:
        pass
    clicked = False
    for attempt in range(4):
        for name_re in [r'^Добавить страницу$', r'^Новая страница$']:
            try:
                btn = page.get_by_role('button', name=re.compile(name_re, re.I))
                if btn.count() > 0:
                    btn.first.scroll_into_view_if_needed()
                    _force_click(btn)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            break
        for sel in [
            'xpath=//button[.//span[normalize-space(text())="Добавить страницу"]]',
            'button:has-text("Добавить страницу")',
            'button:has-text("Новая страница")',
        ]:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state='visible', timeout=3000)
                loc.scroll_into_view_if_needed()
                _force_click(loc)
                clicked = True
                break
            except Exception:
                continue
        if clicked:
            break
        print(f'      → попытка {attempt + 1}/4, жду 1.5с...')
        page.wait_for_timeout(1500)
    if not clicked:
        print('      ⚠️ кнопка «Добавить страницу» не найдена')
        safe_screenshot(page, 'no_add_page_btn')
        return False
    page.wait_for_timeout(1500)
    if rename_page_in_sidebar(page, title):
        print('      ✓ заголовок страницы задан')
    else:
        print('      ⚠️ не переименовалась')
    return True


def add_section(page, text: str, is_first: bool) -> bool:
    print(f'  [SECTION] {text[:60]!r}')
    before = count_questions(page)
    if not open_type_picker(page, is_first):
        print('      ⚠️ панель не открылась')
        return False
    if not wait_for_picker(page, timeout=6000):
        print('      ⚠️ панель не появилась')
        return False
    if not pick_type(page, 'section'):
        return False
    page.wait_for_timeout(1000)
    after = count_questions(page)
    if after <= before:
        print('      ⚠️ секция не добавилась')
        safe_screenshot(page, 'section_not_added')
        return False

    new_q = qat(page, before)
    if new_q is None:
        return False
    if not verify_question_type(new_q, 'section'):
        convert_question_type(page, new_q, 'section')

    for sel in ['textarea[placeholder*="текст" i]',
                'textarea[placeholder*="заголов" i]',
                'textarea[placeholder*="вопрос" i]']:
        for attempt in range(3):
            try:
                q = qat(page, before)
                if q is None:
                    break
                el = q.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click()
                    page.keyboard.press('Control+A')
                    page.keyboard.press('Delete')
                    el.fill(text)
                    print('      ✓ текст секции заполнен')
                    return True
            except Exception:
                page.wait_for_timeout(200)
    print('      ⚠️ не нашёл поле для текста секции')
    safe_screenshot(page, 'section_no_input')
    return False


# ============================================================
#                       ДОБАВЛЕНИЕ ВОПРОСА
# ============================================================
def add_question(page, q, is_first: bool) -> bool:
    label = q['label']
    qtype = q['type']
    print(f'  >>> add_question: type={qtype}, label={label[:60]!r}, '
          f'opt={not q.get("required", True)}, '
          f'desc={bool(q.get("description"))}')

    before = count_questions(page)

    try:
        page.keyboard.press('Escape')
        page.wait_for_timeout(150)
    except Exception:
        pass

    # 1) открыть панель
    if not open_type_picker(page, is_first):
        print('      ⚠️ не открылась панель типов')
        safe_screenshot(page, 'no_picker')
        return False
    if not wait_for_picker(page, timeout=6000):
        print('      ⚠️ панель не появилась')
        safe_screenshot(page, 'picker_timeout')
        return False

    # 2) выбрать тип — до 3 попыток
    picked = False
    for attempt in range(3):
        if pick_type(page, qtype):
            page.wait_for_timeout(1000)
            after = count_questions(page)
            panel_closed = not is_type_picker_open(page)
            if after > before and panel_closed:
                picked = True
                break
            print(f'      ⚠️ попытка {attempt + 1}: '
                  f'вопрос не добавился ({before}→{after}), '
                  f'панель закрыта: {panel_closed}')
        else:
            print(f'      ⚠️ попытка {attempt + 1}: тип не выбран')
        try:
            page.keyboard.press('Escape')
            page.wait_for_timeout(200)
        except Exception:
            pass
        if not open_type_picker(page, is_first):
            break
        if not wait_for_picker(page, timeout=4000):
            break

    if not picked:
        print('      ❌ Не удалось добавить вопрос')
        safe_screenshot(page, 'add_failed')
        return False

    # 3) индекс нового вопроса
    q_index = before
    print('      → вопрос найден в DOM')

    # 4) верификация типа
    new_q = qat(page, q_index)
    if new_q is not None and not verify_question_type(new_q, qtype):
        print('      → пробую исправить тип...')
        if convert_question_type(page, new_q, qtype):
            page.wait_for_timeout(400)

    # 5) заголовок
    if not fill_title(page, q_index, label):
        return False
    page.wait_for_timeout(250)

    # 6) пояснение
    if q.get('description'):
        fill_description(page, q_index, q['description'])
        page.wait_for_timeout(250)

    # 7) обязательность
    if qtype != 'section':
        set_required(page, q_index, q.get('required', True))
        page.wait_for_timeout(150)

    # 8) опции / виджет
    if qtype in ('radio', 'multi') and q['options']:
        fill_options(page, q_index, q['options'])
        page.wait_for_timeout(250)
        if qtype == 'radio':
            w = q.get('widget')
            if w == 'onerow' or (w is None and is_numeric_scale(q['options'])):
                set_widget_onerow(page, q_index)
            elif w == 'stars':
                set_widget_stars(page, q_index)
    elif qtype == 'dropdown' and q['options']:
        fill_options(page, q_index, q['options'])
    elif qtype == 'scale' and q.get('criteria') and q.get('options'):
        fill_scale(page, q_index, q['criteria'], q['options'])

    try:
        page.keyboard.press('Escape')
        page.wait_for_timeout(100)
    except Exception:
        pass

    page.wait_for_timeout(200)
    return True


# ============================================================
#                          MAIN
# ============================================================
def main():
    if not INPUT_FILE.exists():
        print(f'❌ Не найден {INPUT_FILE}')
        sys.exit(1)

    items = parse_questions(INPUT_FILE)

    print('\n--- ПАРСИНГ ---')
    file_q = 0
    for i, it in enumerate(items, 1):
        if it['kind'] == 'question':
            file_q += 1
            print(f'  [{i}] Q#{file_q} type={it["type"]:8} '
                  f'opt={not it.get("required", True)} '
                  f'desc={bool(it.get("description"))} '
                  f'opts={len(it.get("options", []))} '
                  f'crit={len(it.get("criteria", []))} '
                  f'widget={it.get("widget")} '
                  f'| {it["label"][:50]!r}')
        elif it['kind'] == 'page':
            print(f'  [{i}] PAGE {it.get("title")!r}')
        elif it['kind'] == 'section':
            print(f'  [{i}] SECTION {it.get("text", "")[:60]!r}')
    print('--- /ПАРСИНГ ---\n')

    total_q = sum(1 for it in items if it['kind'] == 'question')
    total_pages = sum(1 for it in items if it['kind'] == 'page')
    total_sections = sum(1 for it in items if it['kind'] == 'section')
    print(f'✅ В файле: вопросов — {total_q}, страниц — {total_pages}, '
          f'секций — {total_sections}')

    if total_q == 0:
        sys.exit(1)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            slow_mo=SLOW_MO,
            no_viewport=True,
            args=['--start-maximized'],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(ADMIN_URL, wait_until='domcontentloaded')

        print()
        print('=' * 64)
        print('Войдите в нужный Яндекс-аккаунт.')
        print('Когда увидите страницу «Список форм» / админку Яндекс.Форм — нажмите Enter.')
        print('=' * 64)
        input('>>> Enter: ')

        print('\n📝 Создаю форму...')
        try:
            if not is_in_editor(page):
                print('→ Ищу "Создать форму"...')
                if not click_first(page, [
                    'button[aria-label="Создать форму"]',
                    'button:has-text("Создать форму")',
                ], timeout=10000):
                    raise RuntimeError('Не нашёл "Создать форму".')
                page.wait_for_timeout(2000)
                if not is_in_editor(page):
                    print('→ Ищу "Создать пустую форму"...')
                    if not click_first(page, [
                        'text=Создать пустую форму',
                        'div:has-text("Создать пустую форму")',
                    ], timeout=8000):
                        raise RuntimeError('Не нашёл "Создать пустую форму".')
                    page.wait_for_timeout(2000)
            wait_for_editor(page)
        except Exception as e:
            print(f'❌ {e}')
            input('Enter — закрыть браузер...')
            context.close()
            return

        print(f'📋 Добавляю {total_q} вопрос(ов)…')
        success = 0
        failed = 0
        first_page_seen = False

        for it in items:
            if page.is_closed():
                print('❌ Браузер закрыт. Прерываю.')
                break

            if it['kind'] == 'page':
                cur_count = count_questions(page)
                if cur_count == 0 and not first_page_seen:
                    print(f'  [PAGE] {it["title"]} — первая страница (auto)')
                    first_page_seen = True
                    if rename_page_in_sidebar(page, it['title']):
                        print('      ✓ заголовок первой страницы задан')
                    else:
                        print('      ℹ️ пропущено')
                else:
                    add_page(page, it['title'])
                continue

            if it['kind'] == 'section':
                try:
                    is_first = (count_questions(page) == 0)
                    add_section(page, it['text'], is_first=is_first)
                except Exception as e:
                    print(f'      ❌ Секция: {e}')
                    safe_screenshot(page, 'section_exc')
                continue

            is_first = (count_questions(page) == 0)
            try:
                ok = add_question(page, it, is_first=is_first)
                if ok:
                    print('      ✓')
                    success += 1
                else:
                    print('      ⚠️ пропущен')
                    failed += 1
                    safe_screenshot(page, f'fail_{success + failed}')
            except Exception as e:
                print(f'      ❌ {e}')
                failed += 1
                safe_screenshot(page, f'exc_{success + failed}')
                if page.is_closed():
                    print('❌ Браузер закрыт после ошибки. Прерываю.')
                    break

        print()
        print('=' * 64)
        print(f'✅ Готово. Успешно: {success}, с ошибками: {failed}')
        print('=' * 64)
        try:
            input('Enter — закрыть браузер...')
        except EOFError:
            pass
        context.close()


if __name__ == '__main__':
    main()