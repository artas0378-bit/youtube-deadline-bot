import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
    logging.warning("ВНИМАНИЕ: Задайте корректный BOT_TOKEN в файле .env!")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Хранилище данных пользователей
# Структура:
# {
#   "users": {
#       "chat_id": {
#           "day": 0,                   # Текущий день цикла (от 0 до 14)
#           "status": "active",         # "active", "paused", "finished"
#           "answered_today": false,    # Ответил ли пользователь на сегодняшний утренний пуш
#           "start_date": "YYYY-MM-DD",  # Дата старта цикла
#           "stage_index": 0,           # Индекс текущего этапа (0 - Сценарий, 1 - Звук, 2 - Монтаж, 3 - Правки/Превью)
#           "last_push_date": "YYYY-MM-DD", # Дата последней отправки утреннего пуша
#           "custom_tasks": null        # Если задачи сдвинулись, храним актуальный список задач на каждый день цикла
#       }
#   }
# }
DATA_FILE = "data.json"

# Справочник базовых этапов (оригинальный план по дням цикла):
# 14-дневный цикл от 0 до 14.
# Релиз ролика проходит по Понедельникам раз в 14 дней.
# День 0 (Понедельник): Релиз прошлого ролика! Выходной / Отдых.
# Дни 1–4 (Вторник — Пятница): Написание сценария (4 дня).
# Дни 5–7 (Суббота — Понедельник): Запись звука / Рассылка / Подготовка (3 дня).
# Дни 8–11 (Вторник — Пятница): Монтаж видео (4 дня).
# Дни 12–13 (Суббота — Воскресенье): Финальные правки, превью, загрузка на YouTube (2 дня).
# День 14 (Понедельник): РЕЛИЗ нового ролика! (Автоматического перехода нет, цикл останавливается и ждет ручного перезапуска).

STAGES = [
    {"name": "Написание сценария", "days": [1, 2, 3, 4]},
    {"name": "Запись звука / Рассылка / Подготовка", "days": [5, 6, 7]},
    {"name": "Монтаж видео", "days": [8, 9, 10, 11]},
    {"name": "Финальные правки, превью, загрузка на YouTube", "days": [12, 13]}
]

DEFAULT_TASKS = {
    0: "Выходной / Отдых после релиза прошлого ролика! 💤",
    1: "Написание сценария (День 1 из 4) ✍️",
    2: "Написание сценария (День 2 из 4) ✍️",
    3: "Написание сценария (День 3 из 4) ✍️",
    4: "Написание сценария (День 4 из 4) ✍️",
    5: "Запись звука / Рассылка / Подготовка (День 1 из 3) 🎙️",
    6: "Запись звука / Рассылка / Подготовка (День 2 из 3) 🎙️",
    7: "Запись звука / Рассылка / Подготовка (День 3 из 3) 🎙️",
    8: "Монтаж видео (День 1 из 4) 🎬",
    9: "Монтаж видео (День 2 из 4) 🎬",
    10: "Монтаж видео (День 3 из 4) 🎬",
    11: "Монтаж видео (День 4 из 4) 🎬",
    12: "Финальные правки, превью, загрузка на YouTube (День 1 из 2) 🛠️",
    13: "Финальные правки, превью, загрузка на YouTube (День 2 из 2) 🛠️",
    14: "🚀 РЕЛИЗ НОВОГО РОЛИКА! Ура! 🎉 (Цикл завершен, перезапустите через /reset)"
}

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")
        return {"users": {}}

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

def get_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выполнил", callback_value="done_today"),
            InlineKeyboardButton(text="⏩ На завтра", callback_value="postpone_tomorrow")
        ],
        [
            InlineKeyboardButton(text="🚀 Завершил этап досрочно", callback_value="finish_stage_early")
        ]
    ])
    # Новая версия aiogram использует callback_data вместо callback_value, поправим ниже при определении фабрики или напрямую строками.
    return keyboard

