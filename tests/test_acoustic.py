# -*- coding: utf-8 -*-
"""
E2E живой машины: то, что не проверить разбором файла. НИЧЕГО НЕ ВОСПРОИЗВОДИТ.

Часть 1 — ЖЕЛЕЗО, молча. Для каждого микрофона (внутренний и Bluetooth-гарнитура)
    открывается настоящий поток и читаются блоки: проверяется, что устройство
    находится, поток стартует, сэмплы идут и калибровка фона получает осмысленное
    число. Ни одного звука наружу.

    Почему НЕ loopback «динамики → микрофон», как было раньше: такой тест был
    одновременно громким и бесполезным.
      * Windows вычитает из микрофонного входа то, что сама вывела на динамики
        (эхоподавление) — Whisper на таком сигнале выдаёт мусор.
      * При подключённой гарнитуре звук вообще не попадает в комнату: Senary/Nahimic
        отдают его на активное устройство, то есть в уши, а микрофон ноутбука ловит
        в лучшем случае утечку. Проверялось бы не железо, а маршрутизация Windows.
      * И он пугает: 11 секунд чужого синтезированного голоса без предупреждения.

Часть 2 — ДЕМОН ЦЕЛИКОМ, тоже молча. Вместо микрофона подставляется WAV, всё
    остальное настоящее: глобальный хоткей, потоки, тумблер старт/стоп, Groq,
    буфер обмена. Бипы отключены.

Что остаётся ручным тестом (нужен живой голос, см. README): разборчивость диктовки
    в микрофон ноутбука и в гарнитуру.

Запуск:  python tests/test_acoustic.py
"""

import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

BASE = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from voice_core import Recorder, list_input_devices, load_config  # noqa: E402
from voice_once import read_wav  # noqa: E402

# Микрофоны, которые должны работать: ноутбучный и гарнитура.
# Второй ищется по двум именам — Windows зовёт HFP-профиль «Головной телефон».
MICS = [
    ("микрофон ноутбука", ["Internal Microphone"]),
    ("Bluetooth-гарнитура", ["HUAWEI FreeBu", "Головной телефон", "Headset"]),
]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# Часть 1: захват с реальных устройств — без единого звука наружу
# --------------------------------------------------------------------------- #

def test_capture(label, preferences):
    print(f"\n--- {label} ---")
    found = [
        d for d in list_input_devices()
        if any(p.lower() in d["name"].lower() for p in preferences) and "WDM-KS" not in d["api"]
    ]
    if not check(f"[{label}] устройство найдено", bool(found),
                 found[0]["name"] if found else "нет в системе — подключено ли?"):
        return

    cfg = load_config()
    cfg["input_device_preferences"] = preferences
    cfg["beep"] = False

    rec = Recorder(cfg)
    try:
        rec._open()
    except Exception as e:  # noqa: BLE001
        check(f"[{label}] поток открывается", False, str(e)[:120])
        return
    check(f"[{label}] поток открывается", True, rec.device_name)

    try:
        blocks = [rec._read_block() for _ in range(20)]  # 2 секунды
    finally:
        rec._close()

    audio = np.concatenate(blocks)
    expected = int(rec.blocksize * 20)
    check(f"[{label}] сэмплы идут непрерывно", len(audio) == expected,
          f"{len(audio)} из {expected}")

    # «Живой» вход шумит хотя бы на уровне единиц АЦП. Ровный цифровой ноль на
    # протяжении двух секунд — признак того, что устройство отдаёт заглушку.
    rms = float(np.sqrt(np.mean(audio ** 2)))
    alive = sum(1 for b in blocks if float(np.sqrt(np.mean(b ** 2))) > 1e-6)
    check(f"[{label}] вход живой, а не цифровая заглушка", alive >= 15,
          f"{alive} из 20 блоков с сигналом, RMS={rms:.5f}")

    # Ровно тот путь, что используется в бою: прогрев + первичная оценка фона.
    rec2 = Recorder(cfg)
    rec2._open()
    try:
        vad = rec2._warmup()
    finally:
        rec2._close()
    check(f"[{label}] калибровка даёт осмысленные пороги",
          0 < vad.start_threshold < 0.5 and vad.stop_threshold < vad.start_threshold,
          f"фон {vad.noise:.5f}, старт {vad.start_threshold:.4f}, тишина {vad.stop_threshold:.4f}")


