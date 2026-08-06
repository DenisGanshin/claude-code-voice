# -*- coding: utf-8 -*-
"""
Программный e2e демона: полный цикл «старт -> запись -> распознавание -> результат».

Демон поднимается с подставным микрофоном и управляется по сокету — ни живой
клавиатуры, ни живого микрофона, ни вставки в чужое окно (`--no-paste`).
Проверяется то, что нельзя проверить по частям: что состояние индикатора живёт
и гаснет, что лампочка и запись не расходятся, что текст доезжает до конца.

Требует ключ Groq (шаг распознавания настоящий) и свободный порт 47843.

Запуск:  python tests/test_daemon_e2e.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from keepalive import OFF_FILE  # noqa: E402
from voice_core import LAST_TRANSCRIPT, load_groq_keys  # noqa: E402
from voice_hotkey import CONTROL_PORT, send_command  # noqa: E402

STATE_FILE = BASE / "_recording.json"
STATUS_FILE = BASE / "_last_run.status"
FIXTURE = BASE / "tests" / "fixture_ru.wav"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def status():
    try:
        return STATUS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def wait_for(predicate, timeout, step=0.2):
    """Ждёт условия, возвращает True/False. Тайм-аут — часть проверки, не аварии."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def main():
    if not load_groq_keys():
        print("нет ключа Groq — e2e демона пропущен")
        return 0
    try:
        send_command("ping", timeout=1.0)
        print(f"порт {CONTROL_PORT} занят: остановите демон (--send quit) и повторите")
        return 2
    except OSError:
        pass  # порт свободен, это и нужно

    # Держим выключатель на время теста: сторож (keepalive.py) ходит по
    # расписанию и, увидев свободный порт, поднимет НАСТОЯЩЕГО демона прямо
    # посреди прогона — тот займёт порт, и тест развалится на ровном месте.
    # Флаг снимаем в finally, каким бы ни был исход.
    off_existed = OFF_FILE.exists()
    if not off_existed:
        OFF_FILE.write_text("идёт tests/test_daemon_e2e.py\n", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(BASE / "voice_hotkey.py"),
         "--fake-mic", str(FIXTURE), "--no-paste", "--no-beep"],
        cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        started = wait_for(lambda: _pings(), timeout=25)
        if not check("демон поднялся и отвечает по сокету", started):
            return 1

        send_command("toggle")
        check("запись началась: состояние активно",
              wait_for(lambda: state().get("active") is True, timeout=10),
              f"фаза {state().get('phase')!r}")
        check("демон считает, что пишет",
              json.loads(send_command("state")).get("recording") is True)
        check("у состояния есть срок годности",
              state().get("expires_at") is not None,
              "иначе капсула повиснет, если демон умрёт")

        time.sleep(4.0)  # даём подставному микрофону отдать кусок фразы
        send_command("toggle")

        recognized = wait_for(lambda: "готово" in status(), timeout=60)
        check("текст доехал до конца конвейера", recognized, status())

        # Расшифровка на диске — страховка от потери. Проверяем на живом демоне,
        # а не только модульно: писать её должен он сам, в конце сессии.
        saved_text = ""
        if wait_for(lambda: LAST_TRANSCRIPT.exists(), timeout=5):
            saved_text = LAST_TRANSCRIPT.read_text(encoding="utf-8").strip()
        check("демон сохранил полный текст расшифровки", bool(saved_text),
              f"«{saved_text[:60]}»")
        day = BASE / "_transcripts" / f"{time.strftime('%Y-%m-%d')}.md"
        check("история за день пополнилась", day.exists() and saved_text
              and saved_text in day.read_text(encoding="utf-8"), str(day))
        check("--last вернул бы именно её", _last_returns(saved_text), "буфер обмена")
        check("после записи демон не считает себя пишущим",
              json.loads(send_command("state")).get("recording") is False)
        check("индикатор погас сам",
              wait_for(lambda: state().get("active") is False, timeout=10),
              "состояние снято после показа результата")

        # Отмена: запись идёт, но результата быть не должно.
        send_command("toggle")
        wait_for(lambda: state().get("active") is True, timeout=10)
        time.sleep(1.0)
        send_command("cancel")
        check("отмена гасит запись и не даёт текста",
              wait_for(lambda: "отменена" in status(), timeout=15), status())

        send_command("quit")
        check("демон выходит по команде", proc.wait(timeout=20) is not None)
        check("на выходе состояние погашено", state().get("active") is False)
    finally:
        if proc.poll() is None:
            proc.kill()
        # Выключатель обязан уйти в любом случае: оставленный флаг молча выключит
        # голосовой ввод человеку до следующего start_hotkey.cmd.
        if not off_existed and OFF_FILE.exists():
            try:
                OFF_FILE.unlink()
            except OSError:
                print(f"ВНИМАНИЕ: не убран {OFF_FILE} — голосовой ввод останется "
                      f"выключенным, удалите файл вручную")

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"ИТОГО: {passed}/{len(results)} проверок пройдено")
    for name, ok, detail in results:
        if not ok:
            print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 0 if passed == len(results) else 1


def _pings():
    try:
        return send_command("ping", timeout=1.0) == "ok"
    except OSError:
        return False


def _last_returns(expected):
    """
    Проверить `--last` по-настоящему — он кладёт текст в буфер обмена.

    Буфер человека возвращаем на место: за этой машиной кто-то работает, и у него
    там могло лежать что-то нужное. Тест не имеет права оставлять следы в том,
    чем пользуются прямо сейчас.
    """
    if not expected:
        return False
    import pyperclip
    try:
        before = pyperclip.paste()
    except Exception:  # noqa: BLE001
        before = None
    try:
        subprocess.run([sys.executable, str(BASE / "voice_hotkey.py"), "--last"],
                       cwd=str(BASE), capture_output=True, timeout=20)
        return pyperclip.paste().strip() == expected
    except Exception:  # noqa: BLE001
        return False
    finally:
        if before is not None:
            try:
                pyperclip.copy(before)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
