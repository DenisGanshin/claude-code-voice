# -*- coding: utf-8 -*-
"""
Экранный индикатор записи — капсула вверху экрана поверх всех окон.

Почему не tkinter Canvas (как было раньше): у него нет сглаживания. Скруглённый
угол выходил лесенкой из пикселей, дуга — тем же. Никакой подбор цветов этого не
лечит. Здесь картинку рисует Pillow (форма — с трёхкратным запасом и уменьшением,
то есть с честным антиалиасингом), а на экран её кладёт сама Windows через
UpdateLayeredWindow — окно с попиксельной прозрачностью. Отсюда мягкая тень,
гладкие края и плавное появление.

Отдельный процесс, а не поток демона: свой цикл сообщений, а демон в это время
занят записью и сетью. Связь — через файл состояния `_recording.json`.

Окно прозрачно для мыши (WS_EX_TRANSPARENT) и не появляется в панели задач
(WS_EX_TOOLWINDOW).

    python overlay.py            # обычный запуск (демон делает это сам)
    python overlay.py --demo 12  # прогон всех фаз для проверки внешнего вида
"""

import ctypes
import json
import math
import random
import sys
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "_recording.json"
CONFIG_FILE = BASE / "config.json"

# --------------------------------------------------------------------------- #
# Оформление
# --------------------------------------------------------------------------- #

SS = 3                      # во сколько раз крупнее рисуется форма перед уменьшением
CANVAS = (400, 106)         # холст: капсула + место под тень
PILL = (12, 8, 388, 78)     # сама капсула, 376x70
RADIUS = 35
# Холст шире капсулы — по краям запас под тень. Отступ считаем от КАПСУЛЫ, а не от
# холста: иначе «12 пикселей от края» внизу превратились бы в 40, потому что снизу
# запаса под тень больше, чем сверху.
PILL_INSET = (PILL[0], PILL[1], CANVAS[0] - PILL[2], CANVAS[1] - PILL[3])
DEFAULT_POSITION = "top-center"
DEFAULT_MARGIN = 12         # отступ от кромки рабочей области
DOT = (36, 43, 6.0)         # индикатор состояния: центр и радиус
ROW1, ROW2, TEXT_X = 31, 55, 56
LIMIT_LINE = (56, 356, 67, 2.5)  # нить лимита: слева, справа, верх, толщина

C_PILL = (26, 26, 30, 238)
C_EDGE = (255, 255, 255, 26)      # еле заметная кромка: отделяет плашку от тёмного окна
C_SHADOW = (0, 0, 0, 130)
C_TEXT = (245, 245, 247, 255)
C_DIM = (150, 150, 160, 255)
C_REC = (255, 69, 58, 255)        # красный записи
C_WARN = (255, 159, 10, 255)      # время на исходе
C_WORK = (100, 170, 255, 255)     # обработка после стопа
C_OK = (48, 209, 88, 255)
C_IDLE_BAR = (58, 58, 66, 255)

BARS = 22
BAR_W, BAR_GAP = 3.2, 2.6
WAVE_X, WAVE_Y = 240.0, 40.0
WAVE_MAX = 21.0
WAVE_CX = WAVE_X + BARS * (BAR_W + BAR_GAP) / 2  # центр полосы: сюда же галочка и точки

FONT_DIR = Path("C:/Windows/Fonts")

# Масштаб экрана. Рисуем в ФИЗИЧЕСКИХ пикселях (окно объявлено DPI-aware), поэтому
# при увеличении 125 % всё оформление надо умножить на 1.25 — иначе капсула выйдет
# на четверть мельче системных элементов, а подпись микрофона — мельче читаемых 11 px.
UI = 1.0
F_TIME = F_LIMIT = F_SMALL = None


def font(name, size):
    """Segoe UI — системный шрифт Windows; на его фоне любой другой читается как чужой."""
    for candidate in (name, "segoeui.ttf"):
        path = FONT_DIR / candidate
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                pass
    return ImageFont.load_default()


