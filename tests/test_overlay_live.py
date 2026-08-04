# -*- coding: utf-8 -*-
"""
Ручной e2e экранного индикатора: настоящее окно на настоящем экране.

Аналог браузерного прогона для десктопа: тест не заглядывает в состояние объекта,
а спрашивает Windows, видно ли окно класса ClaudeVoiceOverlay. Именно этого не
хватало 31.07 — капсула «готово» висела поверх всех окон, хотя по коду её давно
должны были погасить.

Что проверяется:
  1. свежее состояние -> капсула видна;
  2. состояние протухло -> капсула сама скрылась (главная починка);
  3. второй индикатор не запускается поверх первого;
  4. команда quit закрывает процесс.

Запуск:  python tests/test_overlay_live.py
На несколько секунд в верхней части экрана появится капсула — так и задумано.
"""

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

STATE_FILE = BASE / "_recording.json"
OVERLAY_CLASS = "ClaudeVoiceOverlay"

user32 = ctypes.windll.user32
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# Окна: спрашиваем систему, а не собственный код
# --------------------------------------------------------------------------- #

ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def overlay_windows():
    """Все окна индикатора: (hwnd, видно ли). Считаем и скрытые — их тоже надо видеть."""
    found = []

    def cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == OVERLAY_CLASS:
            found.append((hwnd, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return found


def visible_count():
    return sum(1 for _, vis in overlay_windows() if vis)


def write_state(**payload):
    """Пишем состояние так же, как демон: временный файл + атомарная подмена."""
    state = {"active": False, "phase": "rec", "started": None, "limit_sec": None,
             "hint": None, "note": None, "level": None, "quit": False, "expires_at": None}
    state.update(payload)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def wait_until(predicate, timeout, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def start_overlay():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw) if pythonw.exists() else sys.executable
    return subprocess.Popen([exe, str(BASE / "overlay.py")], cwd=str(BASE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    print("=== Ручной e2e индикатора (окно появится на экране) ===")

    # Старые индикаторы (в том числе зависший с прошлой версией кода) закрываем
    # штатной командой через файл состояния, а не поиском процессов по имени
    # интерпретатора: чужой python на этой машине трогать нельзя (CLAUDE.md §3a).
    if overlay_windows():
        print("[info] найден запущенный индикатор — прошу его выйти")
        write_state(quit=True)
        wait_until(lambda: not overlay_windows(), 6)
    check("подготовка: старых окон индикатора не осталось", not overlay_windows(),
          f"{len(overlay_windows())} окон")

    write_state(active=False)
    proc = start_overlay()
    try:
        check("индикатор запустился (окно создано)",
              wait_until(lambda: bool(overlay_windows()), 15),
              f"{len(overlay_windows())} окон")

        # 1. Свежее «готово» — капсула должна быть на экране.
        write_state(active=True, phase="done", note="тест: вставлено в активное окно",
                    expires_at=time.time() + 3.0)
        check("свежее состояние: капсула видна", wait_until(lambda: visible_count() == 1, 5),
              f"видимых окон: {visible_count()}")

        # 2. Главное: срок вышел — гаснет само, хотя состояние в файле не менялось.
        #    Именно этот шаг воспроизводит баг 31.07 (демон умер, гасить некому).
        t0 = time.time()
        hidden = wait_until(lambda: visible_count() == 0, 8)
        check("протухшее состояние: капсула погасла сама", hidden,
              f"через {time.time() - t0:.1f} с, видимых: {visible_count()}")
        check("файл состояния при этом не трогали (гасил читатель, не писатель)",
              json.loads(STATE_FILE.read_text(encoding="utf-8"))["active"] is True)

        # 3. Второй индикатор поверх первого не поднимается.
        second = start_overlay()
        try:
            second.wait(timeout=20)
            check("второй индикатор вышел сам", second.returncode == 0,
                  f"код {second.returncode}")
        except subprocess.TimeoutExpired:
            second.kill()
            check("второй индикатор вышел сам", False, "не завершился за 20 с")
        check("окно индикатора по-прежнему одно", len(overlay_windows()) == 1,
              f"{len(overlay_windows())} окон")

        # 4. Штатное завершение.
        write_state(quit=True)
        try:
            proc.wait(timeout=10)
            check("команда quit закрывает индикатор", True, f"код {proc.returncode}")
        except subprocess.TimeoutExpired:
            check("команда quit закрывает индикатор", False, "процесс жив через 10 с")
    finally:
        if proc.poll() is None:
            write_state(quit=True)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        write_state(active=False)

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    for name, _, detail in failed:
        print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
