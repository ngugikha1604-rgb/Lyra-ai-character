import discord
import asyncio
from core import MiniAI
from config import *
from datetime import datetime
import pytz

# ========================
# CONFIG
# ========================

ALLOWED_CHANNEL_ID = 1480581758708875345
PREFIX = "!"

# Check proactive mỗi 60 phút
PROACTIVE_CHECK_INTERVAL = 60 * 60

# ========================
# BOT SETUP
# ========================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)

# Mỗi user có AI instance riêng
ai_instances = {}

def get_ai(user_id: int) -> MiniAI:
    if user_id not in ai_instances:
        ai_instances[user_id] = MiniAI()
        print(f"✓ Created AI instance for user {user_id}")
    return ai_instances[user_id]


# ========================
# PROACTIVE BACKGROUND TASK
# ========================

async def proactive_loop():
    """Background task: check mỗi giờ, tự nhắn nếu user vắng đủ lâu"""
    await client.wait_until_ready()

    channel = client.get_channel(ALLOWED_CHANNEL_ID)
    if not channel:
        print(f"[Proactive] Cannot find channel {ALLOWED_CHANNEL_ID}")
        return

    print(f"[Proactive] Started — checking every {PROACTIVE_CHECK_INTERVAL//60} minutes")

    while not client.is_closed():
        await asyncio.sleep(PROACTIVE_CHECK_INTERVAL)

        for user_id, ai in list(ai_instances.items()):
            try:
                loop = asyncio.get_event_loop()
                msg = await loop.run_in_executor(None, ai.get_proactive_message)

                if msg:
                    await channel.send(msg)
                    # Cập nhật last_message_time để không spam
                    now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh')).isoformat()
                    ai.memory["time_tracking"]["last_message_time"] = now
                    await loop.run_in_executor(None, ai.save_memory)
                    print(f"[Proactive] Sent to user {user_id}: {msg[:60]}")

            except Exception as e:
                print(f"[Proactive] Error for user {user_id}: {e}")


# ========================
# EVENTS
# ========================

@client.event
async def on_ready():
    print(f"✓ Lyra bot is online as {client.user}")
    print(f"✓ Connected to {len(client.guilds)} server(s)")
    client.loop.create_task(proactive_loop())


@client.event
async def on_message(message):

    # Bỏ qua tin nhắn của chính bot
    if message.author == client.user:
        return

    # Bỏ qua nếu không đúng channel (nếu có set)
    if ALLOWED_CHANNEL_ID and message.channel.id != ALLOWED_CHANNEL_ID:
        return

    content = message.content.strip()

    # ========================
    # COMMANDS
    # ========================

    # !reset — reset session của user này
    if content.lower() == f"{PREFIX}reset":
        if message.author.id in ai_instances:
            del ai_instances[message.author.id]
        await message.channel.send("Memory cleared. Starting fresh~")
        return

    # !status — xem trạng thái AI
    if content.lower() == f"{PREFIX}status":
        ai = get_ai(message.author.id)
        await message.channel.send(
            f"Affection: {int(ai.affection)}/100 | "
            f"Mood: {int(ai.mood)} | "
            f"Emotion: {ai.emotion_from_state()}"
        )
        return

    # !help — danh sách lệnh
    if content.lower() == f"{PREFIX}help":
        await message.channel.send(
            "**Lyra Commands:**\n"
            f"`{PREFIX}reset` — Reset memory\n"
            f"`{PREFIX}status` — View current mood & affection\n"
            f"`{PREFIX}help` — Show this message"
        )
        return

    # Bỏ qua tin nhắn bắt đầu bằng prefix khác (lệnh bot khác)
    if content.startswith("!") or content.startswith("/"):
        return

    # Bỏ qua tin nhắn quá ngắn hoặc trống
    if not content or len(content) < 1:
        return

    # ========================
    # CHAT
    # ========================

    # Hiện typing indicator trong lúc AI xử lý
    async with message.channel.typing():
        try:
            ai = get_ai(message.author.id)

            # Chạy ai.chat() trong thread riêng (không block event loop)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, ai.chat, content)

            reply = result.get("reply", "...")
            emotion = result.get("emotion", "neutral")

            # Gửi reply
            await message.channel.send(reply)

            print(f"[{message.author.name}] {content[:50]} → [{emotion}] {reply[:50]}")

        except Exception as e:
            print(f"Discord bot error: {e}")
            await message.channel.send("Something went wrong...")


# ========================
# MAIN
# ========================

if __name__ == "__main__":
    print("Starting Lyra Discord Bot...")
    client.run(DISCORD_TOKEN)