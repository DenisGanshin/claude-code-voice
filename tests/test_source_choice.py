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
# 2. Вердикт «речи в записи нет» — по безразмерным признакам
# --------------------------------------------------------------------------- #
# Каждый признак ловит СВОЙ вид отказа, и по отдельности ни один не достаточен:
# у далёкого гула контраст высокий (модуляция речи сохранилась), у оборванного
# потока форманты в норме (в нулях «нет» ни одной частоты больше, чем другой).
# Поэтому проверяем, что каждый признак срабатывает именно на своём случае.

check("речь проходит вердикт", audio_report(near)["quiet"] is False,
      f"контраст {audio_report(near)['contrast']:.1f}, "
      f"форманты {audio_report(near)['formants']:.2f}")

rep_far = audio_report(far)
check("далёкий гул забракован по формантам", rep_far["quiet"] is True
      and rep_far["formants"] < voice_core.MIN_FORMANT_SHARE,
      f"форманты {rep_far['formants']:.2f} — гул громкий, но нераспознаваемый")

rep_quiet = audio_report(quiet)
check("микрофон, который не слышит, забракован", rep_quiet["quiet"] is True,
      voice_core.quiet_reason(rep_quiet))

# Bluetooth, у которого не поднялся профиль микрофона: ровные нули вместо звука.
# Замер 07.08 — 98% нулей, именно этот случай и стоил человеку задачи.
dropped = np.zeros(SR * 3, dtype=np.float32)
_rng = np.random.default_rng(11)
_idx = _rng.choice(len(dropped), size=int(len(dropped) * 0.02), replace=False)
dropped[_idx] = _rng.normal(0, 3e-4, len(_idx))
rep_dropped = audio_report(dropped)
check("оборванный поток забракован по доле нулей", rep_dropped["quiet"] is True
      and rep_dropped["zeros"] > voice_core.MAX_ZERO_SHARE,
      f"нулей {rep_dropped['zeros'] * 100:.0f}%")
check("причина отказа называется словами, а не числом",
      "поток оборван" in voice_core.quiet_reason(rep_dropped),
      voice_core.quiet_reason(rep_dropped))

check("пустой вход не роняет метрику", audio_report(None)["quiet"] is True)
check("короче одного блока не роняет метрику", speech_score(np.zeros(10)) == (0.0, 0.0))

# Абсолютный порог по умолчанию не применяется: он зависит от чувствительности
# микрофона (см. раздел 6), поэтому включается только вручную.
check("абсолютный порог по умолчанию выключен", voice_core.alarm_score({}) == 0.0)
check("заданный вручную порог добавляет строгости",
      audio_report(near, alarm=1.0)["quiet"] is True,
      "порог выше любого реального уровня — запись обязана быть забракована")
check("мусор в ключе не включает порог случайно",
      voice_core.alarm_score({"speech_alarm_score": "низко"}) == 0.0)

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
    # Необязательный порог по уровню гасим явно, а не наследуем из config.json:
    # на машине, где тест запустили, он может быть задан — и тогда часть проверок
    # пройдёт по чужой настройке, а не по проверяемой логике.
    cfg.update({"beep": False, "show_overlay": False, "multi_source": True,
                "speech_alarm_score": 0})
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

    # Свой порог по абсолютному уровню — дело хозяина машины: кто хочет строже,
    # ставит его руками. Проверяем, что ключ читается и доходит до вердикта.
    voice_core.transcribe = lambda *a, **k: "Добро пожаловать в Казахстан!"
    check("порог берётся из конфигурации",
          voice_core.alarm_score({"speech_alarm_score": 0.004}) == 0.004)
    strict = process_audio(near, cfg, ["key"],
                           report=audio_report(near, alarm=1.0))[0]
    check("заданный порог помечает даже разборчивую запись",
          bool(strict) and strict.startswith(QUIET_RECORDING_MARK),
          "порог выше любого реального уровня — это и проверяем")
finally:
    voice_core.transcribe, voice_core.normalize = _orig_transcribe, _orig_normalize

