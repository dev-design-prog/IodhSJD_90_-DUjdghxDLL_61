"""
Бот-рассылка для Telegram - версия для хостинга Render.

Отличия от обычной версии:
  * секреты берутся из переменных окружения (Environment на Render);
  * сессия аккаунта хранится строкой SESSION_STRING (файл на Render пропадает при перезапуске);
  * настройки рассылки сохраняются в «Избранное» вашего аккаунта (файл mailbot_config.json),
    поэтому переживают перезапуски и деплои;
  * внутри запущен мини-веб-сервер, чтобы Render видел порт и чтобы можно было пинговать сервис;
  * после перезапуска сервиса незавершённая рассылка продолжается сама.

Переменные окружения:
    BOT_TOKEN       токен от @BotFather
    API_ID          с my.telegram.org
    API_HASH        с my.telegram.org
    OWNER_ID        ваш числовой Telegram ID (бот отвечает только ему)
    SESSION_STRING  появится после первого входа (бот пришлёт его в лс)
"""

import asyncio
import html
import io
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# ============================== НАСТРОЙКИ ==============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9045800147:AAGF62Q0ODbmaemIyQXUBDBD2swemnnM-Rs").strip()
API_ID = int(os.environ.get("API_ID", "20283675") or 0)
API_HASH = os.environ.get("API_HASH", "62273acc88d8400298b93b1b95bed2d4").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "806669429") or 0)
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

CONFIG_FILE = Path("data.json")     # локальная копия (на Render пропадает при перезапуске)
CONFIG_TAG = "MAILBOT_CONFIG"       # метка сообщения с настройками в «Избранном»
MIN_INTERVAL = 10                   # минимальный интервал между раундами, сек
CHATS_PER_PAGE = 8

# Текст, который уйдёт в @SpamBot, если он попросит объяснение
APPEAL_TEXT = (
    "Hello! I use Telegram to post announcements in the groups I'm a member of. "
    "I believe the limit was applied by mistake and I will follow the rules. Thank you."
)

# Признаки того, что ограничений нет
SPAMBOT_FREE_KEYS = [
    "no limits are currently applied", "good news", "free as a bird",
    "нет никаких ограничений", "ограничений нет", "сейчас нет ограничений",
]
# Приоритет кнопок в @SpamBot (по подстроке, без регистра).
# Если у вас кнопки называются иначе - допишите сюда; тексты кнопок бот присылает в лог.
SPAMBOT_BUTTON_PRIORITY = [
    "это ошибка", "ошибк", "this is a mistake", "mistake", "error",
    "признаю", "i admit", "i understand", "i agree", "понимаю", "согласен",
    "освобод", "release", "снять", "submit", "отправить",
    "подтвер", "confirm", "да", "yes", "ок", "ok",
]
# ======================================================================

DEFAULT_CFG = {
    "chats": {},          # {id: название}, не сохраняется в Избранное (сканируется при старте)
    "selected": [],       # [id, ...]
    "text": "",           # HTML-текст для отправки
    "text_plain": "",     # обычный текст для превью
    "interval": 3600,     # секунд между раундами
    "count": 1,           # сколько раундов (0 = бесконечно)
    "chat_pause": 5,      # пауза между чатами внутри раунда, сек
    "running": False,     # идёт ли рассылка (нужно для автопродолжения)
    "done": 0,            # сколько раундов уже выполнено
}
PERSIST_KEYS = ["selected", "text", "text_plain", "interval", "count", "chat_pause", "running", "done"]


def load_cfg() -> dict:
    c = dict(DEFAULT_CFG)
    if CONFIG_FILE.exists():
        with suppress(Exception):
            c.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return c


cfg = load_cfg()
tg: TelegramClient | None = None
router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)

_save_handle = None
_save_lock = asyncio.Lock()


