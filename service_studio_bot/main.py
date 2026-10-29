import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv


FAQ_SERVICES = "faq_services"
FAQ_PRICE = "faq_price"
FAQ_BOOKING = "faq_booking"
FAQ_PREPARE = "faq_prepare"
CONTACT_OPERATOR = "contact_operator"

DEFAULT_DB_PATH = "data/bot.sqlite3"

logger = logging.getLogger(__name__)


class OperatorRequestState(StatesGroup):
    waiting_for_message = State()


FAQ_ANSWERS = {
    FAQ_SERVICES: (
        "Mira Studio помогает с индивидуальными услугами для клиентов: "
        "консультации, подготовка, сопровождение и подбор решения под задачу. "
        "Напишите оператору, если хотите уточнить услугу под вашу ситуацию."
    ),
    FAQ_PRICE: (
        "Стоимость зависит от услуги и объёма работы. Базовая консультация — от 1500 ₽. "
        "Точную цену специалист подскажет после короткого описания задачи."
    ),
    FAQ_BOOKING: (
        "Чтобы записаться, напишите удобный день, время и услугу. "
        "Оператор свяжется с вами и подтвердит запись. "
        "Бот сам не оформляет запись автоматически."
    ),
    FAQ_PREPARE: (
        "Перед визитом или консультацией подготовьте краткое описание задачи, "
        "важные детали и, если есть, фото или документы. "
        "Так специалист быстрее подберёт решение."
    ),
    "contacts": (
        "Mira Studio работает по предварительной записи. "
        "Точный адрес и контакты специалист отправит после подтверждения обращения."
    ),
}

KEYWORD_TO_ANSWER = {
    ("цена", "стоимость", "сколько", "прайс"): FAQ_PRICE,
    ("запись", "записаться", "записать", "приём", "прием"): FAQ_BOOKING,
    ("адрес", "где", "контакты", "находитесь"): "contacts",
    ("подготовка", "подготовиться", "что взять"): FAQ_PREPARE,
    ("услуги", "делаете", "помогаете"): FAQ_SERVICES,
}

WELCOME_TEXT = (
    "Здравствуйте! Это FAQ-бот Mira Studio.\n\n"
    "Я помогу быстро ответить на частые вопросы об услугах, стоимости, записи и подготовке. "
    "Если нужен индивидуальный ответ, нажмите «Связаться с оператором»."
)

