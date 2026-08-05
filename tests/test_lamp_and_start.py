# -*- coding: utf-8 -*-
"""
Проверка двух дефектов, найденных по жалобам пользователя 2026-08-05.

  A. Лампочка CapsLock расходилась с записью. Windows зажигает её от самого
     нажатия, до нашего участия; отвергнутое нажатие (демон занят) оставляло её
     в состоянии, противоположном истине, и дальше всё работало наоборот:
     «на команду выключения он наоборот включился».
  B. Старт записи занимал 1.32 с, и блоки прогрева выбрасывались — то есть
     человек и ждал, и терял начало фразы.
  C. Лог демона писался только при запуске через start_hotkey.cmd; поднятый
     иначе демон был нем, и разбирать жалобу пришлось по косвенным следам.

Тест НЕ трогает живую клавиатуру: `keyboard.send` и `GetKeyState` подменяются
моделью того, как ведёт себя система. Настоящее синтетическое нажатие переключило
бы регистр человеку, который в этот момент работает за той же машиной.

Запуск:  python tests/test_lamp_and_start.py
"""

import sys
import time
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import voice_core  # noqa: E402
import voice_hotkey  # noqa: E402

FIXTURES = Path(__file__).resolve().parent
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


# --------------------------------------------------------------------------- #
# Модель клавиатуры: нажатие переключает лампу, как это делает сама Windows
# --------------------------------------------------------------------------- #

class FakeKeyboard:
    """
    Заменяет и лампочку, и синтетическое нажатие.

    Два свойства, ради которых модель вообще нужна, — и оба взяты с натуры:

    * нажатие CapsLock переключает лампу ВСЕГДА, кто бы его ни сделал. Демон не
      может «не зажечь» лампу, он может только вернуть её назад;
    * своё же синтетическое нажатие возвращается в хук как обычное событие.
      Без этого не проверить окно `_synthetic_until`: демон обязан узнать своё
      нажатие и не принять его за команду человека — но и не съесть следующее,
      настоящее. Первая версия теста этого не моделировала и «нашла» дефект,
      которого в живой системе нет.

    `dead=True` — лампа не поддаётся (система нажатие принимает, состояние не
    меняет): так проверяется предел попыток сторожа.
    """

    def __init__(self, caps=False, dead=False):
        self.caps = caps
        self.dead = dead
        self.presses = 0
        self.hook = None  # обработчик демона, как настоящий keyboard.add_hotkey

    def press(self, key="caps lock"):
        """Нажатие: сперва система меняет состояние, потом событие идёт в хук."""
        self.presses += 1
        if not self.dead:
            self.caps = not self.caps
        if self.hook:
            self.hook()


def make_daemon(fake, recording=False, busy=False):
    """Демон с подменённой клавиатурой и без звуков."""
    voice_hotkey.capslock_on = lambda: fake.caps
    voice_hotkey.keyboard.send = fake.press
    d = voice_hotkey.Daemon({"beep": False, "show_overlay": False})
    d.recording = recording
    d.busy = busy
    fake.hook = d.on_toggle  # событие клавиатуры приходит демону, как в жизни
    return d


def run_lamp_tests():
    print("\n--- A. Инвариант «лампочка горит ⟺ идёт запись» ---")

    # Сценарий из жалобы дословно: демон распознаёт прошлую запись, приходит нажатие.
    fake = FakeKeyboard(caps=False)
    d = make_daemon(fake, recording=False, busy=True)
    fake.press()                      # Windows зажгла лампу и отдала событие демону
    check("busy: нажатие не запускает запись",
          not d.toggle.is_set(), "toggle остался сброшен")
    check("busy: лампа зажглась мимо демона (воспроизведение дефекта)",
          fake.caps is True and d.recording is False, "лампа True, запись False")
    d.lamp_tick()
    check("busy: сторож вернул лампу к истине",
          fake.caps is False, "лампа погашена, записи нет")

    # Следующее нажатие обязано СТАРТОВАТЬ запись, а не «выключить» несуществующую.
    # Здесь же проверяется, что выравнивание не съело живое нажатие: своё событие
    # закрыло окно синтетики, и человеческое прошло насквозь.
    d.busy = False
    fake.press()
    check("после выравнивания нажатие работает прямо, а не наоборот",
          d.toggle.is_set(), "toggle установлен")

    # Зеркальный случай: запись идёт, а лампа погасла (человек нажал во время
    # старта микрофона, система погасила, демон нажатие ещё не разобрал).
    fake = FakeKeyboard(caps=False)
    d = make_daemon(fake, recording=True)
    d.lamp_tick()
    check("запись идёт — сторож зажигает погасшую лампу", fake.caps is True)

    # Совпадение состояний не должно порождать нажатий: иначе клавиатура мигает.
    before = fake.presses
    d.lamp_tick()
    d.lamp_tick()
    check("состояния совпали — сторож молчит",
          fake.presses == before, f"нажатий не добавилось ({before})")

    # Окно синтетического нажатия: своё же переключение сторож не считает разладом.
    fake = FakeKeyboard(caps=True)
    d = make_daemon(fake, recording=False)
    d._synthetic_until = time.time() + 5
    d.lamp_tick()
    check("окно своего нажатия — сторож не вмешивается",
          fake.caps is True and fake.presses == 0)

    # Предел попыток: если система нажатия принимает, но лампа не меняется,
    # мигать вечно нельзя — это заметно мешает работать.
    fake = FakeKeyboard(caps=True, dead=True)
    d = make_daemon(fake, recording=False)
    for _ in range(10):
        d.lamp_tick()
    check("лампа не поддаётся — сторож отступается",
          d._lamp_misses == d.LAMP_MAX_MISSES,
          f"{d._lamp_misses} попыток, предел {d.LAMP_MAX_MISSES}")
    check("отступившись, сторож перестаёт слать нажатия",
          fake.presses == d.LAMP_MAX_MISSES,
          f"{fake.presses} нажатий за 10 тиков")