def save_cfg() -> None:
    """Сразу пишет локальный файл, а через 3 сек (без новых правок) - копию в «Избранное»."""
    global _save_handle
    with suppress(Exception):
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    loop = asyncio.get_running_loop()
    if _save_handle:
        _save_handle.cancel()
    _save_handle = loop.call_later(3, lambda: asyncio.create_task(remote_save()))


async def remote_save() -> None:
    try:
        if tg is None or not tg.is_connected() or not await tg.is_user_authorized():
            return
        async with _save_lock:
            data = {k: cfg[k] for k in PERSIST_KEYS}
            bio = io.BytesIO(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            bio.name = "mailbot_config.json"
            old = [m.id async for m in tg.iter_messages("me", search=CONFIG_TAG, limit=20) if m.document]
            await tg.send_file("me", bio, caption=CONFIG_TAG, silent=True)
            if old:
                await tg.delete_messages("me", old)
    except Exception as e:  # noqa: BLE001
        print("Не удалось сохранить настройки в Избранное:", e)


async def remote_load() -> bool:
    try:
        async for m in tg.iter_messages("me", search=CONFIG_TAG, limit=20):
            if m.document:
                raw = await m.download_media(bytes)
                data = json.loads(raw.decode("utf-8"))
                for k in PERSIST_KEYS:
                    if k in data:
                        cfg[k] = data[k]
                return True
    except Exception as e:  # noqa: BLE001
        print("Не удалось загрузить настройки из Избранного:", e)
    return False


class Form(StatesGroup):
    phone = State()
    code = State()
    password = State()
    text = State()
    interval = State()
    count = State()
    pause = State()


# ============================== УТИЛИТЫ ==============================
def parse_duration(s: str):
    """'1h 30m 15s', '1ч 30м', '90', '01:30:15', '05:00' (мм:сс) -> секунды или None"""
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", s)
    if m:
        a, b, c = m.groups()
        if c is None:
            return int(a) * 60 + int(b)
        return int(a) * 3600 + int(b) * 60 + int(c)
    if s.isdigit():
        return int(s)
    if not re.fullmatch(r"(?:\s*\d+\s*[a-zа-я]+)+\s*", s):
        return None
    total = 0
    for num, unit in re.findall(r"(\d+)\s*([a-zа-я]+)", s):
        if unit.startswith(("h", "ч")):
            total += int(num) * 3600
        elif unit.startswith(("m", "м")):
            total += int(num) * 60
        elif unit.startswith(("s", "с")):
            total += int(num)
        else:
            return None
    return total


def fmt_duration(sec: int) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s or not parts:
        parts.append(f"{s}с")
    return " ".join(parts)


async def is_connected() -> bool:
    if not tg.is_connected():
        await tg.connect()
    return await tg.is_user_authorized()


async def scan_chats() -> int:
    chats = {}
    async for d in tg.iter_dialogs():
        if d.is_group:
            chats[str(d.id)] = d.name or str(d.id)
    cfg["chats"] = chats
    cfg["selected"] = [c for c in cfg["selected"] if c in chats]
    save_cfg()
    return len(chats)


async def notify(bot: Bot, text: str) -> None:
    with suppress(Exception):
        await bot.send_message(OWNER_ID, text)


async def show(ev, text: str, kb=None) -> None:
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(text, reply_markup=kb)
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=kb)


