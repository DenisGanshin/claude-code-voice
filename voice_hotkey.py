# -*- coding: utf-8 -*-
"""
Резидентный демон голосового ввода.

  CapsLock   — старт записи, второе нажатие — стоп. Текст расшифровывается
               и вставляется в активное поле ввода. Лампочка клавиши горит,
               пока идёт запись, — она же и служит индикатором.
  Alt+X      — отмена текущей записи (ничего не вставляется).
  Ctrl+Alt+Q — выход из демона.

Работает в любом окне: Claude Code, браузер, редактор. Текст вставляется через
буфер обмена (Ctrl+V) в то окно, из которого начали диктовать; прежнее содержимое
буфера восстанавливается. Если за время распознавания фокус ушёл в другое окно,
вставки НЕ будет: текст останется в буфере, а капсула скажет нажать Ctrl+V.

Полный текст каждой диктовки пишется в _transcripts/ и живёт 30 дней. Вернуть
последнюю в буфер обмена, если она всё-таки потерялась:

    python voice_hotkey.py --last

Тем же самым управляют по сокету, не трогая клавиатуру:

    python voice_hotkey.py --send toggle|cancel|quit|ping

Это единственный способ для автотестов: «нажать» CapsLock программно нельзя —
регистр переключится на живой клавиатуре, а нажатие уйдёт в активное окно.

Запуск: start_hotkey.cmd  (или python voice_hotkey.py)
"""

import atexit
import ctypes
import ctypes.wintypes  # подмодуль не подтягивается сам импортом ctypes
import json
import socket
import sys
import threading
import time
from pathlib import Path

import keyboard
import pyperclip

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keepalive import OFF_FILE  # noqa: E402 — общее имя файла-выключателя
from tray import TrayIcon  # noqa: E402
from voice_core import (  # noqa: E402
    FLASH_TTL_EXTRA,
    KEYS_FILE,
    Recorder,
    beep,
    clear_recording_state,
    ensure_overlay,
    last_transcript,
    level_from_rms,
    load_config,
    load_groq_keys,
    log,
    process_audio,
    release_overlay,
    save_recording,
    save_transcript,
    set_recording_state,
    write_status,
)

CONTROL_PORT = 47843  # bind на localhost = защита от второго демона И канал управления

COMMANDS = ("toggle", "cancel", "quit", "ping", "state")


