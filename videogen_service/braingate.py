# 本地大脑(Ollama)的在途生成守卫。渲染启动时的 unload_before_render 会把
# 模型逐出显存——若此刻聊天/编剧正在生成,那一轮就会被无声打断。渲染方
# 在卸载前先 wait_idle(),生成方用 active() 包住自己的调用,两边互不认识,
# 只共享这一个门闩。
from contextlib import contextmanager
from threading import Condition
import time
from typing import Iterator


class BrainGate:
    def __init__(self) -> None:
        self._cond = Condition()
        self._active = 0

    @contextmanager
    def active(self) -> Iterator[None]:
        with self._cond:
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    @property
    def busy(self) -> bool:
        with self._cond:
            return self._active > 0

    def wait_idle(self, timeout: float) -> bool:
        """等到没有任何在途生成,或超时。True=已空闲;False=超时仍忙,
        调用方自行决断(渲染不能为一条聊天永远饿着)。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True
