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
    for directory in (settings.versions_dir, settings.live_dir):
        directory.mkdir(parents=True, exist_ok=True)

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
    spec = {
        "steps": [s.model_dump() for s in request.steps],
        "env": request.env,
        "timeout_sec": request.timeout_sec,
    }
    task_id = eng.submit(request.skill, spec)
    return {"task_id": task_id, "status": "queued", "skill": request.skill}


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