def ensure_single_instance():
    """
    Два демона на одном хоткее — это две записи и два Ctrl+V подряд.
    Bind порта отдаёт ОС решать гонку, в отличие от pid-файла.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", CONTROL_PORT))
        s.listen(4)
    except OSError:
        return None
    return s  # держим сокет живым всё время работы демона


def send_command(command, timeout=3.0):
    """
    Отдать демону команду, не трогая клавиатуру.

    Зачем: хоткей — CapsLock, и автотест, «нажимающий» его через keyboard.send,
    переключает регистр по-настоящему, на живой клавиатуре пользователя. Пока идёт
    запись, всё, что он печатает, уходит заглавными. Проверять логику сессии надо
    так, чтобы посторонний за той же машиной этого вообще не замечал.
    """
    if command not in COMMANDS:
        raise ValueError(f"неизвестная команда: {command}")
    with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout) as s:
        s.sendall((command + "\n").encode())
        s.settimeout(timeout)
        try:
            return s.recv(64).decode("utf-8", "replace").strip()
        except socket.timeout:
            return ""


user32 = ctypes.windll.user32
# Без явного restype ctypes считает результат 32-битным int, и на x64 старшая
# половина хэндла молча отваливается. Сравнение окон тогда врёт в обе стороны:
# разные окна выглядят одинаковыми, одно и то же — разными.
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR,
                                  ctypes.c_int]


def foreground_window():
    """Хэндл активного окна; 0 — если система его не отдала."""
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def window_title(hwnd, limit=40):
    """Заголовок окна — только чтобы человеку было понятно, куда уехал фокус."""
    if not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(ctypes.wintypes.HWND(hwnd), buf, 256)
        title = buf.value.strip()
    except Exception:  # noqa: BLE001
        return ""
    return title[:limit] + "…" if len(title) > limit else title


def focus_moved(target):
    """
    Ушёл ли фокус из окна, в котором начали диктовать.

    Возвращает None, если всё на месте, иначе описание того, где фокус сейчас, —
    его показываем человеку, чтобы «не вставилось» не выглядело поломкой.
    """
    if not target:
        return None  # окно старта неизвестно — вести себя как раньше
    try:
        if not user32.IsWindow(ctypes.wintypes.HWND(target)):
            return "окно закрыто"
        current = foreground_window()
    except Exception:  # noqa: BLE001
        return None  # проверка сломалась — не она повод не вставлять текст
    if current == target:
        return None
    return window_title(current) or "другое окно"


def paste_text(text, cfg, target=None):
    """
    Вставка в то окно, из которого начали диктовать.

    Возвращает (описание, исход), где исход — один из:
      "pasted"     текст ушёл в то самое окно;
      "clipboard"  штатно оставлен в буфере (авто-вставка выключена);
      "lost"       фокус сменился, вставка отменена, текст ждёт в буфере.

    Три исхода, а не «получилось/нет»: выключенная авто-вставка и уехавший фокус
    оба оставляют текст в буфере, но первое — это то, о чём человек просил, а
    второе — то, о чём он обязан узнать.

    Почему сверяем окно (жалоба 2026-08-06). Между «стоп» и вставкой проходит
    распознавание — на длинной диктовке это несколько секунд, и всё это время
    человек волен переключиться. `keyboard.send` адресует не окно, а систему:
    текст уходит туда, где фокус СЕЙЧАС. Хуже того, следом буфер восстанавливался
    на прежнее содержимое — и диктовка пропадала целиком. Чем длиннее запись, тем
    шире это окно и тем дороже потеря.

    Фокус обратно не отбираем намеренно: человек переключился по своей воле и,
    возможно, уже печатает в другом окне. Текст остаётся в буфере — это
    восстановимо одним Ctrl+V, в отличие от нажатий, уехавших не туда.
    """
    try:
        previous = pyperclip.paste()
    except Exception:  # noqa: BLE001
        previous = None

    pyperclip.copy(text)
    if not cfg.get("auto_paste", True):
        return "в буфере обмена (авто-вставка выключена)", "clipboard"

    # Пользователь ещё может удерживать модификаторы хоткея — иначе Ctrl+Alt+V
    # уедет в приложение как другое сочетание.
    time.sleep(0.25)
    for mod in ("ctrl", "alt", "shift", "windows"):
        try:
            keyboard.release(mod)
        except Exception:  # noqa: BLE001
            pass

    # Сверка ПОСЛЕ паузы, а не до: четверть секунды — тоже время, за которое
    # успевает сработать Alt+Tab.
    moved = focus_moved(target)
    if moved:
        return f"фокус ушёл в «{moved}» — текст в буфере, нажмите Ctrl+V", "lost"

    keyboard.send("ctrl+v")

    # И ещё раз — уже после нажатия. Окно могло смениться в тот же миг; тогда
    # текст, возможно, не дошёл, и затирать им буфер нельзя.
    if focus_moved(target):
        return "фокус сменился при вставке — текст в буфере, нажмите Ctrl+V", "lost"

    if previous is not None:
        def restore():
            time.sleep(1.0)
            try:
                if pyperclip.paste() == text:
                    pyperclip.copy(previous)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=restore, daemon=True).start()
    return "вставлено в активное окно", "pasted"


# Индикатор записи (файл состояния, запуск окна, шкала громкости) живёт в
# voice_core: микрофон открывает ядро, и капсулу поднимает оно же — иначе
# требование «скрытых записей быть не должно» держалось бы только для хоткея,
# а /voice и ask.py писали бы молча.

VK_CAPITAL = 0x14


def capslock_on():
    """Младший бит GetKeyState у переключателей — это состояние «залипания», не нажатие."""
    return bool(ctypes.windll.user32.GetKeyState(VK_CAPITAL) & 1)


class Daemon:
    def __init__(self, overrides=None):
        self.cfg = load_config()
        if overrides:
            self.cfg.update(overrides)
        self.keys = load_groq_keys()
        self.toggle = threading.Event()
        self.cancel = threading.Event()
        self.quit = threading.Event()
        self.busy = False
        self.recording = False
        self.overlay = None
        self._flash_gen = 0  # чтобы отложенное гашение не стёрло уже НОВУЮ запись
        self._synthetic_until = 0.0  # окно, в котором нажатие CapsLock — наше собственное
        self._lamp_lock = threading.Lock()  # set_lamp зовут два потока: сессия и сторож
        self._lamp_misses = 0  # сколько раз подряд лампа не поддалась
        self.tray = None

    # -- обработчики хоткеев (выполняются в потоке хука: только флаги!) ------ #

    def on_toggle(self):
        if time.time() < self._synthetic_until:
            # Это мы сами только что переключили лампочку — не команда пользователя.
            self._synthetic_until = 0.0
            return
        if self.busy:
            # Нажатие отвергнуто, но лампочку Windows УЖЕ переключила: она горит от
            # самого факта нажатия, до всякого нашего участия. Молча выйти нельзя —
            # состояния разъедутся на единицу, и следующее нажатие сработает
            # наоборот (жалоба 2026-08-05: «на выключение он наоборот включился»).
            # Сторож вернёт лампу на место, звук скажет, что команда не принята.
            log("[warn] идёт распознавание предыдущей записи, подождите")
            beep(self.cfg, "error")
            return
        self.toggle.set()

    def on_cancel(self):
        self.cancel.set()
        self.toggle.set()

    def on_quit(self):
        self.quit.set()
        self.toggle.set()

    # -- канал управления --------------------------------------------------- #

    def serve_control(self, sock):
        """
        Тот же сокет, что сторожит второй экземпляр, принимает и команды.

        Отдельного порта не заводим: единственность демона и право им управлять —
        это одно и то же право, и разводить их по двум портам значило бы плодить
        состояние, которое может разъехаться.
        """
        handlers = {"toggle": self.on_toggle, "cancel": self.on_cancel,
                    "quit": self.on_quit, "ping": lambda: None}
        sock.settimeout(0.5)
        while not self.quit.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2.0)
                    command = conn.recv(64).decode("utf-8", "replace").strip()
                    if command == "state":
                        # Состояние лампочки спрашивают у демона, а не читают со
                        # стороны: GetKeyState отдаёт снимок очереди сообщений
                        # вызывающего потока, и в процессе без цикла сообщений
                        # (тест, скрипт) он бывает несвежим.
                        conn.sendall((json.dumps({
                            "recording": self.recording,
                            "capslock": capslock_on(),
                            "busy": self.busy,
                        }) + "\n").encode())
                        continue
                    handler = handlers.get(command)
                    if handler is None:
                        conn.sendall(b"unknown\n")
                        continue
                    handler()
                    conn.sendall(b"ok\n")
                    log(f"[info] команда по сокету: {command}")
                except OSError:
                    pass

    # -- основной цикл ------------------------------------------------------ #

    def run(self, control_sock=None):
        if not self.keys:
            log(f"[error] GROQ_API_KEY не найден в {KEYS_FILE} — демон не стартует")
            return 2

        if control_sock is not None:
            threading.Thread(target=self.serve_control, args=(control_sock,),
                             daemon=True).start()
        threading.Thread(target=self.guard_lamp, daemon=True).start()

        hotkey = self.cfg["hotkey"]
        cancel_key = self.cfg.get("cancel_hotkey", "alt+x")
        quit_key = self.cfg.get("quit_hotkey", "ctrl+alt+q")
        keyboard.add_hotkey(hotkey, self.on_toggle)
        keyboard.add_hotkey(cancel_key, self.on_cancel)
        keyboard.add_hotkey(quit_key, self.on_quit)

        self.start_overlay()
        self.start_tray()
        probe = Recorder(self.cfg)
        log("=" * 62)
        log(f"  Голосовой ввод активен.  Микрофон: {probe.device_name}")
        log(f"  {hotkey.upper()} — старт/стоп   {cancel_key.upper()} — отмена   "
            f"{quit_key.upper()} — выход")
        log("=" * 62)
        write_status("демон запущен, ожидание хоткея")

        while not self.quit.is_set():
            self.toggle.wait(timeout=0.5)
            if not self.toggle.is_set():
                continue
            self.toggle.clear()
            if self.quit.is_set():
                break
            self.cancel.clear()
            self._session()

        self.set_lamp(False)  # выход на ходу не должен оставить включённый CapsLock
        self.stop_overlay()
        if self.tray:
            self.tray.stop()
        log("[info] демон остановлен")
        write_status("демон остановлен")
        return 0

    # -- экранный индикатор ------------------------------------------------- #

    def start_overlay(self):
        """
        Поднять окно индикатора на всё время жизни демона.

        Гасим состояние ПЕРЕД запуском: в файле мог остаться хвост от прошлого
        запуска, и свежий индикатор нарисовал бы капсулу над несуществующей записью.
        """
        clear_recording_state()
        self.overlay = ensure_overlay(self.cfg)

    def stop_overlay(self):
        # force: демон — резидент, на выходе окно закрывается целиком, даже если
        # его рисует индикатор, оставшийся от прошлого запуска.
        release_overlay(force=True)

    # -- значок в трее ------------------------------------------------------- #

    def start_tray(self):
        """
        Значок с единственным пунктом «Выключить инструмент».

        Он нужен именно потому, что появился сторож (`keepalive.py`): демон
        теперь поднимается сам, и «просто закрыть» его больше нельзя — через
        пять минут он вернётся. Инструмент, который сам себя чинит, обязан
        иметь видимый выключатель, иначе человек оказывается заперт внутри
        собственной автоматики.
        """
        try:
            self.tray = TrayIcon(on_stop=self.stop_from_tray,
                                 tooltip="Голосовой ввод: CapsLock — старт/стоп")
            threading.Thread(target=self.tray.run, daemon=True).start()
            self.tray.wait_ready(timeout=5.0)
        except Exception as e:  # noqa: BLE001 — без значка работать можно, без записи нет
            log(f"[warn] значок в трее не создан: {e}")
            self.tray = None

    def stop_from_tray(self):
        """
        Выключение по кнопке: это осознанный отказ от инструмента, а не сбой.

        Поэтому кладём файл-выключатель — сторож обязан его увидеть и НЕ
        поднимать демона обратно. Без файла кнопка была бы обманом: значок
        исчезал бы, а через пять минут всё возвращалось.
        """
        try:
            OFF_FILE.write_text(
                f"выключено через значок в трее {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"включить обратно: start_hotkey.cmd (файл удалится сам)\n",
                encoding="utf-8")
            log("[info] выключено через значок — сторож поднимать не будет")
        except OSError as e:
            log(f"[warn] не удалось создать {OFF_FILE.name}: {e} — "
                f"сторож поднимет демона снова")
        self.on_quit()

    def set_lamp(self, on):
        """
        Привести лампочку CapsLock в соответствие с тем, идёт ли запись.

        Инвариант: горит ⟺ пишем. Без этого лампочка врёт в трёх случаях — отмена
        по Alt+X, упёрлись в лимит длительности, выход из демона на ходу: во всех
        запись прекращается, а второго нажатия CapsLock не было, и он остаётся
        включённым. Плюс зеркальный случай: CapsLock был включён ДО старта, тогда
        нажатие его гасит и запись идёт с погашенной лампочкой.

        Переключаем единственным доступным способом — синтетическим нажатием: менять
        состояние в обход клавиатуры (SetKeyboardState) система видит только внутри
        своего потока. Своё же нажатие приходит обратно в наш хук, поэтому на
        полсекунды открываем окно, в котором on_toggle обязан его проигнорировать.

        Зовут метод два потока — сессия записи и сторож, — поэтому под замком:
        два синтетических нажатия внахлёст переключили бы лампу дважды и вернули
        её ровно туда, откуда чинили.
        """
        if self.cfg.get("hotkey") != "caps lock":
            return
        with self._lamp_lock:
            if capslock_on() == on:
                self._lamp_misses = 0
                return
            self._synthetic_until = time.time() + 0.5
            try:
                keyboard.send("caps lock")
            except Exception as e:  # noqa: BLE001 — лампочка не повод ронять запись
                self._synthetic_until = 0.0
                log(f"[warn] не удалось переключить CapsLock: {e}")

    # Сколько попыток подряд сторож делает, прежде чем отступиться. Без предела
    # клавиатура мигала бы вечно: если система нажатие не принимает, повтор каждые
    # 0.4 с ничего не исправит, а мешать будет заметно.
    LAMP_MAX_MISSES = 3

    def guard_lamp(self):
        """
        Держит инвариант «лампочка горит ⟺ идёт запись» непрерывно, а не по событиям.

        Разовой синхронизации в начале и конце сессии оказалось мало: лампу зажигает
        сама Windows на нажатие клавиши, независимо от того, приняли мы команду или
        отвергли. Каждое отвергнутое нажатие (демон занят распознаванием, старт
        микрофона ещё идёт) оставляло лампу в состоянии, противоположном истине,
        и дальше всё работало наоборот, пока человек не выравнивал вручную.

        Сторож — отдельный поток намеренно: во время распознавания главный цикл
        стоит внутри сетевого вызова, и починить лампу оттуда некому — а именно в
        этот момент нажатия и отвергаются.
        """
        while not self.quit.is_set():
            time.sleep(0.4)
            self.lamp_tick()

    def lamp_tick(self):
        """
        Один шаг сверки. Отдельным методом — чтобы тест мог проверить логику,
        не трогая живую клавиатуру: синтетическое нажатие CapsLock в автотесте
        переключило бы регистр человеку, сидящему за этой же машиной.
        """
        if time.time() < self._synthetic_until:
            return  # ждём, пока наше же нажатие дойдёт до хука
        if capslock_on() == self.recording:
            self._lamp_misses = 0
            return
        if self._lamp_misses >= self.LAMP_MAX_MISSES:
            return
        self._lamp_misses += 1
        log(f"[info] лампочка разошлась с записью — выравниваю "
            f"(попытка {self._lamp_misses})")
        self.set_lamp(self.recording)

    def flash(self, phase, note=None, seconds=1.6):
        """
        Показать итог («готово», «ошибка») и погасить индикатор через паузу.

        Гасим по номеру поколения: если за это время началась новая запись, старый
        таймер обязан промолчать, иначе он стирает чужое состояние.
        """
        self._flash_gen += 1
        gen = self._flash_gen
        # Срок годности чуть больше паузы: обычно гасит таймер, но если демон
        # к тому моменту умрёт, индикатор погасит себя сам — по истёкшему сроку.
        set_recording_state(True, phase=phase, note=note, ttl=seconds + FLASH_TTL_EXTRA)

        def clear():
            if gen == self._flash_gen:
                set_recording_state(False)

        threading.Timer(seconds, clear).start()

    def _session(self):
        """Одна запись: старт -> ждём второе нажатие -> обработка -> вставка."""
        self._flash_gen += 1  # отменяет отложенное гашение от прошлой записи
        # Флаг поднимаем ДО открытия микрофона: пока поток раскручивается, лампочка
        # уже горит (её зажгла Windows на нажатие), и сторож обязан считать это
        # правдой, а не гасить её на те доли секунды, что длится старт.
        self.recording = True
        # Окно-адресат — прямо сейчас, пока фокус ещё там, откуда нажали хоткей.
        # Позже он уже не показатель: между стопом и вставкой человек успевает
        # переключиться, и без этого снимка вставка уходит в чужое окно.
        target = foreground_window()
        rec = Recorder(self.cfg)
        limit = float(self.cfg.get("max_record_sec", 600))
        # Отсчёт — от нажатия, а не от готовности потока: иначе таймер на индикаторе
        # прыгает назад, когда микрофон наконец открылся.
        started = time.time()
        # Индикатор — сразу, ещё до готовности микрофона: человек нажал и должен
        # немедленно увидеть отклик. Иначе пауза читается как «не сработало».
        set_recording_state(True, phase="rec", started=started, limit_sec=limit,
                            hint="включаю микрофон", level=0.0)
        try:
            rec.start(on_ready=lambda: (beep(self.cfg, "start"), log("[rec] пишу…")))
        except Exception as e:  # noqa: BLE001
            log(f"[error] микрофон недоступен: {e}")
            beep(self.cfg, "error")
            self.recording = False
            self.set_lamp(False)
            self.flash("error", "микрофон недоступен")
            return
        self.set_lamp(True)  # запись пошла — лампочка обязана гореть

        write_status(f"запись, устройство: {rec.device_name}")
        device = rec.device_name.split(" [")[0]

        fake = bool(rec.fake_wav)  # звук из файла: капсула обязана это показать

        def show(level=0.0):
            set_recording_state(True, phase="rec", started=started, limit_sec=limit,
                                hint=device, level=level, fake=fake)

        show()
        while not self.toggle.is_set() and not self.quit.is_set():
            if not rec.pump():  # уперлись в максимум длительности
                break
            show(level_from_rms(rec.last_rms))
        self.toggle.clear()

        audio = rec.stop()
        self.recording = False
        # Гасим здесь, а не в конце: дальше идут ветки с return, и любая из них
        # оставила бы лампочку гореть над уже несуществующей записью.
        self.set_lamp(False)
        beep(self.cfg, "stop")

        if self.cancel.is_set():
            self.cancel.clear()
            log("[info] запись отменена")
            write_status("запись отменена")
            self.flash("cancel", "запись отменена")
            return

        if audio is None or len(audio) < 16000 * 0.4:
            log("[info] слишком короткая запись — пропускаю")
            beep(self.cfg, "empty")
            write_status("слишком короткая запись")
            self.flash("cancel", "слишком короткая запись")
            return

        seconds = len(audio) / 16000
        log(f"[info] записано {seconds:.1f} с — распознаю…")
        write_status(f"записано {seconds:.1f} с, распознаю")
        recorded = f"записано {seconds:.1f} с"
        set_recording_state(True, phase="stt", note=recorded)

        # Сохраняем ДО облака: если Groq не ответит, запись не должна пропасть.
        saved = save_recording(audio, self.cfg)

        self.busy = True
        try:
            final, raw, note = process_audio(
                audio, self.cfg, self.keys,
                on_phase=lambda p: set_recording_state(True, phase=p, note=recorded),
            )
        except RuntimeError as e:
            log(f"[error] {e}")
            if saved:
                log(f"[info] запись цела: {saved} — можно прогнать заново: "
                    f"python voice_once.py --file \"{saved}\"")
            beep(self.cfg, "error")
            write_status(f"ошибка: {e}; запись сохранена")
            self.flash("error", "запись сохранена, см. лог", seconds=3.0)
            return
        finally:
            self.busy = False

        if not final:
            log("[info] речь не распознана")
            beep(self.cfg, "empty")
            write_status("речь не распознана")
            self.flash("cancel", "речь не распознана")
            return

        where, outcome = paste_text(final, self.cfg, target)
        # Текст на диск — независимо от того, дошёл он до поля ввода. Файл нужен
        # ровно в том случае, когда не дошёл.
        save_transcript(final, self.cfg, source="hotkey", note=where)
        log(f"[ok] {note}; {where}")
        log(f"     «{final[:120]}{'…' if len(final) > 120 else ''}»")
        write_status(f"готово: {len(final)} симв., {where}")
        if outcome != "lost":
            self.flash("done", where)
            return
        # Не дошло: текст цел, но нужно действие человека. Держим капсулу дольше
        # обычного — она и есть единственный способ узнать об этом, не читая лог.
        #
        # В капсулу идёт ДЕЙСТВИЕ, а не полное описание: подпись обрезается по
        # ширине плашки, и «фокус ушёл в «Telegram» — текст в ...» теряло ровно
        # тот хвост, ради которого капсула и показывается (замер на экране 06.08).
        # Куда именно уехал фокус — в логе и статусе, там место не кончается.
        beep(self.cfg, "error")
        self.flash("clipboard", "нажмите Ctrl+V", seconds=8.0)


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-paste", action="store_true",
                    help="только класть в буфер обмена, не жать Ctrl+V (нужно тестам)")
    ap.add_argument("--no-beep", action="store_true",
                    help="без звуковых сигналов (тесты не должны шуметь в наушники)")
    ap.add_argument("--device", help="подстрока имени микрофона, перекрывает config.json")
    ap.add_argument("--fake-mic", help="WAV вместо микрофона (для автотестов)")
    ap.add_argument("--send", choices=COMMANDS,
                    help="отдать команду уже запущенному демону и выйти "
                         "(так тесты управляют записью, не трогая клавиатуру)")
    ap.add_argument("--last", action="store_true",
                    help="вернуть последнюю расшифровку в буфер обмена")
    args = ap.parse_args()

    if args.last:
        # Демон для этого не нужен намеренно: текст достают в том числе тогда,
        # когда демон упал, — а это как раз случай, в котором он и терялся.
        text = last_transcript()
        if not text:
            log("[info] сохранённых расшифровок нет")
            return 1
        pyperclip.copy(text)
        preview = text[:120] + ("…" if len(text) > 120 else "")
        print(f"в буфере обмена ({len(text)} симв.): «{preview}»")
        return 0

    if args.send:
        try:
            print(send_command(args.send) or "(нет ответа)")
            return 0
        except OSError as e:
            log(f"[error] демон не отвечает на порту {CONTROL_PORT}: {e}")
            return 1

    overrides = {}
    if args.no_paste:
        overrides["auto_paste"] = False
    if args.no_beep:
        overrides["beep"] = False
    if args.device:
        overrides["input_device_preferences"] = [args.device]
    if args.fake_mic:
        overrides["fake_mic_wav"] = args.fake_mic

    # Явный запуск и есть включение: снимаем выключатель, иначе сторож будет
    # молчать и после перезагрузки инструмент опять окажется выключенным —
    # причём без единого следа, почему.
    #
    # Кроме запуска с подставным микрофоном: это автотест, а не решение человека
    # пользоваться инструментом. Иначе тест, поставивший выключатель, чтобы
    # сторож не лез в его порт, сам же этот выключатель и снимает.
    if not args.fake_mic and OFF_FILE.exists():
        try:
            OFF_FILE.unlink()
            log("[info] выключатель снят — сторож снова следит за демоном")
        except OSError as e:
            log(f"[warn] не удалось убрать {OFF_FILE.name}: {e}")

    lock = ensure_single_instance()
    if lock is None:
        log(f"[error] демон уже запущен (порт {CONTROL_PORT} занят). "
            f"Второй экземпляр не нужен.")
        return 1
    # Индикатор — отдельный процесс и переживает демона. Если тот уйдёт, не погасив
    # состояние (Ctrl+C, закрытое окно, падение), капсула останется висеть на экране.
    atexit.register(clear_recording_state)
    try:
        return Daemon(overrides).run(lock)
    except KeyboardInterrupt:
        log("\n[info] прервано пользователем")
        return 0
    finally:
        clear_recording_state()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
