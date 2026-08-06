# -*- coding: utf-8 -*-
"""
ElevenLabs с каскадом ключей.

Лимит символов на бесплатном тарифе привязан к аккаунту, поэтому N ключей = N
независимых бюджетов — та же схема, что с Groq (service-notes/groq.md). Ключи
берутся из keys.local: ELEVENLABS_API_KEY, ELEVENLABS_API_KEY_2, _3 и так далее.

Исчерпание видно по 401 с телом quota_exceeded — на это переключаемся на следующий
ключ молча. Прочие ошибки не глушим: они означают поломку, а не исчерпание.
"""

import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_core import KEYS_FILE  # noqa: E402  — единый источник пути к ключам

API = "https://api.elevenlabs.io/v1"

# ⚠️ Голоса из БИБЛИОТЕКИ (в том числе все 600 русских носителей) на бесплатном
# тарифе через API недоступны: «Free users cannot use library voices via the API»,
# HTTP 402. Добавление голоса в аккаунт (POST /voices/add) проходит, но синтез всё
# равно отбивается. Поле free_users_allowed относится к сайту, а не к API.
# Поэтому здесь только premade — стоковые голоса, они говорят по-русски через
# eleven_multilingual_v2 (с лёгким акцентом, но живо).
VOICES = {
    # мужские, спокойные и внушающие доверие
    "brian":   ("nPczCjzI2devNBz1zQrb", "м, низкий, обволакивающий"),
    "eric":    ("cjVigY5qzO86Huf0OWal", "м, мягкий, располагающий"),
    "daniel":  ("onwK4e9ZLuTAKqWW03F9", "м, ровный, дикторский"),
    "bill":    ("pqHfZKP75CvOlQylNhV4", "м, зрелый, уравновешенный"),
    "chris":   ("iP95p4xoKVk53GoZ742B", "м, обаятельный, простой"),
    "roger":   ("CwhRBWXzGAHq8TQ4Fs17", "м, непринуждённый, звучный"),
    "will":    ("bIHbv24MWmeRgasZH58o", "м, расслабленный"),
    "george":  ("JBFqnCBsd6RMkjVDRZzb", "м, тёплый рассказчик"),
    "river":   ("SAz9YHcvj6GT2YYXdXww", "нейтральный, информативный"),
    # женские
    "matilda": ("XrExE9yKIg1WjnnlVkGX", "ж, знающая, деловая"),
    "jessica": ("cgSgspJ2msm6clMCkdW9", "ж, светлая, тёплая"),
    "bella":   ("hpp4J3VqNfWAUOO0d1Us", "ж, профессиональная, тёплая"),
    "lily":    ("pFZP5JQG7iQjIQuC4Bku", "ж, бархатная, актёрская"),
    "alice":   ("Xb7hH8MSUJpSbSDYk0k2", "ж, чёткая, британская"),
    "sarah":   ("EXAVITQu4vr4xnSDxMaL", "ж, зрелая, уверенная"),
}

MODELS = {
    "quality": "eleven_multilingual_v2",   # 1 кредит/символ
    "turbo":   "eleven_turbo_v2_5",        # 0.5 кредита/символ
    "flash":   "eleven_flash_v2_5",        # 0.5 кредита/символ, минимальная задержка
}


def load_keys():
    """Все ключи ElevenLabs по порядку. Значения нигде не печатаются (CLAUDE.md §2)."""
    if not KEYS_FILE.exists():
        return []
    keys, seen = [], set()
    for line in KEYS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^\s*ELEVENLABS_API_KEY(?:_\d+)?\s*=\s*(\S+)\s*$", line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            keys.append(m.group(1))
    return keys


def _is_quota_error(response):
    """Отличает «кончились символы» от настоящей поломки."""
    if response.status_code not in (401, 429):
        return False
    body = response.text.lower()
    return "quota" in body or "limit" in body or "credits" in body


def balances():
    """
    [(номер ключа, использовано, лимит, примечание)] — сводка по всему каскаду.

    Примечание объясняет, ПОЧЕМУ цифр нет, и это не мелочь. Ключ ElevenLabs можно
    выпустить с урезанными правами: без `user_read` он не отдаёт баланс, но
    прекрасно синтезирует. В сводке без пояснения такой ключ выглядит ровно как
    протухший — и его пойдут перевыпускать вместо того, чтобы пользоваться
    (проверено на втором ключе 2026-08-06: баланс 401, синтез 200).
    """
    out = []
    for i, key in enumerate(load_keys(), 1):
        try:
            r = requests.get(f"{API}/user/subscription",
                             headers={"xi-api-key": key}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                out.append((i, d.get("character_count"), d.get("character_limit"), None))
            elif r.status_code == 401 and "missing_permission" in r.text.lower():
                out.append((i, None, None,
                            "баланс скрыт (нет права user_read) — синтез работает"))
            else:
                out.append((i, None, None, f"HTTP {r.status_code}"))
        except requests.RequestException as e:
            out.append((i, None, None, f"сеть: {e.__class__.__name__}"))
    return out


def synthesize(text, voice_id, model="eleven_multilingual_v2", settings=None, retries=2):
    """
    Возвращает (mp3_bytes, номер сработавшего ключа) либо (None, None).

    Ключи перебираются только при исчерпании квоты. Сетевые сбои — это повод
    повторить на том же ключе, а не жечь следующий бюджет.
    """
    settings = settings or {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0}
    keys = load_keys()
    if not keys:
        raise RuntimeError("ELEVENLABS_API_KEY не найден в keys.local")

    last_error = None
    for idx, key in enumerate(keys, 1):
        for attempt in range(retries):
            try:
                r = requests.post(
                    f"{API}/text-to-speech/{voice_id}",
                    headers={"xi-api-key": key, "Content-Type": "application/json"},
                    json={"text": text, "model_id": model, "voice_settings": settings},
                    timeout=120,
                )
            except requests.RequestException as e:
                last_error = f"сеть: {e}"
                time.sleep(0.6 * (attempt + 1))
                continue

            if r.status_code == 200:
                return r.content, idx
            if _is_quota_error(r):
                last_error = f"ключ #{idx}: квота исчерпана"
                break  # следующий ключ
            last_error = f"ключ #{idx}: HTTP {r.status_code} {r.text[:150]}"
            break

    raise RuntimeError(f"ElevenLabs недоступен — {last_error}")
