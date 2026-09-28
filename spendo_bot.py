import os
import json
import logging
import base64
import re
import time
import uuid
import asyncio
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ─── Настройка логгирования ───────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ─────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS  = os.environ["GOOGLE_CREDENTIALS"]   # JSON-строка
GOOGLE_SHEET_ID     = os.environ["GOOGLE_SHEET_ID"]
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
# Часовой пояс семьи: от него зависят «сегодня», даты записей и время рассылки.
TZ                  = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Nicosia"))
# Во сколько в последний день периода присылать итоги (ЧЧ:ММ по TZ).
REPORT_TIME         = os.environ.get("REPORT_TIME", "20:00")

# Сопоставление Telegram user ID → имя члена семьи.
# Пример значения переменной окружения FAMILY_MEMBERS_JSON:
#   {"111111111": "Влад", "222222222": "Женя"}
# Каждому из списка бот заводит свой лист в таблице (название листа = имя).
# Кого нет в списке — бот не пускает и показывает его Telegram ID
# (удобно для первичной настройки: написал /start — узнал свой ID).
try:
    FAMILY_MEMBERS: dict[str, str] = json.loads(os.environ.get("FAMILY_MEMBERS_JSON", "{}"))
except json.JSONDecodeError:
    logger.warning("FAMILY_MEMBERS_JSON задан некорректно, использую пустой словарь")
    FAMILY_MEMBERS = {}

# ─── Категории и группы ───────────────────────────────────────────────────────
# Единый источник правды: отсюда строится и промпт для Claude,
# и кнопки выбора категории, и группировка в /stats.
CATEGORY_GROUPS: dict[str, list[str]] = {
    "🔒 Обязательные": ["кредиты", "аренда", "интернет", "связь", "подписки"],
    "🏠 Быт":          ["еда", "дом и быт", "детям", "здоровье", "одежда", "красота и уход"],
    "🚗 Транспорт":    ["машина", "дорожные расходы"],
    "🎉 Образ жизни":  ["кафе", "доставка", "бар", "стики", "киоск", "подарки", "билеты и отдых"],
    "📦 Прочее":       ["оборудование", "россия", "непредвиденные"],
}
EXPENSE_CATEGORIES = [c for cats in CATEGORY_GROUPS.values() for c in cats]
INCOME_CATEGORY = "доход"
ALL_CATEGORIES = set(EXPENSE_CATEGORIES) | {INCOME_CATEGORY}
UNSURE = "уточнить"  # Claude ставит это, если по тексту не понять, что куплено

# ─── Google Sheets: отдельный лист на каждого члена семьи ──────────────────────
# Бот пишет в лист с именем автора («Влад», «Амина»). Если листа нет — создаёт.
# Строка раскладывается по заголовкам листа, поэтому порядок колонок в листе
# можно менять, а старый лист с колонкой «Кто» тоже подойдёт.
PERSONAL_HEADER = ["Дата", "Магазин / Место", "Сумма", "Категория", "Тип", "Добавлено"]

_sh_cache = {"sh": None, "expires": 0}
_ws_by_title: dict[str, tuple] = {}  # title -> (worksheet, header)


def _norm(h: str) -> str:
    """Нормализация заголовка: «Магазин/Место» == «Магазин / Место»."""
    return re.sub(r"\s+", "", (h or "").lower())


def get_spreadsheet():
    """Возвращает spreadsheet, переподключаясь не чаще раза в 10 минут."""
    now = time.time()
    if _sh_cache["sh"] is None or now > _sh_cache["expires"]:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        _sh_cache["sh"] = gc.open_by_key(GOOGLE_SHEET_ID)
        _sh_cache["expires"] = now + 600  # кэш на 10 минут
        _ws_by_title.clear()
        logger.info("Google Sheets: новое подключение")
    return _sh_cache["sh"]


def get_person_ws(person: str):
    """Лист автора (создаётся при первой записи) и его заголовок."""
    sh = get_spreadsheet()
    if person in _ws_by_title:
        return _ws_by_title[person]
    try:
        ws = sh.worksheet(person)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=person, rows=1000, cols=len(PERSONAL_HEADER))
        logger.info("Создан лист '%s'", person)
    header = ws.row_values(1)
    if not header:
        ws.append_row(PERSONAL_HEADER, value_input_option="USER_ENTERED")
        header = PERSONAL_HEADER
    _ws_by_title[person] = (ws, header)
    return ws, header


