# -*- coding: utf-8 -*-
"""
Значок в области уведомлений: признак работы и кнопка «выключить инструмент».

Зачем именно значок, а не команда. Демон резидентный, а со сторожем (см.
`watchdog.py`) он ещё и поднимается сам — то есть выключить его «просто закрыв»
больше нельзя. Значит у инструмента, который всегда включён, обязан быть видимый
выключатель, иначе человек оказывается запертым: сочетание клавиш надо помнить,
а значок видно всегда и он же отвечает на вопрос «работает ли оно сейчас».

Написано на голом Win32 намеренно: `pystray` тянул бы новую зависимость в
проект, который ставят на чужие машины, ради одного значка. Оконный класс,
цикл сообщений и HICON тут ровно того же сорта, что уже сделаны в `overlay.py`.

Живёт в потоке демона, а не отдельным процессом: значок означает «демон жив»,
и умирать он обязан вместе с ним, без всякого TTL и присмотра.
"""

import ctypes
import ctypes.wintypes as wt
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ICON_FILE = BASE_DIR / "_tray.ico"

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_NULL = 0x0000
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040

ID_TITLE, ID_STOP = 1, 2

# LRESULT/WPARAM/LPARAM на x64 — 64-битные. Объявишь возврат как c_long — и
# оконная процедура падает на КАЖДОМ сообщении с большим lparam
# («int too long to convert»), причём молча: исключения внутри ctypes-callback
# Windows проглатывает, окно продолжает существовать, и тест «окно создано»
# остаётся зелёным. Ровно та же грабля, что с HWND без restype.
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint,
                             wt.WPARAM, wt.LPARAM)

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.TrackPopupMenu.argtypes = [wt.HANDLE, ctypes.c_uint, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.LoadImageW.restype = wt.HANDLE
user32.SetForegroundWindow.argtypes = [wt.HWND]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", ctypes.c_uint),
                ("uFlags", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint),
                ("hIcon", wt.HICON), ("szTip", ctypes.c_wchar * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", ctypes.c_wchar * 256), ("uVersion", ctypes.c_uint),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def make_icon_file(path=ICON_FILE):
    """
    Нарисовать значок микрофона, если его ещё нет.

    Рисуем, а не кладём готовый .ico в репозиторий: бинарник в git ради 4 КБ
    картинки — это ещё и файл, который сканер утечек обязан считать непросмотренным.
    """
    if path.exists():
        return path
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (245, 245, 247, 255)
    # капсула микрофона
    d.rounded_rectangle([24, 10, 40, 38], radius=8, fill=white)
    # дужка
    d.arc([16, 24, 48, 50], start=0, end=180, fill=white, width=5)
    # ножка и основание
    d.line([32, 46, 32, 54], fill=white, width=5)
    d.line([22, 55, 42, 55], fill=white, width=5)
    try:
        img.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        return path
    except OSError:
        return None


class TrayIcon:
    """
    Значок и его меню. `run()` крутит цикл сообщений — зовите в своём потоке.

    Цикл сообщений обязан жить в том же потоке, где создано окно: Windows
    доставляет сообщения потоку, а не процессу. Отсюда и требование звать
    `run()` из выделенного потока, а `stop()` — откуда угодно (он шлёт
    сообщение, а не трогает окно напрямую).
    """

    def __init__(self, on_stop, tooltip="Голосовой ввод", title="Голосовой ввод работает",
                 stop_label="Выключить инструмент"):
        self.on_stop = on_stop
        self.tooltip = tooltip[:127]
        self.title = title
        self.stop_label = stop_label
        self.hwnd = None
        self._nid = None
        self._ready = threading.Event()
        # Исключение внутри ctypes-callback Windows проглатывает: окно живёт,
        # значок висит, а сообщения не обрабатываются. Собираем ошибки сами —
        # иначе про них узнать неоткуда, и тест «окно создано» будет зелёным.
        self.errors = []
        self._proc = WNDPROC(self._wndproc)  # ссылку держим: иначе соберётся GC

    # -- окно и значок ------------------------------------------------------ #

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            return self._handle(hwnd, msg, wparam, lparam)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"msg={msg:#x}: {e}")
            return 0

    def _handle(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON and lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
            self._popup()
            return 0
        if msg == WM_COMMAND and (wparam & 0xFFFF) == ID_STOP:
            self._fire_stop()
            return 0
        if msg in (WM_CLOSE, WM_DESTROY):
            self._remove()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, ID_TITLE, self.title)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_STOP, self.stop_label)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Без SetForegroundWindow меню остаётся висеть после клика мимо — это
        # известное требование TrackPopupMenu, а не суеверие.
        user32.SetForegroundWindow(self.hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if chosen == ID_STOP:
            self._fire_stop()

    def _fire_stop(self):
        try:
            self.on_stop()
        except Exception:  # noqa: BLE001 — падение обработчика не должно ронять демона
            pass

    def _remove(self):
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def run(self):
        """Создать значок и крутить цикл сообщений до `stop()`."""
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = "ClaudeVoiceTray"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            # Класс уже зарегистрирован этим же процессом — это не ошибка.
            if kernel32.GetLastError() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                self._ready.set()
                return
        self.hwnd = user32.CreateWindowExW(0, "ClaudeVoiceTray", "ClaudeVoiceTray",
                                           0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            self._ready.set()
            return

        hicon = 0
        icon_path = make_icon_file()
        if icon_path:
            hicon = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 0, 0,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon:  # без своей картинки берём системную — лучше, чем пустое место
            hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION

        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = self.tooltip
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._nid = nid
        self._ready.set()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._remove()

    def wait_ready(self, timeout=5.0):
        return self._ready.wait(timeout)

    def stop(self):
        """Убрать значок. Можно звать из любого потока — шлём сообщение."""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