# Определим инлайн-кнопки в стиле aiogram 3.x
def get_action_keyboard(day: int):
    # Если день 0 или 14, кнопки действий не требуются
    if day == 0 or day >= 14:
        return None
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ На сегодня выполнил", callback_data="action_done"),
            InlineKeyboardButton(text="⏩ Перенести на завтра", callback_data="action_postpone")
        ],
        [
            InlineKeyboardButton(text="🚀 Завершил этап досрочно", callback_data="action_complete_stage")
        ]
    ])
    return keyboard

def get_user_task_for_day(user_data, day: int):
    """
    Возвращает текст задачи на указанный день.
    Если у пользователя есть кастомная карта задач (custom_tasks) из-за сдвигов,
    использует её, иначе использует базовую (DEFAULT_TASKS).
    """
    if user_data.get("custom_tasks") and str(day) in user_data["custom_tasks"]:
        return user_data["custom_tasks"][str(day)]
    return DEFAULT_TASKS.get(day, "Задача не определена")

def get_current_stage_name(user_data):
    """
    Определяет текущий этап пользователя. Если у него кастомные задачи, 
    пытается определить по stage_index или по текущему дню.
    """
    day = user_data.get("day", 0)
    if day == 0:
        return "Выходной"
    if day >= 14:
        return "Релиз!"
    
    # Если этап переключался досрочно, ориентируемся на stage_index
    stage_idx = user_data.get("stage_index", -1)
    if 0 <= stage_idx < len(STAGES):
        return STAGES[stage_idx]["name"]
    
    # Иначе по дню
    for stage in STAGES:
        if day in stage["days"]:
            return stage["name"]
    return "Неизвестный этап"

def get_remaining_tasks_list(user_data):
    """
    Генерирует список оставшихся задач до конца цикла.
    """
    current_day = user_data.get("day", 0)
    lines = []
    for d in range(current_day, 15):
        task_text = get_user_task_for_day(user_data, d)
        lines.append(f"• День {d}: {task_text}")
    return "\n".join(lines)

def build_push_message(day: int, user_data):
    """
    Формирует текст ежедневного пуша.
    """
    today_str = datetime.now().strftime("%d.%m.%Y")
    days_left = 14 - day
    task_today = get_user_task_for_day(user_data, day)
    
    msg = (
        f"📅 *Сегодня День {day} из 14* ({today_str})\n"
        f"⏳ *До релиза осталось дней:* {days_left}\n\n"
        f"📋 *Задача на СЕГОДНЯ:*\n{task_today}"
    )
    return msg

# --- Обработчики команд ---

def get_start_date_and_day():
    """
    Вычисляет дату начала 14-дневного цикла (прошлый понедельник от текущей даты)
    и текущий день цикла (разницу в днях между сегодня и этой датой начала).
    Если сегодня понедельник, берется сегодняшний день (будет День 0).
    Если другой день (например, среда), вычисляется дата понедельника этой недели,
    и текущий день цикла будет равен разнице (например, 2 для среды).
    """
    today = datetime.now().date()
    start_date = today - timedelta(days=today.weekday())
    day_diff = (today - start_date).days
    # Гарантируем, что день цикла находится в пределах 0-14
    day = max(0, min(14, day_diff))
    return start_date, day

