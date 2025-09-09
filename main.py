import os
import re
import json
import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ChatAction
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import requests
from typing import Union, Dict, Any, List, Tuple, Optional
import logging
from difflib import SequenceMatcher

# SQLAlchemy imports
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import sessionmaker, relationship, Session, declarative_base
from sqlalchemy.sql import text
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
API_TOKEN = os.getenv('BOT_TOKEN')
GIGACHAT_KEY = os.getenv("GIGACHAT_AUTHORIZATION_KEY")
assert API_TOKEN is not None
assert GIGACHAT_KEY is not None

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# SQLAlchemy setup
DATABASE_URL = "sqlite:///calendar_bot.db"
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Глобальные переменные для токена Gigachat
gigachat_token = None
token_expires_at = None

# Временное хранилище для незавершенных задач
pending_tasks: defaultdict[int, dict] = defaultdict(dict)

# Время уведомлений в минутах
NOTIFICATION_OPTIONS = {
    "5min": {"text": "🔔 За 5 минут", "minutes": 5, "emoji": "⚡"},
    "15min": {"text": "🔔 За 15 минут", "minutes": 15, "emoji": "⏰"},
    "30min": {"text": "🔔 За 30 минут", "minutes": 30, "emoji": "⏲️"},
    "1hour": {"text": "🔔 За 1 час", "minutes": 60, "emoji": "🕐"},
    "2hour": {"text": "🔔 За 2 часа", "minutes": 120, "emoji": "🕑"},
    "1day": {"text": "🔔 За 1 день", "minutes": 1440, "emoji": "📅"},
    "no_notification": {"text": "🔕 Без уведомления", "minutes": 0, "emoji": "🔇"}
}

# Модели базы данных
class Event(Base):
    __tablename__ = "events"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    description = Column(Text, nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    notifications = relationship("Notification", back_populates="event", cascade="all, delete-orphan")

class Notification(Base):
    __tablename__ = "notifications"
    
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    notification_minutes = Column(Integer, nullable=False)
    is_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    event = relationship("Event", back_populates="notifications")

def init_db():
    """Инициализация базы данных"""
    Base.metadata.create_all(bind=engine)

def get_db() -> Session:
    """Получение сессии базы данных"""
    return SessionLocal()

def get_gigachat_token() -> str:
    """Получение токена доступа Gigachat"""
    global gigachat_token, token_expires_at
    
    if gigachat_token and token_expires_at and datetime.now() < token_expires_at:
        return gigachat_token
    
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {GIGACHAT_KEY}"
    }
    
    data = {
        "scope": "GIGACHAT_API_PERS"
    }
    
    try:
        response = requests.post(url, headers=headers, data=data, verify=False)
        
        if response.status_code == 200:
            token_data = response.json()
            gigachat_token = token_data["access_token"]
            expires_in = int(token_data.get("expires_in", 1800))
            token_expires_at = datetime.now() + timedelta(seconds=expires_in - 60)
            
            logger.info(f"Получен новый токен Gigachat, действует до: {token_expires_at}")
            return gigachat_token
        else:
            logger.error(f"Ошибка получения токена Gigachat: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Исключение при получении токена Gigachat: {e}")
        return None

