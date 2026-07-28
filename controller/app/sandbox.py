"""短暫沙箱容器的生命週期管理。

檔案交換為什麼用 ``put_archive`` / ``get_archive`` 而不是共用卷：

* socket proxy 把 ``VOLUMES=0`` 關掉，controller 本來就不能建立卷。
* 就算能，共用一個可寫卷等於在受信任的 controller 與不受信任的沙箱之間開了
  一條持續存在的通道。用 archive 的話，兩者之間沒有任何共用的檔案系統 ——
  資料只在我們明確選擇的時機、以明確選擇的內容進出。

``/containers/{id}/archive`` 屬於 proxy 已放行的 containers 區段，所以這個做法
不需要放寬任何白名單。
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import time
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from .config import settings
from .docker_client import force_remove

log = logging.getLogger(__name__)

# 沙箱內部的路徑，與 sandbox/Dockerfile 對應。
_WORK_DIR = "/work"
_TASK_PATH = f"{_WORK_DIR}/task.json"
_RESULT_PATH = f"{_WORK_DIR}/result.json"
_OUT_DIR = f"{_WORK_DIR}/out"

# 沙箱使用者的 UID/GID（見 sandbox/Dockerfile）。放進 tar 的檔案必須標成這個
# 屬主，沙箱行程才讀得到我們送進去的東西。
_SANDBOX_UID = 10001
_SANDBOX_GID = 10001

# 願意從沙箱拉回來的產物總量上限。沒有這道防線，一個失控（或惡意）的任務
# 可以回傳一份 TB 級的 tar 把 controller 撐爆。
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

# 容器日誌保留的位元組上限。
_MAX_LOG_BYTES = 256 * 1024


@dataclass
class SandboxOutcome:
    """一次沙箱執行的結果。"""

    container_id: str | None = None
    exit_code: int | None = None
    status: str = "error"          # success | failed | timeout | error
    result: dict[str, Any] | None = None   # 沙箱寫出的 result.json
    artifacts: dict[str, bytes] = field(default_factory=dict)  # 相對路徑 -> 內容
    logs: str = ""
    error: str | None = None
    duration_sec: float = 0.0


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    """把 {容器內路徑: 內容} 打包成可餵給 put_archive 的 tar。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = _SANDBOX_UID
            info.gid = _SANDBOX_GID
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _extract_tar(stream: Any, *, strip_prefix: str = "") -> dict[str, bytes]:
    """把 get_archive 的串流解成 {相對路徑: 內容}。

    這裡處理的是「不受信任的」資料 —— tar 由沙箱內執行的任意程式碼產生，所以
    每個成員都要檢查，不能無條件相信。
    """
    raw = b"".join(stream)
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise ValueError(
            f"產物封存 {len(raw)} 位元組超過 {_MAX_ARTIFACT_BYTES} 上限"
        )

    out: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for member in tar.getmembers():
            # 只收一般檔案。symlink / hardlink / 裝置檔可以被用來指到封存
            # 範圍以外的地方，直接丟掉。
            if not member.isfile():
                continue

            name = member.name
            if strip_prefix and name.startswith(strip_prefix):
                name = name[len(strip_prefix) :]
            name = name.lstrip("/")
            if not name:
                continue

            # 路徑穿越防護。晉升階段會再驗一次，但一份不受信任的封存不該
            # 有機會在記憶體裡就先騙過我們。
            if ".." in name.split("/"):
                log.warning("丟棄含路徑穿越的產物：%r", member.name)
                continue

            total += member.size
            if total > _MAX_ARTIFACT_BYTES:
                raise ValueError("產物解壓後超過大小上限")

            fh = tar.extractfile(member)
            if fh is None:
                continue
            out[name] = fh.read()
    return out


