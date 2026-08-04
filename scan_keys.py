# -*- coding: utf-8 -*-
"""
Сканер клавиш: показывает, ЧТО именно видит Windows при нажатии.

Нужен потому, что часть клавиш ноутбука (в первую очередь Fn) обрабатывается
контроллером клавиатуры внутри железа и до операционной системы не доходит вовсе —
такую клавишу нельзя привязать ни к чему, и выяснить это можно только замером.

    python scan_keys.py [секунды]
"""

import sys
import time
from collections import Counter

import keyboard

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 25
seen = Counter()


def on_event(event):
    if event.event_type != "down":
        return
    key = f"{event.name} (scan_code={event.scan_code})"
    seen[key] += 1
    print(f"  {time.strftime('%H:%M:%S')}  {key}", flush=True)


keyboard.hook(on_event)
print(f"Слушаю клавиатуру {SECONDS} с. Нажимайте клавиши.", flush=True)
time.sleep(SECONDS)
keyboard.unhook_all()

print("\n--- итог ---")
if not seen:
    print("НИ ОДНОГО события не пришло.")
else:
    for key, count in seen.most_common():
        print(f"  {count:>3} раз  {key}")
