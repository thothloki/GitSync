"""Background task helpers for the GitSync GUI."""

from __future__ import annotations

import traceback
from typing import Any, Callable, Optional

from .qt_compat import QtCore


class _WorkerSignals(QtCore.QObject):
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(object)


class _Worker(QtCore.QRunnable):
    def __init__(self, function: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            result = self.function(*self.args, **self.kwargs)
        except BaseException as exc:  # pass the exception to the GUI thread
            try:
                self.signals.failed.emit(exc)
            except Exception:
                # Qt can be shutting down while a worker is finishing.
                pass
            return
        try:
            self.signals.finished.emit(result)
        except Exception:
            pass


class TaskRunner(QtCore.QObject):
    """Run Git operations one at a time without blocking the Qt event loop."""

    busy_changed = QtCore.Signal(bool)

    def __init__(self, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        # Git operations against one working tree must never overlap.
        self._pool.setMaxThreadCount(1)
        self._workers = set()
        self._busy = False
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(
        self,
        function: Callable[..., Any],
        *args: Any,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        **kwargs: Any,
    ) -> None:
        if self._closed:
            return
        worker = _Worker(function, args, kwargs)
        self._workers.add(worker)

        def finished(result: Any) -> None:
            self._workers.discard(worker)
            self._update_busy()
            if self._closed:
                return
            if on_success is not None:
                try:
                    on_success(result)
                except BaseException:
                    # A callback exception must not kill the worker thread or
                    # leave the runner permanently busy.
                    traceback.print_exc()

        def failed(error: BaseException) -> None:
            self._workers.discard(worker)
            self._update_busy()
            if self._closed:
                return
            if on_error is not None:
                try:
                    on_error(error)
                except BaseException:
                    traceback.print_exc()

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self._update_busy()
        self._pool.start(worker)

    def _update_busy(self) -> None:
        busy = bool(self._workers)
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Stop accepting work and wait briefly for active Git calls."""
        if self._closed:
            return not self._workers
        self._closed = True
        finished = self._pool.waitForDone(int(timeout_ms))
        if not finished:
            self._pool.clear()
        self._workers.clear()
        self._update_busy()
        return finished

    def wait_for_idle(self, timeout_ms: int = 5000) -> bool:
        """Wait for queued work to finish, primarily for the exit hook."""
        if not self._workers:
            return True
        # The pool's waitForDone is bounded and does not spin the Python event
        # loop, which is important while handling a main-window close event.
        return self._pool.waitForDone(int(timeout_ms))


__all__ = ["TaskRunner"]