def test_hardware():
    print("\n=== 1. Железо: захват с реальных микрофонов (без звука) ===")
    d_in, d_out = sd.default.device
    devices = sd.query_devices()
    print(f"    по умолчанию в Windows: вход «{devices[d_in]['name']}», "
          f"выход «{devices[d_out]['name']}»")
    for label, prefs in MICS:
        test_capture(label, prefs)


# --------------------------------------------------------------------------- #
# Часть 2: демон целиком, микрофон подменён файлом
# --------------------------------------------------------------------------- #

def test_hotkey_daemon():
    print("\n=== 2. Демон: команда -> речь -> команда -> буфер обмена ===")
    import pyperclip

    from voice_hotkey import send_command

    fixture = FIXTURES / "fixture_ru.wav"
    fixture_sec = len(read_wav(fixture)) / 16000
    status = BASE / "_last_run.status"
    if status.exists():
        status.unlink()
    pyperclip.copy("__ДО_ТЕСТА__")

    proc = subprocess.Popen(
        [sys.executable, str(BASE / "voice_hotkey.py"),
         "--no-paste", "--no-beep", "--fake-mic", str(fixture)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(BASE),
    )

    def wait_status(substr, timeout):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                return False, "демон умер"
            if status.exists():
                try:
                    last = status.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                if substr in last:
                    return True, last
            time.sleep(0.3)
        return False, last

    try:
        ok, detail = wait_status("демон запущен", 25)
        if not check("демон стартовал и повесил хоткей", ok, detail):
            return

        time.sleep(0.5)
        # Управляем по сокету, а не нажатием клавиш: хоткей — CapsLock, и
        # keyboard.send переключил бы регистр на живой клавиатуре пользователя.
        send_command("toggle")                                      # старт
        ok, detail = wait_status("запись", 10)
        check("команда «toggle» включила запись", ok, detail)

        time.sleep(fixture_sec + 1.5)                        # даём «наговорить» фразу
        send_command("toggle")                                      # стоп

        ok, detail = wait_status("готово", 90)
        check("демон довёл запись до готового текста", ok, detail)

        clip = pyperclip.paste()
        check("текст попал в буфер обмена",
              clip != "__ДО_ТЕСТА__" and len(clip) > 10, f"«{clip[:100]}»")
        low = clip.lower()
        check("в буфере именно надиктованное (термины на месте)",
              "n8n" in low and "остгрес" in low, f"«{clip[:100]}»")
        check("текст очищен от слов-паразитов",
              "короче" not in low and "ну и потом" not in low, f"«{clip[:100]}»")
    finally:
        try:
            send_command("quit")
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        try:
            _, err = proc.communicate(timeout=5)
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-8:]
            if tail:
                print("    --- лог демона (хвост):")
                for line in tail:
                    print("        " + line)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Часть 2b: лампочка CapsLock не должна врать
# --------------------------------------------------------------------------- #

