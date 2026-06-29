import os
import asyncio
import base64
import html
import io
import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

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
SEEN_IDS_FILE = os.environ.get("SEEN_IDS_FILE", "seen_ids.json")

LINK_READER_ENABLED = os.environ.get("LINK_READER_ENABLED", "false").lower() == "true"
VISION_ENABLED = os.environ.get("VISION_ENABLED", "true").lower() == "true"
MAX_LINKS = int(os.environ.get("MAX_LINKS", "2"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "900"))
VISION_MAX_IMAGE_DIM = int(os.environ.get("VISION_MAX_IMAGE_DIM", "1280"))
VISION_MAX_IMAGE_BYTES = int(os.environ.get("VISION_MAX_IMAGE_BYTES", "2500000"))

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


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


async def fetch_link_preview(url: str) -> str:
    if not LINK_READER_ENABLED:
        return ""

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html_text = r.text[:12000]

        title = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        desc = re.search(
            r'<meta[^>]+(?:name|property)=["\'](?:description|og:description|twitter:description)["\'][^>]+content=["\'](.*?)["\']',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        og_title = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )

        t = title.group(1).strip() if title else ""
        if not t and og_title:
            t = og_title.group(1).strip()
        d = desc.group(1).strip() if desc else ""

        t = html.unescape(re.sub(r"\s+", " ", t))[:250]
        d = html.unescape(re.sub(r"\s+", " ", d))[:500]

        if t or d:
            return f"\n\n[Link: {url}]\nBaşlık: {t}\nAçıklama: {d}"
    except Exception as exc:
        print(f"⚠️ Link okunamadı: {url} | {exc}")

    return f"\n\n[Link: {url}]"


def extract_links(message) -> List[str]:
    links: List[str] = []
    text = message.text or ""

    if message.entities:
        for entity in message.entities:
            if isinstance(entity, MessageEntityUrl):
                links.append(text[entity.offset : entity.offset + entity.length])
            elif isinstance(entity, MessageEntityTextUrl):
                links.append(entity.url)

    # Fallback: sometimes Telegram does not expose URL entities as expected.
    for found in re.findall(r"https?://\S+", text):
        links.append(found.rstrip(").,]"))

    # Keep order, remove duplicates.
    deduped: List[str] = []
    for link in links:
        if link not in deduped:
            deduped.append(link)
    return deduped


def message_has_image(message) -> bool:
    if message.photo:
        return True
    if message.file and getattr(message.file, "mime_type", None):
        return str(message.file.mime_type).startswith("image/")
    return False


def compress_image_for_claude(raw: bytes, media_type: str) -> Tuple[bytes, str]:
    """Resize and convert images to JPEG to reduce vision token/cost and payload size."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)

        # GIF/WebP may be animated; use first frame for analysis.
        if getattr(img, "is_animated", False):
            img.seek(0)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        img.thumbnail((VISION_MAX_IMAGE_DIM, VISION_MAX_IMAGE_DIM))

        quality = 88
        while True:
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            data = out.getvalue()
            if len(data) <= VISION_MAX_IMAGE_BYTES or quality <= 65:
                return data, "image/jpeg"
            quality -= 8
    except Exception as exc:
        print(f"⚠️ Görsel sıkıştırılamadı, orijinal deneniyor: {exc}")
        if media_type in ALLOWED_IMAGE_TYPES and len(raw) <= VISION_MAX_IMAGE_BYTES:
            return raw, media_type
        raise


async def build_image_block(message) -> Optional[Dict[str, Any]]:
    if not VISION_ENABLED or not message_has_image(message):
        return None

    try:
        raw = await message.download_media(file=bytes)
        if not raw:
            return None

        media_type = "image/jpeg"
        if message.file and getattr(message.file, "mime_type", None):
            media_type = str(message.file.mime_type)

        image_bytes, media_type = compress_image_for_claude(raw, media_type)
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            },
        }
    except Exception as exc:
        print(f"⚠️ Görsel işlenemedi: {exc}")
        return None


def build_prompt(text: str, link_context: str, has_image: bool) -> str:
    image_instruction = ""
    if has_image:
        image_instruction = """
Görsel varsa:
- Görseldeki Korece/İngilizce/Türkçe yazıları Türkçeye çevir.
- Tweet, tablo, grafik, dashboard, ranking, fiyat, yüzde, tarih, token/proje isimleri varsa yazıldığı gibi aktar ve Türkçeleştir.
- Görselde net olmayan yerleri uydurma; 'görselde net değil' de.
- Görseldeki yazı ile Telegram mesaj metnini birbirine karıştırma; ayrı bölümde ver.
"""

    return f"""Sen bir crypto Telegram çeviri botusun.

