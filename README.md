# Telegram Korece -> Türkçe AI Relay Bot

Bu bot birden fazla Telegram kaynak kanalını dinler, mesaj metnini Claude Haiku ile Türkçeye çevirip hedef Telegram kanalına gönderir.

Linkleri okumaz. Harici sayfalara gitmez.

## Railway Start Command

python bot.py

## Railway Variables

TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=...
SESSION_STRING=...

SOURCE_CHANNELS=https://t.me/Honeyofwhitesocks_2,https://t.me/KORypto_Announce
TARGET_CHANNEL=https://t.me/KnightOnline58
CLAUDE_MODEL=claude-haiku-4-5-20251001

VISION_ENABLED=false
MAX_OUTPUT_TOKENS=900
VISION_MAX_IMAGE_DIM=1280
VISION_MAX_IMAGE_BYTES=2500000
