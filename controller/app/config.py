"""Controller 的設定，全部來自環境變數。

由 docker-compose.yml 注入。刻意在模組匯入時就做驗證並在設定不合理時直接
爆掉 — 一個接得到 Docker socket proxy 的服務如果帶著半套設定啟動，是很糟的
失敗模式。寧可在啟動時就掛掉。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} 不是整數") from exc
    if value < minimum:
        raise ValueError(f"{name}={value} 低於允許的最小值 {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} 不是浮點數") from exc
    if value < minimum:
        raise ValueError(f"{name}={value} 低於允許的最小值 {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    # --- 身分 -----------------------------------------------------------
    # 多套 stack 並存時把它們區分開來。sandbox 容器會帶上這個標籤，孤兒回收
    # 器也依它過濾 — 沒有這個隔離，A 實例的回收器會把 B 實例正在跑的沙箱
    # 直接砍掉。
    instance: str = field(default_factory=lambda: _env("INSTANCE", "default"))

    # --- Docker 存取 ----------------------------------------------------
    # 永遠指向 socket proxy，絕不直接指向 /var/run/docker.sock。controller
    # 完全沒有掛載 Docker socket，這是刻意的：它對 Docker 的所有存取都必須
    # 經過 proxy 的白名單。
    docker_host: str = field(
        default_factory=lambda: _env("DOCKER_HOST", "tcp://docker-socket-proxy:2375")
    )
    docker_api_timeout: int = field(
        default_factory=lambda: _env_int("DOCKER_API_TIMEOUT", 60)
    )

    # --- 沙箱 -----------------------------------------------------------
    sandbox_image: str = field(
        default_factory=lambda: _env("SANDBOX_IMAGE", "hermes-sandbox:local")
    )
    # 直接寫死網路名稱交給 Docker。刻意不呼叫 network inspect —— socket proxy
    # NETWORKS 設為 0，任何 /networks 請求都會拿到 403。
    sandbox_network: str = field(default_factory=lambda: _env("SANDBOX_NETWORK", ""))
    sandbox_timeout_sec: int = field(
        default_factory=lambda: _env_int("SANDBOX_TIMEOUT_SEC", 900)
    )
    sandbox_memory_mb: int = field(
        default_factory=lambda: _env_int("SANDBOX_MEMORY_MB", 2048, minimum=64)
    )
    sandbox_cpus: float = field(
        default_factory=lambda: _env_float("SANDBOX_CPUS", 2.0, minimum=0.1)
    )
    sandbox_pids_limit: int = field(
        default_factory=lambda: _env_int("SANDBOX_PIDS_LIMIT", 512, minimum=16)
    )
    # 同時可以有幾個進化任務在跑。預設保守 —— 每個沙箱都可能吃掉
    # sandbox_memory_mb 的記憶體。
    max_concurrent_tasks: int = field(
        default_factory=lambda: _env_int("MAX_CONCURRENT_TASKS", 2)
    )

    # --- 檔案系統 -------------------------------------------------------
    #
    # 兩個共用卷，controller 有 rw、runtime 只有 ro，而且**在兩個容器裡掛在
    # 完全相同的絕對路徑上**。這個對稱性是必要條件，不是美感問題：
    # live_dir 裡放的是指向 versions_dir 的 symlink，symlink 在哪個容器裡都
    # 是同一串字，兩邊路徑不一致就會變成 dangling link。
    #
    # 為什麼版本庫要獨立成一個卷、而且刻意放在 /opt/data/skills 之外：
    # 上游的技能掃描器（agent/skill_utils.py 的 iter_skill_index_files）用
    # os.walk(followlinks=True) 掃整個 skills 目錄，只排除 .git / .archive /
    # node_modules 等固定清單。版本庫若落在掃描範圍內，保留的每一個歷史版本
    # 都會被當成一個獨立的線上技能 —— 對自我進化的 agent 來說，那代表它會
    # 同時看到同一個技能的五個不同世代。放到掃描範圍外就從根本上不會發生。
    live_dir: Path = field(
        default_factory=lambda: Path(_env("LIVE_SKILLS_DIR", "/opt/data/skills/evolved"))
    )
    versions_dir: Path = field(
        default_factory=lambda: Path(_env("SKILL_VERSIONS_DIR", "/opt/data/skill-versions"))
    )
    state_dir: Path = field(default_factory=lambda: Path(_env("STATE_DIR", "/state")))
    # 每個技能保留幾個歷史版本以供回滾。
    keep_versions: int = field(default_factory=lambda: _env_int("KEEP_VERSIONS", 5))

    # --- 靜態掃描 -------------------------------------------------------
    scanner_policy_file: Path = field(
        default_factory=lambda: Path(
            _env("SCANNER_POLICY_FILE", "/opt/controller/policy.yaml")
        )
    )
    # 為 false 時掃描結果只記錄不阻擋。除非在做政策調校，否則不要關掉。
    scanner_enforce: bool = field(
        default_factory=lambda: _env("SCANNER_ENFORCE", "1").lower()
        not in {"0", "false", "no"}
    )

    # --- HTTP -----------------------------------------------------------
    host: str = field(default_factory=lambda: _env("CONTROLLER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("CONTROLLER_PORT", 9200))

    # --- 回收器 ---------------------------------------------------------
    reaper_interval_sec: int = field(
        default_factory=lambda: _env_int("REAPER_INTERVAL_SEC", 120)
    )

    @property
    def sandbox_label(self) -> dict[str, str]:
        return {"hermes.instance": self.instance, "hermes.role": "sandbox"}

    def validate(self) -> None:
        if not self.sandbox_network:
            raise ValueError(
                "SANDBOX_NETWORK 未設定。沙箱容器必須明確指定要加入的網路 —— "
                "留空會讓 Docker 使用預設 bridge，等於繞過本 stack 的網路隔離。"
            )
        if not self.docker_host.startswith("tcp://"):
            raise ValueError(
                f"DOCKER_HOST={self.docker_host!r} 不是 tcp:// 位址。controller "
                "必須經由 socket proxy 存取 Docker，絕不可直連 unix socket。"
            )
        if not self.live_dir.is_absolute() or not self.versions_dir.is_absolute():
            raise ValueError(
                "LIVE_SKILLS_DIR 與 SKILL_VERSIONS_DIR 必須是絕對路徑 —— "
                "它們同時也是 runtime 容器內的掛載點，相對路徑沒有意義。"
            )
        # 版本庫落在 runtime 的技能掃描樹裡面，會讓每一個保留的歷史版本都被
        # 當成獨立的線上技能。這是靜默的錯誤（agent 會看到五個世代的同名技能
        # 而不是報錯），所以在啟動時就擋掉。
        if self.versions_dir.is_relative_to(self.live_dir.parent):
            raise ValueError(
                f"SKILL_VERSIONS_DIR={self.versions_dir} 位於技能掃描樹 "
                f"{self.live_dir.parent} 之內。版本庫必須掛在掃描範圍外的獨立路徑。"
            )


settings = Settings()
settings.validate()
