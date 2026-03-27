import asyncio
import logging
import os
import re
import shutil
import zipfile
from uuid import uuid4
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)

# ------------------------- Конфигурация -------------------------
TOKEN = os.environ.get("TOKEN", "8561123984:AAEaAWZoM1BGofGlzH2nh2eMJuKa16JTr4E")  # ⚠️ Заменить на реальный токен
MAX_PACK_SIZE_MB = 50
PROGRESS_BAR_LENGTH = 20
PROGRESS_BAR_FILL = "█"
PROGRESS_BAR_EMPTY = "-"

# ------------------------- Логирование -------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ------------------------- Глобальные данные -------------------------
current_tasks = {}  # user_id -> {"task": asyncio.Task, "data": ...}
stats = {"stickers": 0, "packs": 0}

# ------------------------- Утилиты -------------------------
async def async_cleanup_files(*paths):
    """Удаляет файлы и папки асинхронно, игнорируя None и несуществующие пути."""
    def _cleanup():
        for path in paths:
            if path is None:
                continue
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    logger.debug(f"Удалён файл: {path}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить файл {path}: {e}")
            elif os.path.isdir(path):
                try:
                    shutil.rmtree(path, ignore_errors=True)
                    logger.debug(f"Удалена папка: {path}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить папку {path}: {e}")
    await asyncio.to_thread(_cleanup)

async def get_folder_size_mb(folder):
    """Возвращает размер папки в мегабайтах асинхронно."""
    if not os.path.exists(folder):
        return 0.0
    
    def _size():
        total = 0
        for root, dirs, files in os.walk(folder):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
        return total / (1024 * 1024)
    return await asyncio.to_thread(_size)

def progress_bar(done, total, length=PROGRESS_BAR_LENGTH):
    """Возвращает прогресс-бар с процентами и счётчиком."""
    filled = int(done / total * length)
    perc = int(done / total * 100)
    bar = PROGRESS_BAR_FILL * filled + PROGRESS_BAR_EMPTY * (length - filled)
    return f"[{bar}] {perc}% ({done}/{total})"

def format_size_mb(size_mb: float) -> str:
    """Красиво форматирует размер в мегабайтах."""
    if size_mb < 0.1:
        return f"{size_mb * 1024:.0f} KB"
    elif size_mb < 1:
        return f"{size_mb * 1024:.1f} KB"
    elif size_mb < 1024:
        return f"{size_mb:.2f} MB"
    else:
        return f"{size_mb / 1024:.2f} GB"