@dp.message(Command("start"))
async def cmd_start(message: Message):
    chat_id = str(message.chat.id)
    data = load_data()
    
    start_date, day = get_start_date_and_day()
    
    # Инициализация нового цикла с вычисленным днем
    # Если день 0 - это выходной, значит отвечать сегодня не надо (answered_today = True).
    # Иначе, если мы посреди цикла, то по умолчанию answered_today = False (пользователь должен ответить)
    answered_today = True if day == 0 or day >= 14 else False
    
    # Определим stage_index по дню цикла
    stage_idx = -1
    for i, stage in enumerate(STAGES):
        if day in stage["days"]:
            stage_idx = i
            break
            
    data["users"][chat_id] = {
        "day": day,
        "status": "active",
        "answered_today": answered_today,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "stage_index": stage_idx,
        "last_push_date": datetime.now().strftime("%Y-%m-%d"),
        "custom_tasks": None
    }
    save_data(data)
    
    welcome_msg = (
        "👋 Привет! Я твой бот-контролер дедлайнов для YouTube-канала.\n\n"
        "📅 Мы запускаем **14-дневный цикл** работы над новым роликом!\n"
        "Каждое утро в 09:00 я буду присылать тебе актуальную задачу на день и кнопки контроля.\n\n"
        f"🧭 **Старт цикла вычислен от понедельника:** {start_date.strftime('%d.%m.%Y')}.\n"
        f"🚦 **Сегодня День {day} из 14**.\n"
    )
    
    if day == 0:
        welcome_msg += "🎉 Сегодня выходной и отдых после прошлого релиза! Набирайся сил.\n\n"
    else:
        task_today = get_user_task_for_day(data["users"][chat_id], day)
        welcome_msg += f"📋 **Задача на СЕГОДНЯ:**\n{task_today}\n\n"
        
    welcome_msg += (
        "ℹ️ Доступные команды:\n"
        "📊 /status — Твой текущий прогресс и оставшиеся задачи\n"
        "🔄 /reset — Сбросить цикл и начать сначала\n\n"
        "Желаю продуктивного цикла! 🔥"
    )
    await message.answer(welcome_msg, parse_mode="Markdown")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    chat_id = str(message.chat.id)
    data = load_data()
    
    if chat_id not in data["users"]:
        await message.answer("⚠️ Вы еще не запустили цикл дедлайнов. Нажмите /start, чтобы начать!")
        return
    
    user_data = data["users"][chat_id]
    day = user_data["day"]
    days_left = 14 - day
    stage_name = get_current_stage_name(user_data)
    
    status_text = (
        f"📊 **Текущий статус проекта:**\n"
        f"• **Дата старта цикла:** {datetime.strptime(user_data['start_date'], '%Y-%m-%d').strftime('%d.%m.%Y')}\n"
        f"• **День цикла:** {day} из 14\n"
        f"• **Текущий этап:** {stage_name}\n"
        f"• **До релиза осталось:** {days_left} дн.\n\n"
        f"📋 **Список оставшихся задач:**\n"
        f"{get_remaining_tasks_list(user_data)}"
    )
    
    # Добавим инлайн-клавиатуру, если день активный и пользователь еще не ответил
    keyboard = None
    if 0 < day < 14 and not user_data.get("answered_today", False):
        keyboard = get_action_keyboard(day)
        status_text += "\n\n⚠️ Вы еще не дали отчет по сегодняшней задаче! Пожалуйста, выберите действие ниже:"
        
    await message.answer(status_text, parse_mode="Markdown", reply_markup=keyboard)

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    chat_id = str(message.chat.id)
    data = load_data()
    
    start_date, day = get_start_date_and_day()
    answered_today = True if day == 0 or day >= 14 else False
    
    stage_idx = -1
    for i, stage in enumerate(STAGES):
        if day in stage["days"]:
            stage_idx = i
            break
            
    data["users"][chat_id] = {
        "day": day,
        "status": "active",
        "answered_today": answered_today,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "stage_index": stage_idx,
        "last_push_date": datetime.now().strftime("%Y-%m-%d"),
        "custom_tasks": None
    }
    save_data(data)
    
    reset_msg = (
        "🔄 **Цикл дедлайнов успешно сброшен!**\n\n"
        f"🧭 **Старт цикла вычислен от понедельника:** {start_date.strftime('%d.%m.%Y')}.\n"
        f"🚀 Мы начали новый 14-дневный отсчет. Сегодня День {day}.\n"
    )
    
    if day == 0:
        reset_msg += "🌴 Сегодня День 0 — Выходной / Отдых. Завтра начнется этап написания сценария!"
    else:
        task_today = get_user_task_for_day(data["users"][chat_id], day)
        reset_msg += f"📋 **Задача на СЕГОДНЯ:**\n{task_today}"
        
    await message.answer(reset_msg, parse_mode="Markdown")

# --- Обработчики нажатий на кнопки ---

