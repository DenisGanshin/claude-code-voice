# -*- coding: utf-8 -*-
"""
Ядро голосового вывода: текст -> речь в динамики.

Пара к voice_core.py (слух). Используется двумя обёртками:
  * speak.py — просто сказать
  * ask.py   — сказать и тут же выслушать ответ

Движок: edge-tts (нейроголоса Microsoft, бесплатно и без ключа), при отсутствии
сети — SAPI Irina, которая есть в Windows всегда.
"""

import asyncio
import re
import sys

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from voice_core import BASE_DIR, load_config, log  # noqa: E402

DEFAULT_TTS = {
    # Каскад движков: пробуем по порядку, пока какой-то не сработает.
    #   eleven — живой голос, но лимит символов на бесплатном тарифе;
    #   piper  — локальный, мгновенный, безлимитный: рабочая лошадка;
    #   edge   — если моделей Piper нет на диске;
    #   sapi   — есть в Windows всегда, последняя надежда.
    "tts_chain": ["eleven", "piper", "edge", "sapi"],
    # Что озвучивать дорогим голосом. Всё остальное уходит на piper: лимита
    # ElevenLabs хватает примерно на 80 коротких реплик в месяц на ключ.
    "tts_premium_max_chars": 300,
    "eleven_voice": "artem",
    "eleven_model": "quality",           # quality | turbo | flash
    "eleven_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0},
    "piper_voice": "irina",   # irina | denis | ruslan | dmitri — audit: ok имена моделей Piper
    "tts_voice": "ru-RU-DmitryNeural",   # голос edge-tts
    "tts_rate": "+8%",          # чуть быстрее нейтрального: так речь звучит деловито
    "tts_volume": "+0%",
    "tts_max_chars": 700,       # длиннее слушать невозможно, читать глазами быстрее
    "tts_engine": None,         # если задан — использовать только его, без каскада
}

VOICES = {
    "male": "ru-RU-DmitryNeural",
    "female": "ru-RU-SvetlanaNeural",
}


# --------------------------------------------------------------------------- #
# Подготовка текста: то, что читается глазами, вслух звучит абсурдно
# --------------------------------------------------------------------------- #

# Заменяем то, что иначе будет прочитано побуквенно или неверно.
PRONOUNCE = [
    (r"\bn8n\b", "эн восемь эн"),
    (r"\bCLAUDE\.md\b", "клод точка эм де"),
    (r"\bAPI\b", "А-П-И"),
    (r"\bCRM\b", "си-эр-эм"),
    (r"\bVAD\b", "вад"),
    (r"\bTTS\b", "ти-ти-эс"),
    (r"\bSTT\b", "эс-ти-ти"),
    (r"\bLLM\b", "эл-эл-эм"),
    (r"\be2e\b", "и-ту-и"),
    (r"\bUI\b", "ю-ай"),
    (r"\bBT\b", "блютус"),
    (r"§", "параграф "),
    (r"→", " то есть "),
    (r"×", " на "),
]


def _speakable_path(match):
    """
    Путь или имя файла — в произносимый вид: `D:/…/tests/test_vad.py` → «тест вад».

    Вслух важно только «о каком файле речь». Каталоги, расширение и подчёркивания
    на слух не несут информации, зато занимают несколько секунд каждый.
    """
    token = match.group(0)
    name = re.split(r"[\\/]", token)[-1]
    name = re.sub(r"\.(?:py|js|cjs|mjs|ts|json|md|txt|wav|log|ps1|cmd)$", "", name)
    return name.replace("_", " ").replace("-", " ")


def fix_mojibake(text):
    """
    Чинит текст, прочитанный как cp1251 вместо UTF-8: «Р“РѕР»РѕСЃ» вместо «Голос».

    Страховка, а не основной механизм (кодировка потоков настраивается в voice_core).
    Нужна потому, что цена ошибки здесь особая: голос не падает с ошибкой, а вслух
    зачитывает бессмыслицу — пользователь слышит тарабарщину и не понимает, почему.

    Признак — кириллица, где почти каждая вторая буква «Р» или «С» с диакритикой.
    """
    if not text or "Р" not in text:
        return text
    suspicious = len(re.findall(r"[РС][-ӿ]", text))
    if suspicious < max(3, len(text) * 0.08):
        return text
    try:
        fixed = text.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # чиним только если стало заметно чище
    if len(re.findall(r"[РС][-ӿ]", fixed)) < suspicious:
        log("[warn] текст пришёл в неверной кодировке — восстановил перед озвучкой")
        return fixed
    return text


