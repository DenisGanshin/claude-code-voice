# -*- coding: utf-8 -*-
"""
Защита диктовки от смены фокуса + хранение расшифровок.

Жалоба 2026-08-06: человек переключил окно, пока шло распознавание, — текст
уехал в чужое окно, а через секунду буфер восстановился на прежнее содержимое,
и диктовка пропала целиком.

Что проверяем:
  A. фокус на месте        -> вставляем, буфер восстанавливается;
  B. фокус уехал           -> Ctrl+V НЕ жмём, буфер остаётся с текстом;
  C. окно старта закрыто   -> то же самое;
  D. фокус уехал В МОМЕНТ вставки (между сверкой и нажатием) -> буфер не трогаем;
  E. авто-вставка выключена -> это штатный режим, а не потеря;
  F. окно старта неизвестно -> ведём себя как раньше;
  G. расшифровки: пишутся, читаются, чистятся по сроку.

Ни живой клавиатуры, ни живого буфера обмена: `keyboard.send` и `pyperclip`
подменены. Настоящее нажатие Ctrl+V уехало бы в окно человека, сидящего за этой
же машиной, а настоящий `copy` затёр бы то, что он держит в буфере.

Хэндлы окон — НЕ выдуманные числа: `focus_moved` спрашивает у Windows, живо ли
окно, и на выдуманном хэндле любая проверка вырождалась бы в «окно закрыто».
Берём настоящие окна рабочего стола и оболочки.

Запуск:  python tests/test_focus_guard.py
"""

import ctypes
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import voice_core  # noqa: E402
import voice_hotkey  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


# --------------------------------------------------------------------------- #
# Подставные клавиатура и буфер обмена
# --------------------------------------------------------------------------- #

class FakeClipboard:
    def __init__(self, initial=""):
        self.value = initial

    def copy(self, text):
        self.value = text

    def paste(self):
        return self.value


class FakeKeyboard:
    """Считает нажатия вместо того, чтобы их делать."""

    def __init__(self):
        self.sent = []

    def send(self, combo):
        self.sent.append(combo)

    def release(self, key):
        pass


def run_paste(text, cfg, target, focus_sequence, clipboard_initial="__БЫЛО__",
              settle=0.0):
    """
    Один вызов paste_text на подставном окружении.

    focus_sequence — что будет отдавать GetForegroundWindow при последовательных
    вызовах (последнее значение повторяется). Так проверяется и случай, когда
    фокус уехал между сверкой и нажатием.

    settle — сколько подождать, НЕ снимая подмену. Восстановление буфера отложено
    на секунду в отдельном потоке; сними подмену раньше — и поток проснётся уже с
    настоящим pyperclip, то есть полезет в буфер обмена живого человека за
    соседним окном. Ждать надо внутри, а не снаружи.
    """
    clip = FakeClipboard(clipboard_initial)
    kbd = FakeKeyboard()
    seq = list(focus_sequence)

    def fake_foreground():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    saved = (voice_hotkey.pyperclip, voice_hotkey.keyboard,
             voice_hotkey.foreground_window)
    voice_hotkey.pyperclip = clip
    voice_hotkey.keyboard = kbd
    voice_hotkey.foreground_window = fake_foreground
    try:
        where, outcome = voice_hotkey.paste_text(text, cfg, target)
        if settle:
            time.sleep(settle)
    finally:
        (voice_hotkey.pyperclip, voice_hotkey.keyboard,
         voice_hotkey.foreground_window) = saved
    return where, outcome, clip, kbd


SETTLE = 1.6  # чуть больше секундной паузы перед восстановлением буфера


def check_fake_indicator():
    """
    Капсула при подставном источнике обязана выглядеть иначе, чем при записи.

    Инцидент 2026-08-06: автотест с `--fake-mic` поднял на экране обычную красную
    капсулу с таймером — со стороны неотличимо от того, что человека пишут.
    Микрофон при этом не открывался, но проверить это можно было только чтением
    исходников, а индикатору положено отвечать на вопрос «меня сейчас пишут?»
    без чтения кода. Красный цвет означает «да» и не должен означать ничего ещё.
    """
    import json
    from collections import deque

    import overlay

    saved = voice_core.RECORDING_STATE
    voice_core.RECORDING_STATE = BASE / "_test_tmp" / "_state_fake.json"
    voice_core.RECORDING_STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        voice_core.set_recording_state(True, phase="rec", hint="ПОДСТАВНОЙ ФАЙЛ f.wav",
                                       level=0.5, fake=True)
        raw = json.loads(voice_core.RECORDING_STATE.read_text(encoding="utf-8"))
        check("подставной режим доезжает до индикатора", raw.get("fake") is True,
              "иначе окну не из чего сделать вывод")

        voice_core.set_recording_state(True, phase="rec", hint="Микрофон", level=0.5)
        real = json.loads(voice_core.RECORDING_STATE.read_text(encoding="utf-8"))
        check("настоящая запись помечена как настоящая", real.get("fake") is False)
    finally:
        voice_core.RECORDING_STATE = saved

    # build() — чистая функция от состояния; окно для неё не нужно и не создаётся.
    ind = object.__new__(overlay.Indicator)
    ind.levels = deque([0.0] * overlay.BARS, maxlen=overlay.BARS)
    ind.level = 0.0
    ind.started_at = time.time()
    fake_view = ind.build(raw)
    real_view = ind.build(real)
    check("подставная капсула не красная", fake_view["accent"] != overlay.C_REC,
          f"акцент {fake_view['accent']}")
    check("настоящая запись остаётся красной", real_view["accent"] == overlay.C_REC)
    check("подставная капсула подписана", "ПОДСТАВНОЙ" in fake_view["sub"].upper(),
          fake_view["sub"])