def _sandbox_limits() -> dict[str, Any]:
    """沙箱容器的安全與資源設定。

    回傳的是一組要展開給 ``client.containers.create()`` 的 kwargs，而不是一個
    已經組好的 HostConfig。高階 API 會自己從這些 kwargs 組出 HostConfig，
    而且明確拒絕 ``host_config=``（``_create_container_args`` 消化不掉的 kwarg
    會拋 TypeError，訊息還寫成 ``run()``，很容易誤導）。要直接餵 HostConfig
    得改用 ``client.api.create_container()`` 這條低階路徑，但那樣就拿不到
    Container 物件，後面的 put_archive / wait / logs / remove 全都要重寫。
    """
    return dict(
        # 明確指定網路，不呼叫 network inspect —— proxy 的 NETWORKS=0 會擋掉。
        network_mode=settings.sandbox_network,
        # 資源上限。沙箱跑的是自我進化產生的程式碼，無界的迴圈與記憶體暴衝
        # 是預期會發生的事，不是例外狀況。
        mem_limit=f"{settings.sandbox_memory_mb}m",
        # 沒有 swap 空間：mem_limit == memswap_limit 代表容器打到記憶體上限時
        # 直接被 OOM kill，而不是掉進 swap 抖動、把整台主機一起拖下水。
        memswap_limit=f"{settings.sandbox_memory_mb}m",
        nano_cpus=int(settings.sandbox_cpus * 1_000_000_000),
        pids_limit=settings.sandbox_pids_limit,
        # 卸掉所有 capability。沙箱只需要跑使用者層的程式碼。
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        # 明確關掉 AutoRemove。開著的話容器一結束就消失，我們就撈不到
        # result.json 跟日誌了。移除改由下面的 finally 負責，孤兒則由回收器
        # 兜底 —— 「強制移除」依然成立，只是時機由我們掌握。
        auto_remove=False,
        # 給 /tmp 一塊 tmpfs，避免寫到容器可寫層。刻意不加 noexec：pip 在
        # 從原始碼建置套件時會在 TMPDIR 底下執行編譯產物。
        tmpfs={"/tmp": f"rw,nosuid,size={min(settings.sandbox_memory_mb, 512)}m"},
        # 不重啟。任務失敗就是失敗，自動重啟只會製造出殭屍沙箱。
        restart_policy={"Name": "no"},
    )


