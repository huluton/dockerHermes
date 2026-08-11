"""hermes-controller 的 HTTP API。

**這個 API 沒有身分驗證，而且刻意如此。** 它只綁在 hermes-net 上，compose 完全
沒有把它的埠對外發佈，所以能連到它的只有同一個應用層網路上的容器
（runtime、以及短暫的沙箱）。加一層 token 驗證只會製造出「另一個
要保管的祕密」，而防護效果幾乎等於零 —— 真正的邊界是網路拓撲。

如果日後要把這個 API 對外開放，先加驗證再說。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import deps as deps_mod
from . import promote as promote_mod
from .config import settings
from .docker_client import DockerUnavailable, build_client, check_access
from .lifecycle import EvolutionEngine

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("controller")

# 由 lifespan 填入。
_engine: EvolutionEngine | None = None
_access_report: dict[str, Any] = {}


def engine() -> EvolutionEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="進化引擎尚未就緒")
    return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    global _engine, _access_report

    log.info("controller 啟動中（實例=%s）", settings.instance)
    log.info("Docker 端點：%s", settings.docker_host)
    log.info("線上技能目錄：%s", settings.live_dir)
    log.info("技能版本庫：%s", settings.versions_dir)

    client = build_client()
    _access_report = check_access(client)
    log.info("Docker 存取自我檢查：%s", _access_report)

    # 兩個目錄都必須在第一次晉升之前就存在。它們是掛載點，正常情況下 Docker
    # 已經建好了 —— 這裡只是讓「沒掛卷的手動除錯」也能跑起來。
    for directory in (settings.versions_dir, settings.live_dir, settings.deps_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if settings.sandbox_allow_privileged:
        log.warning(
            "SANDBOX_ALLOW_PRIVILEGED_INSTALL=1 —— 任務可以要求以 root 起沙箱"
            "（apt 裝得動系統套件）。沙箱依然是短暫容器、跑完強制移除，且只補回"
            "六個檔案權限相關的 capability。"
        )

    _engine = EvolutionEngine(client, settings)
    _engine.start()
    log.info("controller 就緒，監聽 %s:%s", settings.host, settings.port)

    try:
        yield
    finally:
        log.info("controller 關閉中")
        if _engine:
            _engine.shutdown()
        client.close()


app = FastAPI(
    title="hermes-controller",
    description="Hermes 自我進化 agent 的進化生命週期管理",
    version="1.0.0",
    lifespan=lifespan,
)


# --- 資料模型 -----------------------------------------------------------


class Step(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    run: str | list[str] = Field(..., description="命令字串（走 shell）或參數陣列（不走 shell）")
    timeout_sec: int = Field(default=600, ge=1, le=7200)
    allow_failure: bool = False


class EvolveRequest(BaseModel):
    skill: str = Field(..., description="要建立或更新的技能名稱")
    steps: list[Step] = Field(..., min_length=1, max_length=20)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = Field(default=900, ge=1, le=7200)

    privileged_install: bool = Field(
        default=False,
        description=(
            "以 root + 六個檔案權限相關的 capability 起沙箱，讓 apt-get install "
            "能用。需要營運端先在 .env 設 SANDBOX_ALLOW_PRIVILEGED_INSTALL=1。"
        ),
    )
    expect_artifacts: bool = Field(
        default=True,
        description=(
            "任務是否應該在 /work/out 產出技能檔案。相依性探測任務（只想在沙箱裡"
            "試裝套件、確認裝得起來）設 false，否則會以「沒有產物」判定失敗。"
        ),
    )

    @field_validator("skill")
    @classmethod
    def _check_skill(cls, value: str) -> str:
        try:
            return promote_mod.validate_skill_name(value)
        except promote_mod.PromotionError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("env")
    @classmethod
    def _check_env(cls, value: dict[str, str]) -> dict[str, str]:
        # 不允許覆寫 PATH / PYTHONPATH / LD_PRELOAD 這類變數 —— 它們會改變
        # 沙箱裡「什麼程式碼會被執行」，等於繞過映像本身的完整性。
        blocked = {"PATH", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "VIRTUAL_ENV"}
        clashes = blocked & set(value)
        if clashes:
            raise ValueError(f"不允許覆寫這些環境變數：{sorted(clashes)}")
        return value


class RollbackRequest(BaseModel):
    version: str = Field(..., min_length=1, max_length=128)


# --- 端點 ---------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """存活探測。刻意保持輕量 —— 不呼叫任何 Docker API。"""
    return {
        "status": "ok" if _engine else "starting",
        "instance": settings.instance,
    }


@app.get("/readyz")
def readyz() -> JSONResponse:
    """就緒探測。會確認 Docker 真的可達。"""
    if _engine is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    try:
        report = check_access(_engine.client)
    except DockerUnavailable as exc:
        return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)
    return JSONResponse({"status": "ready", "docker": report})


@app.get("/status")
def status() -> dict[str, Any]:
    """給人看的整體狀態，用於除錯。"""
    eng = engine()
    return {
        "instance": settings.instance,
        "docker": _access_report,
        "sandbox": {
            "image": settings.sandbox_image,
            "network": settings.sandbox_network,
            "timeout_sec": settings.sandbox_timeout_sec,
            "memory_mb": settings.sandbox_memory_mb,
            "cpus": settings.sandbox_cpus,
            "active": eng.active_count(),
            "max_concurrent": settings.max_concurrent_tasks,
            "allow_privileged_install": settings.sandbox_allow_privileged,
        },
        "deps": {
            "dir": str(settings.deps_dir),
            "pending": len(deps_mod.list_pending(settings.deps_dir)),
        },
        "scanner": {
            "enforcing": settings.scanner_enforce,
            "policy_file": str(settings.scanner_policy_file),
            "denied_imports": len(eng.policy.get("denied_imports", [])),
            "denied_calls": len(eng.policy.get("denied_calls", [])),
            "denied_patterns": len(eng.policy.get("denied_patterns", [])),
        },
        "skills": {
            "live": str(settings.live_dir),
            "versions": str(settings.versions_dir),
            "keep_versions": settings.keep_versions,
        },
    }


@app.post("/evolve", status_code=202)
def evolve(request: EvolveRequest) -> dict[str, Any]:
    """提交一次進化任務。

    立刻回應 202 並附上任務 ID。整套流程（沙箱 → 掃描 → 晉升）在背景執行，
    可能耗時數分鐘 —— 用 GET /tasks/{id} 追蹤進度。
    """
    eng = engine()

    if request.privileged_install and not settings.sandbox_allow_privileged:
        raise HTTPException(
            status_code=403,
            detail=(
                "這套 stack 沒有開放特權安裝模式。要允許沙箱用 apt 裝系統套件，"
                "在 .env 設 SANDBOX_ALLOW_PRIVILEGED_INSTALL=1 再重啟 controller。"
                "（只影響短暫的沙箱容器；runtime 永遠不會拿到 root。）"
            ),
        )

    spec = {
        "steps": [s.model_dump() for s in request.steps],
        "env": request.env,
        "timeout_sec": request.timeout_sec,
        "privileged_install": request.privileged_install,
        "expect_artifacts": request.expect_artifacts,
    }
    task_id = eng.submit(request.skill, spec)
    return {
        "task_id": task_id,
        "status": "queued",
        "skill": request.skill,
        "privileged_install": request.privileged_install,
    }


@app.get("/tasks")
def list_tasks(
    limit: int = Query(default=50, ge=1, le=500),
    status: Literal["queued", "running", "succeeded", "failed"] | None = None,
) -> dict[str, Any]:
    tasks = engine().store.list(limit=limit, status=status)
    return {"count": len(tasks), "tasks": tasks}


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = engine().store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"找不到任務 {task_id}")
    return task


@app.get("/deps/pending")
def list_pending_deps() -> dict[str, Any]:
    """列出「沙箱裝過、還沒決定要不要進 runtime」的套件清單。

    這個端點只是把 hermes-deps 卷裡的內容讀出來給人看。真正把套件併進
    runtime/deps/*.txt 的動作發生在宿主機上（`make deps-accept`），controller
    沒有、也不該有那個能力。
    """
    pending = deps_mod.list_pending(settings.deps_dir)
    return {"count": len(pending), "pending": pending}


@app.get("/deps/pending/{task_id}")
def get_pending_deps(task_id: str) -> dict[str, Any]:
    try:
        entry = deps_mod.get_pending(settings.deps_dir, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail=f"沒有 {task_id} 的待審依賴清單")
    return entry


@app.post("/deps/pending/{task_id}/resolve")
def resolve_pending_deps(
    task_id: str,
    outcome: Literal["accepted", "rejected"] = Query(
        ..., description="accepted 由 make deps-accept 在併入檔案之後回報；rejected 是否決"
    ),
) -> dict[str, Any]:
    """把一份待審清單從 pending/ 移走，移到 accepted/ 或 rejected/。

    ⚠️ ``outcome=accepted`` **不代表 controller 做了任何事**。真正把套件寫進
    runtime/deps/*.txt 的是宿主機上的 `make deps-accept`；這個端點只負責在那件事
    做完之後，把清單從待審佇列裡拿掉。controller 沒有能力改 runtime 的建置來源，
    這是刻意的設計，不是還沒實作。
    """
    try:
        moved = deps_mod.resolve(settings.deps_dir, task_id, outcome=outcome)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not moved:
        raise HTTPException(status_code=404, detail=f"沒有 {task_id} 的待審依賴清單")
    return {"task_id": task_id, "outcome": outcome}


@app.get("/skills")
def list_skills() -> dict[str, Any]:
    """列出線上技能與各自可用的版本。"""
    skills = promote_mod.list_skills(settings.live_dir, settings.versions_dir)
    return {"count": len(skills), "skills": skills}


@app.post("/skills/{skill}/rollback")
def rollback_skill(skill: str, request: RollbackRequest) -> dict[str, Any]:
    """把某個技能切回先前的版本。

    用的是與晉升相同的原子性 symlink 替換，所以 runtime 絕不會看到中間狀態。
    """
    try:
        result = promote_mod.rollback(
            versions_root=settings.versions_dir,
            live_root=settings.live_dir,
            skill=skill,
            version=request.version,
        )
    except promote_mod.PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "skill": result.skill,
        "version": result.version,
        "live_path": result.live_path,
        "files": result.files,
    }
