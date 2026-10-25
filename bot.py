import telebot
from telebot import types
import subprocess
import os
import uuid
import logging
import imageio_ffmpeg

# Токен берется из переменных окружения (для безопасности на GitHub)
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO)

TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)

user_data = {}
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# --- НАСТРОЙКИ ЭФФЕКТОВ (БАС И ТАЙМИНГИ НЕ ТРОНУТЫ) ---
EQ_FILTER = (
    "equalizer=f=50:t=q:w=1.5:g=20, "
    "equalizer=f=120:t=q:w=1.2:g=14, "
    "equalizer=f=250:t=q:w=1.2:g=-4, "
    "equalizer=f=4000:t=q:w=1.0:g=-3"
)

SPEEDUP_FILTER = f"asetrate=44100*1.15,aresample=44100, {EQ_FILTER}"
SLOWED_FILTER = f"{EQ_FILTER}, asetrate=44100*0.85,aresample=44100"
NORMAL_FILTER = EQ_FILTER

# --- ФУНКЦИЯ ОБРАБОТКИ (ИСПРАВЛЕНА ГРОМКОСТЬ) ---
def process_with_time(input_path, output_path, filter_str, start_sec, volume):
    gain = volume / 100.0

    # Компрессор срабатывает только на пиках (threshold=0.8), не душит громкость
    compressor = "acompressor=threshold=0.8:ratio=10:attack=0.01:release=0.05:makeup=1.5"
    
    final_filter = f"{filter_str}, volume={gain}, {compressor}"

    if start_sec <= 0:
        command = [
            FFMPEG_PATH, '-y', '-i', input_path,
            '-af', final_filter,
            '-vn', '-b:a', '320k', output_path
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        complex_filter = (
            f"[0:a]atrim=0:{start_sec},asetpts=PTS-STARTPTS[part1]; "
            f"[0:a]atrim={start_sec},asetpts=PTS-STARTPTS,{final_filter}[part2]; "
            f"[part1][part2]concat=n=2:v=0:a=1[out]"
        )
        command = [
            FFMPEG_PATH, '-y', '-i', input_path,
            '-filter_complex', complex_filter,
            '-map', '[out]',
            '-vn', '-b:a', '320k', output_path
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# --- ХЕНДЛЕРЫ БОТА ---

@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(
        message.chat.id, 
        "Привет! 🎧 Я бот для создания мощных басбуст-ремиксов.\n\n"
        "1. Отправь мне аудиофайл.\n"
        "2. Напиши, с какой секунды начинать ремикс (или 0 для всего трека).\n"
        "3. Напиши громкость (от 0 до 1000, где 100 — обычно).\n"
        "4. Выбери режим обработки!"
    )

@bot.message_handler(content_types=['audio', 'document', 'voice'])
def handle_audio(message):
    file_info = None
    if message.audio:
        file_info = bot.get_file(message.audio.file_id)
        file_name = f"{uuid.uuid4()}.mp3"
    elif message.document and message.document.mime_type.startswith('audio'):
        file_info = bot.get_file(message.document.file_id)
        file_name = f"{uuid.uuid4()}.mp3"
    elif message.voice:
        file_info = bot.get_file(message.voice.file_id)
        file_name = f"{uuid.uuid4()}.ogg"
    else:
        bot.reply_to(message, "Пожалуйста, отправь аудиофайл.")
        return

    msg = bot.reply_to(message, "⏳ Скачиваю файл...")
    
    input_path = os.path.join(TEMP_DIR, file_name)
    downloaded_file = bot.download_file(file_info.file_path)
    with open(input_path, 'wb') as new_file:
        new_file.write(downloaded_file)
        
    user_data[message.chat.id] = {"path": input_path, "start_sec": 0, "volume": 100, "state": "waiting_sec"}
    
    bot.edit_message_text(
        "✅ Файл получен!\n\n"
        "Напиши, с какой секунды начать ремикс (например: 30).\n"
        "Если хочешь обработать весь трек — напиши 0.",
        chat_id=message.chat.id,
        message_id=msg.message_id
    )

@bot.message_handler(func=lambda message: message.text and message.text.strip().lstrip('-').isdigit())
def handle_numbers(message):
    chat_id = message.chat.id
    
    if chat_id not in user_data:
        return

    state = user_data[chat_id].get("state")
    if not state:
        return

    val = int(message.text.strip())

    if state == "waiting_sec":
        if val < 0:
            bot.reply_to(message, "⚠️ Секунда не может быть отрицательной. Введи 0 или больше.")
            return
        user_data[chat_id]["start_sec"] = val
        user_data[chat_id]["state"] = "waiting_volume"
        bot.reply_to(
            message, 
            f"✅ Старт с {val} сек.\n\n"
            "Теперь напиши громкость:\n"
            "• 0 — тишина\n"
            "• 100 — обычно (как в оригинале)\n"
            "• 1000 — очень громко (все частоты поднимаются)"
        )

    elif state == "waiting_volume":
        if val < 0 or val > 1000:
            bot.reply_to(message, "⚠️ Громкость должна быть от 0 до 1000. Попробуй еще раз.")
            return
        user_data[chat_id]["volume"] = val
        user_data[chat_id]["state"] = "ready"
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_normal = types.InlineKeyboardButton("🔥 Normal Bassboost", callback_data='process_normal')
        btn_slowed = types.InlineKeyboardButton("🐢 Slowed Bassboost", callback_data='process_slowed')
        btn_speedup = types.InlineKeyboardButton("🐇 Speed Up Bassboost", callback_data='process_speedup')
        markup.add(btn_normal, btn_slowed, btn_speedup)
        
        bot.reply_to(
            message,
            f"✅ Громкость: {val}.\nТеперь выбери режим:",
            reply_markup=markup
        )

PROCESSING_MODES = {
    "normal": {"name": "🔥 Normal Bassboost"},
    "slowed": {"name": "🐢 Slowed Bassboost"},
    "speedup": {"name": "🐇 Speed Up Bassboost"}
}

@bot.callback_query_handler(func=lambda call: call.data.startswith('process_'))
def process_audio(call):
    chat_id = call.message.chat.id
    mode_key = call.data.replace('process_', '')
    
    if chat_id not in user_data or "path" not in user_data[chat_id] or user_data[chat_id]["state"] != "ready":
        bot.answer_callback_query(call.id, "Ошибка: настройки потерялись. Отправь трек заново.")
        return
        
    input_path = user_data[chat_id]["path"]
    start_sec = user_data[chat_id]["start_sec"]
    volume = user_data[chat_id]["volume"]
    output_path = os.path.join(TEMP_DIR, f"out_{uuid.uuid4()}.mp3")
    
    bot.answer_callback_query(call.id, "Начинаю обработку...")
    bot.edit_message_text(
        f"⚙️ Рендерю трек...\n"
        f"Режим: {PROCESSING_MODES[mode_key]['name']}\n"
        f"С секунды: {start_sec}\n"
        f"Громкость: {volume}",
        chat_id=chat_id,
        message_id=call.message.message_id
    )
    
    if mode_key == "normal":
        audio_filter = NORMAL_FILTER
    elif mode_key == "slowed":
        audio_filter = SLOWED_FILTER
    else:
        audio_filter = SPEEDUP_FILTER
        
    try:
        process_with_time(input_path, output_path, audio_filter, start_sec, volume)
        
        with open(output_path, 'rb') as audio:
            bot.send_audio(
                chat_id, audio, 
                caption=f"Твой ремикс готов! 🎵\nРежим: {PROCESSING_MODES[mode_key]['name']}\n"
                        f"С секунды: {start_sec}\nГромкость: {volume}"
            )
            
    except subprocess.CalledProcessError as e:
        bot.send_message(chat_id, "❌ Ошибка при обработке аудио.")
        logging.error(f"FFmpeg error: {e.stderr.decode()}")
        
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        if chat_id in user_data:
            del user_data[chat_id]

print("Бот запущен и готов к работе...")
bot.infinity_polling()