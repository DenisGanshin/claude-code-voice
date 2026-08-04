# -*- coding: utf-8 -*-
"""
Программный e2e голосового ввода: WAV-фикстуры -> Groq -> проверка текста.

Микрофон не участвует — проверяется всё остальное: распознавание, глоссарий,
нормализация, защита от галлюцинаций. Акустический тракт проверяет test_acoustic.py.

Запуск:  python tests/test_pipeline.py
Код возврата 0 — все проверки прошли.
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from voice_core import (  # noqa: E402
    fix_terms_case,
    is_hallucination,
    load_config,
    load_glossary,
    load_groq_keys,
    process_audio,
    resample_to_16k,
)
from voice_once import read_wav  # noqa: E402

import numpy as np  # noqa: E402

FIXTURES = Path(__file__).resolve().parent
PARASITES = ["короче", "как бы", "ну ,", "э-э", "м-м"]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# A. Юнит-проверки без сети
# --------------------------------------------------------------------------- #

def unit_tests():
    print("\n=== A. Юнит-проверки (без сети) ===")
    terms = load_glossary()

    check(
        "fix_terms_case: N8n -> n8n",
        fix_terms_case("Посмотри воркфлоу в N8n сейчас", terms) == "Посмотри воркфлоу в n8n сейчас",
        fix_terms_case("Посмотри воркфлоу в N8n сейчас", terms),
    )
    check(
        "fix_terms_case: словоформа «манеже» -> «Манеже»",
        "Манеже" in fix_terms_case("вебхук в манеже отвалился", terms),
        fix_terms_case("вебхук в манеже отвалился", terms),
    )
    check(
        "fix_terms_case: не ломает заглавную в начале предложения",
        fix_terms_case("Вебхук отвалился.", terms).startswith("Вебхук"),
        fix_terms_case("Вебхук отвалился.", terms),
    )
    check(
        "fix_terms_case: AIRTABLE -> Airtable",
        "Airtable" in fix_terms_case("выгрузи из AIRTABLE контакты", terms),
        fix_terms_case("выгрузи из AIRTABLE контакты", terms),
    )
    check("is_hallucination: пустая строка", is_hallucination(""))
    check("is_hallucination: «Продолжение следует...»", is_hallucination("Продолжение следует..."))
    check(
        "is_hallucination: «Субтитры сделал DimaTorzok»",
        is_hallucination("Субтитры сделал DimaTorzok"),
    )
    check(
        "is_hallucination: нормальный текст не считается мусором",
        not is_hallucination("Посмотри воркфлоу в n8n и сделай коммит"),
    )

    # Ресемпл: синус 1 кГц из 48 кГц в 16 кГц должен сохранить частоту и длительность
    sr = 48000
    t = np.arange(sr) / sr
    sine = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = resample_to_16k(sine, sr)
    peak_bin = int(np.argmax(np.abs(np.fft.rfft(out))))
    peak_hz = peak_bin * 16000 / len(out)
    check(
        "resample_to_16k: длительность сохранена",
        abs(len(out) - 16000) <= 2,
        f"{len(out)} сэмплов",
    )
    check(
        "resample_to_16k: частота тона сохранена",
        abs(peak_hz - 1000) < 20,
        f"пик на {peak_hz:.0f} Гц",
    )


# --------------------------------------------------------------------------- #
# B. E2E через Groq на звуковых фикстурах
# --------------------------------------------------------------------------- #

def ensure_fixtures():
    needed = [
        "fixture_ru.wav",
        "fixture_question.wav",
        "fixture_person.wav",
        "fixture_terms.wav",
        "fixture_silence.wav",
    ]
    if all((FIXTURES / n).exists() for n in needed):
        return
    print("[info] генерирую звуковые фикстуры (SAPI, голос Irina)…", flush=True)
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(FIXTURES / "make_fixtures.ps1")],
        check=True, capture_output=True,
    )


def run_fixture(name, cfg, keys):
    audio = read_wav(FIXTURES / name)
    final, raw, note = process_audio(audio, cfg, keys)
    print(f"\n--- {name} ({len(audio) / 16000:.1f} с)")
    print(f"    сырое:  {raw}")
    print(f"    итог:   {final}")
    return final, raw


def e2e_tests():
    print("\n=== B. E2E через Groq (фикстуры) ===")
    ensure_fixtures()
    cfg = load_config()
    keys = load_groq_keys()
    if not keys:
        check("ключ Groq найден", False, "GROQ_API_KEY отсутствует в keys.local")
        return

    # 1. Императив + термины
    final, raw = run_fixture("fixture_ru.wav", cfg, keys)
    low = (final or "").lower()
    check("ru: термин n8n в нижнем регистре", "n8n" in (final or ""), final)
    check("ru: «Манеж» с заглавной", "Манеж" in (final or ""))
    check("ru: Railway распознан", "railway" in low)
    check(
        "ru: императив сохранён (посмотри/сделай, не «посмотрим»)",
        ("посмотри" in low or "глянь" in low) and "посмотрим" not in low and "сделаем" not in low,
        final,
    )
    check(
        "ru: слова-паразиты вычищены",
        not any(p in low for p in PARASITES),
        final,
    )
    check(
        "ru: объём не раздут (не пересказ и не дописка)",
        0.5 <= len(final) / max(1, len(raw)) <= 2.0,
        f"{len(final)}/{len(raw)} симв.",
    )

    # 2. Вопрос обязан остаться вопросом
    final, _ = run_fixture("fixture_question.wav", cfg, keys)
    check("question: вопросительный знак на месте", "?" in (final or ""), final)
    check(
        "question: не превратился в утверждение от первого лица мн. ч.",
        "посмотрим" not in (final or "").lower(),
        final,
    )

    # 3. Первое лицо + самоперебив («нет, погоди» отменяет предыдущее)
    final, _ = run_fixture("fixture_person.wav", cfg, keys)
    low = (final or "").lower()
    check("person: первое лицо сохранено («я хочу»)", "я хочу" in low, final)
    check(
        "person: самоперебив разрешён в пользу финальной формулировки",
        "статус" in low and "погоди" not in low,
        final,
    )

    # 4. Плотность терминов
    final, _ = run_fixture("fixture_terms.wav", cfg, keys)
    low = (final or "").lower()
    check("terms: Airtable", "airtable" in low, final)
    check("terms: Postgres", "postgres" in low)
    check("terms: Green API распознан", "green" in low or "грин" in low)

    # 5. Тишина не должна превращаться в текст
    audio = read_wav(FIXTURES / "fixture_silence.wav")
    final, raw, note = process_audio(audio, cfg, keys)
    print(f"\n--- fixture_silence.wav\n    сырое:  {raw!r}\n    итог:   {final!r}")
    check(
        "silence: тишина не даёт текста (защита от галлюцинаций Whisper)",
        final is None,
        f"вернулось: {final!r}",
    )


def main():
    unit_tests()
    e2e_tests()

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    if failed:
        print("Провалено:")
        for name, _, detail in failed:
            print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