def run_task(
    client: docker.DockerClient,
    task_id: str,
    task_spec: dict[str, Any],
    *,
    on_container_created: Any = None,
) -> SandboxOutcome:
    """在一個全新的短暫容器裡執行進化任務。

    ``on_container_created`` 會在容器 ID 出現時立刻被呼叫，讓生命週期管理端
    能把它登記進「進行中」集合，避免回收器把一個剛出生的容器誤認成孤兒。
    """
    outcome = SandboxOutcome()
    started = time.monotonic()
    container: Container | None = None

    # 讓沙箱裡的執行器也知道時間預算，這樣它能自己優雅收尾；外層的硬性
    # 逾時只是最後手段。
    task_payload = {**task_spec, "id": task_id}
    task_payload.setdefault("timeout_sec", settings.sandbox_timeout_sec)

    try:
        try:
            container = client.containers.create(
                image=settings.sandbox_image,
                labels={
                    **settings.sandbox_label,
                    "hermes.task": task_id,
                },
                **_sandbox_limits(),
                # 不對外開任何埠，也不繼承 controller 的環境變數。沙箱能拿到
                # 的環境變數只有映像內建的，加上 task.json 裡明確指定的。
                environment={"SANDBOX_WORK_DIR": _WORK_DIR},
                working_dir=_WORK_DIR,
                network_disabled=False,
                detach=True,
            )
        except ImageNotFound:
            outcome.error = (
                f"找不到沙箱映像 {settings.sandbox_image!r}。"
                "請先執行 `make build`。"
            )
            return outcome

        outcome.container_id = container.id
        if on_container_created:
            on_container_created(container.id)

        # 在啟動「之前」把任務定義送進去。這樣沙箱一開始執行就一定看得到
        # task.json，不需要任何輪詢或就緒握手。
        payload = json.dumps(task_payload, ensure_ascii=False).encode("utf-8")
        container.put_archive(_WORK_DIR, _tar_bytes({"task.json": payload}))

        container.start()

        # 外層硬性逾時。沙箱裡的執行器有自己的預算並會嘗試優雅收尾，但如果
        # 它整個卡死（D 狀態的 I/O、fork 炸彈耗盡 pids），只有這裡救得了。
        # 多給 30 秒緩衝，讓內層逾時有機會先觸發並留下比較有用的結果。
        wait_timeout = settings.sandbox_timeout_sec + 30
        try:
            wait_result = container.wait(timeout=wait_timeout)
            outcome.exit_code = wait_result.get("StatusCode")
        except Exception as exc:  # noqa: BLE001 — docker SDK 逾時的例外型別不穩定
            log.warning("沙箱 %s 等待逾時（%ss）：%s", task_id, wait_timeout, exc)
            outcome.status = "timeout"
            outcome.error = f"容器超過 {wait_timeout} 秒仍未結束，已強制終止"
            try:
                container.kill()
            except (NotFound, APIError, DockerException):
                pass

        # 日誌與產物在移除容器「之前」收集 —— 這正是不用 AutoRemove 的理由。
        try:
            outcome.logs = container.logs(tail=2000).decode("utf-8", errors="replace")[
                -_MAX_LOG_BYTES:
            ]
        except (APIError, DockerException) as exc:
            outcome.logs = f"(取得日誌失敗：{exc})"

        outcome.result = _fetch_result(container)
        outcome.artifacts = _fetch_artifacts(container)

        if outcome.status != "timeout":
            if outcome.result:
                outcome.status = outcome.result.get("status", "error")
            elif outcome.exit_code == 0:
                # 退出碼是 0 卻沒有 result.json 的話，不能當成功處理 ——
                # 我們沒有任何證據說明它到底做了什麼。
                outcome.status = "error"
                outcome.error = "沙箱以 0 結束但沒有寫出 result.json"
            else:
                outcome.status = "failed"

        return outcome

    except (APIError, DockerException) as exc:
        outcome.status = "error"
        outcome.error = f"Docker API 錯誤：{exc}"
        return outcome
    except Exception as exc:  # noqa: BLE001
        outcome.status = "error"
        outcome.error = f"未預期的錯誤：{exc!r}"
        return outcome
    finally:
        outcome.duration_sec = round(time.monotonic() - started, 3)
        # 完成後強制移除。無論上面走的是哪條路徑都會執行到這裡。
        # 唯一漏掉的情況是 controller 行程本身被 SIGKILL —— 那由回收器負責。
        if container is not None:
            force_remove(container)


def _fetch_result(container: Container) -> dict[str, Any] | None:
    """把沙箱寫出的 result.json 拉回來。"""
    try:
        stream, _ = container.get_archive(_RESULT_PATH)
    except (NotFound, APIError, DockerException) as exc:
        log.warning("容器 %s 沒有 result.json：%s", container.id[:12], exc)
        return None

    try:
        files = _extract_tar(stream)
    except (tarfile.TarError, ValueError) as exc:
        log.warning("解開 result.json 失敗：%s", exc)
        return None

    payload = files.get("result.json")
    if payload is None:
        return None
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("result.json 不是合法 JSON：%s", exc)
        return None
    return parsed if isinstance(parsed, dict) else None


def _fetch_artifacts(container: Container) -> dict[str, bytes]:
    """把 /work/out 底下的候選技能檔案拉回來。"""
    try:
        stream, _ = container.get_archive(_OUT_DIR)
    except (NotFound, APIError, DockerException):
        # 沒有產物是完全正常的（例如純測試任務）。
        return {}

    try:
        # get_archive 回來的成員路徑會以目錄名為前綴（"out/..."），剝掉它，
        # 讓路徑相對於候選技能的根目錄。
        return _extract_tar(stream, strip_prefix="out/")
    except (tarfile.TarError, ValueError) as exc:
        log.warning("解開產物失敗：%s", exc)
        return {}