Ana görev:
Önce Telegram mesaj metnini ve varsa görseldeki yazıları Türkçeye çevir.
Özetlemeden önce kaynakta yazan her önemli cümleyi koru.
Son bölümde yalnızca kısa bir yorum/not yazabilirsin.

Çok önemli kurallar:
- Harici linkleri asla okuma, ziyaret etme veya özetleme.
- Sadece Telegram mesajının kendi metnini ve varsa gönderilen görseli analiz et.
- Mesaj metnini mümkün olduğunca cümle cümle çevir.
- Görseldeki yazıları da mümkün olduğunca cümle cümle çevir.
- Korece olumsuzluklara çok dikkat et:
  * 안 나오다 = çıkmamak
  * 안 나올 가능성 = çıkmama ihtimali
  * 없어 보인다 = yok gibi görünüyor
  * 아닐 수 있다 = olmayabilir
- 'Token çıkar' ile 'token çıkmaz/çıkmayabilir' anlamını asla ters çevirme.
- Crypto jargonunu koru: DeFi, DEX, ETF, staking, airdrop, mainnet, testnet, validator, OTC, liquidity, Series B, FDV vb.
- Bilmediğin şeyi uydurma.
- Kaynak metinde olmayan bilgiyi çeviri bölümüne ekleme.
- Kısa Not bölümünde resmi açıklama mı, kanal yorumu mu, beklenti mi olduğunu 2-3 cümleyle belirt.
- Yatırım tavsiyesi verme.

{image_instruction}

Çıktı formatı:
🇹🇷 Mesaj Çevirisi:
[Telegram mesaj metninin Türkçe çevirisi. Metin yoksa 'Mesaj metni yok.' yaz.]

🖼️ Görsel Çevirisi:
[Görsel varsa görseldeki yazıların ve görünen önemli bilgilerin Türkçe çevirisi. Görsel yoksa 'Görsel yok.' yaz.]

📌 Kısa Not:
[2-3 cümle. Sadece bağlam/yorum: resmi açıklama mı, kanal yorumu mu, beklenti mi? Abartma, yatırım tavsiyesi verme.]

🏷️ Etiketler:
[varsa proje/token/konu isimleri]

Telegram mesaj metni:
{text.strip() if text.strip() else '[Metin yok / sadece görsel olabilir]'}

Link bağlamı:
[Link okuma kapalı. Harici linkleri dikkate alma.]
"""


async def call_claude(prompt: str, image_block: Optional[Dict[str, Any]] = None) -> str:
    content: List[Dict[str, Any]] = []
    if image_block:
        content.append(image_block)
    content.append({"type": "text", "text": prompt})

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
                "messages": [{"role": "user", "content": content}],
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic hata {r.status_code}: {r.text[:1000]}")
        data = r.json()
        return data["content"][0]["text"].strip()


async def send_long_message(client: TelegramClient, target: Union[str, int], text: str) -> None:
    # Telegram message limit is around 4096 chars. Split safely.
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
    print(f"🧠 Model: {CLAUDE_MODEL} | Vision: {VISION_ENABLED} | Link reader: {LINK_READER_ENABLED}")

    @user_client.on(events.NewMessage(chats=SOURCE_CHATS))
    async def handler(event):
        message = event.message
        chat_id = event.chat_id or "unknown"
        msg_id = message.id
        seen_key = f"{chat_id}:{msg_id}"

        if seen_key in seen_ids:
            return

        text = message.text or ""
        has_image = message_has_image(message)

        if not text.strip() and not has_image:
            seen_ids.add(seen_key)
            save_seen_ids(seen_ids)
            return

        print(f"📨 Yeni mesaj: {seen_key} | text={bool(text.strip())} image={has_image}")

        link_context = ""
        if text.strip() and LINK_READER_ENABLED:
            for url in extract_links(message)[:MAX_LINKS]:
                link_context += await fetch_link_preview(url)

        image_block = await build_image_block(message)
        prompt = build_prompt(text, link_context, image_block is not None)

        try:
            result = await call_claude(prompt, image_block=image_block)

            chat = await event.get_chat()
            source_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
            source_username = getattr(chat, "username", None)

            prefix = f"🔔 Kaynak Kanal: {source_name}"
            if source_username:
                prefix += f"\n🔗 https://t.me/{source_username}"

            if image_block:
                prefix += "\n🖼️ Görsel Analizi: Var"

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