def test_capslock_lamp():
    """
    Инвариант: CapsLock горит тогда и только тогда, когда идёт запись.

    Проверяется путь, который его ломал: отмена по Alt+X прекращает запись, но
    второго нажатия CapsLock не было — и лампочка оставалась гореть.
    """
    print("\n=== 2b. Лампочка CapsLock: горит ровно во время записи ===")
    import json as _json

    from voice_hotkey import send_command

    if load_config().get("hotkey") != "caps lock":
        print("    (хоткей не CapsLock — проверка не применима)")
        return

    status = BASE / "_last_run.status"
    if status.exists():
        status.unlink()
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "voice_hotkey.py"), "--no-paste", "--no-beep",
         "--fake-mic", str(FIXTURES / "fixture_ru.wav")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(BASE),
    )

    def state():
        # Состояние спрашиваем у демона: у него есть цикл сообщений, а значит и
        # свежий GetKeyState. В процессе теста он был бы несвежим.
        return _json.loads(send_command("state"))

    def wait(cond, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return None
            try:
                s = state()
                if cond(s):
                    return s
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        return None

    try:
        if wait(lambda s: True, 30) is None:
            check("демон стартовал", False, "не ответил на команду state")
            return
        check("до записи лампочка погашена", not state()["capslock"])

        send_command("toggle")
        got = wait(lambda s: s["recording"], 15)
        check("запись началась", got is not None)
        time.sleep(1.5)
        s = state()
        check("во время записи лампочка горит", s["capslock"], f"{s}")
        check("наше собственное переключение не остановило запись", s["recording"])

        send_command("cancel")
        got = wait(lambda s: not s["recording"], 20)
        check("отмена прекратила запись", got is not None)
        time.sleep(0.8)
        s = state()
        check("ПОСЛЕ ОТМЕНЫ лампочка погашена", not s["capslock"], f"{s}")
    finally:
        try:
            send_command("quit")
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()


# --------------------------------------------------------------------------- #
# Часть 3: живой звук — ТОЛЬКО по флагу --loud
# --------------------------------------------------------------------------- #

def test_loopback():
    """
    Единственная проверка VAD на настоящем микрофоне и настоящем воздухе:
    фраза играет в динамики, автостоп обязан сработать по паузе.

    Разборчивость здесь не проверяется и проверена быть не может — Windows вычитает
    из микрофонного входа то, что сама вывела (эхоподавление). Слова проверяет часть 2.

    Играем строго в устройство ПО УМОЛЧАНИЮ и только убедившись, что это динамики:
    31.07 тест просил «Speakers (Senary Audio)», а звук ушёл в надетую гарнитуру —
    Senary отдаёт на активное устройство Windows. Проверять маршрут надо ДО, а не ПОСЛЕ.
    """
    import threading

    print("\n=== 3. Живой звук: динамики -> микрофон (флаг --loud) ===")
    out_idx = sd.default.device[1]
    out_name = sd.query_devices()[out_idx]["name"]
    if not any(k in out_name.lower() for k in ("speaker", "senary", "динамик")):
        check("вывод Windows — динамики, а не гарнитура", False,
              f"«{out_name}» — звук уйдёт в уши; переключите вывод и повторите")
        return
    check("вывод Windows — динамики, а не гарнитура", True, out_name)

    audio_out = read_wav(FIXTURES / "fixture_terms.wav")
    phrase_sec = len(audio_out) / 16000
    print(f"    сейчас прозвучит {phrase_sec:.0f} с синтезированной речи")

    cfg = load_config()
    cfg["input_device_preferences"] = ["Internal Microphone"]
    cfg["silence_stop_sec"] = 2.0
    cfg["wait_for_speech_sec"] = 12
    cfg["beep"] = False  # бип попал бы в запись и сбил калибровку фона

    def play():
        time.sleep(1.6)  # успеть пройти прогрев и калибровку
        sd.play(audio_out, 16000, blocking=True)

    thread = threading.Thread(target=play, daemon=True)
    thread.start()

    t0 = time.time()
    captured = Recorder(cfg).record_auto()
    elapsed = time.time() - t0
    thread.join(timeout=5)

    if not check("VAD услышал звук из комнаты", captured is not None,
                 "тишина — проверьте громкость динамиков"):
        return
    seconds = len(captured) / 16000
    peak = float(np.max(np.abs(captured)))
    check("уровень сигнала пригоден для распознавания", peak > 0.05,
          f"пик {peak:.3f}, RMS {float(np.sqrt(np.mean(captured ** 2))):.4f}")
    check("автостоп сработал по паузе, а не по таймауту", elapsed < phrase_sec + 8,
          f"{elapsed:.1f} с при длине фразы {phrase_sec:.1f} с")
    check("длительность захвата близка к длине фразы", seconds >= phrase_sec * 0.6,
          f"{seconds:.1f} с из {phrase_sec:.1f} с")
    print("     (разборчивость на этом пути не проверяется — мешает AEC драйвера)")


def main():
    loud = "--loud" in sys.argv
    test_hardware()
    test_hotkey_daemon()
    test_capslock_lamp()
    if loud:
        test_loopback()
    else:
        print("\n(живой звук пропущен — запустите с --loud, если можно шуметь)")
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    for name, _, detail in failed:
        print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
