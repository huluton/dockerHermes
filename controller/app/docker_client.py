"""經由 docker-socket-proxy 存取 Docker 的薄封裝。

這裡有兩件事很重要，不然容易踩雷：

1. **不要呼叫任何被 proxy 擋掉的 API。** proxy 被設定成
   ``NETWORKS=0``、``SWARM=0``、``SECRETS=0``、``CONFIGS=0``。docker SDK 有些
   看起來人畜無害的便利用法背地裡會打這些端點 —— 最典型的是
   ``containers.run(network=...)``，它會先 inspect 網路。所以底下一律直接組
   ``HostConfig``。

2. **回收器必須依實例過濾。** 只用 ``hermes.role=sandbox`` 篩選，在同一台
   主機上跑第二套 stack 時，就會把別套正在執行的沙箱一起砍掉。
"""

from __future__ import annotations

import logging

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from .config import settings

log = logging.getLogger(__name__)


class DockerUnavailable(RuntimeError):
    """無法透過 socket proxy 連上 Docker。"""


def build_client() -> docker.DockerClient:
    """建立指向 socket proxy 的 Docker client。

    刻意用 ``version="auto"``。docker SDK 預設會鎖在一個它自己編譯進去的 API
    版本上，那個版本可能比宿主機 daemon 還新，於是每個呼叫都回 400。自動協商
    只需要 ``GET /version``，那是 proxy 預設就放行的端點。
    """
    try:
        client = docker.DockerClient(
            base_url=settings.docker_host,
            version="auto",
            timeout=settings.docker_api_timeout,
        )
    except DockerException as exc:
        raise DockerUnavailable(
            f"經由 {settings.docker_host} 連線 Docker 失敗：{exc}"
        ) from exc

    return client


def check_access(client: docker.DockerClient) -> dict[str, object]:
    """啟動時的自我檢查：確認我們該有的權限都有、不該有的都沒有。

    這是刻意做的。權限設定錯誤如果不在啟動時暴露出來，就會延後到第一個
    進化任務跑到一半才炸 —— 那時候已經有一個沙箱容器在外面晃了。
    """
    report: dict[str, object] = {}

    try:
        report["docker_version"] = client.version().get("Version", "unknown")
    except (APIError, DockerException) as exc:
        raise DockerUnavailable(f"透過 proxy 讀取 Docker 版本失敗：{exc}") from exc

    # 必須有：列出容器（回收器與生命週期管理都依賴它）。
    try:
        client.containers.list(all=True, filters={"label": "hermes.role=sandbox"})
        report["containers_api"] = "ok"
    except (APIError, DockerException) as exc:
        raise DockerUnavailable(
            f"容器 API 不可用 —— socket proxy 需要 CONTAINERS=1：{exc}"
        ) from exc

    # 必須有：沙箱映像存在。缺了它每個任務都會失敗，而且錯誤訊息很難懂。
    try:
        client.images.get(settings.sandbox_image)
        report["sandbox_image"] = "present"
    except ImageNotFound:
        report["sandbox_image"] = "missing"
        log.warning(
            "沙箱映像 %s 在 Docker daemon 上不存在。請先執行 `make build`；"
            "在此之前所有進化任務都會失敗。",
            settings.sandbox_image,
        )
    except (APIError, DockerException) as exc:
        report["sandbox_image"] = f"unknown ({exc})"

    # 必須「沒有」：網路 API。proxy 應該要拒絕 Network API。如果這裡
    # 竟然成功了，代表 proxy 設定比預期寬鬆 —— 大聲記下來，但不要因此拒絕
    # 啟動（可能是操作者刻意放寬的）。
    try:
        client.networks.list()
    except (APIError, DockerException):
        report["networks_api"] = "denied (符合預期)"
    else:
        report["networks_api"] = "allowed"
        log.warning(
            "socket proxy 放行了 Network API。這應該要被拒絕 —— "
            "請確認 docker-compose.yml 裡 NETWORKS=0。"
        )

    return report


def list_sandboxes(client: docker.DockerClient, *, all_states: bool = True) -> list[Container]:
    """列出「本實例」的沙箱容器。

    兩個標籤都要帶。少了 instance 這一項，這個函式就會變成一把跨 stack 的獵槍。
    """
    label_filters = [f"{k}={v}" for k, v in settings.sandbox_label.items()]
    try:
        return client.containers.list(all=all_states, filters={"label": label_filters})
    except (APIError, DockerException) as exc:
        log.error("列出沙箱容器失敗：%s", exc)
        return []


def force_remove(container: Container) -> bool:
    """強制移除一個容器，把「已經不在了」視為成功。

    沙箱必須在完成後強制移除。這個函式是各條移除路徑（正常結束、
    逾時、孤兒回收）共用的收斂點。
    """
    try:
        container.remove(force=True)
        return True
    except NotFound:
        # 已經被移除了 —— 想要的結果已經達成。AutoRemove 或另一個回收循環
        # 搶先一步時會走到這裡。
        return True
    except (APIError, DockerException) as exc:
        log.error("移除容器 %s 失敗：%s", container.id[:12], exc)
        return False


def reap_orphans(client: docker.DockerClient, *, active_ids: set[str]) -> int:
    """移除不屬於任何進行中任務的沙箱容器。

    孤兒的來源：controller 在建立容器與移除容器之間被砍掉。生命週期管理裡的
    ``finally`` 涵蓋不了行程被 SIGKILL 的情況，所以需要這個兜底機制 ——
    啟動時跑一次，之後週期性跑。
    """
    removed = 0
    for container in list_sandboxes(client):
        if container.id in active_ids:
            continue
        log.info(
            "回收孤兒沙箱 %s（狀態=%s，任務=%s）",
            container.id[:12],
            container.status,
            container.labels.get("hermes.task", "?"),
        )
        if force_remove(container):
            removed += 1
    return removed