def gigachat_reply(prompt: str) -> str:
    """Отправка запроса в Гигачат"""
    token = get_gigachat_token()
    if not token:
        return "⚠ Не удалось получить токен авторизации Gigachat"
    
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {
        "model": "GigaChat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }
    
    try:
        resp = requests.post(url, headers=headers, json=data, verify=False)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            logger.error(f"Gigachat API error: {resp.status_code} - {resp.text}")
            return f"⚠ Ошибка Gigachat: {resp.text}"
    except Exception as e:
        logger.error(f"Gigachat connection error: {e}")
        return f"⚠ Gigachat недоступен: {e}"

def parse_message_with_ai(msg: str) -> str:
    """Улучшенное преобразование сообщения в JSON через Gigachat"""
    current_datetime = datetime.now()
    current_date_str = current_datetime.strftime("%Y-%m-%d")
    current_time_str = current_datetime.strftime("%H:%M")
    tomorrow_str = (current_datetime + timedelta(days=1)).strftime('%Y-%m-%d')
    
    json_prompt = f"""[КОНТЕКСТ] Сегодня {current_date_str}, время {current_time_str}

[ЗАДАЧА] Преобразуй сообщение пользователя в JSON с полями:
- command: одна из команд ниже
- event: описание события (может быть частичным для поиска)
- datetime: YYYY-MM-DDTHH:MM (если указано время)
- new_datetime: YYYY-MM-DDTHH:MM (для команд изменения времени)
- new_event: новое описание события (для команды change_description)

[КОМАНДЫ]:
- "create" - создать событие
- "delete" - удалить событие  
- "change_time" - изменить время события
- "change_date" - изменить дату события
- "change_description" - изменить описание события
- "change_full" - полностью изменить событие (время + описание)
- "list" - показать события

[ПРАВИЛА]:
1. Отвечай ТОЛЬКО валидным JSON без текста!
2. Если "завтра" = {tomorrow_str}, "сегодня" = {current_date_str}
3. Время без минут (15) = 15:00
4. Всегда используй 2025+ годы
5. Для поиска события достаточно ключевых слов

[ПРИМЕРЫ]:
Ввод: "Создай встречу завтра в 15:30"
Ответ: {{"command": "create", "event": "Встреча", "datetime": "{tomorrow_str}T15:30"}}

Ввод: "Измени время встречи на 16:00"  
Ответ: {{"command": "change_time", "event": "встреча", "new_datetime": "{current_date_str}T16:00"}}

Ввод: "Перенеси встречу с врачом на завтра 14:00"
Ответ: {{"command": "change_time", "event": "встреча с врачом", "new_datetime": "{tomorrow_str}T14:00"}}

Ввод: "Удали покупки"
Ответ: {{"command": "delete", "event": "покупки"}}

Ввод: "Переименуй встречу в собрание"
Ответ: {{"command": "change_description", "event": "встреча", "new_event": "собрание"}}

Сообщение: {msg}"""
    
    return gigachat_reply(json_prompt)

def parse_time_input(text: str) -> tuple[str, str] | None:
    """Парсит текстовый ввод времени в формате ЧЧ:ММ или даты"""
    text = text.strip().lower()
    
    # Парсинг времени ЧЧ:ММ
    time_match = re.match(r'^(\d{1,2}):(\d{2})$', text)
    if time_match:
        hour, minute = time_match.groups()
        hour, minute = int(hour), int(minute)
        
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return ("time", f"{hour:02d}:{minute:02d}")
    
    # Парсинг только часов
    hour_match = re.match(r'^(\d{1,2})$', text)
    if hour_match:
        hour = int(hour_match.group(1))
        if 0 <= hour <= 23:
            return ("time", f"{hour:02d}:00")
    
    # Парсинг даты
    date_patterns = [
        (r'^сегодня$', datetime.now().strftime('%Y-%m-%d')),
        (r'^завтра$', (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')),
        (r'^послезавтра$', (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d')),
    ]
    
    for pattern, fixed_date in date_patterns:
        if re.match(pattern, text):
            return ("date", fixed_date)
    
    # Парсинг ДД.ММ и ДД.ММ.ГГГГ
    date_match1 = re.match(r'^(\d{1,2})\.(\d{1,2})$', text)
    if date_match1:
        day, month = map(int, date_match1.groups())
        year = datetime.now().year
        try:
            parsed_date = datetime(year, month, day)
            if parsed_date <= datetime.now():
                parsed_date = parsed_date.replace(year=year + 1)
            return ("date", parsed_date.strftime('%Y-%m-%d'))
        except ValueError:
            pass
    
    date_match2 = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', text)
    if date_match2:
        day, month, year = map(int, date_match2.groups())
        try:
            parsed_date = datetime(year, month, day)
            return ("date", parsed_date.strftime('%Y-%m-%d'))
        except ValueError:
            pass
    
    return None

def similarity(a: str, b: str) -> float:
    """Вычисляет схожесть двух строк от 0 до 1"""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def find_similar_events(db: Session, user_id: int, query: str, threshold: float = 0.3) -> List[Event]:
    """Находит похожие события по описанию"""
    events = db.query(Event).filter(
        Event.user_id == user_id,
        Event.scheduled_at > datetime.now()
    ).all()
    
    similar_events = []
    query_words = set(query.lower().split())
    
    for event in events:
        # Вычисляем схожесть по полному тексту
        full_similarity = similarity(query, event.description)
        
        # Проверяем пересечение ключевых слов
        event_words = set(event.description.lower().split())
        word_intersection = len(query_words.intersection(event_words))
        word_similarity = word_intersection / max(len(query_words), len(event_words)) if query_words or event_words else 0
        
        # Финальная схожесть как максимум из двух метрик
        final_similarity = max(full_similarity, word_similarity)
        
        if final_similarity >= threshold:
            similar_events.append(event)
    
    # Сортируем по убыванию схожести
    return sorted(similar_events, key=lambda e: e.scheduled_at)

def validate_and_parse_json(json_str: str) -> Union[Dict[str, Any], None]:
    """Валидация и парсинг JSON строки с улучшенной логикой"""
    try:
        json_str = json_str.strip()
        if json_str.startswith('```json'):
            json_str = json_str[7:]
        if json_str.endswith('```'):
            json_str = json_str[:-3]
        json_str = json_str.strip()
        
        parsed_json = json.loads(json_str)
        
        # Список валидных команд
        valid_commands = [
            'create', 'delete', 'change_time', 'change_date', 
            'change_description', 'change_full', 'list'
        ]
        
        if parsed_json.get('command') not in valid_commands:
            logger.error(f"Invalid command: {parsed_json.get('command')}")
            return None
        
        # Обязательные поля для разных команд
        command = parsed_json.get('command')
        
        if command in ['create']:
            if not parsed_json.get('event'):
                return None
        elif command in ['delete', 'change_time', 'change_date', 'change_description', 'change_full']:
            if not parsed_json.get('event'):
                return None
        
        # Валидация datetime полей
        for field in ['datetime', 'new_datetime']:
            if field in parsed_json and parsed_json[field]:
                try:
                    dt = datetime.fromisoformat(parsed_json[field])
                    
                    # Если время в прошлом, корректируем на будущее
                    if dt <= datetime.now():
                        if dt.year < datetime.now().year:
                            dt = dt.replace(year=datetime.now().year)
                            if dt <= datetime.now():
                                dt = dt.replace(year=datetime.now().year + 1)
                        else:
                            dt = dt + timedelta(days=1)
                        
                        parsed_json[field] = dt.isoformat()
                        logger.info(f"Время скорректировано на будущее: {parsed_json[field]}")
                        
                except ValueError:
                    logger.error(f"Invalid datetime format: {parsed_json[field]}")
                    return None
        
        return parsed_json
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        return None
    except Exception as e:
        logger.error(f"JSON validation error: {e}")
        return None

def create_event_with_notification(
    db: Session,
    user_id: int,
    description: str,
    scheduled_at: datetime,
    notification_minutes: int
) -> Event:
    """Создание события с уведомлением"""
    # Создаем событие
    event = Event(
        user_id=user_id,
        description=description,
        scheduled_at=scheduled_at
    )
    db.add(event)
    db.flush()  # Получаем ID события
    
    # Создаем уведомление если нужно
    if notification_minutes > 0:
        notification = Notification(
            event_id=event.id,
            notification_minutes=notification_minutes
        )
        db.add(notification)
    
    db.commit()
    return event

def schedule_notifications():
    """Планирование всех неотправленных уведомлений"""
    db = get_db()
    try:
        # Получаем все неотправленные уведомления
        notifications = db.execute(text("""
            SELECT 
                n.id as notification_id,
                n.event_id,
                e.description,
                e.scheduled_at,
                n.notification_minutes,
                datetime(e.scheduled_at, '-' || n.notification_minutes || ' minutes') as notify_at,
                e.user_id
            FROM notifications n
            INNER JOIN events e ON n.event_id = e.id
            WHERE n.is_sent = 0
            AND datetime(e.scheduled_at, '-' || n.notification_minutes || ' minutes') > datetime('now')
        """)).fetchall()
        
        for notification in notifications:
            notify_at = datetime.fromisoformat(notification.notify_at)
            
            # Планируем уведомление
            job_id = f"notification_{notification.notification_id}"
            
            # Удаляем существующую задачу если есть
            try:
                scheduler.remove_job(job_id)
            except:
                pass
            
            scheduler.add_job(
                send_notification,
                trigger=DateTrigger(run_date=notify_at),
                args=[notification.user_id, notification.event_id, notification.notification_id],
                id=job_id
            )
            
        logger.info(f"Запланировано {len(notifications)} уведомлений")
    finally:
        db.close()

def create_notification_keyboard() -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора времени уведомления"""
    keyboard = []
    
    keyboard.append([
        InlineKeyboardButton(text="⚡ 5 мин", callback_data="notify_5min"),
        InlineKeyboardButton(text="⏰ 15 мин", callback_data="notify_15min"),
        InlineKeyboardButton(text="⏲️ 30 мин", callback_data="notify_30min")
    ])
    
    keyboard.append([
        InlineKeyboardButton(text="🕐 1 час", callback_data="notify_1hour"),
        InlineKeyboardButton(text="🕑 2 часа", callback_data="notify_2hour")
    ])
    
    keyboard.append([
        InlineKeyboardButton(text="📅 1 день", callback_data="notify_1day"),
        InlineKeyboardButton(text="🔕 Без уведомления", callback_data="notify_no_notification")
    ])
    
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def create_event_selection_keyboard(events: List[Event], action: str) -> InlineKeyboardMarkup:
    """Создание клавиатуры для выбора события"""
    keyboard = []
    
    for event in events[:5]:  # Максимум 5 событий
        time_str = event.scheduled_at.strftime("%d.%m %H:%M")
        display_event = event.description if len(event.description) <= 30 else event.description[:27] + "..."
        
        button_text = f"📝 {display_event}\n📅 {time_str}"
        callback_data = f"{action}_{event.id}"
        
        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def send_notification(user_id: int, event_id: int, notification_id: int) -> None:
    """Отправка уведомления пользователю"""
    db = get_db()
    try:
        # Получаем информацию о событии и уведомлении
        result = db.execute(text("""
            SELECT 
                e.description,
                e.scheduled_at,
                n.notification_minutes
            FROM events e
            INNER JOIN notifications n ON e.id = n.event_id
            WHERE e.id = :event_id AND n.id = :notification_id
        """), {"event_id": event_id, "notification_id": notification_id}).fetchone()
        
        if result:
            description, scheduled_at, notification_minutes = result
            scheduled_dt = datetime.fromisoformat(scheduled_at)
            
            # Форматируем сообщение
            time_str = scheduled_dt.strftime("%d.%m.%Y %H:%M")
            
            if notification_minutes >= 1440:
                days = notification_minutes // 1440
                time_left = f"через {days} дн."
            elif notification_minutes >= 60:
                hours = notification_minutes // 60
                time_left = f"через {hours} ч."
            else:
                time_left = f"через {notification_minutes} мин."
            
            message_text = (
                f"🔔 **НАПОМИНАНИЕ!**\n\n"
                f"📝 **{description}**\n"
                f"⏰ {time_str} ({time_left})\n\n"
                f"📌 Не забудьте про это событие!"
            )
            
            await bot.send_message(chat_id=user_id, text=message_text, parse_mode="Markdown")
            
            # Отмечаем уведомление как отправленное
            db.execute(text(
                "UPDATE notifications SET is_sent = 1 WHERE id = :notification_id"
            ), {"notification_id": notification_id})
            db.commit()
            
            logger.info(f"Уведомление отправлено пользователю {user_id} для события {event_id}")
        
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления: {e}")
    finally:
        db.close()

# Callback handlers
@dp.callback_query(F.data.startswith("notify_"))
async def handle_notification_selection(callback: CallbackQuery) -> None:
    """Обработка выбора времени уведомления"""
    assert callback.from_user is not None
    assert callback.message is not None
    
    user_id = callback.from_user.id
    notification_type = callback.data.split("_")[1]
    
    if user_id in pending_tasks:
        task_data = pending_tasks[user_id]
        notification_info = NOTIFICATION_OPTIONS.get(notification_type)
        
        if notification_info and "event" in task_data and "datetime" in task_data:
            await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            
            scheduled_at = datetime.fromisoformat(task_data["datetime"])
            
            db = get_db()
            try:
                event = create_event_with_notification(
                    db=db,
                    user_id=user_id,
                    description=task_data["event"],
                    scheduled_at=scheduled_at,
                    notification_minutes=notification_info["minutes"]
                )
                
                # Планируем уведомление
                schedule_notifications()
                
                time_str = scheduled_at.strftime("%d.%m.%Y %H:%M")
                
                success_text = (
                    f"✅ **Событие создано!**\n\n"
                    f"📝 **Событие:** {task_data['event']}\n"
                    f"⏰ **Время:** {time_str}\n"
                    f"🔔 **Уведомление:** {notification_info['text']}\n\n"
                    f"📱 Уведомление придет автоматически"
                )
                
                await callback.message.edit_text(success_text, parse_mode="Markdown")
                del pending_tasks[user_id]
                
            except Exception as e:
                logger.error(f"Ошибка при создании события: {e}")
                await callback.message.edit_text("❌ **Ошибка при создании события**", parse_mode="Markdown")
            finally:
                db.close()
        else:
            await callback.message.edit_text("❌ Не хватает данных для создания события")
    
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def handle_delete_event(callback: CallbackQuery) -> None:
    """Обработка удаления события"""
    assert callback.from_user is not None
    assert callback.message is not None
    
    user_id = callback.from_user.id
    event_id = int(callback.data.split("_")[1])
    
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
    
    db = get_db()
    try:
        event = db.query(Event).filter(Event.id == event_id, Event.user_id == user_id).first()
        
        if event:
            # Удаляем связанные задачи из планировщика
            notifications = db.query(Notification).filter(Notification.event_id == event_id).all()
            for notification in notifications:
                job_id = f"notification_{notification.id}"
                try:
                    scheduler.remove_job(job_id)
                except:
                    pass
            
            # Удаляем событие (cascade удалит уведомления)
            db.delete(event)
            db.commit()
            
            time_str = event.scheduled_at.strftime("%d.%m.%Y %H:%M")
            success_text = (
                f"🗑️ **Событие удалено!**\n\n"
                f"📝 **Событие:** {event.description}\n"
                f"⏰ **Было запланировано:** {time_str}\n\n"
                f"✅ Все связанные уведомления отменены"
            )
            
            await callback.message.edit_text(success_text, parse_mode="Markdown")
        else:
            await callback.message.edit_text("❌ Событие не найдено")
    
    except Exception as e:
        logger.error(f"Ошибка при удалении события: {e}")
        await callback.message.edit_text("❌ **Ошибка при удалении события**", parse_mode="Markdown")
    finally:
        db.close()
    
    await callback.answer()

@dp.callback_query(F.data == "cancel")
async def handle_cancel(callback: CallbackQuery) -> None:
    """Обработка отмены"""
    assert callback.from_user is not None
    
    user_id = callback.from_user.id
    if user_id in pending_tasks:
        del pending_tasks[user_id]
    
    await callback.message.edit_text("❌ **Операция отменена**", parse_mode="Markdown")
    await callback.answer()

# Command handlers
@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    """Обработчик команды /start"""
    start_text = (
        "🎉 **Добро пожаловать в Calendar AI Bot!**\n\n"
        "🤖 Я ваш умный помощник календаря с ИИ\n\n"
        "✨ **Возможности:**\n"
        "🧠 Понимаю естественный язык\n"
        "💾 Сохраняю в базу данных\n"
        "🔔 Отправляю уведомления\n"
        "⚡ Быстрые команды\n\n"
        "📋 **Команды:** /mytasks, /help\n\n"
        "🚀 **Примеры:**\n"
        "• *Создай встречу завтра в 15:30*\n"
        "• *Удали встречу с врачом*\n"
        "• *Измени время встречи на 16:00*\n"
        "• *Перенеси покупки на завтра 14:00*\n\n"
        "💡 **Просто напишите что нужно!**"
    )
    
    await message.answer(start_text, parse_mode="Markdown")

@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    """Обработчик команды /help"""
    help_text = (
        "🤖 **Руководство по использованию**\n\n"
        "📝 **СОЗДАНИЕ СОБЫТИЙ:**\n"
        "• *Создай встречу завтра в 15:30*\n"
        "• *Напомни позвонить маме в 18:00*\n"
        "• *Поставь задачу купить продукты на завтра*\n\n"
        
        "🗑️ **УДАЛЕНИЕ СОБЫТИЙ:**\n"
        "• *Удали встречу с врачом*\n"
        "• *Убери напоминание про покупки*\n"
        "• *Отмени звонок*\n\n"
        
        "📝 **ИЗМЕНЕНИЕ СОБЫТИЙ:**\n"
        "• *Измени время встречи на 16:00*\n"
        "• *Перенеси встречу на завтра 14:00*\n"
        "• *Переименуй встречу в собрание*\n\n"
        
        "⌨️ **Ручной ввод:**\n"
        "🕐 Время: `15:30, 09, 20:45`\n"
        "📅 Даты: `сегодня, завтра, 25.12`\n\n"
        
        "📋 **Команды:** /mytasks, /help"
    )
    
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("mytasks"))
async def show_tasks_handler(message: Message) -> None:
    """Показать активные события пользователя"""
    assert message.from_user is not None
    user_id = message.from_user.id
    
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    
    db = get_db()
    try:
        events = db.query(Event).filter(
            Event.user_id == user_id,
            Event.scheduled_at > datetime.now()
        ).order_by(Event.scheduled_at).limit(10).all()
        
        if not events:
            await message.answer(
                "📋 **Ваши события**\n\n"
                "📭 *У вас нет активных событий*\n\n"
                "💡 Создайте первое событие:\n"
                "*Например: \"Встреча завтра в 15:30\"*"
            , parse_mode="Markdown")
            return
        
        tasks_text = f"📋 **Ваши события ({len(events)}):**\n\n"
        
        for event in events:
            time_str = event.scheduled_at.strftime("%d.%m.%Y в %H:%M")
            
            # Эмодзи для типа события
            desc_lower = event.description.lower()
            if any(word in desc_lower for word in ['встреча', 'собрание']):
                emoji = "🤝"
            elif any(word in desc_lower for word in ['врач', 'доктор']):
                emoji = "👨‍⚕️"  
            elif any(word in desc_lower for word in ['покупки', 'магазин']):
                emoji = "🛒"
            elif any(word in desc_lower for word in ['звонок', 'позвонить']):
                emoji = "📞"
            else:
                emoji = "📝"
            
            tasks_text += f"{emoji} **{event.description}**\n📅 {time_str}\n\n"
        
        await message.answer(tasks_text, parse_mode="Markdown")
        
    finally:
        db.close()

# Главный обработчик текста
@dp.message(F.text)
async def handle_event_text(message: Message) -> None:
    """Главный обработчик текстовых сообщений с ИИ"""
    assert message.text is not None
    assert message.from_user is not None
    
    user_id = message.from_user.id
    
    # Сначала пытаемся парсить вручную простые команды
    parsed_input = parse_time_input(message.text)
    
    if parsed_input and user_id in pending_tasks:
        # Обрабатываем ручной ввод времени/даты для незавершенных задач
        input_type, input_value = parsed_input
        task_data = pending_tasks[user_id]
        
        if input_type == "time" and "need_time" in task_data:
            task_data["time"] = input_value
            if "date" in task_data:
                task_data["datetime"] = f"{task_data['date']}T{input_value}"
                del task_data["need_time"]
                
                await message.answer(
                    f"⏰ **Время принято!**\n\n"
                    f"📝 **Событие:** {task_data['event']}\n"
                    f"📅 **Дата:** {task_data['date']}\n"
                    f"⏰ **Время:** {input_value}\n\n"
                    f"🔔 **Выберите уведомление:**",
                    reply_markup=create_notification_keyboard()
                )
            else:
                await message.answer(
                    f"⏰ **Время {input_value} принято!**\n\n"
                    f"📅 Теперь укажите дату:\n"
                    f"*Примеры: сегодня, завтра, 25.12*"
                )
            return
            
        elif input_type == "date" and "need_date" in task_data:
            task_data["date"] = input_value
            if "time" in task_data:
                task_data["datetime"] = f"{input_value}T{task_data['time']}"
                del task_data["need_date"]
                
                await message.answer(
                    f"📅 **Дата принята!**\n\n"
                    f"📝 **Событие:** {task_data['event']}\n"
                    f"📅 **Дата:** {input_value}\n"
                    f"⏰ **Время:** {task_data['time']}\n\n"
                    f"🔔 **Выберите уведомление:**",
                    reply_markup=create_notification_keyboard()
                )
            else:
                await message.answer(
                    f"📅 **Дата принята!**\n\n"
                    f"⏰ Теперь укажите время:\n"
                    f"*Примеры: 15:30, 09, 20:45*"
                )
            return
    
    # Используем ИИ для парсинга сложных команд
    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    
    try:
        json_response = parse_message_with_ai(message.text)
        logger.info(f"Gigachat JSON response: {json_response}")
        
        parsed_data = validate_and_parse_json(json_response)
        
        if not parsed_data:
            await message.answer(
                "❌ **Не понял команду**\n\n"
                "💡 **Попробуйте:**\n"
                "• *Создай встречу завтра в 15:30*\n"
                "• *Удали встречу с врачом*\n"
                "• *Измени время встречи на 16:00*\n\n"
                "Используйте /help для справки"
            )
            return
        
        command = parsed_data['command']
        event_query = parsed_data.get('event', '')
        
        db = get_db()
        try:
            if command == "create":
                if not parsed_data.get('datetime'):
                    # Нужно уточнить время
                    pending_tasks[user_id] = {
                        "event": event_query,
                        "need_time": True,
                        "need_date": True
                    }
                    await message.answer(
                        f"📝 **Событие:** {event_query}\n\n"
                        f"⏰ Укажите дату и время:\n"
                        f"*Примеры: завтра 15:30, сегодня 14:00*"
                    )
                    return
                
                # Все данные есть - переходим к выбору уведомления
                pending_tasks[user_id] = {
                    "event": event_query,
                    "datetime": parsed_data['datetime']
                }
                
                dt = datetime.fromisoformat(parsed_data['datetime'])
                time_str = dt.strftime("%d.%m.%Y %H:%M")
                
                await message.answer(
                    f"✅ **Событие понято!**\n\n"
                    f"📝 **Событие:** {event_query}\n"
                    f"⏰ **Время:** {time_str}\n\n"
                    f"🔔 **Выберите уведомление:**",
                    reply_markup=create_notification_keyboard()
                )
            
            elif command == "delete":
                # Поиск похожих событий
                similar_events = find_similar_events(db, user_id, event_query)
                
                if not similar_events:
                    await message.answer(
                        f"🔍 **Поиск событий**\n\n"
                        f"❌ Событие не найдено: *{event_query}*\n\n"
                        f"💡 Используйте /mytasks для просмотра событий"
                    )
                elif len(similar_events) == 1:
                    # Удаляем сразу
                    event_obj = similar_events[0]
                    
                    # Удаляем уведомления из планировщика
                    notifications = db.query(Notification).filter(Notification.event_id == event_obj.id).all()
                    for notification in notifications:
                        try:
                            scheduler.remove_job(f"notification_{notification.id}")
                        except:
                            pass
                    
                    db.delete(event_obj)
                    db.commit()
                    
                    time_str = event_obj.scheduled_at.strftime("%d.%m.%Y %H:%M")
                    await message.answer(
                        f"🗑️ **Событие удалено!**\n\n"
                        f"📝 **Событие:** {event_obj.description}\n"
                        f"⏰ **Было запланировано:** {time_str}\n\n"
                        f"✅ Уведомления отменены"
                    )
                else:
                    # Показываем выбор
                    await message.answer(
                        f"🔍 **Найдено {len(similar_events)} событий**\n\n"
                        f"👇 **Выберите какое удалить:**",
                        reply_markup=create_event_selection_keyboard(similar_events, "delete")
                    )
            
            elif command in ["change_time", "change_date", "change_full"]:
                # Поиск события для изменения
                similar_events = find_similar_events(db, user_id, event_query)
                
                if not similar_events:
                    await message.answer(
                        f"🔍 **Поиск событий**\n\n"
                        f"❌ Событие не найдено: *{event_query}*\n\n"
                        f"💡 Используйте /mytasks для просмотра событий"
                    )
                elif len(similar_events) == 1 and parsed_data.get('new_datetime'):
                    # Изменяем время сразу
                    event_obj = similar_events[0]
                    new_dt = datetime.fromisoformat(parsed_data['new_datetime'])
                    
                    # Удаляем старые уведомления
                    notifications = db.query(Notification).filter(Notification.event_id == event_obj.id).all()
                    for notification in notifications:
                        try:
                            scheduler.remove_job(f"notification_{notification.id}")
                        except:
                            pass
                    
                    event_obj.scheduled_at = new_dt
                    db.commit()
                    
                    # Перепланируем уведомления
                    schedule_notifications()
                    
                    time_str = new_dt.strftime("%d.%m.%Y %H:%M")
                    await message.answer(
                        f"✅ **Событие изменено!**\n\n"
                        f"📝 **Событие:** {event_obj.description}\n"
                        f"⏰ **Новое время:** {time_str}\n\n"
                        f"🔄 Уведомления обновлены"
                    )
                else:
                    await message.answer(
                        f"🔧 **Функция в разработке**\n\n"
                        f"💡 Пока используйте простые команды:\n"
                        f"*Измени встречу на завтра 15:30*"
                    )
            
            elif command == "change_description":
                similar_events = find_similar_events(db, user_id, event_query)
                
                if not similar_events:
                    await message.answer(f"❌ Событие не найдено: *{event_query}*")
                elif len(similar_events) == 1 and parsed_data.get('new_event'):
                    event_obj = similar_events[0]
                    old_desc = event_obj.description
                    event_obj.description = parsed_data['new_event']
                    db.commit()
                    
                    await message.answer(
                        f"✅ **Описание изменено!**\n\n"
                        f"📝 **Было:** {old_desc}\n"
                        f"📝 **Стало:** {event_obj.description}\n"
                        f"⏰ **Время:** {event_obj.scheduled_at.strftime('%d.%m.%Y %H:%M')}"
                    )
                else:
                    await message.answer("🔧 **Функция в разработке**")
            
            elif command == "list":
                # Показываем события (как /mytasks)
                await show_tasks_handler(message)
                return
                
        finally:
            db.close()
        
        logger.info(f"Successfully processed command: {command}")
        
    except Exception as e:
        logger.error(f"Error in handle_event_text: {e}")
        await message.answer(
            "❌ **Произошла ошибка**\n\n"
            "💡 Попробуйте еще раз или используйте /help"
        )

async def main() -> None:
    """Главная функция запуска бота"""
    init_db()
    logger.info("🗄️ База данных инициализирована")
    
    # Тестируем Gigachat
    token = get_gigachat_token()
    if token:
        logger.info("✅ Соединение с Gigachat установлено")
    else:
        logger.warning("❌ Не удалось подключиться к Gigachat")
    
    scheduler.start()
    logger.info("⏰ Планировщик запущен")
    
    schedule_notifications()
    
    try:
        logger.info("🚀 Calendar AI Bot запущен")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("⏹️ Остановка бота")
    finally:
        scheduler.shutdown()
        logger.info("🔚 Планировщик остановлен")

if __name__ == "__main__":
    asyncio.run(main())