# ============================== СПАМ-БЛОК ==============================
async def spam_unblock():
    """Заходит в @SpamBot и жмёт кнопки апелляции. Возвращает (ok, лог)."""
    log = []
    clicked = set()
    try:
        await tg.send_message("SpamBot", "/start")
    except Exception as e:  # noqa: BLE001
        return False, [f"Не удалось написать @SpamBot: {e}"]

    for _ in range(10):
        await asyncio.sleep(3)
        msgs = await tg.get_messages("SpamBot", limit=1)
        if not msgs:
            continue
        m = msgs[0]
        text = (m.message or "").lower()
        log.append("SpamBot: " + (m.message or "")[:200].replace("\n", " "))

        if any(k in text for k in SPAMBOT_FREE_KEYS):
            return True, log

        buttons = [b for row in (m.buttons or []) for b in row]
        if buttons:
            log.append("Кнопки: " + " | ".join(b.text for b in buttons))

        choice = None
        for kw in SPAMBOT_BUTTON_PRIORITY:
            for b in buttons:
                t = b.text.lower()
                hit = (kw in t) if len(kw) > 3 else re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", t)
                if hit and (m.id, b.text) not in clicked:
                    choice = b
                    break
            if choice:
                break

        if choice:
            clicked.add((m.id, choice.text))
            log.append(f"Нажимаю: {choice.text}")
            try:
                await choice.click()
            except Exception as e:  # noqa: BLE001
                log.append(f"Ошибка нажатия: {e}")
            continue

        if re.search(r"напиш|опиш|объясн|write|explain|describe|tell us", text) and not buttons:
            if ("text", m.id) not in clicked:
                clicked.add(("text", m.id))
                log.append("Отправляю текст апелляции")
                await tg.send_message("SpamBot", APPEAL_TEXT)
                continue
        break

    await tg.send_message("SpamBot", "/start")
    await asyncio.sleep(3)
    msgs = await tg.get_messages("SpamBot", limit=1)
    if msgs:
        t = (msgs[0].message or "").lower()
        log.append("Итог: " + (msgs[0].message or "")[:200].replace("\n", " "))
        if any(k in t for k in SPAMBOT_FREE_KEYS):
            return True, log
    return False, log


