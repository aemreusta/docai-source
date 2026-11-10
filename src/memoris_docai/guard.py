from __future__ import annotations

import multiprocessing
import os
import resource
import signal
import threading
from collections.abc import Callable
from typing import Any

from .layout import DocError


class Busy(Exception):
    pass


PRELOAD = [
    "memoris_docai.layout",
    "memoris_docai.images",
    "memoris_docai.render",
    "memoris_docai.docx",
    "memoris_docai.report",
]
_ctx = multiprocessing.get_context("forkserver")
_ctx.set_forkserver_preload(PRELOAD)
_slots = threading.BoundedSemaphore(int(os.environ.get("DOCAI_MAX_JOBS", "2")))


def _limit_self(memory_mb: int) -> None:
    os.setpgrp()
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass
    try:
        limit = memory_mb << 20
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass


def _child(conn, fn: Callable[..., Any], args: tuple, memory_mb: int) -> None:
    _limit_self(memory_mb)
    try:
        conn.send(("ok", fn(*args)))
    except DocError as e:
        conn.send(("doc", e.code))
    except MemoryError:
        conn.send(("doc", "processing-limit"))
    except FileNotFoundError:
        conn.send(("missing", ""))
    except ValueError:
        conn.send(("value", ""))
    except Exception as e:
        conn.send(("error", type(e).__name__))
    finally:
        conn.close()


def run(fn: Callable[..., Any], *args: Any, timeout: float) -> Any:
    if not _slots.acquire(timeout=float(os.environ.get("DOCAI_QUEUE_WAIT_S", "60"))):
        raise Busy
    try:
        recv, send = _ctx.Pipe(duplex=False)
        p = _ctx.Process(
            target=_child,
            args=(send, fn, args, int(os.environ.get("DOCAI_JOB_MEMORY_MB", "4096"))),
            daemon=True,
        )
        p.start()
        send.close()
        try:
            if not recv.poll(timeout):
                raise DocError("processing-limit")
            kind, value = recv.recv()
        except (EOFError, OSError, MemoryError) as e:
            raise DocError("processing-limit") from e
        finally:
            recv.close()
            _kill(p)
    finally:
        _slots.release()
    if kind == "ok":
        return value
    if kind == "doc":
        raise DocError(value)
    if kind == "missing":
        raise FileNotFoundError
    if kind == "value":
        raise ValueError("invalid input")
    raise RuntimeError(f"job failed: {value}")


def _kill(p) -> None:
    if p.is_alive():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
    p.join(5)