def clean_for_speech(text, max_chars=700):
    """
    Превращает markdown-ответ в то, что не стыдно услышать.

    Основная работа — выбросить, а не переписать. Блоки кода, таблицы и длинные
    пути вслух бессмысленны: слушатель всё равно не запомнит `voice_core.py:310`
    на слух, а на чтение этого уходит полминуты. Остаётся смысл, детали — глазами.
    """
    if not text:
        return ""

    t = fix_mojibake(text)
    t = re.sub(r"```.*?```", " Дальше идёт код. ", t, flags=re.DOTALL)
    t = re.sub(r"^\s*\|.*\|\s*$", "", t, flags=re.MULTILINE)      # строки таблиц
    t = re.sub(r"^\s*[-=]{3,}\s*$", "", t, flags=re.MULTILINE)    # разделители
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)                # ссылки -> текст
    t = re.sub(r"`([^`]*)`", r"\1", t)                            # инлайн-код

    # Спец-произношение — ДО всего остального: правила смотрят на исходное написание
    # (CLAUDE.md, n8n), а любая чистка их разрушит.
    for pattern, repl in PRONOUNCE:
        t = re.sub(pattern, repl, t)

    # Пути и имена файлов — тоже ДО снятия разметки: иначе `_` из «improve_claude»
    # успевает стать пробелом и разрывает путь на несвязные слова.
    t = re.sub(r"(\w+\.\w{1,4}):\d+", r"\1", t)   # ссылка на строку вслух бесполезна
    # Абсолютный путь опознаём по букве диска, относительный — по расширению.
    # Без этого условия под «путь» попадает безобидное «19/19» и теряет половину.
    t = re.sub(r"[A-Za-z]:[\\/](?:[\w.\-]+[\\/])*[\w.\-]+", _speakable_path, t)
    t = re.sub(r"(?:[\w.\-]+[\\/])+[\w\-]+\.(?:py|js|cjs|mjs|ts|json|md|txt|wav|log|ps1|cmd)\b",
               _speakable_path, t)
    t = re.sub(r"\b[\w\-]+\.(?:py|js|cjs|mjs|ts|json|md|txt|wav|log|ps1|cmd)\b",
               _speakable_path, t)

    t = re.sub(r"[*#>]+", " ", t)                                 # разметка
    t = re.sub(r"^\s*[-•]\s*", "", t, flags=re.MULTILINE)         # маркеры списка
    t = re.sub(r"[\U0001F000-\U0001FAFF←-⇿☀-➿⬀-⯿]", " ", t)      # эмодзи

    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", ". ", t)
    t = re.sub(r"\n", ". ", t)
    t = re.sub(r"\s*\.\s*\.\s*", ". ", t)
    t = t.strip()

    if len(t) > max_chars:
        cut = t[:max_chars]
        # обрезаем по концу предложения, чтобы фраза не обрывалась на полуслове
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[: end + 1] if end > max_chars * 0.5 else cut) + " Дальше — в тексте."
    return t


# --------------------------------------------------------------------------- #
# Синтез
# --------------------------------------------------------------------------- #

def _decode_mp3(data):
    import miniaudio

    decoded = miniaudio.decode(data)
    samples = np.frombuffer(decoded.samples.tobytes(), dtype=np.int16)
    samples = samples.astype(np.float32) / 32768.0
    if decoded.nchannels > 1:
        samples = samples.reshape(-1, decoded.nchannels).mean(axis=1)
    return samples, decoded.sample_rate


# -- ElevenLabs: живой голос, но платный символами -------------------------- #

