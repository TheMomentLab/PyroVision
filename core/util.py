import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def dyn_sleep(s_time, max_time):
    d_time = time.time() - s_time
    if d_time < max_time:
        time.sleep(max_time-d_time)


def ts_to_epoch_ms(ts: str) -> Optional[float]:
    """
    타임스탬프 문자열을 epoch milliseconds로 변환

    Args:
        ts: "yymmddHHMMSSffffff" 형식 문자열

    Returns:
        epoch milliseconds 또는 None (변환 실패 시)
    """
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%y%m%d%H%M%S%f")
        return dt.timestamp() * 1000.0
    except Exception as e:
        logger.warning("Invalid timestamp format: %s (error: %s)", ts, e)
        return None