def setup_scale():
    global UI, F_TIME, F_LIMIT, F_SMALL
    try:
        UI = ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:  # noqa: BLE001 — до Windows 10 1607 берём масштаб экрана из GDI
        try:
            dc = ctypes.windll.user32.GetDC(None)
            UI = ctypes.windll.gdi32.GetDeviceCaps(dc, 88) / 96.0  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(None, dc)
        except Exception:  # noqa: BLE001
            UI = 1.0
    UI = max(1.0, min(3.0, UI))
    F_TIME = font("seguisb.ttf", round(24 * UI))     # Segoe UI Semibold
    F_LIMIT = font("segoeui.ttf", round(16 * UI))
    F_SMALL = font("segoeui.ttf", round(14 * UI))


def canvas_px():
    return round(CANVAS[0] * UI), round(CANVAS[1] * UI)


setup_scale()  # значения по умолчанию, чтобы модуль можно было импортировать


def load_placement(config_file=CONFIG_FILE):
    """
    Где висеть капсуле: (вертикаль, горизонталь, отступ).

    Читаем config.json напрямую, а не через voice_core: индикатор — отдельный
    процесс, и тянуть ради двух чисел sounddevice с requests значит платить
    полсекунды на старте и держать в памяти звуковой стек, который здесь не нужен.

    Любое непонятное значение молча превращается в положение по умолчанию:
    опечатка в конфиге не повод остаться без индикатора записи.
    """
    position, margin = DEFAULT_POSITION, DEFAULT_MARGIN
    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
        position = str(cfg.get("overlay_position") or position).strip().lower()
        margin = float(cfg.get("overlay_margin_px", margin))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    vertical, _, horizontal = position.partition("-")
    if vertical not in ("top", "bottom"):
        vertical = "top"
    if horizontal not in ("left", "center", "right"):
        horizontal = "center"
    return vertical, horizontal, max(0.0, margin)


PHASES = {
    "stt": ("расшифровываю", C_WORK),
    "edit": ("привожу в порядок", C_WORK),
    "done": ("готово", C_OK),
    "error": ("ошибка", C_REC),
    "cancel": ("отменено", C_DIM),
}

# --------------------------------------------------------------------------- #
# Win32: слоёное окно
# --------------------------------------------------------------------------- #

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020     # клики проходят насквозь
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080      # не показывать в панели задач и Alt+Tab
WS_EX_NOACTIVATE = 0x08000000      # никогда не забирать фокус у активного окна
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
PM_REMOVE, WM_QUIT, WM_DESTROY = 0x0001, 0x0012, 0x0002

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


SPI_GETWORKAREA = 0x0030


