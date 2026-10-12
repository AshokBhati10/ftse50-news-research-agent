"""Structured (logfmt) logging with the current job id attached automatically."""

from __future__ import annotations

import contextvars
import logging
from typing import Any

job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="-")


def _fmt(v: Any) -> str:
    s = ",".join(map(str, v)) if isinstance(v, (list, tuple, set)) else str(v)
    return f'"{s}"' if (not s or any(c in s for c in ' ="')) else s


def event(logger: logging.Logger, name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit ``event=<name> job_id=<id> key=value ...`` (never log secrets in fields)."""
    parts = [f"event={name}", f"job_id={job_id_var.get()}"] + [f"{k}={_fmt(v)}" for k, v in fields.items()]
    logger.log(level, " ".join(parts))
