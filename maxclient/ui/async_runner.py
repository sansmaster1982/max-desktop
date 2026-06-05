"""Запуск блокирующих вызовов сервиса вне GUI-треда.

Сетевые методы MaxService блокируют до ответа сервера. Дёргать их из главного
потока нельзя — UI зависнет. run_async() кладёт работу в QThreadPool и
возвращает результат/ошибку обратно в GUI-тред через Qt-сигналы.

ВАЖНО: задачи держим в _pending до доставки сигнала. Иначе QThreadPool
авто-удаляет QRunnable после run(), Python собирает объект сигналов раньше,
чем GUI-тред обработает очередь — и колбэк молча не срабатывает (UI «зависает»
на индикаторе). setAutoDelete(False) + _pending устраняют эту гонку.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

# Живые задачи: держим ссылку до finished/failed, иначе сигнал может потеряться.
_pending: set["_Task"] = set()


class _Signals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        super().__init__()
        self.setAutoDelete(False)  # время жизни контролируем сами
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:  # noqa: BLE001 — доставляем текст ошибки в UI
            self.signals.failed.emit(str(e) or e.__class__.__name__)
        else:
            self.signals.finished.emit(result)


def run_async(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    **kwargs: Any,
) -> _Task:
    task = _Task(fn, args, kwargs)
    _pending.add(task)

    def _done(result: Any) -> None:
        try:
            if on_done is not None:
                on_done(result)
        finally:
            _pending.discard(task)

    def _fail(message: str) -> None:
        try:
            if on_error is not None:
                on_error(message)
        finally:
            _pending.discard(task)

    task.signals.finished.connect(_done)
    task.signals.failed.connect(_fail)
    QThreadPool.globalInstance().start(task)
    return task