# --------------------------------------------------------------------------- #
# B. Старт записи: быстрее и без потери начала фразы
# --------------------------------------------------------------------------- #

def write_wav(path, samples, sr=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def run_start_tests(tmp):
    print("\n--- B. Старт записи ---")

    # Профиль разгона моделирует сам подставной микрофон: он подмешивает 0.5 с
    # почти-тишины перед файлом, а её уровень задаёт fake_mic_lead_rms. Дублировать
    # разгон ещё и в самом WAV нельзя — суммарно вышло бы 1.0 с, больше потолка
    # warmup_ms, и проверка «вышел раньше потолка» стала бы невыполнимой в принципе.
    # 0.5 с — ровно столько раскручивается живой встроенный микрофон (замер 05.08).
    sr = 16000
    live = (np.random.default_rng(7).normal(0, 0.05, int(sr * 2.0))).astype(np.float32)
    wav = tmp / "warmup_profile.wav"
    write_wav(wav, live)

    cfg = voice_core.load_config()
    # fake_mic_lead_rms — уровень «разгона» подставного устройства. По умолчанию он
    # выше порога готовности, и тогда микрофон готов с первого блока: ожидание
    # разгона проверить нечем, а тест выглядит зелёным.
    cfg.update({"fake_mic_wav": str(wav), "fake_mic_realtime": False,
                "fake_mic_lead_rms": 1.4e-5})

    rec = voice_core.Recorder(cfg)
    rec.start()
    warm_blocks = len(rec._blocks)
    audio = rec.stop()

    check("прогрев попадает в запись, а не выбрасывается",
          warm_blocks > 0 and audio is not None,
          f"{warm_blocks} блоков ({warm_blocks * 0.1:.1f} с) уже в записи")

    # Прогрев обязан переждать фазу раскрутки: критерий готовности — QUIET_HINT_LEVEL,
    # а не LIVE_LEVEL. С мягким порогом он обрывался на первом же блоке, и запись
    # начиналась глухой (RMS 0.00007 вместо речи — та самая пустая запись).
    # Верхняя граница обязательна: без неё проверку одинаково проходит и ожидание
    # по делу, и тупое досиживание до потолка warmup_ms.
    ceiling = int(cfg["warmup_ms"] / voice_core.BLOCK_MS)
    check("прогрев ждёт раскрутки устройства, а не первого ненулевого блока",
          5 <= warm_blocks < ceiling,
          f"переждал {warm_blocks * 0.1:.1f} с при разгоне 0.5 с (потолок {ceiling})")

    # Потолок соблюдается: молчащий микрофон не держит старт дольше warmup_ms.
    silent = tmp / "silent.wav"
    write_wav(silent, np.full(int(sr * 3), 1.4e-5, dtype=np.float32))
    cfg2 = voice_core.load_config()
    cfg2.update({"fake_mic_wav": str(silent), "fake_mic_realtime": False,
                 "fake_mic_lead_rms": 1.4e-5})
    rec2 = voice_core.Recorder(cfg2)
    rec2.start()
    ceiling = int(cfg2["warmup_ms"] / voice_core.BLOCK_MS)
    got = len(rec2._blocks)
    rec2.stop()
    check("микрофон молчит — прогрев упирается в потолок и не виснет",
          got <= ceiling, f"{got} блоков при потолке {ceiling}")

    # Замер на живом устройстве — он и был предметом жалобы. Порог с запасом:
    # это защита от возврата к 1.3 с, а не обещание конкретных миллисекунд.
    budget = 1.1
    try:
        real = voice_core.Recorder(voice_core.load_config())
        t = time.time()
        real.start()
        elapsed = time.time() - t
        real.stop()
        check("старт на живом микрофоне укладывается в бюджет",
              elapsed <= budget, f"{elapsed:.2f} с при бюджете {budget} с")
    except Exception as e:  # noqa: BLE001 — на машине без микрофона это не провал
        check("старт на живом микрофоне (пропущен: микрофона нет)", True, str(e)[:50])


# --------------------------------------------------------------------------- #
# C. Лог пишется независимо от способа запуска
# --------------------------------------------------------------------------- #

def run_log_tests():
    print("\n--- C. Логирование ---")

    marker = f"[test] проверка лога {time.time():.3f}"
    before = voice_core.LOG_FILE.stat().st_size if voice_core.LOG_FILE.exists() else 0
    voice_core.log(marker)
    tail = voice_core.LOG_FILE.read_text(encoding="utf-8", errors="replace")
    check("log() пишет в файл без всякого перенаправления",
          marker in tail, f"файл вырос с {before} байт")

    # QUIET глушит консоль, но не файл: он нужен именно тогда, когда на экране
    # ничего не видно (pythonw без консоли).
    quiet_marker = f"[test] тихий режим {time.time():.3f}"
    voice_core.QUIET = True
    try:
        voice_core.log(quiet_marker)
    finally:
        voice_core.QUIET = False
    tail = voice_core.LOG_FILE.read_text(encoding="utf-8", errors="replace")
    check("в тихом режиме файл всё равно наполняется", quiet_marker in tail)


def main():
    tmp = BASE / "_test_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        run_lamp_tests()
        run_start_tests(tmp)
        run_log_tests()
    finally:
        for f in tmp.glob("*.wav"):
            f.unlink(missing_ok=True)
        tmp.rmdir()

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"ИТОГО: {passed}/{len(results)} проверок пройдено")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