@dp.callback_query(F.data == "action_done")
async def process_action_done(callback: CallbackQuery):
    chat_id = str(callback.message.chat.id)
    data = load_data()
    
    if chat_id not in data["users"]:
        await callback.answer("Ошибка: пользователь не найден.", show_alert=True)
        return
        
    user_data = data["users"][chat_id]
    
    if user_data.get("answered_today", False):
        await callback.answer("Вы уже отметили выполнение или перенесли задачу на сегодня!", show_alert=True)
        return

    user_data["answered_today"] = True
    save_data(data)
    
    await callback.answer()
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ *Отчет принят:* Красава! Хорош, отдыхай до завтра 🔥",
        parse_mode="Markdown",
        reply_markup=None
    )

@dp.callback_query(F.data == "action_postpone")
async def process_action_postpone(callback: CallbackQuery):
    chat_id = str(callback.message.chat.id)
    data = load_data()
    
    if chat_id not in data["users"]:
        await callback.answer("Ошибка: пользователь не найден.", show_alert=True)
        return
        
    user_data = data["users"][chat_id]
    
    if user_data.get("answered_today", False):
        await callback.answer("Вы уже приняли решение на сегодня!", show_alert=True)
        return

    # Логика переноса задачи на завтра:
    # 1. Сдвигаем все последующие задачи на 1 день вперед вплоть до дня 13.
    # 2. Вчерашняя/сегодняшняя невыполненная задача становится задачей на завтра.
    # 3. При этом сгорает 1 резервный/выходной день. Фактически мы переопределяем custom_tasks.
    # Создадим кастомную карту задач, если ее нет.
    day = user_data["day"]
    
    if day >= 13:
        # На 13 дне переносить уже некуда, так как 14 - это релиз и цикл завершается/останавливается.
        await callback.answer("⚠️ Предпоследний день перед релизом! Перенести задачу дальше нельзя.", show_alert=True)
        return

    custom_tasks = user_data.get("custom_tasks")
    if not custom_tasks:
        custom_tasks = {str(k): v for k, v in DEFAULT_TASKS.items()}
    
    # Сдвигаем задачи начиная со следующего дня (day+1) до 13 дня.
    # То есть задача, которая была на сегодня (day), должна перенестись на завтра (day+1).
    # А та, что была на завтра, переносится дальше, сдвигая весь хвост и сокращая время на финальные этапы.
    # Сохраняем задачу сегодняшнего дня, чтобы вставить её на завтра.
    task_to_move = custom_tasks[str(day)]
    
    # Сдвиг хвоста
    for d in range(13, day, -1):
        custom_tasks[str(d)] = custom_tasks[str(d - 1)]
    
    # Задача на завтра теперь равна перенесенной задаче
    custom_tasks[str(day + 1)] = f"⚠️ [ПЕРЕНЕСЕНО] {task_to_move}"
    
    user_data["custom_tasks"] = custom_tasks
    user_data["answered_today"] = True
    save_data(data)
    
    await callback.answer()
    await callback.message.edit_text(
        callback.message.text + "\n\n⏩ *Отчет принят:* Понял, задача перенесена. Завтра нужно обязательно доделать! 🎯",
        parse_mode="Markdown",
        reply_markup=None
    )

