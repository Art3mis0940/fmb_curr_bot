import re
import time
import logging
import asyncio
import socket
import aiohttp
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from playwright.async_api import async_playwright

# --- Настройки ---
BOT_TOKEN = "8708176681:AAFVRA3jx0qludpHn9OP_sJqDcpWuzfcscc"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Глобальное хранилище ---
exchange_rates = {}
last_update_time = 0
user_last_choice = {}
user_last_bot_message_id = {}

# Глобальная переменная для бота (будет инициализирована в main)
bot = None

async def fetch_all_rates():
    """Парсит всю таблицу курсов из вкладки 'В мобильном приложении'."""
    global exchange_rates, last_update_time
    
    logging.info("🌐 Запускаем браузер для обновления курсов...")
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = await context.new_page()
            
            await page.goto("https://bankffin.kz/ru/exchange-rates", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            
            await page.click("text=В мобильном приложении", timeout=5000)
            await page.wait_for_timeout(3000)
            
            all_text = await page.evaluate("document.body.innerText")
            
            pattern = r'([A-Z]{3})\s*/\s*([A-Z]{3})\s+([\d.,]+)\s+([\d.,]+)'
            matches = re.findall(pattern, all_text)
            
            new_rates = {}
            for match in matches:
                cur1, cur2, buy_str, sell_str = match
                pair_name = f"{cur1}/{cur2}"
                
                if pair_name not in new_rates:
                    buy = float(buy_str.replace(',', '.'))
                    sell = float(sell_str.replace(',', '.'))
                    new_rates[pair_name] = {"buy": buy, "sell": sell}
            
            exchange_rates = new_rates
            last_update_time = time.time()
            
            await browser.close()
            logging.info(f"✅ Курсы обновлены. Найдено пар: {len(exchange_rates)}")
            return True
            
    except Exception as e:
        logging.error(f"❌ Ошибка при обновлении курсов: {e}")
        return False

async def daily_rate_updater():
    """Обновляет курсы каждую ночь в 03:00."""
    while True:
        now = datetime.now()
        next_update = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= next_update:
            next_update += timedelta(days=1)
        
        sleep_seconds = (next_update - now).total_seconds()
        logging.info(f"⏳ Следующее обновление через {int(sleep_seconds / 3600)} ч. {int((sleep_seconds % 3600) / 60)} мин.")
        
        await asyncio.sleep(sleep_seconds)
        await fetch_all_rates()

def calculate_topup(amount, rate, buffer_percent=2.0):
    exact_rub = amount / rate
    recommended_rub = exact_rub * (1 + buffer_percent / 100)
    return {
        "amount": amount,
        "rate": rate,
        "exact_rub": round(exact_rub, 2),
        "recommended_rub": round(recommended_rub, 2)
    }

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="RUB → KZT", callback_data="convert_kzt"),
            InlineKeyboardButton(text="RUB → USD", callback_data="convert_usd")
        ]
    ])

async def edit_or_send(user_id, text, parse_mode=None, reply_markup=None):
    """Редактирует последнее сообщение бота или отправляет новое."""
    message_id = user_last_bot_message_id.get(user_id)
    
    try:
        if message_id:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        else:
            msg = await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            user_last_bot_message_id[user_id] = msg.message_id
    except Exception:
        msg = await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
        user_last_bot_message_id[user_id] = msg.message_id

# --- Инициализация роутера ---
dp = Dispatcher()
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name.capitalize() if message.from_user.first_name else "друг"
    
    if user_id in user_last_choice:
        del user_last_choice[user_id]
    
    text = f"Привет, {user_name}! 👋\n\nВыбери направление конвертации:"
    
    msg = await message.answer(text, reply_markup=get_main_keyboard())
    user_last_bot_message_id[user_id] = msg.message_id

@router.callback_query(F.data.in_(["convert_kzt", "convert_usd"]))
async def handle_currency_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    currency = "KZT" if callback.data == "convert_kzt" else "USD"
    user_last_choice[user_id] = currency
    
    currency_name = "тенге" if currency == "KZT" else "долларах"
    
    text = f"💳 Выбрано: RUB → {currency}\n\nВведи сумму в {currency_name} (только число):"
    
    await edit_or_send(user_id, text)
    await callback.answer()

@router.message(F.text)
async def handle_amount(message: Message):
    user_id = message.from_user.id
    
    try:
        await bot.delete_message(chat_id=user_id, message_id=message.message_id)
    except Exception:
        pass
    
    text = message.text.strip().replace(',', '.')
    
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        error_msg = await message.answer("⚠️ Введи только положительное число (например: 1500 или 50.5)")
        await asyncio.sleep(3)
        try:
            await bot.delete_message(chat_id=user_id, message_id=error_msg.message_id)
        except Exception:
            pass
        return
    
    currency = user_last_choice.get(user_id)
    if not currency:
        msg = await message.answer("Сначала выбери направление конвертации:", reply_markup=get_main_keyboard())
        user_last_bot_message_id[user_id] = msg.message_id
        return
    
    message_id = user_last_bot_message_id.get(user_id)
    if message_id:
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=message_id, text="⏳ Считаю...")
        except Exception:
            msg = await message.answer("⏳ Считаю...")
            user_last_bot_message_id[user_id] = msg.message_id
            message_id = msg.message_id
    
    if not exchange_rates:
        await bot.edit_message_text(chat_id=user_id, message_id=message_id, text="❌ Курсы недоступны. Попробуй позже.")
        return
    
    rate = None
    currency_symbol = currency
    
    if currency == "KZT":
        if "RUB/KZT" in exchange_rates:
            rate = exchange_rates["RUB/KZT"]["buy"]
        else:
            await bot.edit_message_text(chat_id=user_id, message_id=message_id, text="❌ Курс RUB/KZT не найден.")
            return
    else:
        if "USD/RUB" in exchange_rates:
            usd_rub_sell = exchange_rates["USD/RUB"]["sell"]
            rate = 1 / usd_rub_sell
        else:
            await bot.edit_message_text(chat_id=user_id, message_id=message_id, text="❌ Курс USD/RUB не найден.")
            return
    
    result = calculate_topup(amount, rate)
    
    response_text = (
        f"💳 *Расчет конвертации (RUB → {currency_symbol})*\n\n"
        f"Сумма: *{result['amount']:.2f} {currency_symbol}*\n"
        f"Курс: *1 RUB = {result['rate']:.4f} {currency_symbol}*\n\n"
        f"Точная сумма: *{result['exact_rub']} RUB*\n"
        f"Рекомендую: *{result['recommended_rub']} RUB*\n"
        f"_(включая запас 2% на колебания курса)_\n\n"
        f"Что посчитаем дальше?"
    )
    
    await bot.edit_message_text(
        chat_id=user_id,
        message_id=message_id,
        text=response_text,
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

# --- Главная функция запуска ---
async def main():
    global bot
    
    # 1. Создаем коннектор и сессию СТРОГО внутри асинхронной функции
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    session = aiohttp.ClientSession(connector=connector)
    
    # 2. Инициализируем бота
    bot = Bot(token=BOT_TOKEN, session=session)
    
    dp.include_router(router)
    
    await fetch_all_rates()
    asyncio.create_task(daily_rate_updater())
    
    logging.info("🤖 Бот запущен и готов к работе!")
    
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())