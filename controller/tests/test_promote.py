"""原子性技能晉升的測試。

重點在於「Runtime 絕對讀不到寫到一半的狀態」這個保證，以及支撐它的兩個
前提：symlink 用相對路徑（兩個容器的路徑對得上）、版本庫與線上目錄分屬
不同的卷（rename 只發生在線上目錄內部）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from app import promote as P


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """模擬正式環境的兩卷佈局：線上目錄與版本庫是分開的兩棵樹。"""
    live = tmp_path / "opt" / "data" / "skills" / "evolved"
    versions = tmp_path / "opt" / "data" / "skill-versions"
    live.mkdir(parents=True)
    versions.mkdir(parents=True)
    return live, versions


def artifacts(body: str = "print('v1')\n") -> dict[str, bytes]:
    return {
        "SKILL.md": b"---\nname: demo\ndescription: demo\n---\n\n# Demo\n",
        "scripts/run.py": body.encode(),
    }


# --- 技能名稱驗證 ----------------------------------------------------------


@pytest.mark.parametrize("name", ["demo", "my-skill", "a.b_c-1", "x" * 64])
def test_valid_names_accepted(name: str) -> None:
    assert P.validate_skill_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "", " ", ".", "..", "../escape", "a/b", "a\\b", "-leading", "UPPER",
        "x" * 65, "sk ill", "sk\x00ill",
    ],
)
def test_dangerous_names_rejected(name: str) -> None:
    with pytest.raises(P.PromotionError):
        P.validate_skill_name(name)


# --- 基本晉升 --------------------------------------------------------------


def test_promote_creates_symlink_to_version(roots) -> None:
    live, versions = roots
    result = P.promote(
        versions_root=versions, live_root=live, skill="demo", artifacts=artifacts()
    )

    link = live / "demo"
    assert link.is_symlink(), "線上項目必須是 symlink，這是原子替換的前提"
    assert (link / "SKILL.md").read_bytes().startswith(b"---")
    assert (link / "scripts" / "run.py").read_text() == "print('v1')\n"
    assert result.files == 2


def test_symlink_target_is_relative(roots) -> None:
    """絕對路徑的 symlink 在 runtime 容器裡會指到不存在的地方。

    這是整套設計裡最容易靜默壞掉的一點：controller 寫進去的路徑字串，會被
    runtime 用自己的 mount namespace 解析。
    """
    live, versions = roots
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts())

    target = os.readlink(live / "demo")
    assert not os.path.isabs(target), f"symlink 目標是絕對路徑：{target}"
    assert target.startswith(".."), f"目標應該走出 live 目錄：{target}"


def test_promotion_requires_skill_md(roots) -> None:
    live, versions = roots
    with pytest.raises(P.PromotionError, match="SKILL.md"):
        P.promote(
            versions_root=versions,
            live_root=live,
            skill="demo",
            artifacts={"scripts/run.py": b"x = 1\n"},
        )


def test_empty_artifacts_rejected(roots) -> None:
    live, versions = roots
    with pytest.raises(P.PromotionError):
        P.promote(versions_root=versions, live_root=live, skill="demo", artifacts={})


# --- 路徑穿越 --------------------------------------------------------------


@pytest.mark.parametrize(
    "escape", ["../../etc/passwd", "/etc/passwd", "scripts/../../../out"]
)
def test_artifact_paths_cannot_escape(roots, escape: str) -> None:
    """產物路徑由沙箱裡執行的任意程式碼決定，屬於不受信任的輸入。"""
    live, versions = roots
    with pytest.raises(P.PromotionError):
        P.promote(
            versions_root=versions,
            live_root=live,
            skill="demo",
            artifacts={"SKILL.md": b"---\nname: d\n---\n", escape: b"pwned"},
        )
    # 失敗的晉升不能留下線上狀態。
    assert not (live / "demo").exists()


def test_failed_promotion_leaves_live_untouched(roots) -> None:
    live, versions = roots
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v1"))
    first = os.readlink(live / "demo")

    with pytest.raises(P.PromotionError):
        P.promote(
            versions_root=versions,
            live_root=live,
            skill="demo",
            artifacts={"SKILL.md": b"---\n---\n", "../escape": b"x"},
        )

    assert os.readlink(live / "demo") == first, "失敗的晉升動到了線上狀態"


# --- 版本管理 --------------------------------------------------------------


def test_successive_promotions_move_the_symlink(roots) -> None:
    live, versions = roots
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v1"))
    v1 = os.readlink(live / "demo")

    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v2"))
    v2 = os.readlink(live / "demo")

    assert v1 != v2
    assert (live / "demo" / "scripts" / "run.py").read_text() == "v2"


def test_same_second_promotions_get_distinct_versions(roots) -> None:
    """版本代號的解析度是秒。同一秒內連兩次晉升不能撞名。"""
    live, versions = roots
    a = P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("a"))
    b = P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("b"))
    assert a.version != b.version


def test_old_versions_are_pruned(roots) -> None:
    live, versions = roots
    for i in range(6):
        P.promote(
            versions_root=versions,
            live_root=live,
            skill="demo",
            artifacts=artifacts(f"v{i}"),
            keep_versions=3,
        )

    kept = sorted(p.name for p in versions.glob("demo-*") if p.is_dir())
    assert len(kept) == 3
    # 線上指向的版本必須還在 —— 清理不能砍掉正在用的那一個。
    assert (live / "demo").resolve().name in kept


def test_pruning_is_scoped_per_skill(roots) -> None:
    """一個技能的清理不能碰到另一個技能的版本。"""
    live, versions = roots
    for i in range(4):
        P.promote(
            versions_root=versions, live_root=live, skill="alpha",
            artifacts=artifacts(f"a{i}"), keep_versions=2,
        )
    P.promote(versions_root=versions, live_root=live, skill="beta", artifacts=artifacts("b"))

    assert len(list(versions.glob("alpha-*"))) == 2
    assert len(list(versions.glob("beta-*"))) == 1


def test_pruning_does_not_touch_prefix_sharing_skills(roots) -> None:
    """技能 demo 的清理不能碰到技能 demo-extra 的版本。

    版本目錄名稱是 `<skill>-<timestamp>`，所以 glob("demo-*") 會一併撈到
    demo-extra 的所有版本 —— 名稱前綴相同，但那是另一個技能的歷史。
    """
    live, versions = roots
    for i in range(3):
        P.promote(
            versions_root=versions, live_root=live, skill="demo-extra",
            artifacts=artifacts(f"e{i}"), keep_versions=5,
        )
    for i in range(4):
        P.promote(
            versions_root=versions, live_root=live, skill="demo",
            artifacts=artifacts(f"d{i}"), keep_versions=1,
        )

    survivors = {p.name for p in versions.iterdir() if p.is_dir()}
    assert len([n for n in survivors if n.startswith("demo-extra-")]) == 3
    assert (live / "demo-extra").resolve().is_dir()
    assert (live / "demo").resolve().is_dir()


def test_rollback_rejects_prefix_sharing_version(roots) -> None:
    """demo 不能回滾到 demo-extra 的版本，即使名稱前綴對得上。"""
    live, versions = roots
    other = P.promote(
        versions_root=versions, live_root=live, skill="demo-extra", artifacts=artifacts("e")
    )
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("d"))

    with pytest.raises(P.PromotionError, match="不屬於"):
        P.rollback(versions_root=versions, live_root=live, skill="demo", version=other.version)


# --- 回滾 ------------------------------------------------------------------


def test_rollback_restores_previous_version(roots) -> None:
    live, versions = roots
    first = P.promote(
        versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v1")
    )
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v2"))
    assert (live / "demo" / "scripts" / "run.py").read_text() == "v2"

    P.rollback(versions_root=versions, live_root=live, skill="demo", version=first.version)
    assert (live / "demo" / "scripts" / "run.py").read_text() == "v1"


def test_rollback_rejects_foreign_version(roots) -> None:
    """不能把 alpha 回滾到 beta 的版本。"""
    live, versions = roots
    beta = P.promote(
        versions_root=versions, live_root=live, skill="beta", artifacts=artifacts()
    )
    P.promote(versions_root=versions, live_root=live, skill="alpha", artifacts=artifacts())

    with pytest.raises(P.PromotionError, match="不屬於"):
        P.rollback(versions_root=versions, live_root=live, skill="alpha", version=beta.version)


@pytest.mark.parametrize("version", ["../../etc", "demo-../../x", "nope"])
def test_rollback_rejects_bad_versions(roots, version: str) -> None:
    live, versions = roots
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts())
    with pytest.raises(P.PromotionError):
        P.rollback(versions_root=versions, live_root=live, skill="demo", version=version)


# --- 併發 ------------------------------------------------------------------


def test_concurrent_promotions_of_same_skill_are_serialised(roots) -> None:
    """MAX_CONCURRENT_TASKS > 1 時，同一個技能可能同時跑完兩個任務。

    沒有 per-skill 鎖的話，A 的清理階段（用 glob 找「這個技能的所有版本」）
    有機會砍掉 B 正在寫入的版本目錄。
    """
    live, versions = roots
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            P.promote(
                versions_root=versions, live_root=live, skill="demo",
                artifacts=artifacts(f"v{n}"), keep_versions=2,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"併發晉升出錯：{errors}"
    # 線上 symlink 必須指向一個真實存在的目錄，不能是被清理掉的殘骸。
    assert (live / "demo").resolve().is_dir()
    assert (live / "demo" / "SKILL.md").exists()


# --- 列表 ------------------------------------------------------------------


def test_list_skills_reports_current_version(roots) -> None:
    live, versions = roots
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v1"))
    second = P.promote(
        versions_root=versions, live_root=live, skill="demo", artifacts=artifacts("v2")
    )

    listed = P.list_skills(live, versions)
    assert len(listed) == 1
    assert listed[0]["skill"] == "demo"
    assert listed[0]["current_version"] == second.version
    assert listed[0]["is_symlink"] is True
    assert len(listed[0]["available_versions"]) == 2


def test_list_skills_on_empty_tree(tmp_path: Path) -> None:
    assert P.list_skills(tmp_path / "nope", tmp_path / "also-nope") == []


# --- 檔案模式 --------------------------------------------------------------


def test_promoted_files_are_not_executable(roots) -> None:
    """技能檔案是要被讀取的，不是要被執行的。"""
    live, versions = roots
    P.promote(versions_root=versions, live_root=live, skill="demo", artifacts=artifacts())

    mode = (live / "demo" / "scripts" / "run.py").stat().st_mode & 0o777
    assert mode == 0o644, f"檔案模式是 {oct(mode)}，應為 0o644"
