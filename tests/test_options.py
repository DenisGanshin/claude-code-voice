# -*- coding: utf-8 -*-
"""
Проверка выбора из вариантов (`ask.py --options`) и срока годности состояния индикатора.

Три части:
  A. Матчер вариантов — на живых фразах и на мутациях (без сети).
  B. Срок годности состояния — протухшее состояние обязано гасить индикатор.
  C. Программный e2e: настоящие записи ответов вместо микрофона -> ask.py -> ВЫБОР.

Фикстуры answer_*.wav — реальные ответы живого человека и в репозиторий не выложены
(см. .gitignore); на чистом клоне часть C пропускается с явной строкой в отчёте.
Синтез речи их не заменит: ошибка, ради которой всё затевалось («машинный JSON» ->
«машинен.json»), рождается именно на живом голосе.

Запуск:  python tests/test_options.py
Код возврата 0 — все проверки прошли. Тест беззвучный: TTS и сигналы заглушены.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from voice_core import load_groq_keys, match_option  # noqa: E402

FIXTURES = Path(__file__).resolve().parent
STATE_FILE = BASE / "_recording.json"

results = []
skipped = []  # приватные фикстуры, которых нет на чистом клоне


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# A. Матчер вариантов
# --------------------------------------------------------------------------- #

# (что услышали, варианты, ожидаемый выбор, ожидаемая уверенность)
MATCH_CASES = [
    # Реальные ответы из диалога 31.07 — ровно те, на которых ловилась ошибка.
    ("машинен.json", ["таблица", "список", "JSON"], "JSON", "exact"),
    ("Машинный JSON.", ["таблица", "список", "JSON"], "JSON", "exact"),
    ("И, пожалуй, оба типа теста.", ["только программный", "оба"], "оба", "exact"),
    ("Давай переписывать целиком.",
     ["точечная правка", "вынести в общий модуль", "переписать целиком"],
     "переписать целиком", "exact"),
    # Ответ мимо списка обязан остаться неопределённым: подгонять его под
    # ближайший вариант — молча соврать спрашивающему.
    ("На твоё усмотрение.", ["только в конце", "на каждом этапе", "молчать"], None, "none"),
    ("Задай мне вопросы голосом.", ["таблица", "список", "JSON"], None, "none"),
    # Словоформы и порядковые указания
    ("Ну давай таблицей.", ["таблица", "список", "JSON"], "таблица", "exact"),
    ("Давай второй.", ["таблица", "список", "JSON"], "список", "likely"),
    ("Первый вариант.", ["таблица", "список"], "таблица", "likely"),
    # «да»/«нет» — часто и есть весь ответ, служебным словом их считать нельзя
    ("Да, сделать.", ["да, сделать", "не надо"], "да, сделать", "exact"),
    ("Не надо, по одному.", ["да, сделать", "не надо, по одному"], "не надо, по одному", "exact"),
    # Пустой вход не должен выбирать ничего
    ("", ["таблица", "список"], None, "none"),
]


def unit_tests():
    print("\n=== A. Матчер вариантов (без сети) ===")
    for text, options, expected, expected_conf in MATCH_CASES:
        got, conf = match_option(text, options)
        check(
            f"match: {text[:38]!r} -> {expected!r}",
            got == expected and conf == expected_conf,
            f"получено {got!r} ({conf})",
        )

    # Неоднозначность: одинаковое совпадение с двумя вариантами — не выбор.
    got, conf = match_option("давай в отчёт", ["отчёт таблицей", "отчёт списком"])
    check("match: ничья между вариантами не даёт выбора", got is None, f"{got!r} ({conf})")

    # Приставка не должна склеивать разные варианты: «пере» есть в обоих.
    got, _ = match_option("перенеси задачу", ["переписать целиком", "перенести на потом"])
    check("match: «пере» не путает соседние варианты", got == "перенести на потом", repr(got))

    # Регистр и ё
    got, _ = match_option("ДАВАЙ ЁМКО", ["емко", "подробно"])
    check("match: регистр и ё не мешают", got == "емко", repr(got))


# --------------------------------------------------------------------------- #
# B. Срок годности состояния индикатора
# --------------------------------------------------------------------------- #

def state_ttl_tests():
    print("\n=== B. Срок годности состояния индикатора ===")
    import overlay  # импорт здесь: тянет ctypes/PIL, юнитам матчера они не нужны

    ind = overlay.Indicator.__new__(overlay.Indicator)  # без окна и графики

    fresh = {"active": True, "phase": "done", "expires_at": time.time() + 30}
    check("state: свежее «готово» показывается", ind._fresh(fresh).get("active") is True)

    stale = {"active": True, "phase": "done", "expires_at": time.time() - 1}
    check("state: протухшее «готово» гасит индикатор", ind._fresh(stale).get("active") is False)

    off = {"active": False, "phase": "done"}
    check("state: погашенное состояние не воскресает", ind._fresh(off).get("active") is False)

    # Файл от старой версии демона: срока нет вообще — судим по времени файла.
    saved = STATE_FILE.read_text(encoding="utf-8") if STATE_FILE.exists() else None
    try:
        # Писатель: живое состояние обязано получить срок, погашенное — нет.
        # Проверяем именно запись на диск, а не аргументы: между сторонами
        # только файл, и разойтись они могут молча.
        import voice_hotkey

        voice_hotkey.set_recording_state(True, phase="stt", note="тест")
        written = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        check("writer: живое состояние получает срок годности",
              isinstance(written.get("expires_at"), (int, float))
              and written["expires_at"] > time.time(),
              repr(written.get("expires_at")))

        voice_hotkey.clear_recording_state()
        written = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        check("writer: погашенное состояние живёт без срока",
              written.get("active") is False and written.get("expires_at") is None,
              repr(written.get("expires_at")))

        STATE_FILE.write_text(json.dumps({"active": True, "phase": "done"}), encoding="utf-8")
        old = time.time() - (overlay.Indicator.LEGACY_TTL + 5)
        os.utime(STATE_FILE, (old, old))
        legacy = {"active": True, "phase": "done"}
        check(
            "state: состояние без срока протухает по времени файла",
            ind._fresh(legacy).get("active") is False,
        )
    finally:
        if saved is not None:
            STATE_FILE.write_text(saved, encoding="utf-8")


# --------------------------------------------------------------------------- #
# C. E2E: живые записи вместо микрофона
# --------------------------------------------------------------------------- #

# (файл, что сказано, варианты, ожидаемый выбор)
E2E_CASES = [
    ("answer_json.wav", "машинный JSON",
     "таблица|список|JSON", "JSON"),
    ("answer_both.wav", "оба типа теста",
     "только программный|оба", "оба"),
    ("answer_rewrite.wav", "переписывать целиком",
     "точечная правка|вынести в общий модуль|переписать целиком", "переписать целиком"),
    # Свободный ответ: выбора нет, но расшифровка обязана доехать наверх
    ("answer_freeform.wav", "на твоё усмотрение",
     "только в конце|на каждом этапе|молчать", None),
]


def run_ask(fixture, options):
    """Запуск ask.py с подставным микрофоном. Возвращает (choice, confidence, answer)."""
    env = dict(os.environ, VOICE_FAKE_MIC=str(FIXTURES / fixture), VOICE_MUTE="1")
    proc = subprocess.run(
        [sys.executable, str(BASE / "ask.py"), "--quiet", "--options", options,
         "--wait", "20", "Тестовый вопрос"],
        cwd=str(BASE), env=env, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    choice = confidence = answer = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ВЫБОР:"):
            body = line[len("ВЫБОР:"):].strip()
            choice, _, tail = body.rpartition(" (")
            choice = choice.strip() or None
            confidence = tail.rstrip(")")
        elif line.startswith("ОТВЕТ:"):
            answer = line[len("ОТВЕТ:"):].strip()
    if choice == "—":
        choice = None
    return choice, confidence, answer, proc


def e2e_tests():
    print("\n=== C. E2E: настоящие записи ответов -> ask.py ===")
    if not load_groq_keys():
        check("ключ Groq найден", False, "GROQ_API_KEY отсутствует в keys.local")
        return

    for fixture, said, options, expected in E2E_CASES:
        path = FIXTURES / fixture
        if not path.exists():
            # Записи голоса живого человека в репозиторий не выкладываются, поэтому
            # на чистом клоне их нет. Это не провал: провалом был бы молчаливый
            # пропуск, поэтому о нём печатается отдельная строка и он попадает
            # в итоговую сводку.
            skipped.append(fixture)
            print(f"[ПРОП] e2e: {fixture} — записи нет на диске "
                  f"(приватные фикстуры не публикуются)", flush=True)
            continue
        t0 = time.time()
        choice, confidence, answer, proc = run_ask(fixture, options)
        print(f"\n--- {fixture} ({said}) за {time.time() - t0:.1f} с")
        print(f"    ВЫБОР: {choice!r} ({confidence})")
        print(f"    ОТВЕТ: {answer!r}")
        if answer is None:
            print(f"    stderr: {(proc.stderr or '').strip()[-300:]}")

        check(f"e2e {fixture}: расшифровка доехала", bool(answer), repr(answer))
        check(
            f"e2e {fixture}: выбор = {expected!r}",
            choice == expected,
            f"получено {choice!r} ({confidence})",
        )
        if expected is None:
            check(
                f"e2e {fixture}: свободный ответ отдан целиком, а не подогнан",
                bool(answer) and confidence == "не определён",
                f"{confidence!r}",
            )


def main():
    unit_tests()
    state_ttl_tests()
    if "--no-net" not in sys.argv:
        e2e_tests()

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    for name, _, detail in failed:
        print(f"  - {name}" + (f" — {detail}" if detail else ""))
    if skipped:
        # Пропуски объявляются вслух: «зелёный» результат без этой строки означал
        # бы, что e2e прошёл, хотя он не запускался вовсе.
        print(f"ПРОПУЩЕНО: {len(skipped)} e2e-проверок — нет приватных записей "
              f"({', '.join(skipped)})")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
