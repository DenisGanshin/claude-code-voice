# -*- coding: utf-8 -*-
"""
Тесты выбора источника записи и тревоги «в записи нет речи».

Что именно проверяется — инцидент 2026-08-07. Подключённая Bluetooth-гарнитура
лежала в другой комнате, человек говорил в ноутбук, а расшифровка выдала
«Добро пожаловать в Казахстан!» — Whisper на тишине не молчит, он ДОДУМЫВАЕТ
текст, и результат выглядит как обычная фраза, а не как ошибка.

Главный тест здесь — `далёкий гул проигрывает близкой речи`. Он доказывает,
что метрика меряет РАЗБОРЧИВОСТЬ, а не громкость: далёкий источник даёт
низкочастотный гул, который по общему RMS может обойти близкий микрофон, и
выбор «по громкости» увёл бы нас ровно в тот же провал. Без этого теста
`speech_score` неотличима от обычного RMS — на «речи против тишины» они
согласны обе.

Микрофон НЕ открывается: все сигналы синтетические.

Запуск:  python tests/test_source_choice.py
"""

import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from voice_core import (  # noqa: E402
    QUIET_RECORDING_MARK,
    SPEECH_ALARM_SCORE,
    Recorder,
    audio_report,
    load_config,
    process_audio,
    speech_score,
    trim_silence,
)
import voice_core  # noqa: E402

SR = 16000
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# Синтетические сигналы
# --------------------------------------------------------------------------- #

def voiced(sec=3.0, f0=120.0, amp=0.30, sr=SR):
    """
    Голос вблизи: основной тон + гармоники, усиленные в формантных областях.

    Не «звук вообще», а именно сигнал с энергией в 300-3400 Гц — это то, чем
    различаются слова. Форманты взяты типовые для мужского голоса (700/1200/2600 Гц).
    """
    t = np.arange(int(sec * sr)) / sr
    sig = np.zeros_like(t)
    for k in range(1, 30):
        f = f0 * k
        if f > 3800:
            break
        # усиление около формант, спад вдали от них
        gain = sum(np.exp(-((f - c) ** 2) / (2 * 250.0 ** 2)) for c in (700, 1200, 2600))
        sig += (gain + 0.05) * np.sin(2 * np.pi * f * t + k)
    sig /= np.max(np.abs(sig)) + 1e-12
    # огибающая слогов: без пауз это гудок, а не речь
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)
    return (sig * env * amp).astype(np.float32)


def distant_rumble(sec=3.0, amp=0.55, sr=SR):
    """
    Тот же голос «через стену»: остался гул основного тона, форманты срезаны.

    Именно это записал микрофон 07.08 — 86% энергии ниже 300 Гц. Амплитуда
    НАМЕРЕННО выше, чем у `voiced`: сигнал громче по RMS и всё равно бесполезен.
    """
    t = np.arange(int(sec * sr)) / sr
    sig = np.zeros_like(t)
    for k, f in enumerate((110.0, 165.0, 220.0), start=1):
        sig += np.sin(2 * np.pi * f * t + k) / k
    sig /= np.max(np.abs(sig)) + 1e-12
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)
    return (sig * env * amp).astype(np.float32)


def near_silence(sec=3.0, amp=2e-5, sr=SR):
    """Микрофон, который ничего не слышит: шум на уровне 0.00002 — как 07.08."""
    rng = np.random.default_rng(3)
    return rng.normal(0, amp, int(sec * sr)).astype(np.float32)


# --------------------------------------------------------------------------- #
# 1. Метрика меряет разборчивость, а не громкость
# --------------------------------------------------------------------------- #

near = voiced()
far = distant_rumble()
quiet = near_silence()

near_rms = float(np.sqrt(np.mean(near ** 2)))
far_rms = float(np.sqrt(np.mean(far ** 2)))
near_score = speech_score(near)[0]
far_score = speech_score(far)[0]

check(
    "подготовка: далёкий гул ГРОМЧЕ близкой речи по RMS",
    far_rms > near_rms,
    f"гул {far_rms:.4f} > речь {near_rms:.4f} — иначе тест ниже ничего не доказывает",
)
check(
    "далёкий гул проигрывает близкой речи по речевой метрике",
    near_score > far_score * 3,
    f"речь {near_score:.5f} против гула {far_score:.5f}",
)
check(
    "выбор по громкости дал бы ОБРАТНЫЙ ответ",
    far_rms > near_rms and near_score > far_score,
    "это и есть причина, по которой метрика не может быть RMS",
)