def _signed_amount(amount, type_: str) -> float:
    """Доход всегда положительный, расход всегда отрицательный."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return amount
    return abs(amount) if type_ == "доход" else -abs(amount)


def _row_for_header(r: dict, person: str, now_str: str, header: list[str]) -> list:
    type_ = r.get("type", "расход")
    by_name = {
        "дата": r.get("date", ""),
        "кто": person,
        "магазин/место": r.get("store", ""),
        "сумма": _signed_amount(r.get("amount", 0), type_),
        "категория": r.get("category", ""),
        "тип": type_,
        "добавлено": now_str,
    }
    return [by_name.get(_norm(h), "") for h in header]


def add_rows_batch(rows: list[dict], person: str):
    """Записывает строки в лист автора одним запросом — избегает 429."""
    ws, header = get_person_ws(person)
    now_str = now_local().strftime("%d.%m.%Y %H:%M")
    ws.append_rows(
        [_row_for_header(r, person, now_str, header) for r in rows],
        value_input_option="USER_ENTERED",
    )


def add_row(data: dict, person: str):
    """Записывает одну строку в лист автора."""
    add_rows_batch([data], person)


def read_all_sheets() -> list[tuple[str, list[list[str]]]]:
    """Все листы таблицы одним запросом: [(название, строки), ...]."""
    sh = get_spreadsheet()
    titles = [w.title for w in sh.worksheets()]
    ranges = ["'" + t.replace("'", "''") + "'" for t in titles]
    resp = sh.values_batch_get(ranges)
    value_ranges = resp.get("valueRanges", [])
    return [(t, vr.get("values", [])) for t, vr in zip(titles, value_ranges)]


# ─── Расчётный период (с 26-го, либо с 25-го, если 26-е — сб/вс) ──────────────
def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


def _period_start_for_month(year: int, month: int) -> date:
    """26-е число месяца — либо 25-е, если 26-е выпадает на субботу/воскресенье
    (в этом случае зарплата приходит на день раньше)."""
    day26 = date(year, month, 26)
    if day26.weekday() in (5, 6):  # 5 = суббота, 6 = воскресенье
        return date(year, month, 25)
    return day26


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    m = year * 12 + (month - 1) + delta
    return m // 12, m % 12 + 1


def get_period_start(reference_date: date | None = None) -> date:
    """Дата начала расчётного периода, в который попадает reference_date."""
    if reference_date is None:
        reference_date = today_local()
    this_start = _period_start_for_month(reference_date.year, reference_date.month)
    if reference_date >= this_start:
        return this_start
    y, m = _shift_month(reference_date.year, reference_date.month, -1)
    return _period_start_for_month(y, m)


def get_period_end(period_start: date) -> date:
    """Последний день периода — день перед началом следующего периода."""
    y, m = _shift_month(period_start.year, period_start.month, 1)
    return _period_start_for_month(y, m) - timedelta(days=1)


# ─── Кто пишет боту ────────────────────────────────────────────────────────────
def get_person_name(update: Update) -> str | None:
    """Имя члена семьи по Telegram ID или None, если пользователя нет в списке."""
    user = update.effective_user
    if user is None:
        return None
    return FAMILY_MEMBERS.get(str(user.id))


async def _deny(update: Update):
    user = update.effective_user
    uid = user.id if user else "?"
    await update.message.reply_text(
        "⛔ Тебя нет в списке семьи.\n"
        f"Твой Telegram ID: {uid}\n"
        "Добавь его в переменную FAMILY_MEMBERS_JSON и перезапусти бота."
    )


# ─── Claude API ────────────────────────────────────────────────────────────────
_categories_list = "\n".join(f"- {c}" for c in EXPENSE_CATEGORIES + [INCOME_CATEGORY])

SYSTEM_PROMPT = f"""Ты — помощник для учёта личных финансов семьи в Никосии (Кипр).
Твоя задача — извлечь ВСЕ позиции из чека и вернуть строго JSON без пояснений.

Если это чек с несколькими позициями — верни список items.
Если это одна трата (текст от пользователя) — верни один объект.

