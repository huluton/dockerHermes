"""沙箱容器設定的測試。

這一組測試的存在理由很具體：`_sandbox_limits()` 回傳的 kwargs 會被展開餵給
docker-py 的高階 `containers.create()`，而那個 API 對不認得的 kwarg 是**執行期**
才拋 TypeError —— 而且訊息寫成 `run() got an unexpected keyword argument`，指向
一個根本沒被呼叫的方法。沒有這組測試的話，一個打錯的 kwarg 要等到真的提交
一次進化任務、看著它以 status=error 收場，才會被發現。

這裡不需要 Docker daemon：檢查的是 kwargs 名稱與 docker-py 的白名單對不對得上，
以及安全設定有沒有被人不小心放寬。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("SANDBOX_NETWORK", "hermes-test-net")
os.environ.setdefault("SANDBOX_IMAGE", "hermes-sandbox:test")

# importorskip 必須排在 app.sandbox 之前 —— app.sandbox 自己就 import docker，
# 順序反了的話沒裝 docker-py 的環境會是 collection error 而不是 skip。
docker_containers = pytest.importorskip("docker.models.containers")

from app import sandbox as S  # noqa: E402


# --- kwargs 必須被 docker-py 接受 -------------------------------------------


def test_every_limit_kwarg_is_accepted_by_docker_py() -> None:
    """展開給 containers.create() 的每一個 kwarg 都要在 docker-py 的白名單裡。

    docker-py 把 create() 的參數分成兩組：直接傳給 create_container 的
    （RUN_CREATE_KWARGS），與拿去組 HostConfig 的（RUN_HOST_CONFIG_KWARGS）。
    兩組都不認得的 kwarg 會被當成錯誤拋出。
    """
    accepted = set(docker_containers.RUN_CREATE_KWARGS) | set(
        docker_containers.RUN_HOST_CONFIG_KWARGS
    )
    unknown = set(S._sandbox_limits()) - accepted
    assert not unknown, f"docker-py 不接受這些 kwarg：{sorted(unknown)}"


def test_host_config_is_not_passed_directly() -> None:
    """不能傳 host_config= —— 高階 API 會自己組，傳了直接 TypeError。"""
    assert "host_config" not in S._sandbox_limits()


def test_create_call_signature_is_valid() -> None:
    """完整模擬一次 create() 的參數轉換，不碰 daemon。

    `_create_container_args` 就是實際會拋 TypeError 的那個函式。讓它跑一遍，
    等於在沒有 Docker 的情況下驗證整組呼叫參數。
    """
    kwargs = {
        "image": "hermes-sandbox:test",
        "command": None,
        "version": "1.43",
        "labels": {"hermes.role": "sandbox"},
        "environment": {"SANDBOX_WORK_DIR": "/work"},
        "working_dir": "/work",
        "network_disabled": False,
        "detach": True,
        **S._sandbox_limits(),
    }
    created = docker_containers._create_container_args(kwargs)
    assert "HostConfig" in created["host_config"] or created["host_config"]


# --- 安全設定不能被放寬 -----------------------------------------------------


def test_all_capabilities_are_dropped() -> None:
    assert S._sandbox_limits()["cap_drop"] == ["ALL"]


def test_no_new_privileges_is_set() -> None:
    """少了這個，沙箱裡的 setuid 執行檔就能提權。"""
    assert "no-new-privileges:true" in S._sandbox_limits()["security_opt"]


def test_auto_remove_is_off() -> None:
    """AutoRemove 開著的話容器一結束就消失，撈不到 result.json 與日誌。

    「強制移除」由 lifecycle 的 finally 負責，孤兒由回收器兜底。
    """
    assert S._sandbox_limits()["auto_remove"] is False


def test_network_is_pinned_to_the_instance_network() -> None:
    """留空會讓 Docker 用預設 bridge，等於繞過整套網路隔離。"""
    limits = S._sandbox_limits()
    assert limits["network_mode"] == "hermes-test-net"
    assert limits["network_mode"] not in {"", "default", "bridge", "host"}


def test_memory_and_swap_limits_match() -> None:
    """mem_limit == memswap_limit 代表沒有 swap：打到上限直接 OOM kill。"""
    limits = S._sandbox_limits()
    assert limits["mem_limit"] == limits["memswap_limit"]


def test_resource_ceilings_are_set() -> None:
    limits = S._sandbox_limits()
    assert limits["nano_cpus"] > 0
    assert limits["pids_limit"] > 0
    assert limits["restart_policy"] == {"Name": "no"}


def test_tmpfs_on_tmp_is_nosuid() -> None:
    """/tmp 給 tmpfs 避免寫進容器可寫層。

    刻意「不」加 noexec —— pip 從原始碼建套件時會在 TMPDIR 底下執行編譯產物。
    """
    opts = S._sandbox_limits()["tmpfs"]["/tmp"]
    assert "nosuid" in opts
    assert "size=" in opts


# --- tar 解壓：不受信任的輸入 -----------------------------------------------


def _tar(entries: dict[str, bytes], *, kind: int | None = None) -> list[bytes]:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            if kind is not None:
                info.type = kind
            tar.addfile(info, io.BytesIO(payload))
    return [buf.getvalue()]


def test_extract_strips_the_out_prefix() -> None:
    files = S._extract_tar(_tar({"out/SKILL.md": b"x", "out/a/b.py": b"y"}), strip_prefix="out/")
    assert sorted(files) == ["SKILL.md", "a/b.py"]


@pytest.mark.parametrize("evil", ["out/../../etc/passwd", "out/a/../../../x"])
def test_extract_drops_path_traversal(evil: str) -> None:
    """產物 tar 由沙箱內執行的任意程式碼產生，屬於不受信任的輸入。"""
    files = S._extract_tar(_tar({evil: b"pwned"}), strip_prefix="out/")
    assert files == {}


def test_extract_drops_symlinks() -> None:
    """symlink 可以指到封存範圍以外的地方，只收一般檔案。"""
    import tarfile

    files = S._extract_tar(_tar({"out/link": b""}, kind=tarfile.SYMTYPE), strip_prefix="out/")
    assert files == {}


def test_extract_rejects_oversized_archive() -> None:
    oversized = [b"x" * (S._MAX_ARTIFACT_BYTES + 1)]
    with pytest.raises(ValueError):
        S._extract_tar(oversized)