# --------------------------------------------------------------------------- #
# 2. Порог тревоги
# --------------------------------------------------------------------------- #

check("речь выше порога тревоги", speech_score(near)[0] > SPEECH_ALARM_SCORE,
      f"{speech_score(near)[0]:.5f} > {SPEECH_ALARM_SCORE}")
check("почти-тишина ниже порога тревоги", speech_score(quiet)[0] < SPEECH_ALARM_SCORE,
      f"{speech_score(quiet)[0]:.7f} < {SPEECH_ALARM_SCORE}")
check("отчёт помечает пустую запись", audio_report(quiet)["quiet"] is True)
check("отчёт НЕ помечает нормальную запись", audio_report(near)["quiet"] is False)
check("пустой вход не роняет метрику", audio_report(None)["quiet"] is True)
check("короче одного блока не роняет метрику", speech_score(np.zeros(10)) == (0.0, 0.0))

# --------------------------------------------------------------------------- #
# 3. Обрезка тишины — иначе сравнение источников нечестно
# --------------------------------------------------------------------------- #

padded = np.concatenate([np.zeros(SR * 2, dtype=np.float32), near,
                         np.zeros(SR * 3, dtype=np.float32)])
trimmed = trim_silence(padded)
check("тишина по краям срезается", len(trimmed) < len(padded) - SR,
      f"{len(padded)/SR:.1f} с -> {len(trimmed)/SR:.1f} с")
check("сама речь при обрезке не теряется",
      speech_score(trimmed)[0] > speech_score(near)[0] * 0.8,
      f"{speech_score(trimmed)[0]:.5f} против {speech_score(near)[0]:.5f}")
check("запись без речи не режется вслепую", len(trim_silence(quiet)) == len(quiet))

# Без обрезки длинный хвост тишины ронял бы перцентиль — то есть давал бы фору
# уже подрезанному ведущему. Проверяем, что эффект реален, а не выдуман.
check(
    "хвост тишины занижает метрику (значит выравнивать входы обязательно)",
    speech_score(padded)[0] < speech_score(near)[0],
    f"с хвостом {speech_score(padded)[0]:.5f} против {speech_score(near)[0]:.5f}",
)

# --------------------------------------------------------------------------- #
# 4. Выбор источника в _finalize
# --------------------------------------------------------------------------- #

def recorder_with(extra):
    cfg = load_config()
    cfg.update({"beep": False, "show_overlay": False, "multi_source": True})
    rec = Recorder(cfg)
    rec.native_sr = SR
    rec.device_name = "ВЕДУЩИЙ"
    rec._collect_extra = lambda: extra  # noqa: SLF001 — подменяем источник данных, не логику
    return rec


rec = recorder_with([("ДУБЛЬ-ноутбук", near)])
out = rec._finalize(quiet)  # noqa: SLF001
check("дубль с речью побеждает пустого ведущего", rec.chosen_name == "ДУБЛЬ-ноутбук")
check("возвращается звук победителя", np.array_equal(out, trim_silence(near)))
check("результат победителя не помечен пустым", rec.last_report["quiet"] is False)

rec = recorder_with([("ДУБЛЬ-гарнитура", quiet)])
rec._finalize(near)  # noqa: SLF001
check("ведущий с речью не уступает пустому дублю", rec.chosen_name == "ВЕДУЩИЙ")

rec = recorder_with([("ДУБЛЬ-далеко", far)])
rec._finalize(near)  # noqa: SLF001
check("ведущий с речью не уступает громкому далёкому гулу", rec.chosen_name == "ВЕДУЩИЙ")

rec = recorder_with([("ДУБЛЬ-пусто", quiet)])
rec._finalize(quiet)  # noqa: SLF001
check("когда речи нет нигде — запись помечается пустой", rec.last_report["quiet"] is True)

rec = recorder_with([])
check("без дублей ведущий возвращается как раньше",
      np.array_equal(rec._finalize(near), trim_silence(near)))  # noqa: SLF001
check("совсем пустой вход даёт None", recorder_with([])._finalize(None) is None)  # noqa: SLF001

