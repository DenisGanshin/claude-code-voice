# -*- coding: utf-8 -*-
"""
Одна диктовка: запись до паузы -> расшифровка -> нормализация -> текст в stdout.

Используется слэш-командой /voice в Claude Code. Всё, что попадает в stdout,
Claude читает как задачу, поэтому туда идёт ТОЛЬКО текст; диагностика — в stderr.

Запуск:
    python voice_once.py                 # обычный режим
    python voice_once.py --raw           # без LLM-нормализации
    python voice_once.py --devices       # показать доступные микрофоны
    python voice_once.py --file x.wav    # прогнать готовый WAV (для тестов)
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_core  # noqa: E402  — нужен как модуль: через него ставится флаг QUIET
from voice_core import (  # noqa: E402
    KEYS_FILE,
    Recorder,
    beep,
    list_input_devices,
    load_config,
    load_groq_keys,
    log,
    process_audio,
    resample_to_16k,
    save_recording,
    write_status,
)


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return resample_to_16k(data, sr) if sr != 16000 else data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="без LLM-нормализации")
    ap.add_argument("--devices", action="store_true", help="список микрофонов")
    ap.add_argument("--file", help="прогнать готовый WAV вместо микрофона")
    ap.add_argument("--show-raw", action="store_true", help="показать и сырую расшифровку")
    ap.add_argument("--quiet", action="store_true",
                    help="без диагностики в stderr (режим слэш-команды /voice)")
    args = ap.parse_args()

    if args.quiet:
        voice_core.QUIET = True

    cfg = load_config()
    if args.raw:
        cfg["normalize"] = False

    if args.devices:
        for d in list_input_devices():
            print(f"{d['index']:>3} | {d['api']:<20} | {d['default_sr']:>6} Гц | {d['name']}")
        return 0

    keys = load_groq_keys()
    if not keys:
        log(f"[error] GROQ_API_KEY не найден в {KEYS_FILE}")
        beep(cfg, "error")
        print("(ошибка: не найден ключ Groq — голосовой ввод не настроен)")
        return 2

    if args.file:
        audio = read_wav(args.file)
        log(f"[info] файл {args.file}: {len(audio) / 16000:.1f} с")
    else:
        rec = Recorder(cfg)
        log(f"[info] микрофон: {rec.device_name}")
        write_status(f"старт записи, устройство: {rec.device_name}")
        audio = rec.record_auto(on_ready=lambda: (beep(cfg, "start"), log("[info] ГОВОРИТЕ…")))
        if audio is None:
            beep(cfg, "empty")
            write_status("тишина, записи не было")
            print("(тишина: речь не распознана, повторите /voice)")
            return 1
        beep(cfg, "stop")
        log(f"[info] записано {len(audio) / 16000:.1f} с")
        write_status(f"записано {len(audio) / 16000:.1f} с, распознаю")
        save_recording(audio, cfg, prefix="voice")  # до облака: сбой не должен стоить диктовки

    try:
        final, raw, note = process_audio(audio, cfg, keys)
    except RuntimeError as e:
        log(f"[error] {e}")
        beep(cfg, "error")
        print(f"(ошибка распознавания: {e})")
        return 3

    if not final:
        beep(cfg, "empty")
        write_status("пустой результат")
        print("(тишина: речь не распознана, повторите /voice)")
        return 1

    log(f"[info] статус: {note}")
    write_status(f"готово: {len(final)} симв. ({note})")
    if args.show_raw and raw != final:
        log(f"[raw] {raw}")
    print(final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