Формат для чека с позициями:
{{
  "date": "DD.MM.YYYY",
  "store": "название магазина",
  "type": "расход",
  "items": [
    {{"name": "название товара", "amount": 1.23, "category": "категория"}},
    {{"name": "название товара", "amount": 4.56, "category": "категория"}}
  ]
}}

Формат для одной траты:
{{
  "date": "DD.MM.YYYY",
  "store": "место или суть траты",
  "amount": 123.45,
  "category": "категория",
  "type": "расход или доход"
}}

Поле store:
- если место названо — название места (Alphamega, Wolt, киоск, Zorbas);
- если место не названо — коротко суть траты («кофе», «такси», «зарплата»);
- никогда не пиши «не указано».

Список категорий (используй ТОЛЬКО их):
{_categories_list}

Правила категоризации:
- продукты, супермаркет (Alphamega, Lidl, Sklavenitis, Metro), пекарня (Zorbas), мясо, рыба, фрукты → еда
- Wolt, Foody, Bolt Food, любая доставка готовой еды, пицца на дом → доставка
- ресторан, кофейня, кафе, фастфуд на месте → кафе
- бар, алкоголь → бар
- сигареты, табак, IQOS, стики → стики
- покупка в киоске / периптеро, если не сказано, что именно куплено → киоск
  (если сказано явно — например «стики в киоске 8.4» — категория по товару)
- бытовая химия, посуда, мелочи для дома, IKEA, хозтовары → дом и быт
- игрушки, детская одежда, школа, кружки, секции, всё для ребёнка → детям
- аптека, лекарства, анализы, врач, стоматолог, лазер → здоровье
- одежда и обувь для взрослых → одежда
- салон, маникюр, парикмахер, косметика, уход → красота и уход
- бензин, заправка (Petrolina, EKO, Esso), ТО, ремонт, шины, мойка, страховка авто,
  road tax, MOT, парковка, штраф за парковку, лизинг/выплата за машину → машина
- такси, Bolt, автобус, каршеринг, прокат самокатов → дорожные расходы
- авиабилеты, отели, Airbnb, туры, экскурсии, концерты, кино, театр, парки развлечений,
  билеты на мероприятия → билеты и отдых
- подарки, открытки, цветы, свечи в подарок → подарки
- Netflix, Spotify, iCloud, приложения, подписки → подписки
- кредит, займ, рассрочка, платёж по кредиту, ипотека → кредиты
- аренда жилья → аренда
- домашний интернет → интернет
- мобильная связь, пополнение телефона → связь
- техника, электроника, инструменты, музыкальное оборудование → оборудование
- переводы в Россию, расходы в РФ → россия
- зарплата, фриланс, любые поступления → доход
- понятная трата, не подходящая ни под одно правило → непредвиденные

Особый случай — «{UNSURE}»:
если это ОДНА трата текстом и по ней НЕЛЬЗЯ понять, что куплено
(например «магазин 16.20» или «оплатил 20»), поставь category = "{UNSURE}".
Бот сам спросит пользователя. Для позиций из чека «{UNSURE}» не используй —
там название товара видно, выбирай категорию по нему.