def safe_filename(name: str) -> str:
    """Создаёт безопасное имя файла для ZIP."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)

async def safe_delete_message(message):
    """Безопасно удаляет сообщение, игнорируя ошибки."""
    if message:
        try:
            await message.delete()
        except Exception:
            pass

# ------------------------- Команды -------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Я помогу тебе скачивать стикеры из Telegram.\n\n"
        "📌 Отправь мне:\n"
        "• Любой стикер — для скачивания одного стикера\n"
        "• Ссылку вида t.me/addstickers/название — для скачивания целого пака\n\n"
        "После этого выбери формат: PNG, JPG или ZIP.\n\n"
        "🔧 Команды:\n"
        "/stats — статистика\n"
        "/cancel — отмена текущей задачи\n"
        "/help — эта справка"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — запуск бота\n"
        "/help — помощь\n"
        "/stats — статистика\n"
        "/cancel — отмена текущей задачи\n\n"
        "📥 Как скачать стикер:\n"
        "1. Отправь стикер\n"
        "2. Выбери формат (PNG/JPG/ZIP)\n"
        "3. Получи файл\n\n"
        "📦 Как скачать стикер-пак:\n"
        "1. Отправь ссылку t.me/addstickers/название\n"
        "2. Выбери формат\n"
        "3. Получи ZIP-архив со всеми стикерами"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 <b>Статистика бота</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 Стикеров скачано: <b>{stats['stickers']}</b>\n"
        f"📦 Паков скачано: <b>{stats['packs']}</b>",
        parse_mode="HTML"
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    task_info = current_tasks.pop(user_id, None)
    
    if task_info and task_info.get("task") and not task_info["task"].done():
        task_info["task"].cancel()
        await update.message.reply_text("❌ Задача отменена")
        logger.info(f"Пользователь {user_id} отменил задачу")
    else:
        await update.message.reply_text("ℹ️ Нет активных задач")

# ------------------------- Обработка сообщений -------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if update.message.sticker:
        sticker = update.message.sticker
        current_tasks[user_id] = {"data": {"sticker": sticker}, "task": None}
        
        keyboard = [
            [InlineKeyboardButton("🖼 PNG", callback_data="format_png"),
             InlineKeyboardButton("📸 JPG", callback_data="format_jpg"),
             InlineKeyboardButton("📦 ZIP", callback_data="format_zip")]
        ]
        await update.message.reply_text(
            "🎨 Выберите формат для стикера:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif update.message.text:
        match = re.search(r"t\.me/addstickers/(\w+)", update.message.text)
        if match:
            set_name = match.group(1)
            current_tasks[user_id] = {"data": {"set_name": set_name}, "task": None}
            
            keyboard = [
                [InlineKeyboardButton("🖼 PNG (в ZIP)", callback_data="pack_png"),
                 InlineKeyboardButton("📸 JPG (в ZIP)", callback_data="pack_jpg"),
                 InlineKeyboardButton("📦 ZIP (оригинал)", callback_data="pack_zip")]
            ]
            await update.message.reply_text(
                f"📦 Найден пак: <code>{set_name}</code>\n\nВыберите формат:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

# ------------------------- Кнопки -------------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    task_info = current_tasks.get(user_id)
    
    if not task_info:
        await query.message.reply_text("❌ Задача не найдена. Отправьте стикер или ссылку заново.")
        return

    # Определяем, что скачиваем
    if data.startswith("format_") and "sticker" in task_info["data"]:
        format_type = data.split("_")[1]
        sticker = task_info["data"]["sticker"]
        task = asyncio.create_task(download_sticker_single(query, context, sticker, format_type))
        current_tasks[user_id]["task"] = task
        
    elif data.startswith("pack_") and "set_name" in task_info["data"]:
        format_type = data.split("_")[1]
        set_name = task_info["data"]["set_name"]
        task = asyncio.create_task(download_full_pack(query, context, set_name, format_type))
        current_tasks[user_id]["task"] = task
    
    # Удаляем сообщение с кнопками
    await safe_delete_message(query.message)

# ------------------------- Скачивание одного стикера -------------------------
async def download_sticker_single(query, context, sticker, format_type):
    user_id = query.from_user.id
    temp_folder = f"temp_{uuid4().hex}"
    os.makedirs(temp_folder, exist_ok=True)
    
    # Определяем расширение
    if sticker.is_animated:
        ext = ".tgs"
    elif sticker.is_video:
        ext = ".webm"
    else:
        ext = ".webp"
    
    temp_path = os.path.join(temp_folder, f"sticker{ext}")
    original_name = Path(temp_path).stem
    zip_name = None
    
    try:
        file = await context.bot.get_file(sticker.file_id)
        await file.download_to_drive(temp_path)
        
        # Конвертация в JPG
        if format_type == "jpg":
            try:
                im = Image.open(temp_path)
                jpg_path = temp_path.rsplit(".", 1)[0] + ".jpg"
                im.convert("RGB").save(jpg_path, "JPEG")
                await async_cleanup_files(temp_path)
                temp_path = jpg_path
            except UnidentifiedImageError:
                pass
        
        # Создание ZIP с правильным расширением внутри
        if format_type == "zip":
            zip_name = os.path.join(temp_folder, f"{original_name}.zip")
            target_ext = ".png" if ext == ".webp" else ".jpg" if format_type == "jpg" else ext
            with zipfile.ZipFile(zip_name, "w") as zipf:
                arcname = f"{original_name}{target_ext}"
                zipf.write(temp_path, arcname=arcname)
            await query.message.reply_document(
                open(zip_name, "rb"),
                filename=f"sticker_{original_name}.zip"
            )
        else:
            await query.message.reply_document(
                open(temp_path, "rb"),
                filename=f"sticker_{original_name}{'.jpg' if format_type == 'jpg' else '.png' if format_type == 'png' else ext}"
            )
        
        stats["stickers"] += 1
        logger.info(f"Стикер скачан пользователем {user_id}, формат: {format_type}")
        
    except asyncio.CancelledError:
        await query.message.reply_text("❌ Скачивание отменено")
        raise
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")
        logger.error(f"Ошибка скачивания стикера: {e}")
    finally:
        await async_cleanup_files(temp_folder, zip_name)
        current_tasks.pop(user_id, None)

# ------------------------- Скачивание стикер-пака -------------------------
async def download_full_pack(query, context, set_name, format_type):
    user_id = query.from_user.id
    temp_folder = f"temp_pack_{uuid4().hex}"
    os.makedirs(temp_folder, exist_ok=True)
    zip_name = None
    progress_msg = None
    
    try:
        progress_msg = await query.message.reply_text(
            f"📦 Начинаю скачивание пака <code>{set_name}</code>...",
            parse_mode="HTML"
        )
        
        sticker_set = await context.bot.get_sticker_set(set_name)
        stickers = sticker_set.stickers
        total = len(stickers)
        
        if total == 0:
            await progress_msg.edit_text("❌ Пак пуст")
            return
        
        last_percent = -1
        downloaded_files = []
        
        for idx, sticker in enumerate(stickers, 1):
            if sticker.is_animated:
                ext = ".tgs"
            elif sticker.is_video:
                ext = ".webm"
            else:
                ext = ".webp"
            
            temp_path = os.path.join(temp_folder, f"{sticker.file_unique_id}{ext}")
            file = await context.bot.get_file(sticker.file_id)
            await file.download_to_drive(temp_path)
            
            # Конвертация для PNG/JPG
            if format_type in ["png", "jpg"] and ext == ".webp":
                try:
                    im = Image.open(temp_path)
                    new_ext = ".png" if format_type == "png" else ".jpg"
                    new_path = temp_path.rsplit(".", 1)[0] + new_ext
                    if format_type == "png":
                        im.save(new_path, "PNG")
                    else:
                        im.convert("RGB").save(new_path, "JPEG")
                    await async_cleanup_files(temp_path)
                    temp_path = new_path
                except UnidentifiedImageError:
                    pass
            
            downloaded_files.append(temp_path)
            
            # Обновление прогресса
            current_percent = int(idx / total * 100)
            if current_percent != last_percent or idx % 5 == 0 or idx == total:
                await progress_msg.edit_text(
                    f"📦 Скачивание пака <code>{set_name}</code>\n"
                    f"{progress_bar(idx, total)}\n"
                    f"💾 {format_size_mb(await get_folder_size_mb(temp_folder))}",
                    parse_mode="HTML"
                )
                last_percent = current_percent
            
            # Лимит размера
            size_mb = await get_folder_size_mb(temp_folder)
            if size_mb > MAX_PACK_SIZE_MB:
                await progress_msg.edit_text(f"❌ Пак превышает лимит ({MAX_PACK_SIZE_MB} МБ)")
                return
        
        # Создание ZIP
        zip_name = os.path.join(temp_folder, f"{safe_filename(set_name)}.zip")
        with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zipf:
            for idx, file_path in enumerate(downloaded_files, 1):
                if format_type == "png":
                    arcname = f"{idx:04d}.png"
                elif format_type == "jpg":
                    arcname = f"{idx:04d}.jpg"
                else:
                    ext = os.path.splitext(file_path)[1]
                    arcname = f"{idx:04d}{ext}"
                zipf.write(file_path, arcname)
        
        # Удаляем сообщение с прогрессом
        await safe_delete_message(progress_msg)
        
        # Отправка архива
        size_mb = await get_folder_size_mb(temp_folder)
        caption = (
            f"✅ <b>Стикер-пак готов!</b>\n\n"
            f"📦 <code>{set_name}</code>\n"
            f"📊 <b>{total}</b> стикеров\n"
            f"💾 <b>{format_size_mb(size_mb)}</b>\n"
            f"🎨 Формат: <b>{format_type.upper()}</b>"
        )
        await query.message.reply_document(
            open(zip_name, "rb"),
            filename=f"{safe_filename(set_name)}.zip",
            caption=caption,
            parse_mode="HTML"
        )
        
        stats["packs"] += 1
        logger.info(f"Пак {set_name} скачан пользователем {user_id}, формат: {format_type}, размер: {format_size_mb(size_mb)}")
        
    except asyncio.CancelledError:
        await safe_delete_message(progress_msg)
        await query.message.reply_text("❌ Скачивание пака отменено")
        raise
    except Exception as e:
        await safe_delete_message(progress_msg)
        await query.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")
        logger.error(f"Ошибка скачивания пака {set_name}: {e}")
    finally:
        await async_cleanup_files(temp_folder, zip_name)
        current_tasks.pop(user_id, None)

# ------------------------- Запуск бота -------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    
    # Сообщения
    app.add_handler(MessageHandler(
        filters.Sticker.ALL | (filters.TEXT & ~filters.COMMAND),
        handle_message
    ))
    
    # Кнопки
    app.add_handler(CallbackQueryHandler(button_callback))
    
    logger.info("🚀 Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()