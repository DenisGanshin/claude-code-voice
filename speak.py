# -*- coding: utf-8 -*-
"""
Сказать текст вслух.

    python speak.py "Тесты прошли, 19 из 19."
    python speak.py --stdin < answer.txt
    python speak.py --voice female "Готово"
    python speak.py --dry "Проверить, что именно будет произнесено"

Диагностика идёт в stderr, в stdout — ничего (кроме --dry): вывод скрипта попадает
в контекст Claude, и дублировать там уже сказанное незачем.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from speak_core import VOICES, clean_for_speech, load_tts_config, say  # noqa: E402
from voice_core import log  # noqa: E402


def show_quota(cfg):
    """
    Остаток символов ElevenLabs и во что он превращается на текущем пороге.

    Нужен потому, что подмена голоса на запасной выглядит одинаково в двух совсем
    разных случаях: реплика длиннее порога — или квота кончилась. Отличить их на
    слух нельзя, а решение («поднять порог» против «платить за тариф») —
    противоположное.
    """
    import eleven

    rows = eleven.balances()
    if not rows:
        log("[error] ELEVENLABS_API_KEY не найден — премиум-голос недоступен вовсе")
        return 2
    limit = cfg["tts_premium_max_chars"]
    total_left = 0
    for num, used, cap, note in rows:
        if cap is None:
            print(f"ключ #{num}: {note or 'ответа нет'}")
            continue
        left = cap - used
        total_left += left
        print(f"ключ #{num}: осталось {left} из {cap} символов")
    if total_left:
        print(f"итого {total_left} симв. ≈ {total_left // limit} реплик "
              f"по текущему порогу {limit} симв.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", help="что сказать")
    ap.add_argument("--stdin", action="store_true", help="взять текст из stdin")
    ap.add_argument("--voice", choices=sorted(VOICES), help="male | female")
    ap.add_argument("--engine", choices=("eleven", "piper", "edge", "sapi"),
                    help="перекрыть движок (по умолчанию каскад из config.json)")
    ap.add_argument("--rate", help="скорость речи, например +15%% или -10%%")
    ap.add_argument("--dry", action="store_true", help="не произносить, показать текст")
    ap.add_argument("--quota", action="store_true",
                    help="остаток символов ElevenLabs (GET, символы не тратит)")
    args = ap.parse_args()

    if args.quota:
        return show_quota(load_tts_config())

    text = sys.stdin.read() if args.stdin else " ".join(args.text)
    if not text.strip():
        log("[error] нечего произносить")
        return 2

    cfg = load_tts_config()
    if args.voice:
        cfg["tts_voice"] = VOICES[args.voice]
    if args.engine:
        cfg["tts_engine"] = args.engine
    if args.rate:
        cfg["tts_rate"] = args.rate

    if args.dry:
        print(clean_for_speech(text, cfg["tts_max_chars"]))
        return 0

    return 0 if say(text, cfg) else 1


if __name__ == "__main__":
    sys.exit(main())
