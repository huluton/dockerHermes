"""進化生命週期的協調層。

一次進化任務要走的流程：

    提交 → 排入佇列 → 沙箱執行 → 收集產物 → 靜態掃描 → 原子性晉升 → 強制銷毀容器

每一步的失敗都是終局的：任務標記為失敗，線上技能維持原狀。**沒有任何一條
路徑會在掃描沒過的情況下把東西送上線。**

任務狀態存在 SQLite。用 SQLite 而不是純記憶體，是因為 controller 重啟很常見
（映像更新、設定調整），而丟失稽核軌跡對一個「會改自己程式碼的系統」來說是
不能接受的。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import docker

from . import deps as deps_mod
from . import promote as promote_mod
from . import sandbox as sandbox_mod
from . import scanner as scanner_mod
from .config import Settings
from .docker_client import reap_orphans

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    skill         TEXT NOT NULL,
    status        TEXT NOT NULL,
    phase         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    spec_json     TEXT NOT NULL,
    sandbox_json  TEXT,
    scan_json     TEXT,
    promotion_json TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """任務紀錄的 SQLite 儲存層。

    每次操作開一條新連線。相對於一次進化任務動輒數分鐘的耗時，連線成本
    可以忽略，而且這樣就完全不必處理跨執行緒共用連線的問題。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL 讓讀取（API 查詢）不會被寫入（進行中的任務）擋住。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(self, task_id: str, skill: str, spec: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, skill, status, phase, created_at, spec_json) "
                "VALUES (?, ?, 'queued', 'queued', ?, ?)",
                (task_id, skill, _now(), json.dumps(spec, ensure_ascii=False)),
            )

    def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {columns} WHERE id = ?",  # noqa: S608 — 鍵來自程式碼，非使用者輸入
                (*fields.values(), task_id),
            )

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def reset_interrupted(self) -> int:
        """把 controller 重啟時卡在執行中的任務標成失敗。

        這些任務的沙箱容器已經被回收器清掉了，不可能再有結果回來。留著
        'running' 狀態只會讓稽核紀錄說謊。
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'failed', phase = 'interrupted', "
                "finished_at = ?, error = 'controller 在此任務執行期間重啟' "
                "WHERE status IN ('running', 'queued')",
                (_now(),),
            )
            return cursor.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("spec_json", "sandbox_json", "scan_json", "promotion_json"):
        raw = out.pop(key, None)
        field = key.removesuffix("_json")
        try:
            out[field] = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            out[field] = None
    return out


class EvolutionEngine:
    """執行進化任務，並負責沙箱容器的生命週期。"""

    def __init__(self, client: docker.DockerClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.store = TaskStore(settings.state_dir / "tasks.db")
        self.policy = scanner_mod.load_policy(settings.scanner_policy_file)

        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_tasks,
            thread_name_prefix="evolve",
        )
        # 進行中的沙箱容器 ID。回收器會參考這個集合，避免把剛建立、還沒
        # 開始跑的容器誤判成孤兒。
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

        interrupted = self.store.reset_interrupted()
        if interrupted:
            log.warning("將 %d 個被重啟中斷的任務標記為失敗", interrupted)

    # --- 生命週期 -------------------------------------------------------

    def start(self) -> None:
        """啟動時清理孤兒，並開始週期性回收。"""
        removed = reap_orphans(self.client, active_ids=set())
        if removed:
            log.warning("啟動時回收了 %d 個孤兒沙箱容器", removed)

        self._reaper = threading.Thread(
            target=self._reaper_loop, name="reaper", daemon=True
        )
        self._reaper.start()

    def shutdown(self) -> None:
        self._stop.set()
        # 不等進行中的任務跑完 —— 它們可能還要好幾分鐘。沙箱容器會被下一次
        # 啟動時的回收掃描清掉，任務紀錄則由 reset_interrupted() 修正。
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._reaper:
            self._reaper.join(timeout=5)

    def _reaper_loop(self) -> None:
        while not self._stop.wait(self.settings.reaper_interval_sec):
            try:
                with self._active_lock:
                    active = set(self._active)
                removed = reap_orphans(self.client, active_ids=active)
                if removed:
                    log.warning("週期性回收清除了 %d 個孤兒沙箱", removed)
            except Exception as exc:  # noqa: BLE001 — 回收器不能因為單次失敗就死掉
                log.error("回收循環發生錯誤：%s", exc)

    # --- 任務提交 -------------------------------------------------------

    def submit(self, skill: str, spec: dict[str, Any]) -> str:
        """把進化任務排入佇列，立刻回傳任務 ID。"""
        # 在收下任務之前就先驗名稱，讓錯誤在 API 回應裡當場出現，而不是
        # 拖到幾分鐘後才在背景失敗。
        skill = promote_mod.validate_skill_name(skill)

        task_id = f"evo-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        self.store.create(task_id, skill, spec)
        self._executor.submit(self._run, task_id, skill, spec)
        log.info("已排入進化任務 %s（技能=%s）", task_id, skill)
        return task_id

    def _run(self, task_id: str, skill: str, spec: dict[str, Any]) -> None:
        """完整的進化流程。在工作執行緒上執行。"""
        self.store.update(task_id, status="running", phase="sandbox", started_at=_now())

        def track(container_id: str) -> None:
            with self._active_lock:
                self._active.add(container_id)

        outcome: sandbox_mod.SandboxOutcome | None = None
        privileged = bool(spec.get("privileged_install"))
        expect_artifacts = spec.get("expect_artifacts", True)
        try:
            # --- 階段 1：沙箱執行 ---------------------------------------
            outcome = sandbox_mod.run_task(
                self.client,
                task_id,
                spec,
                on_container_created=track,
                privileged=privileged,
            )
            self.store.update(
                task_id,
                sandbox_json=json.dumps(
                    {
                        "container_id": outcome.container_id,
                        "exit_code": outcome.exit_code,
                        "status": outcome.status,
                        "duration_sec": outcome.duration_sec,
                        "logs": outcome.logs,
                        "result": outcome.result,
                        "artifact_paths": sorted(outcome.artifacts),
                    },
                    ensure_ascii=False,
                ),
            )

            if outcome.status != "success":
                self._fail(
                    task_id,
                    "sandbox",
                    outcome.error or f"沙箱執行結果為 {outcome.status}",
                )
                return

            # --- 階段 1.5：記下沙箱裝了什麼 -------------------------------
            # 條件刻意訂在「沙箱步驟全部成功」，而不是「整個任務成功」：一個
            # 只想試裝套件的探測任務不會有技能產物，走不到晉升階段，但它裝了
            # 什麼、驗到什麼版本，正是這裡最該留下來的東西。
            #
            # 這一步失敗絕不能影響任務結果 —— 依賴清單是附帶產出，技能晉升
            # 才是主線。
            try:
                deps_mod.record(
                    self.settings.deps_dir,
                    task_id=task_id,
                    skill=skill,
                    installed=(outcome.result or {}).get("installed"),
                    privileged=privileged,
                    sandbox_status=outcome.status,
                    instance=self.settings.instance,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("任務 %s 的依賴清單記錄失敗：%r", task_id, exc)

            if not outcome.artifacts:
                if not expect_artifacts:
                    # 相依性探測任務：沒有技能要晉升，到這裡就是成功。
                    self.store.update(
                        task_id,
                        status="succeeded",
                        phase="deps-only",
                        finished_at=_now(),
                    )
                    log.info("探測任務 %s 完成（沒有技能產物，符合預期）", task_id)
                    return
                self._fail(
                    task_id, "sandbox", "沙箱執行成功，但 /work/out 底下沒有任何產物"
                )
                return

            # --- 階段 2：靜態安全掃描 ------------------------------------
            self.store.update(task_id, phase="scan")
            report = scanner_mod.scan_artifacts(outcome.artifacts, self.policy)
            self.store.update(
                task_id, scan_json=json.dumps(report.as_dict(), ensure_ascii=False)
            )

            if not report.clean:
                summary = "; ".join(
                    f"{f.path}:{f.line or '?'} {f.detail}" for f in report.findings[:5]
                )
                if self.settings.scanner_enforce:
                    self._fail(
                        task_id,
                        "scan",
                        f"靜態掃描發現 {len(report.findings)} 個問題：{summary}",
                    )
                    return
                # 非強制模式：記錄但放行。只該用在政策調校期間。
                log.warning(
                    "任務 %s 的掃描發現 %d 個問題，但 SCANNER_ENFORCE=0，仍予晉升：%s",
                    task_id, len(report.findings), summary,
                )

            # --- 階段 3：原子性晉升 --------------------------------------
            self.store.update(task_id, phase="promote")
            result = promote_mod.promote(
                versions_root=self.settings.versions_dir,
                live_root=self.settings.live_dir,
                skill=skill,
                artifacts=outcome.artifacts,
                keep_versions=self.settings.keep_versions,
                metadata={
                    "task_id": task_id,
                    "promoted_at": _now(),
                    "instance": self.settings.instance,
                    "scan_clean": report.clean,
                    "scan_findings": len(report.findings),
                },
            )

            self.store.update(
                task_id,
                status="succeeded",
                phase="done",
                finished_at=_now(),
                promotion_json=json.dumps(
                    {
                        "skill": result.skill,
                        "version": result.version,
                        "live_path": result.live_path,
                        "files": result.files,
                        "bytes": result.bytes_written,
                        "pruned_versions": result.pruned_versions,
                    },
                    ensure_ascii=False,
                ),
            )
            log.info("進化任務 %s 完成：%s 晉升至 %s", task_id, skill, result.version)

        except promote_mod.PromotionError as exc:
            self._fail(task_id, "promote", str(exc))
        except Exception as exc:  # noqa: BLE001 — 工作執行緒必須留下紀錄再結束
            log.exception("進化任務 %s 發生未預期錯誤", task_id)
            self._fail(task_id, "error", f"未預期的錯誤：{exc!r}")
        finally:
            if outcome and outcome.container_id:
                with self._active_lock:
                    self._active.discard(outcome.container_id)

    def _fail(self, task_id: str, phase: str, error: str) -> None:
        log.warning("進化任務 %s 於階段 %s 失敗：%s", task_id, phase, error)
        self.store.update(
            task_id, status="failed", phase=phase, finished_at=_now(), error=error
        )

    # --- 查詢 -----------------------------------------------------------

    def active_count(self) -> int:
        with self._active_lock:
            return len(self._active)