FALLBACK_TEXT = (
    "Я пока не нашёл точный ответ. Выберите раздел в FAQ или нажмите "
    "«Связаться с оператором», чтобы оставить сообщение специалисту."
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def get_required_bot_token() -> str:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token or bot_token == "put_your_bot_token_here":
        raise RuntimeError("BOT_TOKEN is not configured. Set it in local .env file.")
    return bot_token


def get_owner_chat_id() -> int | None:
    raw_owner_chat_id = os.getenv("OWNER_CHAT_ID", "").strip()
    if not raw_owner_chat_id:
        logger.warning("OWNER_CHAT_ID is not configured. Owner notifications will be skipped.")
        return None

    try:
        return int(raw_owner_chat_id)
    except ValueError:
        logger.warning("OWNER_CHAT_ID is invalid. Owner notifications will be skipped.")
        return None


def get_db_path() -> Path:
    return Path(os.getenv("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH)


def init_database(db_path: Path) -> None:
    if db_path.parent != Path(""):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                username TEXT,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    logger.info("SQLite database is ready at %s", db_path)


def save_operator_request(
    db_path: Path,
    telegram_user_id: int,
    username: str | None,
    message_text: str,
    created_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO operator_requests (
                telegram_user_id,
                username,
                message_text,
                created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (telegram_user_id, username, message_text, created_at),
        )
        connection.commit()

    logger.info("Operator request saved for telegram_user_id=%s", telegram_user_id)


def build_faq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Услуги", callback_data=FAQ_SERVICES),
                InlineKeyboardButton(text="Стоимость", callback_data=FAQ_PRICE),
            ],
            [
                InlineKeyboardButton(text="Как записаться", callback_data=FAQ_BOOKING),
                InlineKeyboardButton(text="Подготовка", callback_data=FAQ_PREPARE),
            ],
            [InlineKeyboardButton(text="Связаться с оператором", callback_data=CONTACT_OPERATOR)],
        ]
    )


def find_faq_answer(text: str) -> str | None:
    normalized_text = text.casefold()
    for keywords, answer_key in KEYWORD_TO_ANSWER.items():
        if any(keyword in normalized_text for keyword in keywords):
            return FAQ_ANSWERS[answer_key]
    return None


def format_owner_notification(
    telegram_user_id: int,
    username: str | None,
    message_text: str,
    created_at: str,
) -> str:
    username_text = f"@{username}" if username else "не указан"
    return (
        "Новое обращение в Mira Studio FAQ Bot\n\n"
        f"Telegram user id: {telegram_user_id}\n"
        f"Username: {username_text}\n"
        f"Created at: {created_at}\n\n"
        f"Сообщение:\n{message_text}"
    )


async def notify_owner(
    bot: Bot,
    owner_chat_id: int | None,
    telegram_user_id: int,
    username: str | None,
    message_text: str,
    created_at: str,
) -> None:
    if owner_chat_id is None:
        logger.warning("Owner notification skipped: OWNER_CHAT_ID is not configured or invalid.")
        return

    try:
        await bot.send_message(
            chat_id=owner_chat_id,
            text=format_owner_notification(
                telegram_user_id=telegram_user_id,
                username=username,
                message_text=message_text,
                created_at=created_at,
            ),
        )
    except TelegramAPIError as error:
        logger.warning("Failed to send owner notification: %s", error)


def register_handlers(dispatcher: Dispatcher, db_path: Path, owner_chat_id: int | None) -> None:
    @dispatcher.message(CommandStart())
    async def handle_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(WELCOME_TEXT, reply_markup=build_faq_keyboard())

    @dispatcher.callback_query(F.data.in_({FAQ_SERVICES, FAQ_PRICE, FAQ_BOOKING, FAQ_PREPARE}))
    async def handle_faq_callback(callback: CallbackQuery) -> None:
        if callback.message is not None and callback.data is not None:
            await callback.message.answer(FAQ_ANSWERS[callback.data], reply_markup=build_faq_keyboard())
        await callback.answer()

    @dispatcher.callback_query(F.data == CONTACT_OPERATOR)
    async def handle_contact_operator(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(OperatorRequestState.waiting_for_message)
        if callback.message is not None:
            await callback.message.answer(
                "Напишите короткое сообщение для специалиста Mira Studio. "
                "Следующее текстовое сообщение будет сохранено как обращение."
            )
        await callback.answer()

    @dispatcher.message(OperatorRequestState.waiting_for_message, F.text)
    async def handle_operator_request(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.text is None:
            await message.answer("Пожалуйста, отправьте текстовое сообщение для специалиста.")
            return

        request_text = message.text.strip()
        if not request_text:
            await message.answer("Пожалуйста, отправьте непустое текстовое сообщение для специалиста.")
            return

        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_operator_request(
            db_path=db_path,
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            message_text=request_text,
            created_at=created_at,
        )
        await state.clear()
        await message.answer(
            "Спасибо! Ваше обращение принято. Специалист Mira Studio свяжется с вами.",
            reply_markup=build_faq_keyboard(),
        )
        await notify_owner(
            bot=bot,
            owner_chat_id=owner_chat_id,
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            message_text=request_text,
            created_at=created_at,
        )

    @dispatcher.message(OperatorRequestState.waiting_for_message)
    async def handle_non_text_operator_request(message: Message) -> None:
        await message.answer("Пожалуйста, отправьте обращение обычным текстовым сообщением.")

    @dispatcher.message(F.text)
    async def handle_text_question(message: Message) -> None:
        if message.text is None:
            return

        faq_answer = find_faq_answer(message.text)
        if faq_answer is not None:
            await message.answer(faq_answer, reply_markup=build_faq_keyboard())
            return

        await message.answer(FALLBACK_TEXT, reply_markup=build_faq_keyboard())


async def main() -> None:
    setup_logging()
    load_dotenv()

    try:
        bot_token = get_required_bot_token()
    except RuntimeError as error:
        logger.error("%s", error)
        raise SystemExit(1) from error

    owner_chat_id = get_owner_chat_id()
    db_path = get_db_path()
    init_database(db_path)

    bot = Bot(token=bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    register_handlers(dispatcher, db_path=db_path, owner_chat_id=owner_chat_id)

    logger.info("Mira Studio FAQ Bot is starting with long polling.")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())