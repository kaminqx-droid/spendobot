import os
import json
import logging
import base64
import re
from datetime import datetime, timedelta
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

# ─── Google Sheets ─────────────────────────────────────────────────────────────
def get_worksheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.sheet1

def get_budget_limits():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet("Бюджет")
        rows = ws.get_all_values()
        limits = {}
        for row in rows[1:]:
            if len(row) >= 2 and row[0] and row[1]:
                try:
                    limits[row[0].lower().strip()] = float(str(row[1]).replace(",", "."))
                except:
                    pass
        return limits
    except:
        return {}

def ensure_header(ws):
    if ws.row_values(1) == []:
        ws.append_row(
            ["Дата", "Магазин / Место", "Сумма (€)", "Категория", "Тип", "Добавлено"],
            value_input_option="USER_ENTERED",
        )

def add_row(data: dict):
    ws = get_worksheet()
    ensure_header(ws)
    amount = data.get("amount", 0)
    if data.get("type", "расход") == "расход":
        amount = -abs(float(amount))
    else:
        amount = abs(float(amount))
    ws.append_row(
        [
            data.get("date", ""),
            data.get("store", ""),
            amount,
            data.get("category", ""),
            data.get("type", "расход"),
            datetime.now().strftime("%d.%m.%Y %H:%M"),
        ],
        value_input_option="USER_ENTERED",
    )

def get_all_rows():
    ws = get_worksheet()
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    return rows[1:]

def check_budget_alert(category: str) -> str | None:
    limits = get_budget_limits()
    cat_lower = category.lower().strip()
    limit = limits.get(cat_lower)
    if not limit:
        return None
    rows = get_all_rows()
    month = datetime.now().strftime("%m.%Y")
    spent = 0
    for row in rows:
        if len(row) >= 5 and row[0].endswith(month) and row[4] == "расход":
            if row[3].lower().strip() == cat_lower:
                try:
                    spent += abs(float(str(row[2]).replace(",", ".")))
                except:
                    pass
    if spent >= limit:
        over = round(spent - limit, 2)
        return f"🔴 Лимит по «{category}» превышен на {over}€\n(лимит {limit}€, потрачено {round(spent, 2)}€)"
    elif spent >= limit * 0.8:
        remaining = round(limit - spent, 2)
        return f"🟡 По «{category}» осталось {remaining}€ из {limit}€"
    return None

# ─── Claude API ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — помощник для учёта личных финансов семьи в Никосии.
ВАЖНО: Используй ТОЛЬКО эти категории, никаких других:
еда, стики, здоровье, подписки, оборудование, дом и быт, одежда, дорожные расходы, красота и уход, кафе, бар, детям, россия, непредвиденные, доход

ПРАВИЛА КАТЕГОРИЗАЦИИ (строго соблюдай):
- слово "стики" в любом месте → категория "стики"
- сигареты, табак, IQOS, heets, terea → "стики"
- продукты, овощи, фрукты, мясо, молоко, хлеб → "еда"
- аптека, лекарства, анализы, лазер, врач, оптика → "здоровье"
- Netflix, Spotify, Apple, NordVPN, Claude, Amazon Prime → "подписки"
- такси, автобус, парковка, каршеринг, бензин, Uber → "дорожные расходы"
- салон, маникюр, косметика, уход, парикмахер → "красота и уход"
- ресторан, кофейня, кафе, кофе, пицца → "кафе"
- бар, алкоголь, вино, пиво → "бар"
- переводы детям, карманные детям, Uber Eats детям → "детям"
- переводы в Россию, Freedom Bank, расходы РФ → "россия"
- зарплата, фриланс → "доход"
- IKEA, мебель, посуда, бытовая техника → "дом и быт"
- если ни одно не подходит → "непредвиденные"

ПЕРЕВОД: Все названия товаров переводи на русский язык.
Примеры: AVOCADO CYPRUS → Авокадо, ΚΡΕΜΥΔΙΑ KOKKINA → Лук красный, NTOMATES → Томаты

ФОРМАТ — фото чека:
{
  "date": "DD.MM.YYYY",
  "store": "название магазина",
  "type": "расход",
  "items": [
    {"name": "название на русском", "amount": 1.23, "category": "категория"}
  ]
}

