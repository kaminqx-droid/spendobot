import os
import json
import logging
import base64
import re
import time
from datetime import datetime
from pathlib import Path

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ─── Настройка логгирования ───────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Переменные окружения ─────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS  = os.environ["GOOGLE_CREDENTIALS"]
GOOGLE_SHEET_ID     = os.environ["GOOGLE_SHEET_ID"]
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

# ─── Память на исправления названий ──────────────────────────────────────────
CORRECTIONS_FILE = Path("corrections.json")

def load_corrections() -> dict:
    """Загружает словарь исправлений {оригинал_lower: правильное_название}."""
    if CORRECTIONS_FILE.exists():
        try:
            return json.loads(CORRECTIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_correction(original: str, corrected: str):
    """Сохраняет исправление для будущих чеков."""
    corrections = load_corrections()
    corrections[original.lower().strip()] = corrected.strip()
    CORRECTIONS_FILE.write_text(
        json.dumps(corrections, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info(f"Сохранено исправление: '{original}' → '{corrected}'")

def apply_corrections(data: dict) -> dict:
    """Автоматически применяет сохранённые исправления к позициям чека."""
    corrections = load_corrections()
    if not corrections or "items" not in data:
        return data
    for item in data["items"]:
        key = item.get("name", "").lower().strip()
        if key in corrections:
            old = item["name"]
            item["name"] = corrections[key]
            logger.info(f"Автоисправление: '{old}' → '{item['name']}'")
    return data

# ─── Google Sheets с кэшем подключения ────────────────────────────────────────
_ws_cache = {"ws": None, "expires": 0}
_header_added = False


def get_worksheet():
    now = time.time()
    if _ws_cache["ws"] is None or now > _ws_cache["expires"]:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        _ws_cache["ws"] = sh.sheet1
        _ws_cache["expires"] = now + 600
        logger.info("Google Sheets: новое подключение")
    return _ws_cache["ws"]


def ensure_header():
    global _header_added
    if _header_added:
        return
    ws = get_worksheet()
    if ws.row_values(1) == []:
        ws.append_row(
            ["Дата", "Магазин / Место", "Сумма", "Категория", "Тип", "Добавлено"],
            value_input_option="USER_ENTERED",
        )
    _header_added = True


def add_row(data: dict):
    ensure_header()
    ws = get_worksheet()
    ws.append_row(
        [
            data.get("date", ""),
            data.get("store", ""),
            data.get("amount", ""),
            data.get("category", ""),
            data.get("type", "расход"),
            datetime.now().strftime("%d.%m.%Y %H:%M"),
        ],
        value_input_option="USER_ENTERED",
    )
    # Запоминаем номер последней строки для возможной отмены
    _ws_cache["last_row"] = len(ws.get_all_values())


# ─── Claude API ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — помощник для учёта личных финансов семьи в Никосии.
Твоя задача — извлечь ВСЕ позиции из чека и вернуть строго JSON без пояснений.

Если это чек с несколькими позициями — верни список items.
Если это одна трата (текст от пользователя) — верни один объект.

Формат для чека с позициями:
{
  "date": "DD.MM.YYYY",
  "store": "название магазина",
  "type": "расход",
  "items": [
    {"name": "название товара на русском", "amount": 1.23, "category": "категория"},
    {"name": "название товара на русском", "amount": 4.56, "category": "категория"}
  ]
}

Формат для одной траты:
{
  "date": "DD.MM.YYYY",
  "store": "место или не указано",
  "amount": 123.45,
  "category": "категория",
  "type": "расход или доход"
}

Список категорий (используй ТОЛЬКО их):
- еда
- стики
- здоровье
- подписки
- оборудование
- дом и быт
- одежда
- дорожные расходы
- красота и уход
- кафе
- бар
- детям
- россия
- непредвиденные
- аренда
- интернет
- связь
- доход

Правила категоризации:
- сигареты, табак, IQOS, стики → стики
- продукты, еда, фрукты, мясо, молоко → еда
- аптека, лекарства, анализы, лазер → здоровье
- Netflix, Spotify, приложения → подписки
- такси, автобус, парковка, каршеринг, бензин → дорожные расходы
- салон, маникюр, косметика, уход → красота и уход
- ресторан, кофейня, кафе → кафе
- бар, алкоголь → бар
- переводы в Россию, расходы РФ → россия
- зарплата, фриланс → доход
- всё остальное → непредвиденные

Язык чека может быть русский, английский или греческий — распознавай все три.
Названия товаров в поле "name" ВСЕГДА переводи на русский язык.
Примеры: ΜΑΡΟΥΛΙ → Салат, ΝΤΟΜΑΤΕΣ → Помидоры, ΚΑΡΟΤΤΑ → Морковь, AVOCADO → Авокадо,
ΜΗΛΑ → Яблоки, ΣΤΑΦΥΛΙ → Виноград, ΑΓΓΟΥΡΑΚΙΑ → Огурцы, ΠΙΠΕΡΙΑ → Перец,
ΜΑΪΝΤΑΝΟ → Петрушка, ΑΝΗΘΟΣ → Укроп, ΚΡΕΜΜΥΔΙΑ → Лук, ΛΕΜΟΝΙΑ → Лимоны,
ΕΛΙΕΣ → Оливки, ΚΟΛΟΚΥΘΑ → Кабачок/Тыква.
Если дата не указана — используй сегодняшнее число.
Отвечай ТОЛЬКО JSON, без лишнего текста."""


def parse_with_claude(image_b64: str | None = None, text: str | None = None) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.now().strftime("%d.%m.%Y")

    if image_b64:
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
            },
            {"type": "text", "text": f"Сегодня {today}. Распарси этот чек и верни JSON."},
        ]
    else:
        content = f"Сегодня {today}. Пользователь написал: «{text}». Распарси и верни JSON."

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Claude вернул невалидный JSON:\n{raw[:300]}")


def parse_item_corrections(text: str) -> list[tuple[str, str]]:
    """
    Разбирает текст вида:
      'maintano - петрушка\nголубой картофель - бэби картофель'
    Возвращает список (оригинал, исправление).
    """
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Поддержка разделителей: " - ", " — ", ": "
        for sep in [" - ", " — ", ": ", "-", "—"]:
            if sep in line:
                parts = line.split(sep, 1)
                orig = parts[0].strip()
                fixed = parts[1].strip()
                if orig and fixed:
                    results.append((orig, fixed))
                break
    return results


# ─── Клавиатура подтверждения ─────────────────────────────────────────────────
CONFIRM_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ Всё верно, записать"], ["✏️ Исправить"], ["❌ Отменить"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def build_preview(data: dict) -> str:
    if "items" in data:
        total = round(sum(i.get("amount", 0) for i in data["items"]), 2)
        lines = [f"🧾 Чек из {data.get('store', '—')}\n"]
        lines.append(f"📅 Дата: {data.get('date', '—')}\n")
        lines.append("Позиции:")
        for item in data["items"]:
            lines.append(f"  • {item['name']} — {item['amount']}€ [{item['category']}]")
        lines.append(f"\n💰 Итого: {total}€")
        lines.append("\nВсё верно?")
    else:
        emoji = "💸" if data.get("type") == "расход" else "💰"
        lines = [
            f"{emoji} Распознано:\n",
            f"📅 Дата: {data.get('date', '—')}",
            f"🏪 Место: {data.get('store', '—')}",
            f"💵 Сумма: {data.get('amount', 0)}€",
            f"🏷 Категория: {data.get('category', '—')}",
            f"📊 Тип: {data.get('type', '—')}",
            "\nВсё верно?",
        ]
    return "\n".join(lines)


# ─── Telegram handlers ────────────────────────────────────────────────────────
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет последнюю записанную строку из таблицы."""
    last_row = _ws_cache.get("last_row")
    if not last_row:
        await update.message.reply_text("⚠️ Нет записей для отмены в этой сессии.")
        return
    try:
        ws = get_worksheet()
        # Читаем строку перед удалением чтобы показать что удалили
        row_values = ws.row_values(last_row)
        ws.delete_rows(last_row)
        _ws_cache["last_row"] = None
        info = f"{row_values[0]} | {row_values[1]} | {row_values[2]}€ | {row_values[3]}" if row_values else "—"
        await update.message.reply_text(
            f"↩️ Последняя запись удалена:\n{info}"
        )
    except Exception as e:
        logger.exception("Ошибка при отмене записи")
        await update.message.reply_text(f"❌ Не удалось отменить: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я записываю твои расходы и доходы.\n\n"
        "Просто отправь мне:\n"
        "📷 Фото или скриншот чека\n"
        "✏️ Текст, например: «кофе 250р» или «зарплата 80000»\n\n"
        "Команды:\n"
        "/отмена — удалить последнюю запись\n\n"
        "Всё остальное сделаю сам — распознаю и запишу в таблицу.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Распознаю чек...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")

    try:
        data = parse_with_claude(image_b64=image_b64)
        data = apply_corrections(data)  # применяем сохранённые исправления
        context.user_data["pending"] = data
        context.user_data["state"] = "awaiting_confirm"

        await update.message.reply_text(build_preview(data), reply_markup=CONFIRM_KEYBOARD)
    except Exception as e:
        logger.exception("Ошибка при обработке фото")
        await update.message.reply_text(f"❌ Не получилось разобрать чек: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return

    state = context.user_data.get("state")

    # ── Отмена текущего ввода ─────────────────────────────────────────────────
    if state == "awaiting_confirm" and text == "❌ Отменить":
        context.user_data.clear()
        await update.message.reply_text(
            "🚫 Запись отменена. Ничего не сохранено.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # ── Подтверждение ─────────────────────────────────────────────────────────
    if state == "awaiting_confirm" and text == "✅ Всё верно, записать":
        data = context.user_data.pop("pending", None)
        context.user_data.pop("state", None)

        if data is None:
            await update.message.reply_text("⚠️ Нет данных для записи.", reply_markup=ReplyKeyboardRemove())
            return

        try:
            if "items" in data:
                total = round(sum(i.get("amount", 0) for i in data["items"]), 2)
                add_row({
                    "date": data.get("date"),
                    "store": data.get("store"),
                    "amount": total,
                    "category": "еда",
                    "type": data.get("type", "расход"),
                })
            else:
                add_row(data)

            await update.message.reply_text("✅ Записано в таблицу!", reply_markup=ReplyKeyboardRemove())
        except Exception as e:
            logger.exception("Ошибка при записи")
            await update.message.reply_text(f"❌ Ошибка записи: {e}", reply_markup=ReplyKeyboardRemove())
        return

    # ── Запрос исправления ────────────────────────────────────────────────────
    if state == "awaiting_confirm" and text == "✏️ Исправить":
        context.user_data["state"] = "awaiting_correction"
        await update.message.reply_text(
            "✏️ Напиши что исправить.\n\n"
            "Для названий товаров (можно несколько строк):\n"
            "  maintano - Петрушка\n"
            "  голубой картофель - Бэби картофель\n\n"
            "Для общих полей:\n"
            "  магазин - Lidl\n"
            "  сумма - 35.50\n"
            "  категория - кафе\n"
            "  дата - 01.05.2026",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # ── Применение исправлений ────────────────────────────────────────────────
    if state == "awaiting_correction":
        data = context.user_data.get("pending")
        if data is None:
            await update.message.reply_text("⚠️ Нет данных для исправления.")
            context.user_data.clear()
            return

        pairs = parse_item_corrections(text)
        if not pairs:
            await update.message.reply_text(
                "🤔 Не понял формат. Напиши так:\n"
                "  maintano - Петрушка\n"
                "  магазин - Lidl"
            )
            return

        applied = []
        for orig, fixed in pairs:
            orig_lower = orig.lower()

            # Проверяем поля верхнего уровня
            if orig_lower in ("магазин", "место", "store"):
                data["store"] = fixed
                applied.append(f"магазин → {fixed}")
            elif orig_lower in ("сумма", "amount"):
                try:
                    if "items" not in data:
                        data["amount"] = float(fixed.replace(",", "."))
                    applied.append(f"сумма → {fixed}")
                except ValueError:
                    pass
            elif orig_lower in ("категория", "category"):
                data["category"] = fixed.lower()
                applied.append(f"категория → {fixed}")
            elif orig_lower in ("дата", "date"):
                data["date"] = fixed
                applied.append(f"дата → {fixed}")
            elif orig_lower in ("тип", "type"):
                data["type"] = fixed.lower()
                applied.append(f"тип → {fixed}")
            elif "items" in data:
                # Ищем товар по частичному совпадению названия
                matched = False
                for item in data["items"]:
                    if orig_lower in item["name"].lower() or item["name"].lower() in orig_lower:
                        old_name = item["name"]
                        item["name"] = fixed
                        save_correction(old_name, fixed)  # запоминаем на будущее
                        applied.append(f"{old_name} → {fixed}")
                        matched = True
                        break
                if not matched:
                    # Сохраняем как новое правило даже если не нашли в текущем чеке
                    save_correction(orig, fixed)
                    applied.append(f"📝 Запомнил: {orig} → {fixed}")
            else:
                save_correction(orig, fixed)
                applied.append(f"📝 Запомнил: {orig} → {fixed}")

        context.user_data["pending"] = data
        context.user_data["state"] = "awaiting_confirm"

        applied_text = "\n".join(f"  ✓ {a}" for a in applied)
        await update.message.reply_text(
            f"Исправлено:\n{applied_text}\n\nПроверь:\n\n" + build_preview(data),
            reply_markup=CONFIRM_KEYBOARD,
        )
        return

    # ── Новая трата текстом ───────────────────────────────────────────────────
    context.user_data.clear()
    await update.message.reply_text("📝 Обрабатываю...", reply_markup=ReplyKeyboardRemove())

    try:
        data = parse_with_claude(text=text)
        data = apply_corrections(data)
        context.user_data["pending"] = data
        context.user_data["state"] = "awaiting_confirm"

        await update.message.reply_text(build_preview(data), reply_markup=CONFIRM_KEYBOARD)
    except Exception as e:
        logger.exception("Ошибка при обработке текста")
        await update.message.reply_text(f"❌ Не получилось разобрать: {e}")


# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("отмена", cmd_undo))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