Язык чека может быть русский, английский или греческий — распознавай все три.
Если дата не указана — используй сегодняшнее число.
Отвечай ТОЛЬКО JSON, без лишнего текста."""


def parse_with_claude(image_b64: str | None = None, text: str | None = None) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = now_local().strftime("%d.%m.%Y")

    if image_b64:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64,
                },
            },
            {
                "type": "text",
                "text": f"Сегодня {today}. Распарси этот чек и верни JSON.",
            },
        ]
    else:
        content = f"Сегодня {today}. Пользователь написал: «{text}». Распарси и верни JSON."

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw = msg.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(raw)


def _normalize_category(category: str | None, type_: str) -> str:
    """Приводит категорию к списку. Для дохода — всегда 'доход'."""
    if type_ == "доход":
        return INCOME_CATEGORY
    category = (category or "").strip().lower()
    if category in ALL_CATEGORIES or category == UNSURE:
        return category
    return UNSURE


# ─── Telegram handlers ────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я записываю расходы и доходы семьи.\n\n"
        "Просто отправь мне:\n"
        "📷 Фото или скриншот чека\n"
        "✏️ Текст, например: «кофе 3.5» или «зарплата 1000»\n\n"
        "Если по тексту непонятно, что куплено, — спрошу категорию кнопками.\n\n"
        "📊 /stats — отчёт за текущий период (с 26-го, или с 25-го, если 26-е "
        "выпадает на выходной): по каждому члену семьи, по группам и категориям.\n"
        "📊 /stats prev — то же за прошлый период.\n"
        "🧮 /report — итоги с рекомендациями (сам пришлю в последний день периода, 24-го или 25-го).\n\n"
        f"🆔 Твой Telegram ID: {update.effective_user.id}"
    )


def _category_keyboard(key: str) -> InlineKeyboardMarkup:
    # самые частые категории — первыми
    priority = ["еда", "киоск", "стики", "бар", "кафе", "доставка", "дом и быт", "детям", "машина", "подарки"]
    order = priority + [c for c in EXPENSE_CATEGORIES if c not in priority]
    buttons = [
        InlineKeyboardButton(cat, callback_data=f"cat:{key}:{EXPENSE_CATEGORIES.index(cat)}")
        for cat in order
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data=f"cat:{key}:x")])
    return InlineKeyboardMarkup(rows)


async def _ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, person: str):
    key = uuid.uuid4().hex[:10]
    context.user_data.setdefault("pending", {})[key] = {"data": data, "person": person}
    await update.message.reply_text(
        f"🤔 Не понял, что это за трата:\n"
        f"🏪 {data.get('store', '—')} — {data.get('amount', 0)}€\n\n"
        f"Выбери категорию:",
        reply_markup=_category_keyboard(key),
    )


async def on_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, key, choice = query.data.split(":", 2)
    except ValueError:
        return

    pending = context.user_data.get("pending", {}).pop(key, None)
    if pending is None:
        await query.edit_message_text("⌛ Запрос устарел (бот перезапускался). Отправь трату ещё раз.")
        return
    if choice == "x":
        await query.edit_message_text("✖️ Отменено, ничего не записал.")
        return

    data, person = pending["data"], pending["person"]
    data["category"] = EXPENSE_CATEGORIES[int(choice)]
    try:
        add_row(data, person)
        await query.edit_message_text(_confirmation_text(data, person))
    except Exception as e:
        logger.exception("Ошибка при записи после выбора категории")
        await query.edit_message_text(f"❌ Не получилось записать: {e}")


def _fmt(x: float) -> str:
    return f"{x:.2f}€"


def _parse_amount(s: str) -> float | None:
    try:
        return abs(float(s.replace(" ", "").replace(" ", "").replace("€", "").replace(",", ".")))
    except ValueError:
        return None


def collect_records(sheets: list[tuple[str, list[list[str]]]]) -> list[dict]:
    """Все транзакции со всех листов: [{person, date, amount, category, type, store}]."""
    family_names = set(FAMILY_MEMBERS.values())
    records = []
    for title, values in sheets:
        if not values:
            continue
        idx = {_norm(h): i for i, h in enumerate(values[0])}
        # берём только листы с транзакциями (у них есть Дата и Сумма)
        if "дата" not in idx or "сумма" not in idx:
            continue
        for row in values[1:]:
            def cell(name: str, default: str = "") -> str:
                i = idx.get(name)
                return row[i].strip() if i is not None and i < len(row) else default

            try:
                row_date = datetime.strptime(cell("дата"), "%d.%m.%Y").date()
            except ValueError:
                continue
            amount = _parse_amount(cell("сумма", "0"))
            if amount is None:
                continue
            # автор: личный лист → колонка «Кто» (старый общий лист) → «Без автора»
            person = title if title in family_names else (cell("кто") or "Без автора")
            records.append({
                "person": person,
                "date": row_date,
                "amount": amount,
                "category": cell("категория") or "непредвиденные",
                "type": cell("тип") or "расход",
                "store": cell("магазин/место"),
            })
    return records


def compute_stats(records: list[dict], start: date, end: date) -> dict:
    """Сводка за период: по людям и по семье."""
    people: dict[str, dict] = {}
    family = {"доход": 0.0, "расход": 0.0, "categories": {}}
    for r in records:
        if not (start <= r["date"] <= end):
            continue
        p = people.setdefault(r["person"], {"доход": 0.0, "расход": 0.0, "categories": {}})
        if r["type"] == "доход":
            p["доход"] += r["amount"]
            family["доход"] += r["amount"]
        else:
            for bucket in (p, family):
                bucket["расход"] += r["amount"]
                bucket["categories"][r["category"]] = bucket["categories"].get(r["category"], 0.0) + r["amount"]
    return {"start": start, "end": end, "people": people, "family": family}


def group_totals(cats: dict[str, float]) -> list[tuple[str, float, list[tuple[str, float]]]]:
    """[(группа, сумма, [(категория, сумма), ...]), ...] — только непустые группы."""
    extra = [c for c in cats if c not in EXPENSE_CATEGORIES]  # старые/неизвестные → «Прочее»
    out = []
    for g_name, g_cats in CATEGORY_GROUPS.items():
        g_cats = g_cats + (extra if g_name == "📦 Прочее" else [])
        present = sorted([(c, cats[c]) for c in g_cats if cats.get(c)], key=lambda x: -x[1])
        if present:
            out.append((g_name, sum(v for _, v in present), present))
    return out


def format_stats(st: dict) -> str:
    lines = [f"📊 Период {st['start'].strftime('%d.%m.%Y')} – {st['end'].strftime('%d.%m.%Y')}\n"]
    if not st["people"]:
        lines.append("За этот период пока нет записей.")
        return "\n".join(lines)

    for person, data in sorted(st["people"].items()):
        lines.append(f"👤 {person}")
        lines.append(f"  💰 Доход: {_fmt(data['доход'])}   💸 Расход: {_fmt(data['расход'])}")
        for g_name, total, cats in group_totals(data["categories"]):
            lines.append(f"  {g_name} — {_fmt(total)}")
            for c, v in cats:
                lines.append(f"      • {c}: {_fmt(v)}")
        lines.append("")

    fam = st["family"]
    lines.append("👨‍👩‍👧 Итого по семье:")
    lines.append(f"  💰 Доход: {_fmt(fam['доход'])}")
    lines.append(f"  💸 Расход: {_fmt(fam['расход'])}")
    for g_name, total, _ in group_totals(fam["categories"]):
        lines.append(f"  {g_name} — {_fmt(total)}")
    lines.append(f"  📈 Баланс: {_fmt(fam['доход'] - fam['расход'])}")
    return "\n".join(lines)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_person_name(update) is None:
        await _deny(update)
        return
    try:
        period_start = get_period_start(today_local())
        if context.args and context.args[0].lower() in ("prev", "прошлый", "пред"):
            period_start = get_period_start(period_start - timedelta(days=1))
        period_end = get_period_end(period_start)

        records = collect_records(read_all_sheets())
        await update.message.reply_text(format_stats(compute_stats(records, period_start, period_end)))
    except Exception as e:
        logger.exception("Ошибка при формировании статистики")
        await update.message.reply_text(f"❌ Не получилось получить статистику: {e}")


# ─── Настройки в таблице: «Бюджет» и «О нас» ───────────────────────────────────
BUDGET_SHEET = "Бюджет"
ABOUT_SHEET = "О нас"
BUDGET_HEADER = ["Категория", "Лимит в месяц, €", "Комментарий"]
ABOUT_HEADER = ["Вопрос", "Ответ"]
ABOUT_QUESTIONS = [
    "Кто в семье (имена, возраст ребёнка, класс/школа)",
    "Доход Влада в месяц (на руки) и когда приходит",
    "Доход Амины в месяц (на руки) и когда приходит",
    "Другие доходы (фриланс, аренда, подработки)",
    "Кредиты: платёж в месяц / остаток / до какой даты",
    "Машина: платёж / страховка / road tax / ТО — суммы и месяцы",
    "Жильё: аренда или своё, сумма в месяц, коммуналка",
    "Сколько хотим откладывать в месяц (сумма или %)",
    "Подушка безопасности: есть ли и сколько, сколько хотим",
    "Цели: на что копим, сумма, к какому сроку",
    "Крупные траты в ближайшие 6–12 месяцев (школа, отпуск, ремонт, техника)",
    "На чём готовы экономить",
    "На чём экономить НЕ хотим",
    "Как делим деньги между собой (общий бюджет / каждый платит за своё / доли)",
    "Что ещё важно учесть",
]


def ensure_settings_sheets():
    """Создаёт листы «Бюджет» и «О нас» с шаблоном, если их нет."""
    sh = get_spreadsheet()
    titles = {w.title for w in sh.worksheets()}
    if BUDGET_SHEET not in titles:
        ws = sh.add_worksheet(title=BUDGET_SHEET, rows=len(EXPENSE_CATEGORIES) + 10, cols=3)
        ws.update(range_name="A1", values=[BUDGET_HEADER] + [[c, "", ""] for c in EXPENSE_CATEGORIES])
        logger.info("Создан лист '%s'", BUDGET_SHEET)
    if ABOUT_SHEET not in titles:
        ws = sh.add_worksheet(title=ABOUT_SHEET, rows=len(ABOUT_QUESTIONS) + 10, cols=2)
        ws.update(range_name="A1", values=[ABOUT_HEADER] + [[q, ""] for q in ABOUT_QUESTIONS])
        logger.info("Создан лист '%s'", ABOUT_SHEET)


def read_settings(sheets: list[tuple[str, list[list[str]]]]) -> tuple[dict[str, float], list[tuple[str, str]]]:
    """(лимиты по категориям, ответы «О нас») — только заполненные строки."""
    budget: dict[str, float] = {}
    about: list[tuple[str, str]] = []
    for title, values in sheets:
        if title == BUDGET_SHEET:
            for row in values[1:]:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    limit = _parse_amount(row[1])
                    if limit:
                        budget[row[0].strip().lower()] = limit
        elif title == ABOUT_SHEET:
            for row in values[1:]:
                if len(row) >= 2 and row[0].strip() and row[1].strip():
                    about.append((row[0].strip(), row[1].strip()))
    return budget, about


# ─── Итоги периода с рекомендациями ───────────────────────────────────────────
REPORT_PROMPT = """Ты — семейный финансовый консультант. Семья живёт в Никосии (Кипр), валюта — евро.
Расчётный месяц у семьи — от зарплаты до зарплаты (с 26-го, или с 25-го, если 26-е выпало на выходной).