def main():
    user32 = ctypes.windll.user32
    live = int(user32.GetDesktopWindow())          # заведомо живое окно
    other = int(user32.GetShellWindow()) or live + 1
    dead = 0x7FFF0000                               # заведомо не окно
    cfg = {"auto_paste": True}
    text = "проверочная диктовка"

    # -- A. фокус на месте -------------------------------------------------- #
    where, outcome, clip, kbd = run_paste(text, cfg, live, [live], settle=SETTLE)
    check("фокус на месте: Ctrl+V нажат", kbd.sent == ["ctrl+v"], f"{kbd.sent}")
    check("фокус на месте: исход pasted", outcome == "pasted", f"{outcome} / {where}")
    check("фокус на месте: прежний буфер восстановлен", clip.value == "__БЫЛО__",
          f"в буфере {clip.value!r}")

    # -- B. фокус уехал ------------------------------------------------------ #
    # settle: если бы восстановление буфера запустилось, за это время оно бы
    # успело сработать — и проверка ниже поймала бы затирание.
    where, outcome, clip, kbd = run_paste(text, cfg, live, [other], settle=SETTLE)
    check("фокус уехал: Ctrl+V НЕ нажат", kbd.sent == [], f"{kbd.sent}")
    check("фокус уехал: исход lost", outcome == "lost", f"{outcome}")
    check("фокус уехал: подсказка про Ctrl+V", "Ctrl+V" in where, where)
    check("фокус уехал: текст остался в буфере", clip.value == text,
          f"в буфере {clip.value!r} — именно здесь диктовка и пропадала")

    # -- C. окно старта закрыто --------------------------------------------- #
    where, outcome, clip, kbd = run_paste(text, cfg, dead, [other])
    check("окно закрыто: вставки нет", kbd.sent == [] and outcome == "lost",
          f"{outcome} / {where}")
    check("окно закрыто: текст в буфере", clip.value == text)

    # -- D. фокус уехал МЕЖДУ сверкой и нажатием ---------------------------- #
    # Первый вызов — сверка до Ctrl+V (фокус ещё на месте), второй — после.
    where, outcome, clip, kbd = run_paste(text, cfg, live, [live, other],
                                          settle=SETTLE)
    check("фокус уехал при вставке: замечено", outcome == "lost", f"{outcome}")
    check("фокус уехал при вставке: буфер не затёрт", clip.value == text,
          f"в буфере {clip.value!r}")

    # -- E. авто-вставка выключена ------------------------------------------ #
    where, outcome, clip, kbd = run_paste(text, {"auto_paste": False}, live, [live])
    check("авто-вставка выключена: это не потеря", outcome == "clipboard",
          f"{outcome} — иначе демон пищал бы ошибкой в штатном режиме")
    check("авто-вставка выключена: Ctrl+V не нажат", kbd.sent == [])

    # -- F. окно старта неизвестно ------------------------------------------ #
    where, outcome, clip, kbd = run_paste(text, cfg, 0, [other])
    check("окно неизвестно: ведём себя как раньше", outcome == "pasted",
          "нулевой хэндл не повод отказывать во вставке")

    # -- G. расшифровки ------------------------------------------------------ #
    tmp = BASE / "_test_tmp" / "transcripts"
    tmp.mkdir(parents=True, exist_ok=True)
    saved_last = voice_core.LAST_TRANSCRIPT
    voice_core.LAST_TRANSCRIPT = tmp / "_last.txt"
    tcfg = {"keep_transcripts": True,
            "transcripts_dir": str(Path("_test_tmp") / "transcripts"),
            "transcripts_ttl_days": 30}
    try:
        long_text = "первая строка\n\n## не заголовок, а текст диктовки\nвторая"
        path = voice_core.save_transcript(long_text, tcfg, source="тест")
        check("расшифровка записана в историю", path is not None and path.exists(),
              str(path))
        body = path.read_text(encoding="utf-8") if path else ""
        check("история содержит текст целиком", long_text in body,
              "переводы строк и решётки внутри текста не должны его резать")
        check("последняя расшифровка читается дословно",
              voice_core.last_transcript() == long_text,
              "именно её достаёт --last, когда вставка не удалась")

        old = tmp / "2020-01-01.md"
        old.write_text("древняя запись", encoding="utf-8")
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        removed = voice_core.cleanup_transcripts(tmp, 30)
        check("просроченная история удалена", removed == 1 and not old.exists(),
              f"убрано {removed}")
        check("свежая история не тронута", path.exists())

        voice_core.LAST_TRANSCRIPT = tmp / "_missing.txt"
        check("нет расшифровок — не падаем", voice_core.last_transcript() is None)

        off = voice_core.save_transcript("текст", {"keep_transcripts": False})
        check("выключенное хранение ничего не пишет", off is None)
    finally:
        voice_core.LAST_TRANSCRIPT = saved_last

    # -- H. подставной источник отличим от настоящей записи ------------------ #
    check_fake_indicator()

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
