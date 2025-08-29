import os
import re
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

load_dotenv()
API_TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

scheduler = AsyncIOScheduler()

# Хранилище для задач пользователей
user_tasks: defaultdict[int, list[tuple[str, datetime]]] = defaultdict(list)

def parse_event_and_time(text):
    """
    Парсит текст события и извлекает время и описание события.
    Поддерживает форматы:
    - "встреча завтра в 15:30"
    - "позвонить маме через 2 часа" 
    - "собрание 25.12.2025 14:00"
    - "напоминание 14:30"
    """
    
    # Паттерны для извлечения времени
    patterns = [
        # Формат: ЧЧ:ММ
        (r'(\d{1,2}):(\d{2})', 'time_only'),
        # Формат: через X часов/минут
        (r'через\s+(\d+)\s+(час[а-я]*|минут[а-я]*)', 'relative_time'),
        # Формат: ДД.ММ.ГГГГ ЧЧ:ММ
        (r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})', 'full_datetime'),
        # Формат: завтра в ЧЧ:ММ
        (r'завтра\s+в\s+(\d{1,2}):(\d{2})', 'tomorrow'),
        # Формат: сегодня в ЧЧ:ММ
        (r'сегодня\s+в\s+(\d{1,2}):(\d{2})', 'today'),
    ]
    
    event_text = text.strip()
    target_datetime = None
    
    for pattern, pattern_type in patterns:
        match = re.search(pattern, text.lower())
        if match:
            try:
                if pattern_type == 'time_only':
                    # Только время - устанавливаем на сегодня
                    hour, minute = map(int, match.groups())
                    now = datetime.now()
                    target_datetime = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    
                    # Если время уже прошло, устанавливаем на завтра
                    if target_datetime <= now:
                        target_datetime += timedelta(days=1)
                        
                elif pattern_type == 'relative_time':
                    # Относительное время
                    amount = int(match.group(1))
                    unit = match.group(2).lower()
                    
                    if 'час' in unit:
                        target_datetime = datetime.now() + timedelta(hours=amount)
                    elif 'минут' in unit:
                        target_datetime = datetime.now() + timedelta(minutes=amount)
                        
                elif pattern_type == 'full_datetime':
                    # Полная дата и время
                    day, month, year, hour, minute = map(int, match.groups())
                    target_datetime = datetime(year, month, day, hour, minute)
                    
                elif pattern_type == 'tomorrow':
                    # Завтра в указанное время
                    hour, minute = map(int, match.groups())
                    tomorrow = datetime.now() + timedelta(days=1)
                    target_datetime = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    
                elif pattern_type == 'today':
                    # Сегодня в указанное время
                    hour, minute = map(int, match.groups())
                    today = datetime.now()
                    target_datetime = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    
                    # Если время уже прошло, устанавливаем на завтра
                    if target_datetime <= today:
                        target_datetime += timedelta(days=1)
                
                # Удаляем найденное время из текста события
                event_text = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
                break
                
            except ValueError:
                continue
    
    # Убираем лишние слова из текста события
    cleanup_words = ['завтра', 'сегодня', 'через', 'в', 'на', 'час', 'часа', 'часов', 'минут', 'минуты', 'минута']
    for word in cleanup_words:
        event_text = re.sub(rf'\b{word}\b', '', event_text, flags=re.IGNORECASE)
    
    event_text = ' '.join(event_text.split())
    
    return event_text, target_datetime

async def send_notification(chat_id: int, event_text: str):
    """Отправка уведомления пользователю"""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Напоминание!\n\n{event_text}"
        )
    except Exception as e:
        print(f"Ошибка отправки уведомления: {e}")

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "Привет! Я твой личный помощник календаря 📅\n\n"
        "Команды:\n"
        "/task - создать событие с напоминанием\n"
        "/mytasks - показать активные задачи\n\n"
        "Попробуй создать первое событие!"
    )

@dp.message(Command("task"))
async def calendar_handler(message: Message):
    await message.answer(
        "📝 Напиши событие и время для напоминания\n\n"
        "Примеры:\n"
        "• встреча завтра в 15:30\n"
        "• позвонить маме через 2 часа\n"
        "• собрание 25.12.2025 14:00\n"
        "• купить продукты 18:00\n"
        "• тренировка сегодня в 19:00"
    )

@dp.message(Command("mytasks"))
async def show_tasks_handler(message: Message):
    user_id = message.from_user.id
    
    if not user_tasks[user_id]:
        await message.answer("У вас нет активных задач")
        return
    
    tasks_text = "📋 Ваши активные задачи:\n\n"
    for i, task_info in enumerate(user_tasks[user_id], 1):
        event_text, scheduled_time = task_info
        time_str = scheduled_time.strftime("%d.%m.%Y %H:%M")
        tasks_text += f"{i}. {event_text}\n   ⏱ {time_str}\n\n"
    
    await message.answer(tasks_text)

@dp.message(F.text)
async def handle_event_text(message: Message):
    try:
        # Парсим событие и время
        event_text, target_datetime = parse_event_and_time(message.text)
        
        if target_datetime is None:
            await message.answer(
                "❌ Не удалось распознать время в вашем сообщении.\n\n"
                "Попробуйте использовать один из форматов:\n"
                "• встреча завтра в 15:30\n"
                "• позвонить маме через 2 часа\n"
                "• собрание 25.12.2025 14:00\n"
                "• напоминание 18:00"
            )
            return
        
        if target_datetime <= datetime.now():
            await message.answer("❌ Время не может быть в прошлом")
            return
        
        if not event_text:
            event_text = "Напоминание"
        
        # Сохраняем задачу для пользователя
        user_id = message.from_user.id
        
        # Создаем задачу в планировщике
        job_id = f"user_{user_id}_task_{len(user_tasks[user_id])}"
        
        scheduler.add_job(
            send_notification,
            trigger=DateTrigger(run_date=target_datetime),
            args=[message.chat.id, event_text],
            id=job_id
        )
        
        # Сохраняем информацию о задаче
        user_tasks[user_id].append((event_text, target_datetime))
        
        # Подтверждение создания задачи
        time_str = target_datetime.strftime("%d.%m.%Y %H:%M")
        await message.answer(
            f"✅ Задача создана!\n\n"
            f"📝 Событие: {event_text}\n"
            f"⏰ Время: {time_str}\n\n"
            f"Я напомню вам в указанное время!"
        )
        
    except Exception as e:
        await message.answer("❌ Произошла ошибка при обработке события")
        print(f"Ошибка: {e}")

async def main():
    scheduler.start()
    print("Планировщик запущен")
    
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