@dp.callback_query(F.data == "action_complete_stage")
async def process_action_complete_stage(callback: CallbackQuery):
    chat_id = str(callback.message.chat.id)
    data = load_data()
    
    if chat_id not in data["users"]:
        await callback.answer("Ошибка: пользователь не найден.", show_alert=True)
        return
        
    user_data = data["users"][chat_id]
    day = user_data["day"]
    
    # Определим, на каком этапе мы находимся по дню или по stage_index
    current_stage_idx = user_data.get("stage_index", -1)
    if current_stage_idx == -1:
        # Если stage_index еще не инициализирован, определим его по текущему дню
        for i, stage in enumerate(STAGES):
            if day in stage["days"]:
                current_stage_idx = i
                break
                
    if current_stage_idx == -1 or current_stage_idx >= len(STAGES):
        await callback.answer("⚠️ На данном дне цикла досрочно завершить этап нельзя!", show_alert=True)
        return
        
    next_stage_idx = current_stage_idx + 1
    
    # Логика досрочного завершения:
    # 1. Дни календаря (day) НЕ сбиваются! На следующий день бот просто начнет давать задачи из нового этапа.
    # 2. Переопределяем задачи во все дни текущего этапа, начиная с ЗАВТРА (day+1), до начала следующего этапа,
    #    а также при необходимости сдвигаем задачи следующего этапа назад, чтобы дать больше времени на него.
    # Лучшее решение: мы переназначаем задачи с завтрашнего дня (day + 1) на задачи следующего этапа.
    # Давайте найдем, на какой день по плану должен был начаться следующий этап, или просто заполним оставшиеся дни текущего этапа первыми днями следующего этапа.
    custom_tasks = user_data.get("custom_tasks")
    if not custom_tasks:
        custom_tasks = {str(k): v for k, v in DEFAULT_TASKS.items()}
        
    if next_stage_idx < len(STAGES):
        next_stage_name = STAGES[next_stage_idx]["name"]
        next_stage_days = STAGES[next_stage_idx]["days"]
        
        # Получаем задачи следующего этапа (в оригинальном списке)
        next_stage_tasks = [DEFAULT_TASKS[d] for d in next_stage_days]
        
        # Все дни, начиная с завтрашнего (day+1) до конца следующего этапа включительно,
        # переопределяем под задачи следующего этапа, чтобы начать его раньше.
        # То есть завтра у нас начнется первый день следующего этапа.
        start_day_for_next = day + 1
        
        # Заполняем задачи следующего этапа по порядку, начиная с start_day_for_next
        idx_task = 0
        for d in range(start_day_for_next, 14):
            if idx_task < len(next_stage_tasks):
                custom_tasks[str(d)] = f"🚀 [ДОСРОЧНО] {next_stage_tasks[idx_task]}"
                idx_task += 1
            else:
                # Если задачи следующего этапа кончились, а до релиза еще есть дни, 
                # то это резервные дни для финальных правок или отдыха перед релизом!
                # Заполним задачами этапа финальных правок (последний этап) или просто отдыхом
                last_stage_days = STAGES[-1]["days"]
                last_stage_tasks = [DEFAULT_TASKS[ld] for ld in last_stage_days]
                custom_tasks[str(d)] = f"🍀 Резервное время: {last_stage_tasks[min(d - start_day_for_next - len(next_stage_tasks), len(last_stage_tasks)-1)]}"
                
        user_data["stage_index"] = next_stage_idx
        confirm_text = f"🚀 *Этап завершен досрочно!* Текущий статус переключен на следующий этап: **{next_stage_name}**.\n📅 Календарные дни не сбились (сегодня День {day}). Завтра ты начнешь задачи нового этапа!"
    else:
        # Если это был последний этап перед релизом
        # Переопределяем оставшиеся дни до 13 на отдых / финальную подготовку
        for d in range(day + 1, 14):
            custom_tasks[str(d)] = "😎 Все этапы завершены досрочно! Время отдыха перед релизом 🌴"
        user_data["stage_index"] = len(STAGES)
        confirm_text = "🚀 *Все основные этапы завершены досрочно!* До релиза ты можешь отдыхать и готовиться к публикации ролика! 🎉"

    user_data["custom_tasks"] = custom_tasks
    user_data["answered_today"] = True
    save_data(data)
    
    await callback.answer()
    await callback.message.edit_text(
        callback.message.text + f"\n\n{confirm_text}",
        parse_mode="Markdown",
        reply_markup=None
    )

# --- Планировщик (APScheduler) и фоновые задачи ---