ФОРМАТ — текст:
{
  "date": "DD.MM.YYYY",
  "store": "место или не указано",
  "amount": 123.45,
  "category": "категория",
  "type": "расход или доход"
}

Язык чека: русский, английский, греческий.
Если дата не указана — используй сегодняшнее число.
Отвечай ТОЛЬКО JSON, без пояснений."""

def parse_with_claude(image_b64: str | None = None, text: str | None = None) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%d.%m.%Y")
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
                "text": f"Сегодня {today}. Это фото чека. Извлеки КАЖДУЮ позицию отдельно, переведи названия на русский язык и верни JSON согласно формату.",
            },
        ]
    else:
        content = f"Сегодня {today}. Пользователь написал: «{text}». Определи категорию строго по правилам и верни JSON."

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = msg.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(raw)

# ─── Статистика ───────────────────────────────────────────────────────────────
def get_stats_today():
    rows = get_all_rows()
    today = datetime.now().strftime("%d.%m.%Y")
    total, count = 0, 0
    for row in rows:
        if len(row) >= 5 and row[0] == today and row[4] == "расход":
            try:
                total += abs(float(str(row[2]).replace(",", ".")))
                count += 1
            except:
                pass
    return total, count

def get_stats_month():
    rows = get_all_rows()
    month = datetime.now().strftime("%m.%Y")
    expense, income = 0, 0
    for row in rows:
        if len(row) >= 5 and row[0].endswith(month):
            try:
                amount = abs(float(str(row[2]).replace(",", ".")))
                if row[4] == "расход":
                    expense += amount
                else:
                    income += amount
            except:
                pass
    return expense, income

def get_stats_by_category():
    rows = get_all_rows()
    month = datetime.now().strftime("%m.%Y")
    cats = {}
    for row in rows:
        if len(row) >= 5 and row[0].endswith(month) and row[4] == "расход":
            cat = row[3] or "другое"
            try:
                cats[cat] = cats.get(cat, 0) + abs(float(str(row[2]).replace(",", ".")))
            except:
                pass
    return dict(sorted(cats.items(), key=lambda x: x[1], reverse=True))

def get_weekly_stats():
    rows = get_all_rows()
    today = datetime.now()
    week_ago = today - timedelta(days=7)
    expense, income, cats = 0, 0, {}
    for row in rows:
        if len(row) >= 5:
            try:
                row_date = datetime.strptime(row[0], "%d.%m.%Y")
                if week_ago <= row_date <= today:
                    amount = abs(float(str(row[2]).replace(",", ".")))
                    if row[4] == "расход":
                        expense += amount
                        cat = row[3] or "другое"
                        cats[cat] = cats.get(cat, 0) + amount
                    else:
                        income += amount
            except:
                pass
    return expense, income, dict(sorted(cats.items(), key=lambda x: x[1], reverse=True))

# ─── Telegram handlers ────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я записываю расходы и доходы.\n\n"
        "Отправь мне:\n"
        "📷 Фото чека — распознаю каждую позицию\n"
        "✏️ Текст: «стики 8€» или «кофе 2.5€»\n\n"
        "Команды:\n"
        "/today — траты за день\n"
        "/month — итоги за месяц\n"
        "/categories — разбивка с бюджетом\n"
        "/week — итоги за 7 дней\n"
        "/delete — удалить запись"
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, count = get_stats_today()
    today = datetime.now().strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"📅 Сегодня ({today}):\n\n"
        f"💸 Потрачено: -{round(total, 2)}€\n"
        f"🧾 Записей: {count}"
    )

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expense, income = get_stats_month()
    month = datetime.now().strftime("%m.%Y")
    await update.message.reply_text(
        f"📊 За {month}:\n\n"
        f"📉 Расходы: -{round(expense, 2)}€\n"
        f"📈 Доходы: +{round(income, 2)}€\n"
        f"💰 Баланс: {round(income - expense, 2)}€"
    )

async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cats = get_stats_by_category()
    limits = get_budget_limits()
    month = datetime.now().strftime("%m.%Y")
    if not cats:
        await update.message.reply_text(f"За {month} записей нет.")
        return
    lines = [f"📂 Категории за {month}:\n"]
    for cat, amount in cats.items():
        limit = limits.get(cat.lower().strip())
        if limit:
            pct = int(amount / limit * 100)
            bar = "🔴" if pct >= 100 else "🟡" if pct >= 80 else "🟢"
            lines.append(f"{bar} {cat}: -{round(amount, 2)}€ / {limit}€ ({pct}%)")
        else:
            lines.append(f"• {cat}: -{round(amount, 2)}€")
    lines.append(f"\n🧾 Итого: -{round(sum(cats.values()), 2)}€")
    await update.message.reply_text("\n".join(lines))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expense, income, cats = get_weekly_stats()
    lines = ["📆 За последние 7 дней:\n"]
    lines.append(f"📉 Расходы: -{round(expense, 2)}€")
    lines.append(f"📈 Доходы: +{round(income, 2)}€")
    lines.append(f"💰 Баланс: {round(income - expense, 2)}€")
    if cats:
        lines.append("\nПо категориям:")
        for cat, amount in cats.items():
            lines.append(f"• {cat}: -{round(amount, 2)}€")
    await update.message.reply_text("\n".join(lines))

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_rows()
    if not rows:
        await update.message.reply_text("Записей нет.")
        return
    last_rows = rows[-5:]
    keyboard = []
    for i, row in enumerate(last_rows):
        real_index = len(rows) - len(last_rows) + i
        if len(row) >= 4:
            try:
                amount_display = f"{abs(float(str(row[2]).replace(',', '.')))}€"
            except:
                amount_display = row[2]
            label = f"{row[0]} | {row[1][:12]} | {amount_display} | {row[3]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"del_{real_index + 2}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="del_cancel")])
    await update.message.reply_text(
        "Выбери запись для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "del_cancel":
        await query.edit_message_text("Отменено.")
        return
    row_num = int(query.data.replace("del_", ""))
    try:
        ws = get_worksheet()
        row_data = ws.row_values(row_num)
        ws.delete_rows(row_num)
        await query.edit_message_text(
            f"🗑 Удалено: {row_data[0]} | {row_data[1]} | {row_data[2]}€ | {row_data[3]}"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Распознаю чек...")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    try:
        data = parse_with_claude(image_b64=image_b64)
        if "items" in data:
            categories_used = set()
            for item in data["items"]:
                add_row({
                    "date": data.get("date"),
                    "store": data.get("store"),
                    "amount": item.get("amount"),
                    "category": item.get("category"),
                    "type": data.get("type", "расход"),
                })
                categories_used.add(item.get("category", ""))
            await _send_receipt_confirmation(update, data)
            for cat in categories_used:
                alert = check_budget_alert(cat)
                if alert:
                    await update.message.reply_text(alert)
        else:
            add_row(data)
            await _send_confirmation(update, data)
            alert = check_budget_alert(data.get("category", ""))
            if alert:
                await update.message.reply_text(alert)
    except Exception as e:
        logger.exception("Ошибка фото")
        await update.message.reply_text(f"❌ Не получилось разобрать чек: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
    await update.message.reply_text("⏳ Обрабатываю...")
    try:
        data = parse_with_claude(text=text)
        add_row(data)
        await _send_confirmation(update, data)
        alert = check_budget_alert(data.get("category", ""))
        if alert:
            await update.message.reply_text(alert)
    except Exception as e:
        logger.exception("Ошибка текста")
        await update.message.reply_text(f"❌ Не получилось разобрать: {e}")

async def _send_confirmation(update: Update, data: dict):
    emoji = "💸" if data.get("type") == "расход" else "💰"
    amount = abs(float(data.get("amount", 0)))
    sign = "-" if data.get("type") == "расход" else "+"
    await update.message.reply_text(
        f"{emoji} Записано!\n\n"
        f"📅 {data.get('date', '—')}\n"
        f"🏪 {data.get('store', '—')}\n"
        f"💶 {sign}{round(amount, 2)}€\n"
        f"📂 {data.get('category', '—')}"
    )

async def _send_receipt_confirmation(update: Update, data: dict):
    lines = [f"🧾 {data.get('store', '—')} · {data.get('date', '')}:\n"]
    total = 0
    for item in data["items"]:
        lines.append(f"• {item['name']} — -{item['amount']}€ [{item['category']}]")
        total += item.get("amount", 0)
    lines.append(f"\n💶 Итого: -{round(total, 2)}€")
    lines.append(f"✅ {len(data['items'])} позиций записано")
    await update.message.reply_text("\n".join(lines))

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CallbackQueryHandler(handle_delete_callback, pattern="^del_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