def work_area():
    """
    Область экрана без панели задач — (лево, верх, право, низ) в физических пикселях.

    Полный экран здесь не годится: панель задач по умолчанию стоит внизу, и капсула,
    поставленная «внизу по центру», оказалась бы наполовину под ней. Работает это
    только потому, что процесс объявлен DPI-осведомлённым: до объявления система
    отдала бы логические координаты, а рисуем мы в физических.
    """
    rect = RECT()
    try:
        if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 — не ответила система, берём весь экран
        pass
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _declare():
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
        wintypes.HDC, ctypes.POINTER(POINT), wintypes.DWORD,
        ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    # Без явных типов ctypes считает, что функция возвращает и принимает 32-битный int.
    # На 64-битной Windows дескриптор в него не влезает: он либо молча обрезается,
    # либо падает «int too long to convert». Описываем ВСЁ, что оперирует HDC/HWND.
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                             wintypes.LPVOID, wintypes.UINT]
    ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    ctypes.windll.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class LayeredWindow:
    """
    Окно, содержимое которого целиком задаётся картинкой RGBA.

    Windows требует ПРЕДУМНОЖЕННУЮ альфу: цвет каждого пикселя должен быть заранее
    помножен на его прозрачность. Без этого полупрозрачные края капсулы светлеют
    ореолом — типовой признак «самодельного» оверлея.
    """

    _wndproc_ref = None  # ссылку держим живой: сборщик мусора не должен съесть колбэк

    def __init__(self, width, height):
        _declare()
        self.w, self.h = width, height
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        LayeredWindow._wndproc_ref = WNDPROC(wndproc)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = LayeredWindow._wndproc_ref
        cls.hInstance = hinst
        cls.lpszClassName = "ClaudeVoiceOverlay"
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "ClaudeVoiceOverlay", "Claude Voice", WS_POPUP,
            0, 0, width, height, None, None, hinst, None,
        )

        screen_dc = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(screen_dc)
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height          # минус = сверху вниз, как в PIL
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0           # BI_RGB
        info = BITMAPINFO()
        info.bmiHeader = header
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), 0,
                                             ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.dc, self.bitmap)
        user32.ReleaseDC(None, screen_dc)
        self.visible = False

    def blit(self, image, x, y, alpha=1.0):
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        a = rgba[:, :, 3].astype(np.uint16)
        rgb = (rgba[:, :, :3].astype(np.uint16) * a[:, :, None] // 255).astype(np.uint8)
        bgra = np.dstack([rgb[:, :, 2], rgb[:, :, 1], rgb[:, :, 0], rgba[:, :, 3]])
        buf = np.ascontiguousarray(bgra)
        ctypes.memmove(self.bits, buf.ctypes.data, buf.nbytes)

        blend = BLENDFUNCTION(AC_SRC_OVER, 0,
                              max(0, min(255, int(alpha * 255))), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(POINT(int(x), int(y))),
            ctypes.byref(SIZE(self.w, self.h)), self.dc,
            ctypes.byref(POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA,
        )

    def show(self):
        if not self.visible:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True

    def hide(self):
        if self.visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False

    def raise_top(self):
        """Полноэкранные окна перехватывают верхний слой — периодически возвращаемся наверх."""
        user32.SetWindowPos(self.hwnd, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def pump(self):
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                return False
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return True


# --------------------------------------------------------------------------- #
# Рисование
# --------------------------------------------------------------------------- #

def mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def fmt(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def ellipsize(draw, text, fnt, max_width):
    if draw.textlength(text, font=fnt) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_width:
        text = text[:-1]
    return text + "…"


def render(state):
    """
    Кадр индикатора. Форма рисуется крупно и уменьшается — так получается
    сглаживание, которого у Canvas нет; текст рисуется уже в конечном размере,
    потому что Pillow сглаживает шрифты сам и уменьшение сделало бы его мягким.
    """
    s = SS * UI                 # координаты формы: запас на сглаживание × масштаб экрана
    big = Image.new("RGBA", (round(CANVAS[0] * s), round(CANVAS[1] * s)), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    x1, y1, x2, y2 = [v * s for v in PILL]
    accent = state["accent"]

    # Тень: та же капсула, размытая и сдвинутая вниз. Без неё плашка выглядит
    # наклейкой на экране, а не предметом над ним.
    shadow = Image.new("RGBA", big.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x1, y1 + 5 * s, x2, y2 + 5 * s), RADIUS * s, fill=C_SHADOW)
    shadow = shadow.filter(ImageFilter.GaussianBlur(7 * s))
    big.alpha_composite(shadow)

    d.rounded_rectangle((x1, y1, x2, y2), RADIUS * s, fill=C_PILL,
                        outline=C_EDGE, width=max(1, round(s)))

    # Точка состояния: пульсирует плавно. Резкое вкл/выкл читается как дефект.
    pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(state["phase_t"] * 3.4))
    dot = mix(C_PILL[:3] + (255,), accent, pulse if state["recording"] else 1.0)
    cx, cy, r = DOT[0] * s, DOT[1] * s, DOT[2] * s
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dot)

    if state["recording"]:
        _draw_wave(d, state, accent, s)
    else:
        _draw_working(d, state, accent, s)

    # Тонкая нить лимита по низу капсулы — сообщает долю, не отнимая места.
    # Слева выровнена по тексту: если начать от точки, нить наезжает на скругление.
    if state["recording"] and state["limit"]:
        lx1, lx2, ly, lh = [v * s for v in LIMIT_LINE]
        d.rounded_rectangle((lx1, ly, lx2, ly + lh), lh / 2, fill=C_IDLE_BAR)
        filled = lx1 + (lx2 - lx1) * state["ratio"]
        if filled > lx1 + lh:
            d.rounded_rectangle((lx1, ly, filled, ly + lh), lh / 2, fill=accent)

    img = big.resize(canvas_px(), Image.LANCZOS)
    _draw_text(img, state)
    return img


def _draw_wave(d, state, accent, s):
    """История громкости: новый столбик приходит справа, старые уезжают влево."""
    levels = state["levels"]
    for i in range(BARS):
        level = levels[i] if i < len(levels) else 0.0
        x = (WAVE_X + i * (BAR_W + BAR_GAP)) * s
        half = max(1.7, level * WAVE_MAX) * s
        colour = accent if level > 0.06 else C_IDLE_BAR
        d.rounded_rectangle((x, WAVE_Y * s - half, x + BAR_W * s, WAVE_Y * s + half),
                            BAR_W * s / 2, fill=colour)


def _draw_working(d, state, accent, s):
    """Фаза обработки: три точки бегущей волной — видно, что процесс жив."""
    cx, cy, size = WAVE_CX * s, WAVE_Y * s, 9 * s
    width = max(2, round(1.6 * s))
    if state["phase"] == "done":
        # Работа кончилась — бегущие точки соврали бы, что она идёт.
        d.line([(cx - size, cy), (cx - size * 0.25, cy + size * 0.7),
                (cx + size, cy - size * 0.8)], fill=accent, width=width, joint="curve")
        return
    if state["phase"] in ("cancel", "error"):
        h = size * 0.7
        d.line([(cx - h, cy - h), (cx + h, cy + h)], fill=accent, width=width)
        d.line([(cx - h, cy + h), (cx + h, cy - h)], fill=accent, width=width)
        return
    for i in range(3):
        t = (state["phase_t"] * 2.2 - i * 0.5) % 2.4
        lift = max(0.0, math.sin(t * math.pi)) if t < 1 else 0.0
        x = cx + (i - 1) * 21 * s
        r = (3.6 + 1.8 * lift) * s
        colour = mix(C_IDLE_BAR, accent, 0.25 + 0.75 * lift)
        d.ellipse((x - r, cy - r, x + r, cy + r), fill=colour)


def _draw_text(img, state):
    d = ImageDraw.Draw(img)
    x0 = TEXT_X * UI
    if state["recording"]:
        main, limit = fmt(state["elapsed"]), fmt(state["limit"])
        d.text((x0, ROW1 * UI), main, font=F_TIME, fill=C_TEXT, anchor="lm")
        x = x0 + d.textlength(main, font=F_TIME) + 12 * UI
        # Разделитель рисуем сами: символ «│» Segoe UI отрисовывает как заметный
        # прямоугольник — в строке он читается как посторонний значок.
        d.rectangle((x, (ROW1 - 8) * UI, x + max(1, round(UI)), (ROW1 + 7) * UI),
                    fill=(96, 96, 104))
        d.text((x + 11 * UI, (ROW1 + 1) * UI), limit, font=F_LIMIT, fill=C_DIM, anchor="lm")
    else:
        d.text((x0, ROW1 * UI), state["title"], font=F_TIME, fill=C_TEXT, anchor="lm")

    if state["sub"]:
        d.text((x0, ROW2 * UI), ellipsize(d, state["sub"], F_SMALL, 250 * UI),
               font=F_SMALL, fill=C_DIM, anchor="lm")


# --------------------------------------------------------------------------- #
# Цикл
# --------------------------------------------------------------------------- #

class Indicator:
    FADE = 0.16  # секунды на появление и угасание

    def __init__(self, demo_sec=0):
        self.demo_sec = demo_sec
        self.demo_started = time.time()
        self.demo_level = 0.35
        self.canvas = canvas_px()
        self.win = LayeredWindow(*self.canvas)
        self.levels = deque([0.0] * BARS, maxlen=BARS)
        self.last_state = {"active": False}
        self.level = 0.0
        self.alpha = 0.0
        self.started_at = time.time()
        self.last_top = 0.0
        self.hidden = True   # чтобы положение перечиталось перед первым показом
        self.vertical, self.horizontal, self.margin = load_placement()
        self.work = work_area()

    def refresh_placement(self):
        """
        Перечитать положение перед показом капсулы.

        Раз в появление, а не раз в кадр: тридцать чтений файла в секунду ради
        настройки, которую меняют раз в год, — плата ни за что. Но и раз в запуск
        мало: индикатор живёт неделями вместе с демоном, и правка config.json
        иначе не действовала бы до перезагрузки. Заодно обновляем рабочую область —
        она меняется от подключённого монитора и переехавшей панели задач.
        """
        self.vertical, self.horizontal, self.margin = load_placement()
        self.work = work_area()

    def placement(self, offset):
        """
        Левый верхний угол холста. `offset` — смещение анимации появления.

        Капсула выезжает со СВОЕЙ стороны экрана: сверху — сверху, снизу — снизу.
        Движение считывается быстрее изменения яркости, но только если направлено
        оттуда, откуда предмет приходит; выезжающая вверх нижняя капсула читается
        как сбой отрисовки.
        """
        left, top, right, bottom = self.work
        cw, ch = self.canvas
        m = self.margin * UI

        if self.horizontal == "left":
            x = left + m - PILL_INSET[0] * UI
        elif self.horizontal == "right":
            x = right - cw - m + PILL_INSET[2] * UI
        else:
            x = left + (right - left - cw) / 2

        if self.vertical == "bottom":
            y = bottom - ch - m + PILL_INSET[3] * UI + offset
        else:
            y = top + m - PILL_INSET[1] * UI - offset
        return int(round(x)), int(round(y))

    # -- состояние ----------------------------------------------------------- #

    # Состояние без срока годности (файл от старой версии демона) держим не дольше
    # этого — писатель тогда не умел протухать, а висеть вечно капсула не должна.
    LEGACY_TTL = 15.0

    def read_state(self):
        if self.demo_sec:
            return self._demo_state()
        try:
            self.last_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Файл могли перезаписывать прямо сейчас. Держимся за последнее
            # прочитанное: считать сбой чтения за «записи нет» — значит мигать.
            pass
        return self._fresh(self.last_state)

    def _fresh(self, state):
        """
        Протухшее состояние = погасший индикатор.

        Индикатор живёт дольше демона и не знает, жив ли тот. Единственный
        надёжный признак — свежесть состояния: писатель обновляет его постоянно,
        поэтому «давно не менялось» означает «писателя больше нет». Без этого
        капсула «готово» висела на экране бесконечно (31.07): демон завершился
        раньше, чем сработало его отложенное гашение.
        """
        if not state.get("active"):
            return state
        expires = state.get("expires_at")
        if expires is None:
            try:
                expires = STATE_FILE.stat().st_mtime + self.LEGACY_TTL
            except OSError:
                return state
        if time.time() > float(expires):
            return {"active": False}
        return state

    def _demo_state(self):
        """Прогон всех фаз: запись -> расшифровка -> редактура -> готово."""
        t = time.time() - self.demo_started
        if t > self.demo_sec:
            return {"quit": True}
        rec_part = self.demo_sec * 0.6
        if t < rec_part:
            self.demo_level += (random.random() - 0.45) * 0.4
            self.demo_level = max(0.02, min(1.0, self.demo_level))
            return {"active": True, "phase": "rec", "started": time.time() - t * 30,
                    "limit_sec": 600, "hint": "HUAWEI FreeBuds 5i",
                    "level": self.demo_level}
        rest = (t - rec_part) / max(0.1, self.demo_sec - rec_part)
        phase = "stt" if rest < 0.45 else ("edit" if rest < 0.85 else "done")
        return {"active": True, "phase": phase, "note": "записано 14.2 с"}

    def build(self, raw):
        recording = raw.get("phase", "rec") == "rec"
        elapsed = max(0.0, time.time() - float(raw.get("started") or time.time()))
        limit = float(raw.get("limit_sec") or 0)
        ratio = min(1.0, elapsed / limit) if limit else 0.0

        if recording:
            # Демон отдаёт громкость 10 раз в секунду, кадров же 30: без сглаживания
            # каждый столбик троился бы и волна выглядела ступенчатой.
            target = float(raw.get("level") or 0.0)
            self.level = self.level + (target - self.level) * 0.5
            self.levels.append(self.level)
            accent = C_WARN if ratio > 0.8 else C_REC
            title, sub = "", raw.get("hint") or ""
        else:
            title, accent = PHASES.get(raw.get("phase"), ("работаю", C_WORK))
            sub = raw.get("note") or ""

        return {
            "recording": recording, "elapsed": elapsed, "limit": limit, "ratio": ratio,
            "levels": list(self.levels), "accent": accent, "title": title, "sub": sub,
            "phase": raw.get("phase", "rec"), "phase_t": time.time() - self.started_at,
        }

    # -- главный цикл -------------------------------------------------------- #

    def run(self):
        frame = 1 / 30.0
        while True:
            if not self.win.pump():
                break
            raw = self.read_state()
            if raw.get("quit"):
                break

            target = 1.0 if raw.get("active") else 0.0
            step = frame / self.FADE
            self.alpha += max(-step, min(step, target - self.alpha))

            if self.alpha <= 0.01:
                self.win.hide()
                self.hidden = True
                time.sleep(0.1)          # индикатор молчит — не жечь процессор
                continue

            if self.hidden:
                self.refresh_placement()
                self.hidden = False

            state = self.build(raw)
            img = render(state)
            # При появлении капсула чуть выезжает из-за края экрана — движение
            # считывается быстрее, чем изменение яркости.
            offset = int((1.0 - self.alpha) * 10 * UI)
            self.win.blit(img, *self.placement(offset), alpha=self.alpha)
            self.win.show()

            now = time.time()
            if now - self.last_top > 2.0:
                self.win.raise_top()
                self.last_top = now
            time.sleep(frame)


MUTEX_NAME = "Local\\claude-voice-overlay"
ERROR_ALREADY_EXISTS = 183


def already_running():
    """
    Один индикатор на систему.

    Демон запускает индикатор при старте, но не он его хоронит: пережить демона
    (Ctrl+C, падение) индикатор может запросто. Без этой проверки каждый новый
    запуск демона добавлял бы ещё один невидимый процесс поверх прежних.
    Уступает новый: старый уже держит окно и читает тот же файл состояния,
    так что нового демона он обслужит ничем не хуже.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return False
        globals()["_MUTEX"] = handle  # держим дескриптор живым до конца процесса
        return kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 — без мьютекса просто работаем как раньше
        return False


def main():
    if already_running():
        return 0
    # Без этого Windows растягивает окно на мониторе с масштабом >100 %,
    # и вместо гладкой капсулы получается мыло.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 — старая Windows: хватит и системного режима
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    # Только теперь система сообщит НАСТОЯЩИЙ масштаб: до объявления
    # DPI-осведомлённости она честно врёт про 96 точек на дюйм.
    setup_scale()

    demo = 0
    if "--demo" in sys.argv:
        i = sys.argv.index("--demo")
        demo = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 12
    Indicator(demo).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
