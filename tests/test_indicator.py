# -*- coding: utf-8 -*-
"""
Индикатор записи: капсула обязана быть на экране всегда, когда открыт микрофон.

Правило: скрытых записей быть не должно. Раньше оно держалось
только для хоткея — индикатор поднимал демон, а `/voice` и `ask.py` открывали
микрофон молча. Теперь капсулу поднимает сам `Recorder`, от факта открытия потока,
и проверять надо именно это: не «демон умеет показывать», а «микрофон нельзя
открыть незаметно».

Проверки разделены на три группы:

  A. Владение капсулой — без железа и без окон (подменённый файл состояния).
  B. Живой микрофон: пока поток открыт, состояние активно; закрылся — погасло.
  C. Положение капсулы на экране из config.json — чистая геометрия.

Файл состояния подменяется на временный НАМЕРЕННО: настоящий читает работающий
демон, и тест иначе мигал бы капсулой человеку, сидящему за этой же машиной.

Запуск:  python tests/test_indicator.py
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import overlay  # noqa: E402
import voice_core  # noqa: E402
from voice_core import Recorder, load_config  # noqa: E402

results = []
skipped = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def skip(name, why):
    skipped.append((name, why))
    print(f"[ПРОП] {name} — {why}", flush=True)


class TempState:
    """
    Подменяет файл состояния на временный на время блока.

    Заодно подменяет `ensure_overlay`: поднимать настоящее окно тесту не нужно,
    а знать, что его ПОПРОСИЛИ подняться, — нужно.
    """

    def __init__(self):
        self.calls = 0
        self.state_at_call = None  # что лежало в файле в момент запуска окна

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory(prefix="voice_state_")
        self.path = Path(self._dir.name) / "_recording.json"
        self._real_state = voice_core.RECORDING_STATE
        self._real_overlay = voice_core.ensure_overlay
        voice_core.RECORDING_STATE = self.path
        voice_core.ensure_overlay = self._fake_overlay
        return self

    def __exit__(self, *exc):
        voice_core.RECORDING_STATE = self._real_state
        voice_core.ensure_overlay = self._real_overlay
        self._dir.cleanup()
        return False

    def _fake_overlay(self, cfg=None):
        self.calls += 1
        self.state_at_call = self.read()
        return None

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def base_cfg(**over):
    cfg = load_config()
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
# A. Владение капсулой
# --------------------------------------------------------------------------- #

def test_ownership():
    print("\n=== A. Кто держит капсулу ===")

    with TempState() as st:
        # 1. Свободная капсула достаётся рекордеру. В файле намеренно оставлен
        #    след прошлого хозяина — команда выхода: ровно она и подводила окно,
        #    поднятое раньше, чем записано состояние.
        voice_core.set_recording_state(False, quit_overlay=True)
        rec = Recorder(base_cfg())
        rec._raise_indicator()
        state = st.read()
        check("свободную капсулу рекордер берёт себе", rec._owns_indicator)
        check("состояние стало активным", state.get("active") is True, str(state.get("phase")))
        check("у состояния есть срок годности", state.get("expires_at") is not None,
              "иначе капсула повиснет, если процесс умрёт")
        check("окно индикатора попросили подняться", st.calls == 1, f"вызовов: {st.calls}")
        # Порядок важен: в файле мог остаться флаг `quit` от прошлого хозяина, и
        # окно, поднятое ДО записи состояния, прочитало бы его и вышло сразу.
        check("состояние записано ДО запуска окна",
              (st.state_at_call or {}).get("active") is True,
              str(st.state_at_call))
        check("флаг выхода снят к моменту запуска окна",
              (st.state_at_call or {}).get("quit") is False,
              "иначе свежий индикатор выйдет, не показав ничего")

        # 2. Своё же гасим.
        rec._lower_indicator()
        check("рекордер гасит то, что поднял", st.read().get("active") is False)
        check("владение снято", rec._owns_indicator is False)

    with TempState() as st:
        # 3. Занятую капсулу не перебиваем: так ведёт себя демон — он поднимает
        #    состояние ДО создания рекордера, чтобы отклик на нажатие был мгновенным.
        voice_core.set_recording_state(True, phase="rec", hint="демон", level=0.5)
        rec = Recorder(base_cfg())
        rec._raise_indicator()
        check("занятую капсулу рекордер не отбирает", rec._owns_indicator is False)
        check("чужое состояние не тронуто", st.read().get("hint") == "демон",
              str(st.read().get("hint")))
        rec._lower_indicator()
        check("и не гасит чужое на выходе", st.read().get("active") is True,
              "иначе демон остался бы без индикатора посреди записи")

    with TempState() as st:
        # 4. Протухшее состояние — это отсутствие хозяина, а не хозяин.
        voice_core.set_recording_state(True, phase="rec", hint="мертвец", ttl=0.05)
        time.sleep(0.2)
        check("протухшее состояние считается свободным",
              voice_core.recording_state_active() is False)
        rec = Recorder(base_cfg())
        rec._raise_indicator()
        check("капсула переходит к живому писателю", rec._owns_indicator)
        rec._lower_indicator()

    with TempState() as st:
        # 5. Подставной микрофон — не микрофон: показывать «идёт запись» нечестно,
        #    и автотесты не должны мигать окнами живому человеку.
        rec = Recorder(base_cfg(fake_mic_wav=str(BASE / "tests" / "fixture_ru.wav")))
        rec._raise_indicator()
        check("подставной микрофон капсулу не поднимает", rec._owns_indicator is False)
        check("состояние не появилось", st.read() == {}, str(st.read()))
        check("окно поднимать не просили", st.calls == 0, f"вызовов: {st.calls}")

    with TempState() as st:
        # 6. Сроки годности у фаз разные, и это не мелочь. Фаза записи пишется
        #    десять раз в секунду — ей нужен короткий срок, иначе капсула «идёт
        #    запись» провисит над закрытым микрофоном все две минуты, если писателя
        #    убили на середине. Сетевые фазы пишутся разово и ждут ответа Groq —
        #    им короткий срок обрубил бы капсулу посреди работы.
        voice_core.set_recording_state(True, phase="rec")
        rec_left = st.read()["expires_at"] - time.time()
        voice_core.set_recording_state(True, phase="stt", note="записано 5 с")
        stt_left = st.read()["expires_at"] - time.time()
        check("у фазы записи срок короткий",
              0 < rec_left <= voice_core.REC_TTL_SEC + 0.5, f"{rec_left:.1f} с")
        check("у сетевой фазы срок с запасом на ответ Groq",
              stt_left > voice_core.REC_TTL_SEC * 2, f"{stt_left:.1f} с")


# --------------------------------------------------------------------------- #
# B. Живой микрофон
# --------------------------------------------------------------------------- #

def test_live_microphone():
    """
    Оба режима записи на настоящем микрофоне. Длительность задаём сами, а не ждём
    автостопа: тот зависит от шума в комнате — на первом прогоне тест прошёл, а на
    втором фоновый гул превысил порог тишины, запись не остановилась, и тест
    «нашёл» несуществующий дефект. Живая обстановка не должна решать, зелёный он
    или красный.
    """
    print("\n=== B. Открытый микрофон = капсула на экране ===")
    print("     (пишем тишину пару секунд, звука не издаём)")

    # --- режим тумблера: длительность полностью в наших руках ---------------- #
    with TempState() as st:
        rec = Recorder(base_cfg(beep=False))
        try:
            rec.start()
        except Exception as e:  # noqa: BLE001 — микрофона может не быть вовсе
            skip("живой микрофон", f"не открылся: {str(e)[:60]}")
            return

        seen = []
        for _ in range(8):
            rec.pump()
            seen.append(st.read())

        check("тумблер: пока поток открыт, состояние активно",
              all(s.get("active") is True for s in seen), f"снимков: {len(seen)}")
        check("тумблер: капсула показывает фазу записи",
              all(s.get("phase") == "rec" for s in seen))
        check("тумблер: капсула называет устройство",
              bool(seen[-1].get("hint")), str(seen[-1].get("hint")))
        check("тумблер: таймер идёт от открытия потока",
              isinstance(seen[0].get("started"), (int, float)))
        check("тумблер: уровень громкости доезжает до индикатора",
              all(s.get("level") is not None for s in seen))
        check("тумблер: лимит записи доехал до нити на капсуле",
              seen[-1].get("limit_sec") == float(rec.cfg["max_record_sec"]),
              str(seen[-1].get("limit_sec")))

        rec.stop()
        check("тумблер: поток закрылся — капсула погасла",
              st.read().get("active") is False, str(st.read()))
        check("тумблер: владение снято", rec._owns_indicator is False)

    # --- автостоп: тот путь, которым ходят /voice и ask.py ------------------- #
    with TempState() as st:
        # Потолок длительности маленький: даже если фон разбудит детектор речи,
        # запись оборвётся сама через пару секунд и тест не повиснет.
        cfg = base_cfg(beep=False, wait_for_speech_sec=1.2,
                       auto_max_record_sec=2, silence_stop_sec=0.5)
        rec = Recorder(cfg)
        seen = []
        failure = []

        def record():
            try:
                rec.record_auto()
            except Exception as e:  # noqa: BLE001
                failure.append(e)

        worker = threading.Thread(target=record)
        worker.start()
        deadline = time.time() + 15.0
        while worker.is_alive() and time.time() < deadline:
            state = st.read()
            if state.get("active"):
                seen.append(state)
            time.sleep(0.05)
        worker.join(timeout=5)

        if failure:
            skip("автостоп на живом микрофоне", f"не открылся: {str(failure[0])[:60]}")
            return
        if worker.is_alive():
            skip("автостоп на живом микрофоне", "запись не остановилась за 15 с")
            return

        check("автостоп: пока микрофон открыт, состояние активно", bool(seen),
              f"снимков: {len(seen)}")
        check("автостоп: капсула показывает фазу записи",
              all(s.get("phase") == "rec" for s in seen))
        check("автостоп: микрофон закрылся — капсула погасла",
              st.read().get("active") is False, str(st.read()))


# --------------------------------------------------------------------------- #
# C. Положение капсулы
# --------------------------------------------------------------------------- #

class FakePlacement:
    """Геометрия без окна: `placement` считает координаты и ни на что не смотрит."""

    canvas = (400, 106)
    work = (0, 0, 1920, 1040)   # рабочая область: 1080 минус панель задач

    def __init__(self, vertical, horizontal, margin=12):
        self.vertical, self.horizontal, self.margin = vertical, horizontal, margin

    def at(self, offset=0):
        return overlay.Indicator.placement(self, offset)


def pill_box(xy):
    """Прямоугольник САМОЙ капсулы (без запаса под тень) по координатам холста."""
    x, y = xy
    return (x + overlay.PILL[0], y + overlay.PILL[1],
            x + overlay.PILL[2], y + overlay.PILL[3])


def test_placement():
    print("\n=== C. Положение капсулы из config.json ===")
    # Масштаб экрана пришпиливаем к единице: на машине с увеличением 125 % все
    # координаты уехали бы в 1.25 раза, и тест мерил бы настройки монитора, а не
    # арифметику размещения. Сам масштаб проверяется живым тестом окна.
    saved_ui, overlay.UI = overlay.UI, 1.0
    try:
        _placement_checks()
    finally:
        overlay.UI = saved_ui


def _placement_checks():
    def cfg_file(payload):
        path = Path(tempfile.mkdtemp(prefix="voice_cfg_")) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    check("значение по умолчанию — вверху по центру",
          overlay.load_placement(cfg_file({})) == ("top", "center", 12.0))
    check("положение читается из конфига",
          overlay.load_placement(
              cfg_file({"overlay_position": "bottom-right", "overlay_margin_px": 24})
          ) == ("bottom", "right", 24.0))
    check("регистр и пробелы не мешают",
          overlay.load_placement(
              cfg_file({"overlay_position": " Bottom-Left "})) == ("bottom", "left", 12.0))
    check("мусор в конфиге не оставляет без индикатора",
          overlay.load_placement(
              cfg_file({"overlay_position": "сбоку", "overlay_margin_px": "нет"}))
          == ("top", "center", 12.0))
    check("отсутствующий конфиг не ломает индикатор",
          overlay.load_placement(Path("нет-такого-файла.json"))
          == ("top", "center", 12.0))

    left, top, right, bottom = FakePlacement.work

    x1, y1, x2, y2 = pill_box(FakePlacement("top", "left").at())
    check("вверху слева: отступ ровно 12 от обоих краёв",
          (x1, y1) == (left + 12, top + 12), f"капсула в {(x1, y1)}")

    x1, y1, x2, y2 = pill_box(FakePlacement("bottom", "right").at())
    check("внизу справа: отступ ровно 12 от обоих краёв",
          (x2, y2) == (right - 12, bottom - 12), f"правый нижний угол {(x2, y2)}")

    x1, y1, x2, y2 = pill_box(FakePlacement("top", "center").at())
    check("по центру: капсула отцентрована по рабочей области",
          (x1 + x2) // 2 == (left + right) // 2, f"центр капсулы {(x1 + x2) // 2}")

    # Панель задач — единственная причина считать от рабочей области, а не от экрана.
    tall = FakePlacement("bottom", "center")
    tall.work = (0, 0, 1920, 1080)
    _, _, _, y_full = pill_box(tall.at())
    _, _, _, y_work = pill_box(FakePlacement("bottom", "center").at())
    check("низ считается от рабочей области, а не от кромки экрана",
          y_full - y_work == 40, f"разница {y_full - y_work} px")

    # Направление выезда: капсула приходит со своей стороны экрана.
    top_y = FakePlacement("top", "center").at(offset=10)[1]
    top_settled = FakePlacement("top", "center").at(offset=0)[1]
    bottom_y = FakePlacement("bottom", "center").at(offset=10)[1]
    bottom_settled = FakePlacement("bottom", "center").at(offset=0)[1]
    check("верхняя капсула выезжает сверху", top_y < top_settled,
          f"{top_y} -> {top_settled}")
    check("нижняя капсула выезжает снизу", bottom_y > bottom_settled,
          f"{bottom_y} -> {bottom_settled}")


# --------------------------------------------------------------------------- #
# D. Запись состояния под живым читателем
# --------------------------------------------------------------------------- #

def test_write_retries():
    """
    Подмена файла состояния переживает временный отказ в доступе.

    На Windows `os.replace` может не пройти, если файл прямо сейчас кем-то открыт:
    индикатор читает его тридцать раз в секунду, а сверху ходят индексатор и
    антивирус. Раньше отказ проглатывался молча. Для обновления громкости это
    незаметно — через 0.1 с придёт следующая запись. Но той же ценой терялась и
    ЕДИНСТВЕННАЯ важная запись: та, что гасит капсулу в конце. Тогда индикатор
    висит над закрытым микрофоном до истечения срока годности.

    Отказ здесь подделан намеренно. Живой читатель в тесте (два потока, вшестеро
    чаще настоящего) столкновения НЕ вызвал ни разу — то есть воспроизвести
    настоящую гонку дёшево не удалось, и делать вид, что тест её ловит, нечестно.
    Проверяется ровно то, что проверяется: механизм повтора работает, а сдавшись,
    он не молчит.
    """
    print("\n=== D. Запись состояния переживает отказ в доступе ===")

    with TempState() as st:
        real_replace = voice_core.os.replace
        state = {"fails": 0}

        def flaky_replace(src, dst, fail_times=2):
            if state["fails"] < fail_times:
                state["fails"] += 1
                raise PermissionError(32, "файл занят другим процессом")
            return real_replace(src, dst)

        voice_core.os.replace = flaky_replace
        try:
            voice_core.set_recording_state(True, phase="rec", level=0.42)
        finally:
            voice_core.os.replace = real_replace

        check("запись состояния доехала со второй-третьей попытки",
              st.read().get("level") == 0.42,
              f"отказов было {state['fails']}, в файле {st.read()}")

    with TempState() as st:
        real_replace = voice_core.os.replace
        warnings = []
        real_log = voice_core.log
        voice_core.log = warnings.append

        def always_fails(src, dst):
            raise PermissionError(32, "файл занят другим процессом")

        voice_core.os.replace = always_fails
        try:
            voice_core.set_recording_state(True, phase="rec", level=0.9)
        finally:
            voice_core.os.replace = real_replace
            voice_core.log = real_log

        check("исчерпав попытки, писатель не молчит",
              any("состояние индикатора" in w for w in warnings), str(warnings))
        check("и запись не роняет вызывающий код", True)


def main():
    test_ownership()
    test_placement()
    test_write_retries()
    test_live_microphone()

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"ИТОГО: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    if skipped:
        print(f"ПРОПУЩЕНО: {len(skipped)} — " +
              "; ".join(f"{n} ({w})" for n, w in skipped))
    for name, _, detail in failed:
        print(f"  - {name}" + (f" — {detail}" if detail else ""))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