# ============================== РАССЫЛКА ==============================
class Mailer:
    task: asyncio.Task | None = None
    stats: dict = {}

    @classmethod
    def running(cls) -> bool:
        return cls.task is not None and not cls.task.done()

    @classmethod
    def start(cls, bot: Bot, resume: bool = False) -> None:
        done = cfg.get("done", 0) if resume else 0
        cls.stats = {"round": done, "sent": 0, "failed": 0, "next_at": None, "started": time.time()}
        cfg["running"] = True
        cfg["done"] = done
        save_cfg()
        cls.task = asyncio.create_task(cls._run(bot))

    @classmethod
    async def stop(cls, manual: bool = True) -> None:
        """manual=False - просто остановить процесс (выключение сервиса), не сбрасывая автопродолжение."""
        if cls.running():
            cls.task.cancel()
            with suppress(asyncio.CancelledError):
                await cls.task
        if manual:
            cfg["running"] = False
            cfg["done"] = 0
            save_cfg()

    @classmethod
    async def _send(cls, bot: Bot, cid: str, retry: bool = True):
        """None = успех, иначе строка с причиной ошибки."""
        try:
            await tg.send_message(int(cid), cfg["text"], parse_mode="html", link_preview=False)
            return None
        except errors.FloodWaitError as e:
            if e.seconds > 600 or not retry:
                return f"FloodWait {e.seconds}с - пропуск"
            await asyncio.sleep(e.seconds + 1)
            return await cls._send(bot, cid, False)
        except errors.SlowModeWaitError as e:
            return f"медленный режим ({e.seconds}с)"
        except errors.PeerFloodError:
            if not retry:
                raise
            await notify(bot, "⚠️ PeerFlood (спам-блок). Пробую снять через @SpamBot…")
            ok, log = await spam_unblock()
            await notify(bot, ("✅ Ограничений нет." if ok else "❌ Снять не удалось.")
                         + "\n\n" + html.escape("\n".join(log)))
            await asyncio.sleep(30)
            return await cls._send(bot, cid, False)
        except errors.RPCError as e:
            return type(e).__name__
        except Exception as e:  # noqa: BLE001
            return str(e)[:100]

    @classmethod
    async def _run(cls, bot: Bot) -> None:
        s = cls.stats
        try:
            while True:
                s["round"] += 1
                t0 = time.time()
                errs = []
                targets = list(cfg["selected"])
                for i, cid in enumerate(targets):
                    err = await cls._send(bot, cid)
                    if err is None:
                        s["sent"] += 1
                    else:
                        s["failed"] += 1
                        errs.append(f"• {html.escape(cfg['chats'].get(cid, cid))}: {html.escape(err)}")
                    if i < len(targets) - 1:
                        await asyncio.sleep(cfg["chat_pause"])

                cfg["done"] = s["round"]
                save_cfg()

                if errs:
                    await notify(bot, f"Раунд {s['round']}: ошибки в {len(errs)} чатах\n" + "\n".join(errs[:15]))

                if cfg["count"] and s["round"] >= cfg["count"]:
                    break
                wait = max(0, cfg["interval"] - (time.time() - t0))
                s["next_at"] = time.time() + wait
                await asyncio.sleep(wait)

            cfg["running"] = False
            cfg["done"] = 0
            save_cfg()
            await notify(bot, f"✅ Рассылка завершена. Раундов: {s['round']}, отправлено: {s['sent']}, ошибок: {s['failed']}.")
        except errors.PeerFloodError:
            cfg["running"] = False
            save_cfg()
            await notify(bot, "⛔ Спам-блок не снялся, рассылка остановлена.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            cfg["running"] = False
            save_cfg()
            await notify(bot, f"⛔ Рассылка остановлена из-за ошибки: {html.escape(str(e))}")
        finally:
            s["next_at"] = None


# ============================== КЛАВИАТУРЫ / ТЕКСТЫ ==============================
def kb_of(rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


async def main_menu():
    if not await is_connected():
        return "Аккаунт не подключен.", kb_of([[("🔑 Подключить аккаунт", "connect")]])
    me = await tg.get_me()
    name = html.escape(me.first_name or me.username or str(me.id))
    run = Mailer.running()
    text = f"Аккаунт: <b>{name}</b>\nГрупп найдено: {len(cfg['chats'])}\nРассылка: {'🟢 идёт' if run else '⚪️ остановлена'}"
    rows = [
        [("⚙️ Настройка рассылки", "settings")],
        [("⏹ Остановить", "stop") if run else ("▶️ Запустить", "run")],
        [("📊 Статус", "status"), ("🔄 Пересканировать чаты", "rescan")],
        [("🛡 Проверить / снять спам-блок", "unblock")],
        [("🚪 Выйти из аккаунта", "logout")],
    ]
    return text, kb_of(rows)


def settings_view():
    prev = html.escape(cfg["text_plain"][:200]) or "— не задан —"
    text = (
        "<b>Настройка рассылки</b>\n\n"
        f"💬 Чатов выбрано: <b>{len(cfg['selected'])}</b> из {len(cfg['chats'])}\n"
        f"📝 Текст: {prev}\n"
        f"⏱ Интервал между раундами: <b>{fmt_duration(cfg['interval'])}</b>\n"
        f"🔁 Повторов (раундов): <b>{cfg['count'] or '∞'}</b>\n"
        f"⏳ Пауза между чатами: <b>{fmt_duration(cfg['chat_pause'])}</b>"
    )
    kb = kb_of([
        [("💬 Выбор чатов", "ch:p:0")],
        [("📝 Текст", "set:text")],
        [("⏱ Интервал", "set:interval"), ("🔁 Повторы", "set:count")],
        [("⏳ Пауза между чатами", "set:pause")],
        [("◀️ Назад", "menu")],
    ])
    return text, kb


def chats_view(page: int):
    items = sorted(cfg["chats"].items(), key=lambda x: x[1].lower())
    pages = max(1, (len(items) + CHATS_PER_PAGE - 1) // CHATS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    for cid, name in items[page * CHATS_PER_PAGE:(page + 1) * CHATS_PER_PAGE]:
        mark = "✅" if cid in cfg["selected"] else "⬜️"
        b.button(text=f"{mark} {name[:40]}", callback_data=f"ch:t:{cid}:{page}")
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"ch:p:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"ch:p:{page + 1}"))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="✅ Выбрать все", callback_data=f"ch:all:{page}"),
          InlineKeyboardButton(text="⬜️ Снять все", callback_data=f"ch:none:{page}"))
    b.row(InlineKeyboardButton(text="✔️ Готово", callback_data="settings"))
    text = f"Выберите чаты для рассылки (выбрано {len(cfg['selected'])} из {len(items)})."
    if not items:
        text = "Групп не найдено. Нажмите «Пересканировать чаты» в меню."
    return text, b.as_markup()


async def go_menu(ev):
    text, kb = await main_menu()
    await show(ev, text, kb)


# ============================== ХЕНДЛЕРЫ: МЕНЮ ==============================
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await go_menu(m)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await go_menu(cb)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ============================== ХЕНДЛЕРЫ: ВХОД В АККАУНТ ==============================
@router.callback_query(F.data == "connect")
async def cb_connect(cb: CallbackQuery, state: FSMContext):
    if await is_connected():
        return await go_menu(cb)
    await state.set_state(Form.phone)
    await show(cb, "Отправьте номер телефона аккаунта в формате <code>+79991234567</code>")


@router.message(Form.phone)
async def st_phone(m: Message, state: FSMContext):
    phone = re.sub(r"[^\d+]", "", m.text or "")
    try:
        if not tg.is_connected():
            await tg.connect()
        sent = await tg.send_code_request(phone)
    except errors.PhoneNumberInvalidError:
        return await m.answer("Неверный номер, попробуйте ещё раз.")
    except Exception as e:  # noqa: BLE001
        return await m.answer(f"Ошибка: {html.escape(str(e))}")
    await state.update_data(phone=phone, hash=sent.phone_code_hash)
    await state.set_state(Form.code)
    await m.answer(
        "Код отправлен в Telegram. Введите его <b>через тире или пробелы</b>, например <code>1-2-3-4-5</code> "
        "(если отправить код целиком, Telegram может его аннулировать)."
    )


async def finish_login(m: Message, state: FSMContext):
    await state.clear()
    await remote_load()          # вернуть сохранённые настройки, если они есть
    cfg["running"] = False       # после ручного входа рассылку сами не запускаем
    n = await scan_chats()
    await m.answer(f"✅ Аккаунт подключен. Найдено групп: {n}")
    if not SESSION_STRING:
        s = tg.session.save()
        await m.answer(
            "🔐 <b>Важно!</b> Чтобы бот не терял вход после перезапусков Render, скопируйте строку ниже "
            "и добавьте её в Render → ваш сервис → Environment → переменная <code>SESSION_STRING</code>.\n\n"
            f"<code>{s}</code>\n\n"
            "⚠️ Эта строка = полный доступ к вашему аккаунту. Никому не показывайте. "
            "После добавления в Render удалите это сообщение."
        )
    await go_menu(m)


@router.message(Form.code)
async def st_code(m: Message, state: FSMContext):
    data = await state.get_data()
    code = re.sub(r"\D", "", m.text or "")
    with suppress(Exception):
        await m.delete()
    try:
        await tg.sign_in(data["phone"], code, phone_code_hash=data["hash"])
    except errors.SessionPasswordNeededError:
        await state.set_state(Form.password)
        return await m.answer("Включена двухфакторная защита. Введите пароль (сообщение будет удалено).")
    except errors.PhoneCodeInvalidError:
        return await m.answer("Неверный код, введите ещё раз.")
    except errors.PhoneCodeExpiredError:
        await state.clear()
        return await m.answer("Код истёк. Начните заново: /start")
    except Exception as e:  # noqa: BLE001
        return await m.answer(f"Ошибка: {html.escape(str(e))}")
    await finish_login(m, state)


@router.message(Form.password)
async def st_password(m: Message, state: FSMContext):
    pwd = m.text or ""
    with suppress(Exception):
        await m.delete()
    try:
        await tg.sign_in(password=pwd)
    except errors.PasswordHashInvalidError:
        return await m.answer("Неверный пароль, попробуйте ещё раз.")
    except Exception as e:  # noqa: BLE001
        return await m.answer(f"Ошибка: {html.escape(str(e))}")
    await finish_login(m, state)


@router.callback_query(F.data == "logout")
async def cb_logout(cb: CallbackQuery):
    await Mailer.stop()
    await remote_save()
    with suppress(Exception):
        await tg.log_out()
    await tg.connect()
    await cb.message.answer(
        "Вы вышли из аккаунта. Если в Render есть переменная <code>SESSION_STRING</code>, "
        "удалите её, иначе при перезапуске бот будет пытаться использовать старую сессию."
    )
    await go_menu(cb)


# ============================== ХЕНДЛЕРЫ: НАСТРОЙКИ ==============================
@router.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = settings_view()
    await show(cb, text, kb)


@router.callback_query(F.data.startswith("ch:"))
async def cb_chats(cb: CallbackQuery):
    parts = cb.data.split(":")
    action = parts[1]
    page = 0
    if action == "p":
        page = int(parts[2])
    elif action == "t":
        cid, page = parts[2], int(parts[3])
        if cid in cfg["selected"]:
            cfg["selected"].remove(cid)
        else:
            cfg["selected"].append(cid)
        save_cfg()
    elif action == "all":
        page = int(parts[2])
        cfg["selected"] = list(cfg["chats"].keys())
        save_cfg()
    elif action == "none":
        page = int(parts[2])
        cfg["selected"] = []
        save_cfg()
    text, kb = chats_view(page)
    await show(cb, text, kb)


async def ask(cb: CallbackQuery, state: FSMContext, st: State, prompt: str):
    await state.set_state(st)
    await show(cb, prompt, kb_of([[("◀️ Отмена", "settings")]]))


@router.callback_query(F.data == "set:text")
async def cb_set_text(cb: CallbackQuery, state: FSMContext):
    await ask(cb, state, Form.text, "Отправьте текст рассылки. Форматирование (жирный, курсив, ссылки) сохранится.")


@router.callback_query(F.data == "set:interval")
async def cb_set_interval(cb: CallbackQuery, state: FSMContext):
    await ask(cb, state, Form.interval,
              "Как часто повторять рассылку? Примеры: <code>1h 30m 15s</code>, <code>2ч</code>, "
              "<code>45м</code>, <code>90</code> (секунды), <code>01:30:00</code>.")


@router.callback_query(F.data == "set:count")
async def cb_set_count(cb: CallbackQuery, state: FSMContext):
    await ask(cb, state, Form.count, "Сколько раз отправить пост (раундов)? Число. <code>0</code> = без ограничения.")


@router.callback_query(F.data == "set:pause")
async def cb_set_pause(cb: CallbackQuery, state: FSMContext):
    await ask(cb, state, Form.pause, "Пауза между чатами внутри одного раунда (например <code>5</code> или <code>10s</code>).")


async def back_to_settings(m: Message, state: FSMContext):
    await state.clear()
    text, kb = settings_view()
    await m.answer(text, reply_markup=kb)


@router.message(Form.text)
async def st_text(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("Нужно текстовое сообщение.")
    cfg["text"] = m.html_text
    cfg["text_plain"] = m.text
    save_cfg()
    await back_to_settings(m, state)


@router.message(Form.interval)
async def st_interval(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if sec is None or sec < MIN_INTERVAL:
        return await m.answer(f"Не понял или слишком мало (минимум {MIN_INTERVAL} с). Пример: <code>1h 30m 15s</code>")
    cfg["interval"] = sec
    save_cfg()
    await back_to_settings(m, state)


@router.message(Form.count)
async def st_count(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        return await m.answer("Нужно целое число (0 = бесконечно).")
    cfg["count"] = int(m.text.strip())
    save_cfg()
    await back_to_settings(m, state)


@router.message(Form.pause)
async def st_pause(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if sec is None or sec < 1:
        return await m.answer("Не понял. Минимум 1 секунда, пример: <code>5</code>")
    cfg["chat_pause"] = sec
    save_cfg()
    await back_to_settings(m, state)


# ============================== ХЕНДЛЕРЫ: ЗАПУСК И СЕРВИС ==============================
@router.callback_query(F.data == "run")
async def cb_run(cb: CallbackQuery):
    if Mailer.running():
        return await go_menu(cb)
    if not cfg["selected"]:
        return await cb.answer("Сначала выберите чаты", show_alert=True)
    if not cfg["text"]:
        return await cb.answer("Сначала задайте текст", show_alert=True)
    Mailer.start(cb.bot)
    await go_menu(cb)


@router.callback_query(F.data == "stop")
async def cb_stop(cb: CallbackQuery):
    await Mailer.stop()
    await go_menu(cb)


@router.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery):
    s = Mailer.stats or {}
    if not s:
        text = "Рассылка ещё не запускалась."
    else:
        nxt = ""
        if Mailer.running() and s.get("next_at"):
            nxt = f"\nСледующий раунд через: {fmt_duration(max(0, s['next_at'] - time.time()))}"
        total = cfg["count"] or "∞"
        text = (f"Состояние: {'🟢 идёт' if Mailer.running() else '⚪️ остановлена'}\n"
                f"Раунд: {s['round']}/{total}\nОтправлено: {s['sent']}\nОшибок: {s['failed']}{nxt}")
    await show(cb, text, kb_of([[("🔄 Обновить", "status")], [("◀️ Назад", "menu")]]))


@router.callback_query(F.data == "rescan")
async def cb_rescan(cb: CallbackQuery):
    n = await scan_chats()
    await cb.answer(f"Найдено групп: {n}", show_alert=True)
    await go_menu(cb)


@router.callback_query(F.data == "unblock")
async def cb_unblock(cb: CallbackQuery):
    await cb.answer("Проверяю через @SpamBot…")
    ok, log = await spam_unblock()
    text = ("✅ Ограничений нет." if ok else "❌ Ограничения остались или ответ непонятен.") \
        + "\n\n" + html.escape("\n".join(log))
    await show(cb, text[:4000], kb_of([[("◀️ Назад", "menu")]]))


# ============================== ВЕБ-СЕРВЕР И ЗАПУСК ==============================
async def start_web():
    """Мини-сервер: Render требует открытый порт, а пингер (UptimeRobot) держит сервис бодрым."""
    async def index(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def main():
    global tg
    missing = [n for n, v in [("BOT_TOKEN", BOT_TOKEN), ("API_ID", API_ID),
                              ("API_HASH", API_HASH), ("OWNER_ID", OWNER_ID)] if not v]
    if missing:
        raise SystemExit("Не заданы переменные окружения: " + ", ".join(missing))

    await start_web()   # сначала порт, чтобы Render сразу увидел сервис

    tg = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH,
                        device_model="Mailing Bot", app_version="1.0")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await tg.connect()
    if await tg.is_user_authorized():
        await remote_load()
        await scan_chats()
        want = cfg.get("running") and cfg["selected"] and cfg["text"]
        if want and not (cfg["count"] and cfg.get("done", 0) >= cfg["count"]):
            Mailer.start(bot, resume=True)
            await notify(bot, f"♻️ Сервис перезапущен, рассылка продолжена (выполнено раундов: {cfg.get('done', 0)}).")
    try:
        await dp.start_polling(bot)
    finally:
        await Mailer.stop(manual=False)
        await remote_save()
        await tg.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
