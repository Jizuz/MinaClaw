"""
任务队列
- 有界优先队列
- 固定并发 worker 池
- 每任务超时
- 支持取消
- 通过 asyncio.Queue 回传事件
"""
import asyncio
import time
import uuid
import heapq
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

from utils.logger import get_logger, log_extra

log = get_logger("task_queue")


@dataclass(order=True)
class Task:
    priority: int
    created_at: float = field(compare=False)
    task_id: str = field(compare=False, default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = field(compare=False, default="")
    api_key: str = field(compare=False, default="")
    payload: Dict[str, Any] = field(compare=False, default_factory=dict)
    event_queue: asyncio.Queue = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)
    started_at: Optional[float] = field(compare=False, default=None)
    finished_at: Optional[float] = field(compare=False, default=None)

class TaskQueue:
    def __init__(self, 
                 maxsize: int = 200, 
                 workers: int = 4,
                 task_timeout: int = 300):
        self.maxsize = maxsize
        self.workers = workers
        self.task_timeout = task_timeout

        self._queue: list[Task] = []
        self._cond = asyncio.Condition()
        self._tasks: Dict[str, Task] = {}
        self._running: Dict[str, asyncio.Task] = {}

        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.rejected_full = 0

        self._worker_tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._runner = None

    async def start(self, runner):
        self._runner = runner
        self._stop.clear()
        for i in range(self.workers):
            self._worker_tasks.append(
                asyncio.create_task(self._worker_loop(i))
            )
        log.info(f"task_queue started workers={self.workers} capacity={self.maxsize}")

    async def stop(self):
        self._stop.set()
        async with self._cond:
            self._cond.notify_all()
        for t in self._worker_tasks:
            t.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        for t in self._running.values():
            t.cancel()
        log.info("task_queue stopped")

    async def submit(
        self,
        session_id: str,
        api_key: str,
        payload: Dict[str, Any],
        priority: int = 10,
        event_queue: asyncio.Queue | None = None,
    ) -> Task | None:
        async with self._cond:
            if len(self._queue) >= self.maxsize:
                self.rejected_full += 1
                log.warning("task rejected: queue full", extra=log_extra(queue_size=len(self._queue)))
                return None

            task = Task(
                priority=priority,
                created_at=time.time(),
                session_id=session_id,
                api_key=api_key,
                payload=payload,
                event_queue=event_queue or asyncio.Queue(),
            )
            heapq.heappush(self._queue, task)
            self._tasks[task.task_id] = task
            self.submitted += 1
            self._cond.notify()
            log.info("task submitted", extra=log_extra(task_id=task.task_id, queue_size=len(self._queue)))
            return task

    async def cancel(self, task_id: str) -> bool:
        async with self._cond:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.cancelled = True
            run = self._running.get(task_id)
            if run:
                run.cancel()
            self.cancelled += 1
            log.info("task cancelled", extra=log_extra(task_id=task_id))
            return True

    async def cancel_by_session(self, session_id: str) -> int:
        n = 0
        async with self._cond:
            for task in list(self._tasks.values()):
                if task.session_id == session_id and not task.finished_at:
                    task.cancelled = True
                    run = self._running.get(task.task_id)
                    if run:
                        run.cancel()
                    n += 1
        self.cancelled += n
        if n:
            log.info("session tasks cancelled", extra=log_extra(session_id=session_id, count=n))
        return n

    async def _pop(self) -> Task | None:
        async with self._cond:
            while not self._queue and not self._stop.is_set():
                await self._cond.wait()
            if self._stop.is_set():
                return None
            return heapq.heappop(self._queue)

    async def _worker_loop(self, idx: int):
        while not self._stop.is_set():
            try:
                task = await self._pop()
                if task is None:
                    return
                if task.cancelled:
                    await self._emit(task, {"type": "cancelled"})
                    continue

                task.started_at = time.time()
                wait = round(task.started_at - task.created_at, 3)
                log.info("task started", extra=log_extra(task_id=task.task_id, wait=wait))

                run = asyncio.create_task(self._run_task(task))
                self._running[task.task_id] = run
                try:
                    await asyncio.wait_for(run, timeout=self.task_timeout)
                    self.completed += 1
                    dur = round(time.time() - task.started_at, 3)
                    log.info("task completed", extra=log_extra(task_id=task.task_id, duration=dur))
                except asyncio.TimeoutError:
                    run.cancel()
                    self.failed += 1
                    log.error("task timeout", extra=log_extra(task_id=task.task_id))
                    await self._emit(task, {"type": "error", "message": "任务超时"})
                except asyncio.CancelledError:
                    self.cancelled += 1
                    log.info("task cancelled while running", extra=log_extra(task_id=task.task_id))
                    await self._emit(task, {"type": "cancelled"})
                except Exception as e:
                    self.failed += 1
                    log.exception("task error", extra=log_extra(task_id=task.task_id))
                    await self._emit(task, {"type": "error", "message": str(e)})
                finally:
                    self._running.pop(task.task_id, None)
                    task.finished_at = time.time()
                    await self._emit(task, {"type": "__eos__"})
            except asyncio.CancelledError:
                return

    async def _run_task(self, task: Task):
        await self._runner(
            session_id=task.session_id,
            api_key=task.api_key,
            payload=task.payload,
            event_queue=task.event_queue,
            task_id=task.task_id,
        )

    async def _emit(self, task: Task, event: dict):
        if task.event_queue is not None:
            await task.event_queue.put(event)

    def snapshot(self) -> dict:
        waiting = len(self._queue)
        running = len(self._running)
        waits = [t.started_at - t.created_at
                 for t in self._tasks.values()
                 if t.started_at and not t.cancelled]
        avg_wait = round(sum(waits) / len(waits), 3) if waits else 0.0
        return {
            "queue_size": waiting,
            "running": running,
            "capacity": self.maxsize,
            "workers": self.workers,
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "rejected_full": self.rejected_full,
            "avg_wait_sec": avg_wait,
        }


task_queue = TaskQueue(maxsize=200, workers=4, task_timeout=300)