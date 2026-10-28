import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def _utcnow():
    return datetime.now(timezone.utc)


@dataclass
class Settings:
    data_dir: Path
    time_source: Callable[[], datetime] = _utcnow
    max_sessions: int = 200
    idle_seconds: int = 6 * 3600
    adapter_factory: Optional[Callable] = None  
    max_model_calls_per_session: int = 30
    max_model_calls_per_day: int = 400

    @classmethod
    def from_env(cls):
        return cls(data_dir=Path(os.environ.get("USUAL_DATA_DIR", "./data")),
                   max_model_calls_per_session=int(os.environ.get("USUAL_MAX_CALLS_PER_SESSION", "30")),
                   max_model_calls_per_day=int(os.environ.get("USUAL_MAX_CALLS_PER_DAY", "400")))
