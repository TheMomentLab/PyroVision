import queue
from typing import Any, Optional


class DoubleBuffer:
    """
    최신 프레임 한 개만 보관하는 얇은 버퍼.
    - 내부적으로 maxsize=1 큐를 사용해 덮어쓰기 경합을 줄임
    - 읽을 것이 없으면 마지막으로 본 값을 돌려줘 busy-wait를 완화
    """

    def __init__(self, maxsize: int = 1):
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize or 1)
        self._last: Optional[Any] = None

    def write(self, frame: Any) -> None:
        """
        최신 프레임을 기록.
        큐가 차 있으면 오래된 항목을 버리고 새 프레임으로 덮어쓴다.
        """
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.queue.put(frame)

    def read(self, timeout: Optional[float] = None) -> Optional[Any]:
        """
        최신 프레임을 반환.
        - timeout 지정 시 해당 시간 안에 새 항목을 기다림
        - 큐가 비어 있으면 마지막으로 본 값을 반환(None 가능)
        """
        try:
            item = (
                self.queue.get(timeout=timeout)
                if timeout is not None
                else self.queue.get_nowait()
            )
            self._last = item
            return item
        except queue.Empty:
            return self._last
