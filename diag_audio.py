# -*- coding: utf-8 -*-
"""
Что было в записи: уровень, речь, источник — по сохранённым WAV.

Зачем отдельный инструмент. 2026-08-07 расшифровка выдала «Добро пожаловать в
Казахстан!» вместо продиктованного текста, и чтобы понять, почему, пришлось
писать разовые скрипты: считать спектр, искать частотный потолок, сравнивать
записи между собой. Всё это — один вопрос, который задают каждый раз, когда
расшифровка выглядит странно: «а что вообще попало в микрофон?».

Микрофон НЕ открывается: читаются только уже сохранённые файлы.

    python diag_audio.py                 # все записи из _recordings
    python diag_audio.py файл.wav        # конкретный файл
    python diag_audio.py --spectrum      # + профиль по частотам

Как читать: «речь» — уровень в полосе разборчивости 300-3400 Гц. Ниже 0.002
расшифровывать нечего: что бы ни вернула модель, она это додумала.
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from voice_core import (  # noqa: E402
    SPEECH_ALARM_SCORE,
    audio_report,
    band_block_rms,
    load_config,
    resample_to_16k,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SR = 16000


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        data = data.astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return resample_to_16k(data, sr) if sr != SR else data


def envelope(audio, width=60):
    """Огибающая записи одной строкой — видно, где говорили, а где молчали."""
    brms = band_block_rms(audio)
    if len(brms) == 0:
        return ""
    step = max(1, len(brms) // width)
    chunks = [brms[i:i + step].max() for i in range(0, len(brms), step)]
    top = max(chunks) or 1.0
    marks = " ▁▂▃▄▅▆▇█"
    return "".join(marks[min(len(marks) - 1, int(9 * (c / top) ** 0.5))] for c in chunks)


def spectrum(audio):
    """Профиль по частотам: у далёкого источника энергия уходит вниз, под 300 Гц."""
    n = 1024
    if len(audio) < n * 2:
        return []
    win = audio[: len(audio) // n * n].reshape(-1, n) * np.hanning(n)
    spec = np.abs(np.fft.rfft(win, axis=1)) ** 2
    energy = spec.sum(axis=1)
    loud = spec[energy >= np.percentile(energy, 80)].mean(axis=0)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    total = loud.sum() + 1e-30
    bands = [(0, 300, "гул/основной тон"), (300, 1000, "форманты 1"),
             (1000, 3400, "форманты 2"), (3400, 8000, "верх")]
    return [(lo, hi, name, 100 * loud[(freqs >= lo) & (freqs < hi)].sum() / total)
            for lo, hi, name in bands]


def main():
    ap = argparse.ArgumentParser(description="Что было в записи")
    ap.add_argument("files", nargs="*", help="WAV-файлы (по умолчанию — все из _recordings)")
    ap.add_argument("--spectrum", action="store_true", help="показать профиль по частотам")
    args = ap.parse_args()

    if args.files:
        paths = [Path(f) for f in args.files]
    else:
        folder = BASE / load_config().get("recordings_dir", "_recordings")
        paths = sorted(folder.glob("*.wav"))
        if not paths:
            print(f"В {folder} нет записей. Записи хранятся сутки — возможно, срок вышел.")
            return 1

    print(f"{'файл':<30} {'длина':>7} {'речь':>9} {'блоки':>7} {'уровень':>9}  вердикт")
    print("-" * 92)
    for p in paths:
        if not p.exists():
            print(f"{p.name:<30}  нет такого файла")
            continue
        audio = read_wav(p)
        rep = audio_report(audio)
        verdict = ("ПУСТО — расшифровка будет выдумана" if rep["quiet"]
                   else "речь есть")
        print(f"{p.name:<30} {rep['sec']:6.1f}с {rep['score']:9.5f} "
              f"{rep['voiced'] * 100:6.0f}% {rep['dbfs']:8.0f}дБ  {verdict}")
        print(f"{'':<30} {envelope(audio)}")
        if args.spectrum:
            for lo, hi, name, pct in spectrum(audio):
                bar = "█" * int(pct / 2)
                print(f"{'':<30} {lo:5d}-{hi:5d} Гц {pct:5.1f}% {bar} {name}")
        print()

    print(f"Порог тревоги: {SPEECH_ALARM_SCORE}. Ниже него в записи нет разборчивой речи —")
    print("проверьте, не выбран ли микрофон, до которого голос не доходит.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