async def auto_postpone_unanswered_users():
    """
    Проверяет всех активных пользователей в конце дня (например, в 23:59).
    Если пользователь не ответил до конца дня, автоматически засчитывается как перенос на завтра.
    """
    logger.info("Запуск автоматического переноса пропущенных задач...")
    data = load_data()
    updated = False
    
    for chat_id, user_data in data["users"].items():
        if user_data.get("status") == "active":
            day = user_data.get("day", 0)
            # Применяем перенос только для активных рабочих дней (с 1 по 12)
            if 0 < day < 13 and not user_data.get("answered_today", False):
                logger.info(f"Пользователь {chat_id} пропустил ответ на День {day}. Выполняем автоперенос.")
                
                custom_tasks = user_data.get("custom_tasks")
                if not custom_tasks:
                    custom_tasks = {str(k): v for k, v in DEFAULT_TASKS.items()}
                
                task_to_move = custom_tasks[str(day)]
                
                # Сдвиг хвоста задач
                for d in range(13, day, -1):
                    custom_tasks[str(d)] = custom_tasks[str(d - 1)]
                
                custom_tasks[str(day + 1)] = f"⚠️ [ПРОПУЩЕНО/ПЕРЕНЕСЕНО] {task_to_move}"
                
                user_data["custom_tasks"] = custom_tasks
                user_data["answered_today"] = True
                updated = True
                
                # Отправим уведомление пользователю об автопереносе
                try:
                    await bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            f"⏰ **Время вышло!** Вы не отправили отчет за сегодня.\n"
                            f"Задача автоматически переносится на завтра. Использован 1 резервный день! 📉"
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление об автопереносе пользователю {chat_id}: {e}")
                    
    if updated:
        save_data(data)

async def send_morning_push():
    """
    Ежедневный утренний пуш в 09:00.
    1. Инкрементирует день цикла на +1 (если статус active).
    2. Отправляет пуш-сообщение с кнопками.
    """
    logger.info("Запуск отправки утренних пушей...")
    data = load_data()
    updated = False
    
    for chat_id, user_data in data["users"].items():
        if user_data.get("status") != "active":
            continue
            
        current_day = user_data.get("day", 0)
        
        if current_day >= 14:
            # Цикл завершен (День 14 - Релиз). Автоматического перехода нет, ждет ручного сброса
            continue
            
        # 1. Переходим на следующий день цикла
        next_day = current_day + 1
        user_data["day"] = next_day
        user_data["answered_today"] = False if 0 < next_day < 14 else True # На 14 день (релиз) кнопки не нужны, отмечаем как True
        user_data["last_push_date"] = datetime.now().strftime("%Y-%m-%d")
        
        # Определим stage_index по новому дню, если он еще не переопределен кастомно
        if user_data.get("stage_index", -1) == -1 or next_day == 1:
            for i, stage in enumerate(STAGES):
                if next_day in stage["days"]:
                    user_data["stage_index"] = i
                    break
        
        updated = True
        
        # 2. Отправка пуша
        msg_text = build_push_message(next_day, user_data)
        keyboard = get_action_keyboard(next_day)
        
        try:
            await bot.send_message(
                chat_id=int(chat_id),
                text=msg_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            logger.info(f"Утренний пуш успешно отправлен пользователю {chat_id} (День {next_day})")
        except Exception as e:
            logger.error(f"Ошибка отправки утреннего пуша пользователю {chat_id}: {e}")
            
    if updated:
        save_data(data)

# --- Запуск бота ---

async def main():
    logger.info("Запуск Telegram-бота дедлайнов...")
    
    # Регистрация фоновых задач в APScheduler
    # 1. Утренний пуш каждый день в 09:00
    scheduler.add_job(
        send_morning_push,
        trigger=CronTrigger(hour=9, minute=0, second=0),
        id="morning_push",
        replace_existing=True
    )
    
    # 2. Автоматический перенос пропущенных ответов в конце дня в 23:59
    scheduler.add_job(
        auto_postpone_unanswered_users,
        trigger=CronTrigger(hour=23, minute=59, second=0),
        id="auto_postpone",
        replace_existing=True
    )
    
    # Запускаем планировщик
    scheduler.start()
    logger.info("Планировщик задач успешно запущен.")
    
    # Запуск polling
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
