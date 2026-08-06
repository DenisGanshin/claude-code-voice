# -*- coding: utf-8 -*-
"""
Лаборатория голосов ElevenLabs: подобрать голос, модель и настройки на слух.

Бесплатный тариф — 10 000 символов в месяц на ключ, и переслушивание способно
спалить их за вечер. Поэтому:
  * каждый синтез кэшируется в _voice_samples/ и повторно НЕ оплачивается;
  * --plan показывает состав прогона и расход, ничего не тратя;
  * --replay слушает только из кэша, вообще без обращений к API;
  * остаток спрашивается у всех ключей каскада до и после прогона.

Этапы сужают выбор, а не перебирают всё подряд:
  A — 5 отобранных голосов + 3 запасных, одна реплика. Выбираем голос.
  B — победитель A на трёх моделях. Выбираем модель (turbo/flash вдвое дешевле).
  C — победитель B с тремя уровнями живости. Выбираем stability.

Запуск:
    python voice_lab.py --plan                     # план и расход, без трат
    python voice_lab.py --stage a                  # синтез + прослушивание
    python voice_lab.py --stage a --no-play        # только скачать
    python voice_lab.py --replay a                 # переслушать из кэша, бесплатно
    python voice_lab.py --stage b --voice artem
    python voice_lab.py --stage c --voice artem --phrase "bad news"
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

# Консоль Windows по умолчанию cp866/cp1251 и роняет вывод на первом же тире.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eleven  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "_voice_samples"

# Ещё не слышанные претенденты. George, River, Alice, Sarah намеренно исключены —
# они уже звучали; добавить их в прогон можно ключом --voices.
STAGE_A = ["brian", "eric", "daniel", "bill", "chris", "matilda", "jessica", "bella"]

SETTINGS = [
    # stability ниже — живее и эмоциональнее, но от запуска к запуску нестабильнее
    ("живо",   {"stability": 0.30, "similarity_boost": 0.75, "style": 0.15}),
    ("средне", {"stability": 0.50, "similarity_boost": 0.75, "style": 0.00}),
    ("ровно",  {"stability": 0.70, "similarity_boost": 0.80, "style": 0.00}),
]

# Реальные реплики ассистента: короткая, вопрос, отчёт с числами, плохая новость.
# Интонационно это очень разные задачи, и голоса расходятся именно на них.
PHRASES = {
    "short":    "Готово. Все тесты прошли.",
    "question": "Нашёл две развилки. Ставим семидесятую модель или хватит восьмой?",
    "report":   "Починил детектор речи. В кафе запись шла триста секунд вместо десяти: "
                "порог тишины считался один раз, на старте, и больше не пересматривался.",
    "bad news": "Прод упал. Вебхук отдаёт ошибку с двух часов ночи, сообщения копятся. "
                "Нужно решение: откатывать или чинить на месте?",
}


def sample_path(voice_key, model, text, tag):
    """Имя кодирует ВСЕ параметры — иначе кэш подсунет сэмпл от других настроек."""
    return SAMPLES / f"{voice_key}_{model}_{tag}_{abs(hash(text)) % 10**8}.mp3"


def play(path):
    import miniaudio

    d = miniaudio.decode(path.read_bytes())
    a = np.frombuffer(d.samples.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
    if d.nchannels > 1:
        a = a.reshape(-1, d.nchannels).mean(axis=1)
    sd.play(a, d.sample_rate, blocking=True)


def build_plan(stage, voice_key, phrase_key, voices=None):
    """[(подпись, ключ голоса, текст, модель, настройки, тег)]"""
    plan = []
    if stage == "a":
        text = PHRASES[phrase_key or "question"]
        for key in (voices or STAGE_A):
            vid, note = eleven.VOICES[key]
            # имя в начале фразы — чтобы на слух было понятно, кто говорит
            plan.append((f"{key} — {note}", key, f"{key.capitalize()}. {text}",
                         eleven.MODELS["quality"], SETTINGS[1][1], "a"))
    elif stage == "b":
        text = PHRASES[phrase_key or "report"]
        for label, model in eleven.MODELS.items():
            plan.append((f"{label} ({model})", voice_key, text, model, SETTINGS[1][1], "b"))
    elif stage == "c":
        text = PHRASES[phrase_key or "bad news"]
        # Голоса чередуются ВНУТРИ уровня живости: так они сравниваются вплотную,
        # а не по памяти через минуту.
        keys = voices or [voice_key]
        for label, settings in SETTINGS:
            for key in keys:
                name = f"{key} — живость «{label}» (stability {settings['stability']})"
                spoken = f"{key.capitalize()}. {text}" if len(keys) > 1 else text
                plan.append((name, key, spoken, eleven.MODELS["quality"],
                             settings, f"c-{label}"))
    return plan


def show_balances():
    rows = eleven.balances()
    if not rows:
        print("[error] ключей ElevenLabs не найдено")
        return 0
    total_left = 0
    for num, used, limit, note in rows:
        if used is None:
            print(f"  ключ #{num}: {note or 'недоступен'}")
            continue
        left = limit - used
        total_left += left
        print(f"  ключ #{num}: осталось {left} из {limit} символов")
    print(f"  ИТОГО доступно: {total_left} символов")
    return total_left


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("a", "b", "c"))
    ap.add_argument("--replay", choices=("a", "b", "c"), help="только из кэша, без трат")
    ap.add_argument("--plan", action="store_true", help="показать план и расход, ничего не делать")
    ap.add_argument("--voice", help="ключ голоса для этапов B и C (например brian)")
    ap.add_argument("--voices", help="свой список голосов для этапа A через запятую")
    ap.add_argument("--phrase", choices=sorted(PHRASES))
    ap.add_argument("--no-play", action="store_true", help="только синтез, без звука")
    ap.add_argument("--pause", type=float, default=0.7)
    args = ap.parse_args()

    stage = args.stage or args.replay or "a"
    if stage == "c" and args.voices:
        args.voice = args.voice or args.voices.split(",")[0].strip()
    if stage in ("b", "c") and not args.voice:
        print(f"[error] для этапов B и C нужен --voice, например: "
              f"--voice {', '.join(list(eleven.VOICES)[:3])}", file=sys.stderr)
        return 2
    if args.voice and args.voice not in eleven.VOICES:
        print(f"[error] неизвестный голос «{args.voice}». Есть: {', '.join(eleven.VOICES)}",
              file=sys.stderr)
        return 2

    custom = [v.strip() for v in args.voices.split(",")] if args.voices else None
    if custom:
        unknown = [v for v in custom if v not in eleven.VOICES]
        if unknown:
            print(f"[error] неизвестные голоса: {', '.join(unknown)}. "
                  f"Есть: {', '.join(eleven.VOICES)}", file=sys.stderr)
            return 2
    plan = build_plan(stage, args.voice, args.phrase, custom)
    to_pay = [p for p in plan if not sample_path(p[1], p[3], p[2], p[5]).exists()]
    chars = sum(len(p[2]) for p in to_pay)

    print(f"Этап {stage.upper()}: {len(plan)} проб, из них новых {len(to_pay)}")
    for label, key, text, model, settings, tag in plan:
        mark = "кэш" if sample_path(key, model, text, tag).exists() else "синтез"
        print(f"  [{mark:6s}] {label}")
    if plan:
        print(f"\nРеплика: «{plan[0][2][:90]}…»")
    print(f"Расход этого прогона: ~{chars} символов"
          + (" (модели turbo/flash тратят вдвое меньше)" if stage == "b" else ""))
    print("Бюджет каскада:")
    show_balances()

    if args.plan:
        print("\n(--plan: ничего не синтезировано и не воспроизведено)")
        return 0

    print()
    SAMPLES.mkdir(exist_ok=True)
    for label, key, text, model, settings, tag in plan:
        path = sample_path(key, model, text, tag)
        if not path.exists():
            if args.replay:
                print(f"[пропуск] {label} — нет в кэше")
                continue
            try:
                data, key_num = eleven.synthesize(text, eleven.VOICES[key][0], model, settings)
            except RuntimeError as e:
                print(f"[error] {label}: {e}", file=sys.stderr)
                continue
            path.write_bytes(data)
            print(f"[новый ключ#{key_num}] {label}", flush=True)
        else:
            print(f"[кэш]   {label}", flush=True)
        if not args.no_play:
            play(path)
            time.sleep(args.pause)

    if not args.replay and to_pay:
        print("\nОстаток после прогона:")
        show_balances()
    return 0


if __name__ == "__main__":
    sys.exit(main())