def _synth_eleven(text, cfg):
    limit = cfg["tts_premium_max_chars"]
    if len(text) > limit:
        # Пишем ФАКТИЧЕСКУЮ длину, а не только порог. Иначе непонятно, случайность
        # это или система: вопрос «почему запасной голос стал основным?» (2026-08-05)
        # родился ровно из этой строки — порог 300 срезал почти каждую реплику,
        # и в логе было не видно, насколько именно она его превысила.
        log(f"[info] реплика {len(text)} симв. > порога {limit} — озвучиваю "
            f"локально, чтобы не жечь лимит ElevenLabs")
        return None
    try:
        import eleven

        voice_key = cfg["eleven_voice"]
        voice_id = eleven.VOICES.get(voice_key, (voice_key, ""))[0]
        model = eleven.MODELS.get(cfg["eleven_model"], cfg["eleven_model"])
        data, key_num = eleven.synthesize(text, voice_id, model, cfg["eleven_settings"])
        log(f"[info] ElevenLabs: голос {voice_key}, ключ #{key_num}, {len(text)} симв.")
        return _decode_mp3(data)
    except Exception as e:  # noqa: BLE001 — нет ключа, кончилась квота, нет сети
        log(f"[warn] ElevenLabs недоступен ({str(e)[:90]})")
        return None


# -- Piper: локальный, мгновенный, безлимитный ------------------------------ #

_PIPER_CACHE = {}


def _synth_piper(text, cfg):
    """
    Загруженная модель кэшируется: первая загрузка ~3 с, дальше синтез 0.4 с
    на фразу. Без кэша каждая реплика начиналась бы с трёхсекундной паузы.
    """
    name = cfg["piper_voice"]
    model_path = BASE_DIR / "tts_models" / f"ru_RU-{name}-medium.onnx"
    if not model_path.exists():
        return None
    try:
        if name not in _PIPER_CACHE:
            from piper import PiperVoice

            _PIPER_CACHE[name] = PiperVoice.load(str(model_path))
        voice = _PIPER_CACHE[name]
        chunks = [c.audio_float_array for c in voice.synthesize(text)]
        if not chunks:
            return None
        return np.concatenate(chunks), voice.config.sample_rate
    except Exception as e:  # noqa: BLE001
        log(f"[warn] Piper недоступен ({str(e)[:90]})")
        return None


# -- edge-tts: бесплатно, но нужна сеть ------------------------------------- #

async def _edge_bytes(text, voice, rate, volume):
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return bytes(buf)


def _synth_edge(text, cfg):
    try:
        mp3 = asyncio.run(
            _edge_bytes(text, cfg["tts_voice"], cfg["tts_rate"], cfg["tts_volume"])
        )
    except Exception as e:  # noqa: BLE001 — нет сети, таймаут, смена API у Microsoft
        log(f"[warn] edge-tts недоступен ({str(e)[:80]})")
        return None
    return _decode_mp3(mp3) if mp3 else None


def speak_sapi(text):
    """Запасной голос: он есть в Windows всегда, интернет не нужен."""
    try:
        import win32com.client

        win32com.client.Dispatch("SAPI.SpVoice").Speak(text)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[error] системный голос тоже недоступен: {e}")
        return False


SYNTHS = {"eleven": _synth_eleven, "piper": _synth_piper, "edge": _synth_edge}


def synthesize(text, cfg):
    """Первый движок каскада, который справился. Возвращает (samples, sr) или None."""
    chain = [cfg["tts_engine"]] if cfg.get("tts_engine") else list(cfg["tts_chain"])
    for name in chain:
        if name == "sapi":
            continue  # у SAPI нет сэмплов, он играет сам — обрабатывается в say()
        fn = SYNTHS.get(name)
        if fn is None:
            continue
        result = fn(text, cfg)
        if result is not None:
            return result
    return None


def say(text, cfg=None, raw=False):
    """
    Произносит текст. Возвращает True, если что-то прозвучало.

    raw=True — не чистить текст (нужно, когда фраза уже подготовлена).
    """
    cfg = cfg or load_tts_config()
    speech = text if raw else clean_for_speech(text, cfg["tts_max_chars"])
    if not speech.strip():
        return False

    if cfg.get("tts_engine") == "sapi":
        return speak_sapi(speech)

    result = synthesize(speech, cfg)
    if result is None:
        return speak_sapi(speech)  # последняя надежда: системный голос

    samples, sr = result
    sd.play(samples, sr, blocking=True)
    return True


def load_tts_config():
    cfg = dict(DEFAULT_TTS)
    cfg.update(load_config())
    for key, value in DEFAULT_TTS.items():
        cfg.setdefault(key, value)
    return cfg
