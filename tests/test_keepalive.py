# -*- coding: utf-8 -*-
"""
Сторож демона и выключатель в трее.

`keepalive.py` намеренно НЕ импортирует `voice_hotkey` — иначе каждая проверка
(а они каждые пять минут круглые сутки) тянула бы numpy и sounddevice. Плата за
это — продублированный номер порта, и первая же проверка здесь стережёт именно
его: разъехавшиеся константы означают сторожа, который вечно «не видит» живого
демона и поднимает второго.

Демона тест не запускает и не останавливает: `start_daemon` подменяется. Живой
демон — рабочий инструмент человека за этой машиной, тест не имеет права его
трогать.

Значок в трее показывается на полсекунды по-настоящему (иначе проверять нечего),
затем убирается.

Запуск:  python tests/test_keepalive.py
"""

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import keepalive  # noqa: E402
import voice_hotkey  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def main():
    # -- A. константы не разъехались ---------------------------------------- #
    check("порт сторожа совпадает с портом демона",
          keepalive.CONTROL_PORT == voice_hotkey.CONTROL_PORT,
          f"{keepalive.CONTROL_PORT} vs {voice_hotkey.CONTROL_PORT}")
    check("файл-выключатель один и тот же",
          keepalive.OFF_FILE == voice_hotkey.OFF_FILE,
          f"{keepalive.OFF_FILE.name}")
    check("сторож знает, где лежит демон",
          keepalive.DAEMON.exists(), str(keepalive.DAEMON))

    # -- B. выключатель останавливает сторожа -------------------------------- #
    started = []
    real_start = keepalive.start_daemon
    keepalive.start_daemon = lambda: (started.append(1), True)[1]
    off_existed = keepalive.OFF_FILE.exists()
    backup = keepalive.OFF_FILE.read_text(encoding="utf-8") if off_existed else None
    try:
        keepalive.OFF_FILE.write_text("тест", encoding="utf-8")
        verdict = keepalive.check()
        check("выключатель виден", keepalive.turned_off())
        check("при выключателе сторож не поднимает демона",
              not started and "выключено" in verdict, verdict)

        keepalive.OFF_FILE.unlink()
        check("выключатель снят", not keepalive.turned_off())
    finally:
        keepalive.start_daemon = real_start
        if off_existed:
            keepalive.OFF_FILE.write_text(backup, encoding="utf-8")
        elif keepalive.OFF_FILE.exists():
            keepalive.OFF_FILE.unlink()

    # -- C. проверка живости не врёт ---------------------------------------- #
    real_port = keepalive.CONTROL_PORT
    keepalive.CONTROL_PORT = 47999  # заведомо никем не занят
    try:
        check("на пустом порту демон считается мёртвым",
              keepalive.daemon_alive(timeout=0.5) is False)
    finally:
        keepalive.CONTROL_PORT = real_port

    # -- D. значок в трее ---------------------------------------------------- #
    import threading

    import tray

    icon = tray.make_icon_file()
    check("значок нарисован", icon is not None and icon.exists(),
          str(icon) if icon else "Pillow не установлен")

    fired = []
    t = tray.TrayIcon(on_stop=lambda: fired.append(1), tooltip="тест")
    threading.Thread(target=t.run, daemon=True).start()
    ready = t.wait_ready(timeout=5.0)
    check("значок создан и цикл сообщений жив", ready and t.hwnd is not None,
          f"hwnd={t.hwnd}")
    time.sleep(0.5)
    t.stop()
    time.sleep(0.5)
    # Главная проверка значка. Исключения внутри оконной процедуры Windows
    # проглатывает, поэтому «окно создано» ничего не доказывает: до этой
    # проверки тест был зелёным, пока процедура падала на каждом сообщении.
    check("оконная процедура не сыпала ошибками", not t.errors,
          "; ".join(t.errors[:3]))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"ИТОГО: {passed}/{len(results)} проверок пройдено")
    for name, ok, detail in results:
        if not ok:
            print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
