import os
import asyncio
import json
import re
from typing import List, Union
from urllib.parse import urlparse

import httpx
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# =========================
# Required environment vars
# =========================
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SESSION_STRING = os.environ["SESSION_STRING"]

# =========================
# Optional environment vars
# =========================
SOURCE_CHANNEL_RAW = os.environ.get("SOURCE_CHANNELS") or os.environ.get(
    "SOURCE_CHANNEL", "https://t.me/Honeyofwhitesocks_2"
)
TARGET_CHANNEL_RAW = os.environ.get("TARGET_CHANNEL", "https://t.me/KnightOnline58")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "900"))
SEEN_IDS_FILE = os.environ.get("SEEN_IDS_FILE", "seen_ids.json")


def normalize_chat_ref(value: str) -> Union[str, int]:
    """Accept @username, https://t.me/name, t.me/name, or numeric chat ids."""
    value = value.strip()
    if not value:
        raise ValueError("Empty Telegram chat reference")

    if re.fullmatch(r"-?\d+", value):
        return int(value)

    if value.startswith("https://t.me/") or value.startswith("http://t.me/") or value.startswith("t.me/"):
        if not value.startswith("http"):
            value = "https://" + value

        parsed = urlparse(value)
        path = parsed.path.strip("/")

        if path and not path.startswith("c/"):
            return "@" + path.split("/")[0]

        return value

    return value


def parse_source_chats(raw: str) -> List[Union[str, int]]:
    return [normalize_chat_ref(item) for item in raw.split(",") if item.strip()]


SOURCE_CHATS = parse_source_chats(SOURCE_CHANNEL_RAW)
TARGET_CHANNEL = normalize_chat_ref(TARGET_CHANNEL_RAW)


def load_seen_ids() -> set:
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))

    return set()


def save_seen_ids(seen_ids: set) -> None:
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen_ids)), f, ensure_ascii=False, indent=2)


def build_prompt(text: str) -> str:
    return f"""Sen Korece ve İngilizce kripto/finans Telegram mesajlarını Türkçeye çeviren bir çeviri botusun.

Sadece aşağıdaki Telegram mesaj metnini Türkçeye çevir ve en alta anlaşılır bir özet ekle.

Kurallar:
- Görsel varsa dikkate alma, görsel analizi yapma.
- Linkleri açma, link içeriği hakkında yorum yapma.
- Çeviri bölümünde özetleme yapma; kaynak metindeki anlamı koru.
- Türkçesi doğal, kısa ve Telegram'da okunabilir olsun.
- Kaynakta olmayan bilgi, yorum veya yatırım tavsiyesi ekleme.
- Özel isimleri, token/proje adlarını ve kripto-finans jargonunu koru.
- Crypto/finans metinlerinde "narrative" kelimesini genelde "anlatı" olarak çevir; "hikaye" deme.
- "Decentralized AI" için "merkeziyetsiz AI" veya "merkeziyetsiz yapay zekâ" kullan.
- Türkçede tekrar eden ifadeleri doğal şekilde birleştir ama anlamı değiştirme.
- Korece olumsuzluklara dikkat et:
  안 나오다 = çıkmamak
  안 나올 가능성 = çıkmama ihtimali
  아닐 수 있다 = olmayabilir

Format:
🇹🇷 Mesaj Çevirisi:
[Çeviri]

🧠 Özet:
[Mesajın ne demek istediğini kısa ve anlaşılır şekilde özetle. Yorum, tahmin veya kaynakta olmayan bilgi ekleme.]

Telegram mesaj metni:
{text.strip()}
"""


async def call_claude(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=70) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt,
                            }
                        ],
                    }
                ],
            },
        )

        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic hata {r.status_code}: {r.text[:1000]}")

        data = r.json()
        return data["content"][0]["text"].strip()


async def send_long_message(client: TelegramClient, target: Union[str, int], text: str) -> None:
    max_len = 3800

    if len(text) <= max_len:
        await client.send_message(target, text)
        return

    parts = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break

        cut = remaining.rfind("\n", 0, max_len)
        if cut < 1000:
            cut = max_len

        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    for i, part in enumerate(parts, start=1):
        await client.send_message(target, f"{part}\n\n({i}/{len(parts)})")


async def main() -> None:
    seen_ids = load_seen_ids()

    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    bot_client = TelegramClient("bot_session", API_ID, API_HASH)

    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)

    print("✅ Bot çalışıyor, kanal izleniyor...")
    print(f"📥 Kaynak: {SOURCE_CHATS}")
    print(f"📤 Hedef: {TARGET_CHANNEL}")
    print(f"🧠 Model: {CLAUDE_MODEL}")

    @user_client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        message = event.message
        chat_id = event.chat_id or "unknown"
        msg_id = message.id
        seen_key = f"{chat_id}:{msg_id}"

        if seen_key in seen_ids:
            return

        text = (message.text or "").strip()

        if not text:
            seen_ids.add(seen_key)
            save_seen_ids(seen_ids)
            return

        print(f"📨 Yeni mesaj: {seen_key}")

        try:
            prompt = build_prompt(text)
            result = await call_claude(prompt)

            chat = await event.get_chat()
            source_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
            source_username = getattr(chat, "username", None)

            prefix = f"🔔 Kaynak Kanal: {source_name}"
            if source_username:
                prefix += f"\n🔗 https://t.me/{source_username}"

            output = f"{prefix}\n\n{result}"

            await send_long_message(bot_client, TARGET_CHANNEL, output)
            print(f"✅ Gönderildi: {seen_key}")

        except Exception as exc:
            print(f"❌ Hata: {seen_key} | {exc}")

        seen_ids.add(seen_key)
        save_seen_ids(seen_ids)

    await user_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
