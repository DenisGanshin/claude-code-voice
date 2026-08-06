# -*- coding: utf-8 -*-
"""
Сторож демона: поднимает голосовой ввод, если тот не отвечает.

Зачем. Автозапуск был ровно один — ярлык в папке Startup, то есть он срабатывает
**при входе в Windows**. Этого хватает на «компьютер перезагрузили» и не хватает
ни на что другое: 05.08 демона остановили командой, машина с 28 июля не
перезагружалась — и голосовой ввод пролежал сутки, причём узнали об этом
случайно. Инструмент, который «всегда включён», обязан переживать не только
перезагрузку, но и собственную остановку.

Выключатель. Если рядом лежит `_daemon.off`, сторож молчит — значит человек
выключил инструмент через значок в трее, и поднимать его обратно нельзя: тогда
выключателя фактически нет. Файл убирается при запуске `start_hotkey.cmd`
(явный запуск и есть включение).

Держится лёгким намеренно: только `socket` и `subprocess`. Импорт `voice_core`
потянул бы numpy и sounddevice — несколько секунд на каждую проверку, а она
случается каждые пять минут круглые сутки.

Имя файла не `watchdog.py` сознательно: так называется популярный пакет с PyPI,
и лежащий рядом одноимённый модуль затеняет его для всего, что запускается из
этой папки.

Запуск:  pythonw keepalive.py        (обычно — из Планировщика заданий)
         python  keepalive.py --status   что сторож видит прямо сейчас
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:  # без этого кириллица в выводе ломается под редиректом (cp1251)
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent
DAEMON = BASE_DIR / "voice_hotkey.py"
OFF_FILE = BASE_DIR / "_daemon.off"
LOG_FILE = BASE_DIR / "_keepalive.log"

# Держать в согласии с voice_hotkey.CONTROL_PORT. Не импортируем оттуда
# намеренно (см. шапку); за совпадением следит tests/test_keepalive.py.
CONTROL_PORT = 47843

LOG_MAX_BYTES = 128 * 1024
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            LOG_FILE.write_text(tail[len(tail) // 2:], encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def daemon_alive(timeout=2.0):
    """Отвечает ли демон по своему каналу управления."""
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout) as s:
            s.sendall(b"ping\n")
            s.settimeout(timeout)
            return s.recv(16).decode("utf-8", "replace").strip() == "ok"
    except OSError:
        return False


def turned_off():
    return OFF_FILE.exists()


def start_daemon():
    """
    Поднять демона без консольного окна и не привязывая его к себе.

    DETACHED_PROCESS обязателен: сторож живёт секунду и умирает, а демон должен
    остаться. Без открепления он ушёл бы следом за родителем.
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw) if pythonw.exists() else sys.executable
    try:
        subprocess.Popen(
            [exe, str(DAEMON)], cwd=str(BASE_DIR),
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except OSError as e:
        log(f"[error] не удалось запустить демона: {e}")
        return False


def check(quiet=True):
    """
    Один проход. Возвращает строку с тем, что произошло.

    Молчит, когда всё в порядке: сторож ходит каждые пять минут, и запись
    «всё хорошо» триста раз в сутки превратила бы лог в шум, в котором
    настоящий подъём демона уже не найти.
    """
    if turned_off():
        return "выключено пользователем (_daemon.off) — не вмешиваюсь"
    if daemon_alive():
        return "демон отвечает"
    log("[warn] демон не отвечает — поднимаю")
    if not start_daemon():
        return "запуск не удался, см. лог"
    for _ in range(20):  # старт со звуковым устройством занимает пару секунд
        time.sleep(0.5)
        if daemon_alive():
            log("[ok] демон поднят")
            return "демон был мёртв, поднят"
    log("[error] демон запущен, но не отвечает на порту — возможно, завис")
    return "запущен, но не отвечает"


def main():
    verbose = "--status" in sys.argv
    result = check()
    if verbose:
        print(result)
        print(f"порт {CONTROL_PORT}, выключатель: "
              f"{'ЕСТЬ' if turned_off() else 'нет'} ({OFF_FILE.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