Тебе дают JSON: получатель, профиль семьи («О нас»), бюджет по категориям,
статистика текущего и прошлого периода по каждому человеку и по семье.

Напиши рекомендации ЛИЧНО получателю (обращайся на «ты»), по-русски:
- 3–6 пунктов, каждый — конкретное действие с цифрами (сколько, по какой категории, за счёт чего);
- сравни с прошлым периодом и с бюджетом: где превышение, где рост, что хорошо;
- если в профиле есть цели/накопления — посчитай, успеваем ли, и сколько откладывать в следующем периоде;
- если бюджет не заполнен — предложи лимиты на следующий период по основным категориям
  (от фактических трат и доходов), коротко;
- если доходы явно записаны не все (расход сильно больше дохода) — скажи об этом одной строкой;
- никакой воды и общих советов вида «ведите учёт» — только выводы из этих данных;
- не выдумывай факты о семье, которых нет в данных.

Формат: обычный текст для Telegram, без Markdown-разметки (без **, #, таблиц),
пункты начинай с «• ». Не длиннее 1500 символов."""


def _stats_for_llm(st: dict) -> dict:
    def conv(d: dict) -> dict:
        return {
            "доход": round(d["доход"], 2),
            "расход": round(d["расход"], 2),
            "по_категориям": {k: round(v, 2) for k, v in sorted(d["categories"].items(), key=lambda x: -x[1])},
        }
    return {
        "период": f"{st['start'].strftime('%d.%m.%Y')} – {st['end'].strftime('%d.%m.%Y')}",
        "по_людям": {p: conv(d) for p, d in st["people"].items()},
        "семья": conv(st["family"]),
    }


def generate_recommendations(recipient: str, cur: dict, prev: dict,
                             budget: dict[str, float], about: list[tuple[str, str]]) -> str:
    payload = {
        "получатель": recipient,
        "о_нас": {q: a for q, a in about},
        "бюджет_лимиты_в_месяц": budget,
        "текущий_период": _stats_for_llm(cur),
        "прошлый_период": _stats_for_llm(prev),
    }
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=REPORT_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    return msg.content[0].text.strip()


def build_report(recipient: str, ref_day: date) -> str:
    """Полный текст итогов периода, в который попадает ref_day, для одного человека."""
    sheets = read_all_sheets()
    records = collect_records(sheets)
    budget, about = read_settings(sheets)

    start = get_period_start(ref_day)
    end = get_period_end(start)
    prev_start = get_period_start(start - timedelta(days=1))
    cur = compute_stats(records, start, end)
    prev = compute_stats(records, prev_start, start - timedelta(days=1))

    me = cur["people"].get(recipient, {"доход": 0.0, "расход": 0.0, "categories": {}})
    me_prev = prev["people"].get(recipient, {"расход": 0.0})
    fam, fam_prev = cur["family"], prev["family"]

    def delta(now: float, before: float) -> str:
        if not before:
            return ""
        pct = (now - before) / before * 100
        return f" ({'+' if pct >= 0 else ''}{pct:.0f}% к прошлому)"

    lines = [
        f"🗓 Итоги периода {start.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}",
        "",
        f"👤 {recipient}",
        f"  💰 Доход: {_fmt(me['доход'])}",
        f"  💸 Расход: {_fmt(me['расход'])}{delta(me['расход'], me_prev['расход'])}",
    ]
    top = sorted(me["categories"].items(), key=lambda x: -x[1])[:5]
    if top:
        lines.append("  Топ трат: " + ", ".join(f"{c} {_fmt(v)}" for c, v in top))

    lines += [
        "",
        "👨‍👩‍👧 Семья",
        f"  💰 Доход: {_fmt(fam['доход'])}",
        f"  💸 Расход: {_fmt(fam['расход'])}{delta(fam['расход'], fam_prev['расход'])}",
        f"  📈 Баланс: {_fmt(fam['доход'] - fam['расход'])}",
    ]
    for g_name, total, _ in group_totals(fam["categories"]):
        lines.append(f"  {g_name} — {_fmt(total)}")

    if budget:
        over, ok = [], []
        for cat, limit in budget.items():
            fact = fam["categories"].get(cat, 0.0)
            (over if fact > limit else ok).append((cat, fact, limit))
        lines += ["", "📋 Бюджет (семья)"]
        for cat, fact, limit in sorted(over, key=lambda x: -(x[1] - x[2])):
            lines.append(f"  🔴 {cat}: {_fmt(fact)} из {_fmt(limit)} (+{_fmt(fact - limit)})")
        if ok:
            lines.append(f"  🟢 В рамках: {len(ok)} из {len(budget)} категорий")

    try:
        recs = generate_recommendations(recipient, cur, prev, budget, about)
        lines += ["", "🤖 Рекомендации", recs]
    except Exception:
        logger.exception("Не удалось получить рекомендации")
        lines += ["", "🤖 Рекомендации сейчас недоступны (ошибка Claude API)."]

    if not about:
        lines += ["", "ℹ️ Заполните лист «О нас» в таблице — рекомендации станут точнее."]
    return "\n".join(lines)


async def _send_long(bot, chat_id: int, text: str):
    """Telegram режет сообщения на 4096 символах — шлём частями."""
    while text:
        cut = text[:4000]
        if len(text) > 4000 and "\n" in cut:
            cut = cut[:cut.rfind("\n")]
        await bot.send_message(chat_id=chat_id, text=cut)
        text = text[len(cut):].lstrip("\n")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Итоги с рекомендациями по запросу (по умолчанию — текущий период)."""
    person = get_person_name(update)
    if person is None:
        await _deny(update)
        return
    await update.message.reply_text("🧮 Считаю итоги и готовлю рекомендации...")
    ref = today_local()
    if context.args and context.args[0].lower() in ("prev", "прошлый", "пред"):
        ref = get_period_start(ref) - timedelta(days=1)
    try:
        text = await asyncio.to_thread(build_report, person, ref)
        await _send_long(context.bot, update.effective_chat.id, text)
    except Exception as e:
        logger.exception("Ошибка при формировании отчёта")
        await update.message.reply_text(f"❌ Не получилось собрать отчёт: {e}")


async def job_period_end(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная проверка: если сегодня последний день периода (24-е или 25-е) —
    шлём каждому члену семьи его итоги с рекомендациями."""
    today = today_local()
    if today != get_period_end(get_period_start(today)):
        return
    logger.info("Последний день периода — рассылаю итоги")
    for uid, name in FAMILY_MEMBERS.items():
        try:
            text = await asyncio.to_thread(build_report, name, today)
            await _send_long(context.bot, int(uid), text)
        except Exception:
            # чаще всего — человек ещё ни разу не написал боту /start
            logger.exception("Не удалось отправить итоги %s (%s)", name, uid)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    person = get_person_name(update)
    if person is None:
        await _deny(update)
        return
    await update.message.reply_text("🔍 Распознаю чек...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")

    try:
        data = parse_with_claude(image_b64=image_b64)
        if "items" in data:
            type_ = data.get("type", "расход")
            store = data.get("store") or "чек"
            for item in data["items"]:
                cat = _normalize_category(item.get("category"), type_)
                # в чеке спрашивать по каждой позиции неудобно — неизвестное в «непредвиденные»
                item["category"] = "непредвиденные" if cat == UNSURE else cat
            # ✅ Один батч-запрос вместо N отдельных
            rows = [
                {
                    "date": data.get("date"),
                    "store": f"{store} — {item.get('name', '')}".strip(" —"),
                    "amount": item.get("amount"),
                    "category": item["category"],
                    "type": type_,
                }
                for item in data["items"]
            ]
            add_rows_batch(rows, person)
            await _send_receipt_confirmation(update, data, person)
        else:
            await _handle_single(update, context, data, person)
    except Exception as e:
        logger.exception("Ошибка при обработке фото")
        await update.message.reply_text(f"❌ Не получилось разобрать чек: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return

    person = get_person_name(update)
    if person is None:
        await _deny(update)
        return
    await update.message.reply_text("📝 Обрабатываю...")

    try:
        data = parse_with_claude(text=text)
        await _handle_single(update, context, data, person)
    except Exception as e:
        logger.exception("Ошибка при обработке текста")
        await update.message.reply_text(f"❌ Не получилось разобрать: {e}")


async def _handle_single(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, person: str):
    data["type"] = data.get("type") or "расход"
    if (data.get("category") or "").strip().lower() == INCOME_CATEGORY:
        data["type"] = "доход"
    data["category"] = _normalize_category(data.get("category"), data["type"])
    if data["category"] == UNSURE:
        await _ask_category(update, context, data, person)
        return
    add_row(data, person)
    await update.message.reply_text(_confirmation_text(data, person))


def _confirmation_text(data: dict, person: str) -> str:
    emoji = "💸" if data.get("type") == "расход" else "💰"
    return (
        f"{emoji} Записано!\n\n"
        f"👤 Кто: {person}\n"
        f"📅 Дата: {data.get('date', '—')}\n"
        f"🏪 Место: {data.get('store', '—')}\n"
        f"💵 Сумма: {data.get('amount', 0)}€\n"
        f"🏷 Категория: {data.get('category', '—')}\n"
        f"📊 Тип: {data.get('type', '—')}"
    )


async def _send_receipt_confirmation(update: Update, data: dict, person: str):
    lines = [f"🧾 Записано {len(data['items'])} позиций из {data.get('store', '—')} (👤 {person}):\n"]
    total = 0
    for item in data["items"]:
        lines.append(f"• {item.get('name', '—')} — {item.get('amount', 0)}€ [{item['category']}]")
        total += item.get("amount", 0) or 0
    lines.append(f"\n💰 Итого: {round(total, 2)}€")
    await update.message.reply_text("\n".join(lines))


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(on_category_chosen, pattern=r"^cat:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    try:
        ensure_settings_sheets()
    except Exception:
        logger.exception("Не удалось создать листы «Бюджет» / «О нас»")

    # Каждый день в REPORT_TIME проверяем, не последний ли день периода.
    # Нужен пакет python-telegram-bot[job-queue].
    if app.job_queue is None:
        logger.warning("JobQueue недоступен — автоматических итогов не будет. "
                       "Установи python-telegram-bot[job-queue].")
    else:
        hh, mm = (int(x) for x in REPORT_TIME.split(":"))
        app.job_queue.run_daily(job_period_end, time=dtime(hh, mm, tzinfo=TZ), name="period_end")
        logger.info("Итоги периода: каждый день в %s (%s) проверяю, не конец ли месяца", REPORT_TIME, TZ)

    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