# --------------------------------------------------------------------------- #
# 6. Калибровка против РЕАЛЬНЫХ замеров 2026-08-07 и 2026-08-11
# --------------------------------------------------------------------------- #
# Синтетика доказывает, что признаки устроены правильно, но не то, что границы
# стоят в нужном месте. Здесь зафиксированы измерения настоящих записей и
# прочитанный (не выведенный из уровня!) результат их расшифровки. Записи живут
# сутки и в репозиторий не попадают — числа переживают их намеренно.
#
# История. Сначала вердикт ставился по абсолютному уровню речи: 0.008, потом
# 0.004. Замер 11.08 на второй машине (тихий встроенный микрофон) показал, что
# так нельзя ни при каком числе — нормальная разборчивая речь опускается там до
# 0.0020, тогда как ВЫДУМАННЫЙ текст на гарнитуре давал 0.0026. Порог 0.008
# помечал каждую диктовку (25 подряд, все ложные), 0.004 — каждую четвёртую.

# Абсолютный уровень речи на РЕАЛЬНЫХ записях двух машин, вердикт каждой прочитан
# в расшифровке, а не выведен из уровня:
ABSOLUTE_LEVELS = [
    (0.0687, True,  "07.08 гарнитура: нормальный текст"),
    (0.0255, True,  "07.08 гарнитура: нормальный текст, самая тихая из удачных"),
    (0.0026, False, "07.08 гарнитура: «я уже говорила с этой пиксией» — ВЫДУМАНО"),
    (0.0024, False, "07.08 гарнитура: «добрый день, я хочу…» — ВЫДУМАНО"),
    (0.0089, True,  "11.08 встроенный: нормальный текст"),
    (0.00285, True, "11.08 встроенный: «По поводу пункта А…» — нормальный текст"),
    (0.00220, True, "11.08 встроенный: «Нужно разобраться…» — нормальный текст"),
    (0.00200, True, "11.08 встроенный: «Правильно ли я понимаю…» — САМАЯ ТИХАЯ удачная"),
]

worst_good = min(s for s, ok, _ in ABSOLUTE_LEVELS if ok)
best_bad = max(s for s, ok, _ in ABSOLUTE_LEVELS if not ok)

# ГЛАВНАЯ проверка этого раздела: множества пересекаются. Самая тихая удачная
# запись (0.0020 на встроенном микрофоне) ТИШЕ, чем выдуманный текст на гарнитуре
# (0.0026) — значит абсолютного порога, верного для обоих, не существует, и
# возвращать его нельзя. Проверка стоит здесь как памятник: пока она красная,
# никакое одно число не годится в вердикт.
check("абсолютный уровень НЕ разделяет случаи — порога по нему не существует",
      worst_good < best_bad,
      f"самая тихая удачная {worst_good} < самой громкой выдуманной {best_bad}")

# Безразмерные признаки на тех же живых записях 11.08 (31 диктовка, все
# расшифрованы верно) против четырёх видов отказа. Здесь разделение есть, и с
# запасом — именно поэтому вердикт строится на них.
LIVE_RANGES = {"contrast": 5.4, "formants": 0.13, "zeros": 0.19}
FAILURE_CASES = {"шум без модуляции": ("contrast", 1.2),
                 "гул без формант": ("formants", 0.00),
                 "оборванный поток": ("zeros", 0.99)}

check("контраст: живые записи выше минимума с запасом",
      LIVE_RANGES["contrast"] / voice_core.MIN_MODULATION_CONTRAST >= 2.0,
      f"живые ≥{LIVE_RANGES['contrast']} при минимуме "
      f"{voice_core.MIN_MODULATION_CONTRAST}")
check("форманты: живые записи выше минимума с запасом",
      LIVE_RANGES["formants"] / voice_core.MIN_FORMANT_SHARE >= 2.0,
      f"живые ≥{LIVE_RANGES['formants']} при минимуме "
      f"{voice_core.MIN_FORMANT_SHARE}")
check("нули: живые записи ниже предела с запасом",
      voice_core.MAX_ZERO_SHARE / LIVE_RANGES["zeros"] >= 2.0,
      f"живые ≤{LIVE_RANGES['zeros']} при пределе {voice_core.MAX_ZERO_SHARE}")

check("каждый вид отказа отсекается своим признаком с запасом",
      (voice_core.MIN_MODULATION_CONTRAST / 1.2 >= 2.0
       and voice_core.MAX_ZERO_SHARE < 0.99),
      "шум 1.2 против минимума контраста; нули 0.99 против предела")

print()
bad = [r for r in results if not r[1]]
print(f"ИТОГО: {len(results) - len(bad)}/{len(results)}")
if bad:
    for name, _ok, detail in bad:
        print(f"  ПРОВАЛ: {name} — {detail}")
sys.exit(1 if bad else 0)
