# -*- coding: utf-8 -*-
"""
Тесты детектора речи. Быстрые: без Groq, без звуковой карты, без реального времени.

Проверяют ровно то, обо что споткнулись в кафе 2026-07-31: автостоп по паузе не
срабатывал, потому что фон был выше однажды снятого порога, и запись шла 300 с
вместо 11. Сценарии подаются через подставной микрофон с синтетическим гулом.

Ключевой сценарий — «шум ПОСЛЕ калибровки»: именно он воспроизводит настоящий отказ
(порог снят в момент, когда микрофон ещё молчал). Постоянный гул с самого начала
сломанный VAD как раз переживал бы.

Запуск:  python tests/test_vad.py
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from voice_core import AdaptiveVad, Recorder, load_config  # noqa: E402
from voice_once import read_wav  # noqa: E402

SR = 16000
TAIL_SEC = 8.0  # хвост после фразы: на нём и проверяется автостоп

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def base_cfg(fixture, **overrides):
    cfg = load_config()
    cfg.update(
        {
            "fake_mic_wav": str(FIXTURES / fixture),
            "fake_mic_realtime": False,  # логика VAD блочная, от часов не зависит
            "fake_mic_tail_sec": TAIL_SEC,
            "beep": False,
            "wait_for_speech_sec": 10,
        }
    )
    cfg.update(overrides)
    return cfg


def record(fixture, **overrides):
    audio = Recorder(base_cfg(fixture, **overrides)).record_auto()
    return audio, (len(audio) / SR if audio is not None else 0.0)


# --------------------------------------------------------------------------- #
# 1. Пороги: гистерезис и подстройка фона
# --------------------------------------------------------------------------- #

def test_thresholds():
    print("\n=== 1. Пороги AdaptiveVad ===")
    cfg = load_config()

    vad = AdaptiveVad(cfg)
    vad.prime([0.002] * 8)
    check(
        "порог тишины ниже порога старта (гистерезис)",
        vad.stop_threshold < vad.start_threshold,
        f"старт {vad.start_threshold:.4f} > тишина {vad.stop_threshold:.4f}",
    )

    # фон вырос (включилась кофемашина): порог обязан пойти за ним
    quiet = AdaptiveVad(cfg)
    quiet.prime([0.0002] * 8)
    before = quiet.start_threshold
    for _ in range(120):
        quiet.feed(0.012, in_speech=False)
    check(
        "фон переоценивается вверх — порог идёт за обстановкой",
        quiet.start_threshold > before * 2,
        f"{before:.4f} → {quiet.start_threshold:.4f} при гуле 0.0120",
    )

    # ...но не мгновенно, иначе собственная речь задерёт «фон» и оборвёт фразу
    fast = AdaptiveVad(cfg)
    fast.prime([0.0005] * 8)
    for _ in range(10):  # 1 секунда громкой речи
        fast.feed(0.09, in_speech=True)
    check(
        "секунда речи не задирает оценку фона",
        fast.noise < 0.01,
        f"фон {fast.noise:.4f} после 1 с речи 0.0900",
    )

    # вернулась тишина — вниз оценка должна падать сразу, а не «остывать»
    for _ in range(60):
        fast.feed(0.0005, in_speech=False)
    check(
        "падение фона фиксируется мгновенно",
        fast.noise < 0.001,
        f"фон {fast.noise:.4f}",
    )

    # потолок от уровня речи: страховка от обрыва живой фразы в сильном шуме
    loud = AdaptiveVad(cfg)
    loud.prime([0.02] * 8)
    for _ in range(30):
        loud.feed(0.09, in_speech=True)
    check(
        "порог тишины не превышает трети от громкости речи",
        loud.stop_threshold <= loud.speech * cfg["vad_speech_cap"] + 1e-9,
        f"тишина {loud.stop_threshold:.4f} при речи {loud.speech:.4f}",
    )


# --------------------------------------------------------------------------- #
# 2. Автостоп в разных обстановках
# --------------------------------------------------------------------------- #

def test_autostop():
    print("\n=== 2. Автостоп: тихо / гул с начала / гул после калибровки ===")
    fixture = "fixture_terms.wav"
    phrase_sec = len(read_wav(FIXTURES / fixture)) / SR
    limit = phrase_sec + 3.0  # фраза + pre-roll + недорезанный хвост паузы
    print(f"    фраза {phrase_sec:.1f} с, хвост {TAIL_SEC:.0f} с, потолок записи "
          f"{load_config()['auto_max_record_sec']} с")

    cases = [
        ("тихая комната", {}),
        ("гул кафе 0.012 с самого начала", {"fake_mic_noise_rms": 0.012}),
        # главный сценарий отказа: порог снят по тишине, гул пришёл потом
        ("гул 0.012 появился ПОСЛЕ калибровки",
         {"fake_mic_noise_rms": 0.012, "fake_mic_noise_start_sec": 3.0}),
        ("громкий гул 0.020 после калибровки",
         {"fake_mic_noise_rms": 0.020, "fake_mic_noise_start_sec": 3.0}),
    ]

    for name, overrides in cases:
        audio, seconds = record(fixture, **overrides)
        if not check(f"[{name}] речь обнаружена", audio is not None):
            continue
        check(
            f"[{name}] стоп по паузе, а не по хвосту/таймауту",
            seconds < limit,
            f"{seconds:.1f} с (порог {limit:.1f} с)",
        )
        check(
            f"[{name}] фраза не обрезана серединой",
            seconds > phrase_sec * 0.8,
            f"{seconds:.1f} с из {phrase_sec:.1f} с",
        )


# --------------------------------------------------------------------------- #
# 3. Отсутствие речи не должно превращаться в долгую запись
# --------------------------------------------------------------------------- #

def test_no_speech():
    print("\n=== 3. Когда речи нет ===")
    audio, seconds = record("fixture_silence.wav")
    check("тишина — записи нет вообще", audio is None, f"{seconds:.1f} с")

    # Гул может дать ложный старт — это нормально, но он обязан быстро упереться
    # в тишину: 90 секунд шума впустую сжигают лимит Groq и время пользователя.
    audio, seconds = record("fixture_silence.wav", fake_mic_noise_rms=0.012)
    check(
        "ложный старт от гула не превращается в долгую запись",
        audio is None or seconds < 8.0,
        "записи нет" if audio is None else f"{seconds:.1f} с",
    )


def main():
    test_thresholds()
    test_autostop()
    test_no_speech()
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    for name, _, detail in failed:
        print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
