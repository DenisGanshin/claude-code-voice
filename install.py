# -*- coding: utf-8 -*-
"""
Установка голосового интерфейса: зависимости, модель голоса, скилл и команда.

    python install.py            # всё сразу
    python install.py --claude   # только скилл talk и команда /voice
    python install.py --models   # только скачать голос Piper
    python install.py --check    # ничего не менять, показать состояние

Почему установщик на Python, а не .cmd: раскладка файлов в ~/.claude требует
подстановки реального пути в скилл и слэш-команду. В батнике это превращается
в возню с кавычками и кодировкой OEM, где кириллица в комментариях ломает
интерпретатор ещё до первой команды.
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# Установщик запускают в консоли, которая на русской Windows отдаёт cp1251/cp866.
# Любой символ вне неё роняет print с UnicodeEncodeError — и установка обрывается
# на середине: часть файлов уже разложена, часть нет, а человек видит трассировку
# вместо инструкции. Правится здесь, а не импортом из voice_core: тот тянет numpy
# и sounddevice, которых на момент установки ещё нет.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
PLACEHOLDER = "__VOICE_INPUT_DIR__"

# Голос Piper: локальный синтез без сети и без лимитов. Модель не лежит в
# репозитории намеренно — 63 МБ бинарника в git превращают клон в мучение,
# а меняется она никогда.
PIPER_VOICE = "ru_RU-irina-medium"
PIPER_BASE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "ru/ru_RU/irina/medium/")


def say(msg):
    print(msg, flush=True)


def install_deps():
    say("→ зависимости из requirements.txt")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        str(BASE_DIR / "requirements.txt")])
    if r.returncode != 0:
        say("  ОШИБКА: pip не отработал. Проверьте `python --version`.")
        return False
    return True


def install_models(force=False):
    """Скачивает голос Piper. Без него озвучка падает на системный голос Windows."""
    target = BASE_DIR / "tts_models"
    target.mkdir(exist_ok=True)
    ok = True
    for suffix in (".onnx", ".onnx.json"):
        dst = target / f"{PIPER_VOICE}{suffix}"
        if dst.exists() and not force:
            say(f"→ {dst.name} уже на месте ({dst.stat().st_size // 1024} КБ)")
            continue
        url = PIPER_BASE + f"{PIPER_VOICE}{suffix}"
        say(f"→ качаю {dst.name} …")
        try:
            # Во временный файл и переименование: оборванная закачка не должна
            # оставить файл, который выглядит рабочим и падает при загрузке модели.
            tmp = dst.with_suffix(dst.suffix + ".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(dst)
            say(f"  готово, {dst.stat().st_size // 1024} КБ")
        except Exception as e:  # noqa: BLE001 — сеть, прокси, изменившийся URL
            say(f"  не удалось: {str(e)[:120]}")
            say(f"  скачайте вручную: {url}")
            ok = False
    return ok


def install_claude_assets():
    """
    Раскладывает скилл `talk` и слэш-команду `/voice` в каталог Claude Code.

    Путь к скриптам подставляется здесь, а не хранится в файлах: репозиторий
    не знает, куда его склонируют, а внутри скилла нужен абсолютный путь —
    Claude Code запускает команду не из каталога проекта.
    """
    src_root = BASE_DIR / "claude"
    if not src_root.exists():
        say("→ каталога claude/ нет — пропускаю скилл и команду")
        return True

    pairs = [
        (src_root / "skills" / "talk" / "SKILL.md",
         CLAUDE_HOME / "skills" / "talk" / "SKILL.md"),
        (src_root / "commands" / "voice.md",
         CLAUDE_HOME / "commands" / "voice.md"),
    ]
    posix = BASE_DIR.as_posix()
    for src, dst in pairs:
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            backup = dst.with_suffix(dst.suffix + ".bak")
            shutil.copy2(dst, backup)
            say(f"→ прежний {dst.name} сохранён как {backup.name}")
        dst.write_text(src.read_text(encoding="utf-8").replace(PLACEHOLDER, posix),
                       encoding="utf-8")
        say(f"→ {dst}")
    return True


def check():
    """Что уже готово, а что нет. Ничего не меняет."""
    sys.path.insert(0, str(BASE_DIR))
    from voice_core import KEYS_FILE, load_groq_keys  # noqa: E402

    say(f"каталог проекта : {BASE_DIR}")
    say(f"каталог Claude  : {CLAUDE_HOME}")
    say(f"файл ключей     : {KEYS_FILE} "
        f"({'есть' if KEYS_FILE.exists() else 'НЕТ'})")
    keys = load_groq_keys()
    say(f"ключей Groq     : {len(keys)} "
        f"({'достаточно' if keys else 'без них распознавание не работает'})")

    model = BASE_DIR / "tts_models" / f"{PIPER_VOICE}.onnx"
    say(f"голос Piper     : {'есть' if model.exists() else 'НЕТ (python install.py --models)'}")
    for name, path in [("скилл talk", CLAUDE_HOME / "skills" / "talk" / "SKILL.md"),
                       ("команда /voice", CLAUDE_HOME / "commands" / "voice.md")]:
        say(f"{name:16s}: {'установлен' if path.exists() else 'НЕТ'}")
    cfg = BASE_DIR / "config.json"
    say(f"config.json     : {'свой' if cfg.exists() else 'нет (берутся значения по умолчанию)'}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true", help="только скилл и команда")
    ap.add_argument("--models", action="store_true", help="только голос Piper")
    ap.add_argument("--deps", action="store_true", help="только зависимости")
    ap.add_argument("--check", action="store_true", help="показать состояние")
    ap.add_argument("--force", action="store_true", help="перекачать модель заново")
    args = ap.parse_args()

    if args.check:
        return check()

    only = args.claude or args.models or args.deps
    ok = True
    if args.deps or not only:
        ok &= install_deps()
    if args.models or not only:
        ok &= install_models(args.force)
    if args.claude or not only:
        ok &= install_claude_assets()

    if not only:
        say("")
        say("Дальше — два шага, которые за вас никто не сделает:")
        say(f"  1. Положите ключ Groq в {BASE_DIR / '.env'} строкой GROQ_API_KEY=...")
        say("     (ключ бесплатный: https://console.groq.com/keys)")
        say("  2. Скопируйте config.example.json в config.json и впишите свой микрофон")
        say("     (список устройств: python voice_once.py --devices)")
        say("")
        say("Проверка: python install.py --check")
        say("Запуск:   start_hotkey.cmd   (CapsLock — старт/стоп диктовки)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
