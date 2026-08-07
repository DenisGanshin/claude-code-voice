# -*- coding: utf-8 -*-
"""
ЖИВОЙ тест параллельной записи: открывает НАСТОЯЩИЙ микрофон.

⚠ Микрофон реально открывается на ~4 секунды. Запускать только с разрешения.
Ничего не отправляется в облако: записанное остаётся в памяти процесса.

Что доказывает. `test_source_choice.py` проверяет логику выбора на синтетике —
там дубли подставлены руками. Здесь проверяется то, чего синтетика не покажет:
что второй поток на живой звуковой карте вообще открывается, что callback его
наполняет, что дубль пишет ТОТ ЖЕ звук и что сведение не путает источники.

Второго физического микрофона на машине может не быть (07.08 гарнитуру
отключили), поэтому дубль берётся с того же устройства через другой звуковой
API. Для механики это полноценный второй поток: своё открытие, свой буфер,
свой темп. Одинаковость звука здесь не недостаток, а проверка — два потока с
одного микрофона ОБЯЗАНЫ дать близкий уровень, и расхождение означало бы, что
дубль пишет не то, что мы думаем.

Запуск:  python tests/test_multi_source_live.py
"""

import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from voice_core import (  # noqa: E402
    API_RANK,
    Recorder,
    audio_report,
    format_audio_report,
    list_input_devices,
    load_config,
)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def second_path_to_same_mic(lead_name):
    """Тот же микрофон через другой звуковой API — второй независимый поток."""
    lead_key = (lead_name or "").lower()[:25]
    for d in sorted(list_input_devices(), key=lambda d: API_RANK.get(d["api"], 9)):
        if "WDM-KS" in d["api"]:
            continue
        if d["name"].lower()[:25] != lead_key.split(" [")[0][:25]:
            continue
        if f"{d['name']} [{d['api']}]" != lead_name:
            return d["index"], f"{d['name']} [{d['api']}]"
    return None


print("⚠ Сейчас на 4 секунды откроется НАСТОЯЩИЙ микрофон. Звука не будет.")
print()

cfg = load_config()
cfg.update({"beep": False, "show_overlay": False, "multi_source": True})

rec = Recorder(cfg)
rec._open()  # noqa: SLF001 — тестируем внутренний механизм, а не сценарий целиком
lead_name = rec.device_name
check("ведущий микрофон открыт", rec._stream is not None, lead_name)  # noqa: SLF001

# Если второго физического микрофона нет — подменяем список кандидатов на
# «тот же микрофон другим API». Подменяем ИМЕННО список, а не логику открытия:
# проверяемый код остаётся тем самым, что работает в бою.
alt = second_path_to_same_mic(lead_name)
if not rec._extra and alt:  # noqa: SLF001
    rec._extra_stop.set()  # noqa: SLF001
    time.sleep(0.1)
    rec._extra_candidates = lambda _alt=alt: [_alt]  # noqa: SLF001
    rec._start_extra_sources()  # noqa: SLF001

time.sleep(1.5)  # дубль открывается в фоне: даём ему время подняться
with rec._extra_lock:  # noqa: SLF001
    extra_open = len(rec._extra)  # noqa: SLF001
check("дубль поднялся на живом железе", extra_open >= 1,
      f"открыто дублей: {extra_open}"
      + ("" if alt or extra_open else " — второго микрофона в системе нет"))

# Пишем обоими путями одновременно
lead_blocks = []
t0 = time.time()
while time.time() - t0 < 2.5:
    lead_blocks.append(rec._read_block())  # noqa: SLF001
lead_audio = np.concatenate(lead_blocks)

collected = rec._collect_extra()  # noqa: SLF001
check("дубль что-то записал", bool(collected) and len(collected[0][1]) > 0,
      f"{len(collected)} источник(ов)")

if collected:
    dub_name, dub_audio = collected[0]
    lead_rep, dub_rep = audio_report(lead_audio), audio_report(dub_audio)
    print(f"       ведущий «{lead_name}»: {format_audio_report(lead_rep)}")
    print(f"       дубль   «{dub_name}»: {format_audio_report(dub_rep)}")

    check("длительности сопоставимы", abs(lead_rep["sec"] - dub_rep["sec"]) < 2.0,
          f"{lead_rep['sec']:.1f} с против {dub_rep['sec']:.1f} с")
    # Тот же воздух через два API: уровни обязаны быть в одном порядке.
    lo, hi = sorted((lead_rep["rms"] + 1e-9, dub_rep["rms"] + 1e-9))
    check("оба потока слышат ОДИН И ТОТ ЖЕ звук", hi / lo < 10,
          f"уровни различаются в {hi / lo:.1f} раза")
    check("дубль не оказался цифровой тишиной", dub_rep["peak"] > 0,
          f"пик {dub_rep['peak']:.5f}")

rec._close()  # noqa: SLF001
check("потоки закрыты", rec._stream is None and not rec._extra)  # noqa: SLF001

# Проверяем, что в реальной системе список кандидатов не выдумывает лишнего:
# мапперы («Переназначение звуковых устр.») отдают поток устройства по умолчанию,
# то есть дубль ведущего, и писать их бессмысленно.
rec2 = Recorder(cfg)
cands = rec2._extra_candidates()  # noqa: SLF001
print(f"       кандидаты в дубли сейчас: {[n for _i, n in cands] or 'нет (один микрофон в системе)'}")
check("в кандидаты не попал системный маппер",
      not any("переназначение" in n.lower() or "первичный" in n.lower() for _i, n in cands))
check("в кандидаты не попал сам ведущий",
      not any(n.lower()[:25] == (rec2.device_name or "").lower()[:25] for _i, n in cands))

print()
bad = [r for r in results if not r[1]]
print(f"ИТОГО: {len(results) - len(bad)}/{len(results)}")
for name, _ok, detail in bad:
    print(f"  ПРОВАЛ: {name} — {detail}")
sys.exit(1 if bad else 0)
