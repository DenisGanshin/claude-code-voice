# -*- coding: utf-8 -*-
"""
Тесты утечки памяти дублирующих потоков записи (инцидент 2026-11-13).

Гарнитура открывается до полутора секунд, дубль поднимается в фоновом потоке.
Если запись успела закончиться раньше, поток-опоздавший добавлял открытый
`InputStream` в список уже после `_collect_extra`, и тот писал звук без конца,
около 230 МБ в час. Демон за девять дней занял 11.9 ГБ.

Здесь микрофон НЕ открывается: `sd.InputStream` подменён, блоки подаёт тест.

Запуск:  python tests/test_extra_leak.py
"""

import sys
import threading
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import voice_core  # noqa: E402
from voice_core import Recorder  # noqa: E402

SR = voice_core.SAMPLE_RATE
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


class FakeStream:
    made = []
    open_delay = 0.0

    def __init__(self, device=None, callback=None, **kw):
        self.device = device
        self.callback = callback
        self.started = False
        self.closed = False
        self.delay = FakeStream.open_delay
        FakeStream.made.append(self)

    def start(self):
        time.sleep(self.delay)
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True

    def feed(self, frames=160):
        self.callback(np.full((frames, 1), 0.1, dtype=np.float32), frames, None, None)


class FakeSD:
    InputStream = FakeStream

    @staticmethod
    def query_devices(idx=None):
        return {"default_samplerate": SR}


def make_recorder(max_record_sec=600):
    rec = Recorder.__new__(Recorder)
    rec.cfg = {"multi_source": True, "max_record_sec": max_record_sec}
    rec.fake_wav = None
    rec.blocksize = 160
    rec._stream = None
    rec._extra = []
    rec._extra_lock = threading.Lock()
    rec._extra_thread = None
    rec._extra_stop = threading.Event()
    rec._extra_candidates = lambda: [(1, "A"), (2, "B")]
    return rec


def reset(delay=0.0):
    FakeStream.made = []
    FakeStream.open_delay = delay


def main():
    voice_core.sd = FakeSD
    voice_core.log = lambda *a, **k: None

    # 1. Обычный путь: оба дубля пишут, сбор отдаёт звук и закрывает потоки.
    reset()
    rec = make_recorder()
    rec._start_extra_sources()
    rec._extra_thread.join(3)
    for s in FakeStream.made:
        s.feed()
    got = rec._collect_extra()
    check("обычный путь: оба дубля собраны", len(got) == 2, f"собрано {len(got)}")
    check("обычный путь: потоки закрыты", all(s.closed for s in FakeStream.made))

    # 2. Главная проверка: запись закончилась, пока дубль ещё открывался.
    reset(delay=0.3)
    rec = make_recorder()
    rec._start_extra_sources()
    rec._close()
    rec._extra_thread.join(3)
    check("опоздавший дубль не остаётся в списке", rec._extra == [], f"в списке {len(rec._extra)}")
    check("опоздавший дубль закрыт", len(FakeStream.made) >= 1 and all(s.closed for s in FakeStream.made),
          f"потоков {len(FakeStream.made)}, закрыто {sum(s.closed for s in FakeStream.made)}")
    check("после конца записи следующий дубль даже не открывается", len(FakeStream.made) == 1,
          f"открыто потоков {len(FakeStream.made)} из 2 кандидатов")

    # 3. Следующая запись началась до того, как опоздавший дубль открылся.
    reset(delay=0.3)
    rec = make_recorder()
    rec._start_extra_sources()
    rec._close()
    first = list(FakeStream.made)
    first_thread = rec._extra_thread
    FakeStream.open_delay = 0.0
    rec._start_extra_sources()
    second_thread = rec._extra_thread
    first_thread.join(3)
    second_thread.join(3)
    second = [s for s in FakeStream.made if s not in first]
    for s in second:
        s.feed()
    # к моменту первого closed поток первой записи мог ещё не создаться, поэтому берём все, что были созданы до второго запуска
    first_all = [s for s in FakeStream.made if s.delay > 0]
    got = rec._collect_extra()
    check("вторая запись собирает только свои дубли", len(got) == 2, f"собрано {len(got)}")
    check("потоки первой записи закрыты и не попали во вторую",
          len(first_all) >= 1 and all(s.closed for s in first_all), f"потоков первой записи {len(first_all)}")

    # 4. Страховка: после остановки и сверх потолка записи блоки не копятся.
    reset()
    rec = make_recorder(max_record_sec=1)
    rec._start_extra_sources()
    rec._extra_thread.join(3)
    s = FakeStream.made[0]
    for _ in range(2000):
        s.feed(1000)
    held = sum(len(c) for c in rec._extra[0]["chunks"])
    limit = int(SR * (1 + 5)) + 1000
    check("сверх потолка записи блоки не копятся", held <= limit, f"хранится {held} отсчётов, потолок {limit}")
    # Отдельная запись, потолок не достигнут: остановка сама должна прекращать копить.
    reset()
    rec = make_recorder()
    rec._start_extra_sources()
    rec._extra_thread.join(3)
    s = FakeStream.made[0]
    s.feed(1000)
    src = rec._extra[0]
    rec._collect_extra()
    before = len(src["chunks"])
    s.feed(1000)
    s.feed(1000)
    check("после остановки блоки не копятся", before == 1 and len(src["chunks"]) == before,
          f"было {before}, стало {len(src['chunks'])}")

    bad = [r for r in results if not r[1]]
    print(f"\nитого: {len(results) - len(bad)} из {len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