# --------------------------------------------------------------------------- #
# 5. Пометка результата в process_audio
# --------------------------------------------------------------------------- #

_orig_transcribe, _orig_normalize = voice_core.transcribe, voice_core.normalize
voice_core.transcribe = lambda *a, **k: "Добро пожаловать в Казахстан!"
voice_core.normalize = lambda text, cfg, keys: (text, "нормализовано")
try:
    cfg = load_config()
    final, _raw, note = process_audio(quiet, cfg, ["key"], report=audio_report(quiet))
    check("текст с пустой записи помечен прямо в тексте",
          bool(final) and final.startswith(QUIET_RECORDING_MARK), f"«{(final or '')[:60]}»")
    check("пометка объяснена в статусе", "почти пустая" in note)

    final_ok, _raw, note_ok = process_audio(near, cfg, ["key"], report=audio_report(near))
    check("нормальная запись НЕ помечается",
          bool(final_ok) and not final_ok.startswith(QUIET_RECORDING_MARK))

    # Совместимость: старые вызовы без report не должны падать и не должны помечать
    final_no_rep, _r, _n = process_audio(near, cfg, ["key"])
    check("вызов без report работает по-старому",
          bool(final_no_rep) and not final_no_rep.startswith(QUIET_RECORDING_MARK))

    # Известная галлюцинация из списка по-прежнему отбрасывается целиком
    voice_core.transcribe = lambda *a, **k: "Продолжение следует..."
    final_h, _r, note_h = process_audio(near, cfg, ["key"], report=audio_report(near))
    check("галлюцинация из списка по-прежнему отбрасывается", final_h is None, note_h)
finally:
    voice_core.transcribe, voice_core.normalize = _orig_transcribe, _orig_normalize

# --------------------------------------------------------------------------- #
# 6. Порог против РЕАЛЬНЫХ замеров 2026-08-07
# --------------------------------------------------------------------------- #
# Синтетика доказывает, что метрика устроена правильно, но не то, что порог стоит
# в нужном месте. Здесь зафиксированы измеренные скоры настоящих записей и
# прочитанный (не выведенный из уровня!) результат их расшифровки. Записи живут
# сутки и в репозиторий не попадают — числа переживают их намеренно.
#
# Тест держит порог с ДВУХ сторон: подняв его, сломаешь удачные записи; опустив —
# пропустишь выдуманный текст. Первая версия порога (0.002) провалила бы вторую
# половину этой таблицы, и обнаружилось это только расшифровкой.

REAL_MEASUREMENTS = [
    (0.0687, True,  "23:08:47 нормальный текст"),
    (0.0378, True,  "23:25:44 нормальный текст"),
    (0.0345, True,  "23:22:40 нормальный текст"),
    (0.0255, True,  "23:29:33 нормальный текст, самая тихая из удачных"),
    (0.0026, False, "23:32:54 «я уже говорила с этой пиксией» — выдумано"),
    (0.0024, False, "23:31:47 «добрый день, я хочу…» — выдумано"),
    (0.00017, False, "23:10:46 абракадабра"),
    (0.00017, False, "23:10:12 речь не распознана"),
    (0.0,     False, "23:11:29 «Добро пожаловать в Казахстан!»"),
]

for score, should_pass, label in REAL_MEASUREMENTS:
    passes = score >= SPEECH_ALARM_SCORE
    check(f"порог верен для: {label}", passes == should_pass,
          f"скор {score} против порога {SPEECH_ALARM_SCORE}")

worst_good = min(s for s, ok, _ in REAL_MEASUREMENTS if ok)
best_bad = max(s for s, ok, _ in REAL_MEASUREMENTS if not ok)
check("порог не прижат к краю данных",
      worst_good / SPEECH_ALARM_SCORE >= 2.5 and SPEECH_ALARM_SCORE / best_bad >= 2.5,
      f"запас вверх ×{worst_good / SPEECH_ALARM_SCORE:.1f}, вниз ×{SPEECH_ALARM_SCORE / best_bad:.1f}")

print()
bad = [r for r in results if not r[1]]
print(f"ИТОГО: {len(results) - len(bad)}/{len(results)}")
if bad:
    for name, _ok, detail in bad:
        print(f"  ПРОВАЛ: {name} — {detail}")
sys.exit(1 if bad else 0)
