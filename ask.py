# -*- coding: utf-8 -*-
"""
Спросить голосом и услышать ответ — замкнутый цикл.

    python ask.py "Ставим 70b или хватит 8b?"
    python ask.py --options "70b|8b" "Ставим 70b или хватит 8b?"

Произносит вопрос, дожидается конца фразы, включает запись, ловит ответ до паузы
и печатает распознанный текст в stdout. Всё остальное — в stderr.

Зачем ждать конца собственной фразы: микрофон открыт в той же комнате и запишет
свой же голос. Эхоподавление Windows его частично уберёт, но проще не начинать
запись, пока говорим.

Зачем `--options`: спрашивающий знает список допустимых ответов, а распознавание
о нём не знает — и «машинный JSON» приезжает как «машинен.json». Варианты уходят
в подсказку Whisper (контекст) и потом детерминированно сопоставляются с
услышанным. Ответ при этом остаётся свободным: не лёг ни на один вариант —
отдаём расшифровку как есть, а не подгоняем под ближайший.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_core  # noqa: E402
from speak_core import VOICES, load_tts_config, say  # noqa: E402
from voice_core import (  # noqa: E402
    Recorder,
    beep,
    clear_recording_state,
    load_groq_keys,
    log,
    match_option,
    process_audio,
    save_recording,
    set_recording_state,
    write_status,
)

CONFIDENCE_NOTE = {
    "exact": "точно",
    "likely": "похоже",
    "none": "не определён",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+", help="вопрос, который нужно задать вслух")
    ap.add_argument("--voice", choices=sorted(VOICES))
    ap.add_argument("--wait", type=float, default=20.0,
                    help="сколько секунд ждать начала ответа (по умолчанию 20)")
    ap.add_argument("--device", help="подстрока имени микрофона, перекрывает config.json")
    ap.add_argument("--quiet", action="store_true", help="без диагностики в stderr")
    ap.add_argument("--options",
                    help="допустимые ответы через | — уходят в подсказку распознавания "
                         "и сопоставляются с услышанным")
    ap.add_argument("--mute", action="store_true",
                    help="не произносить вопрос вслух (автотесты не должны шуметь)")
    args = ap.parse_args()

    if args.quiet:
        voice_core.QUIET = True

    question = " ".join(args.question)
    options = [o.strip() for o in (args.options or "").split("|") if o.strip()]
    cfg = load_tts_config()
    if args.voice:
        cfg["tts_voice"] = VOICES[args.voice]
    cfg["wait_for_speech_sec"] = args.wait
    if args.device:
        cfg["input_device_preferences"] = [args.device]
    if options:
        cfg["extra_terms"] = options
    mute = args.mute or bool(os.environ.get("VOICE_MUTE"))
    if mute:
        # Режим тишины глушит и сигналы: он существует ради автотестов, а те не
        # должны звучать в наушниках (service-notes/claude-tools.md, 31.07).
        cfg["beep"] = False

    keys = load_groq_keys()
    if not keys:
        log("[error] GROQ_API_KEY не найден — ответ распознать нечем")
        say("Ключ для распознавания не найден, отвечайте текстом.", cfg)
        print("(ошибка: нет ключа Groq — ответьте текстом)")
        return 2

    write_status(f"спрашиваю голосом: {question[:60]}")
    if mute:
        log("[info] режим тишины: вопрос не произносится")
    else:
        say(question, cfg)
        time.sleep(0.25)  # дать затухнуть последнему слогу перед открытием микрофона

    rec = Recorder(cfg)
    log(f"[info] слушаю ответ, микрофон: {rec.device_name}")
    # Капсулу поднимает и гасит сам Recorder — от факта открытия потока. Здесь это
    # особенно важно: вопрос задан голосом, человек мог отойти от экрана и вернуться
    # к уже открытому микрофону, о котором на экране не было бы ни следа.
    audio = rec.record_auto(on_ready=lambda: (beep(cfg, "start"), log("[info] ГОВОРИТЕ…")))
    if audio is None:
        beep(cfg, "empty")
        write_status("ответа не было")
        print("(нет ответа: тишина)")
        return 1

    # Фаза — до сигнала: beep блокирует поток на 140 мс, и капсула мигнула бы.
    recorded = f"записано {len(audio) / 16000:.1f} с"
    set_recording_state(True, phase="stt", note=recorded)
    beep(cfg, "stop")
    write_status(f"ответ записан, {len(audio) / 16000:.1f} с")
    save_recording(audio, cfg, prefix="answer")
    try:
        final, _raw, note = process_audio(
            audio, cfg, keys,
            on_phase=lambda p: set_recording_state(True, phase=p, note=recorded),
        )
    except RuntimeError as e:
        log(f"[error] {e}")
        clear_recording_state()
        print(f"(ошибка распознавания: {e})")
        return 3

    clear_recording_state()
    if not final:
        print("(нет ответа: речь не распознана)")
        return 1

    log(f"[info] статус: {note}")
    write_status(f"ответ получен: {len(final)} симв.")

    if not options:
        print(final)
        return 0

    # Формат из двух строк, а не голый текст: спрашивающему нужно и решение, и то,
    # что человек сказал на самом деле. Уверенность печатается всегда — по ней
    # видно, когда стоит переспросить, а когда ответ лёг на вариант однозначно.
    choice, confidence = match_option(final, options)
    print(f"ВЫБОР: {choice or '—'} ({CONFIDENCE_NOTE[confidence]})")
    print(f"ОТВЕТ: {final}")
    log(f"[info] выбор: {choice!r} ({confidence})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
