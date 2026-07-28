"""原子性技能晉升 —— 用 os.replace() 確保 Runtime 不會讀取到寫入一半的檔案。

問題本身
--------
Runtime 隨時可能在讀技能目錄。如果我們就地覆寫檔案，runtime 有機會讀到寫了
一半的檔案，或是讀到「新舊檔案混雜」的一組不一致狀態。

解法：目錄的原子性替換
----------------------
``os.replace()`` 對單一檔案是原子的，但**沒辦法原子替換一個非空目錄** ——
底層的 ``rename(2)`` 在目標目錄非空時會回 ``ENOTEMPTY``。而一個技能是一整個
目錄（``SKILL.md`` 加上若干支援檔案），必須整組一起換。

標準做法是換 symlink：

1. 把新版本完整寫進 ``.versions/<skill>-<timestamp>/``
2. 建一個指向它的暫時 symlink
3. ``os.replace(暫時symlink, evolved/<skill>)``

``rename(2)`` 蓋過既有 symlink 是原子的，所以 runtime 看到的永遠是「完整的舊
版本」或「完整的新版本」，不存在中間狀態。已經開著舊檔案 handle 的行程會繼續
讀完舊版本，這正是我們要的行為。

兩個卷，以及為什麼 symlink 用相對路徑
--------------------------------------
線上目錄（``live_root``）與版本庫（``versions_root``）是兩個獨立的卷。版本庫
刻意放在 runtime 的技能掃描樹之外 —— 上游用 ``os.walk(followlinks=True)`` 掃
技能目錄，版本庫如果在掃描範圍內，保留的每一個歷史版本都會被當成一個獨立的
線上技能。

跨檔案系統在這裡**不是問題**：``os.replace()`` 搬的是 symlink 本身，來源與
目標都在 ``live_root`` 裡，同一個卷。symlink 指向另一個卷完全沒關係 ——
解析發生在讀取的時候，不是 rename 的時候。

symlink 的內容仍然寫成相對路徑（``os.path.relpath``）。這要求兩個卷在
controller 與 runtime 裡掛在相同的絕對路徑上，config.py 的 validate() 會確認
這件事的前提條件。相對路徑比絕對路徑安全的地方在於：萬一有人把整組卷搬到
別的掛載點，只要兩者的相對關係不變，連結就還是通的。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

# 每個技能一把鎖。同一個技能的兩個進化任務可能同時跑完（MAX_CONCURRENT_TASKS
# 預設是 2），而 _prune_versions 是用 glob 找「這個技能的所有版本目錄」——
# 沒有鎖的話，A 的清理階段有機會把 B 正在寫入的版本目錄砍掉。
#
# 鎖只在單一 controller 行程內有效，這樣就夠了：一個卷同時只由一個 controller
# 掛成 rw。
_skill_locks: dict[str, threading.Lock] = {}
_skill_locks_guard = threading.Lock()


def _skill_lock(skill: str) -> threading.Lock:
    with _skill_locks_guard:
        return _skill_locks.setdefault(skill, threading.Lock())

# 技能名稱：只允許這個字元集。技能名稱最終會變成一段檔案系統路徑，而它的
# 來源是進化任務 —— 也就是不受信任的輸入。
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# 版本目錄名稱的格式：<skill>-<utc timestamp>-<序號>
_VERSION_FMT = "%Y%m%dT%H%M%SZ"


class PromotionError(RuntimeError):
    """晉升失敗，且沒有對線上狀態造成任何變更。"""


@dataclass
class PromotionResult:
    skill: str
    version: str
    version_path: str
    live_path: str
    files: int
    bytes_written: int
    pruned_versions: list[str]


def validate_skill_name(name: str) -> str:
    """驗證技能名稱可安全用作路徑元素。"""
    candidate = (name or "").strip()
    if not _SKILL_NAME_RE.match(candidate):
        raise PromotionError(
            f"技能名稱 {name!r} 不合法。允許：小寫英數起頭，"
            "後接 [a-z0-9._-]，總長最多 64。"
        )
    # _SKILL_NAME_RE 已經涵蓋這些情況，但這裡明確再擋一次 —— 這是安全性檢查，
    # 有人日後放寬正規表示式時，這幾行還在。
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise PromotionError(f"技能名稱 {name!r} 含有路徑元素")
    return candidate


def _version_re(skill: str) -> re.Pattern[str]:
    """比對「屬於這個技能」的版本目錄名稱。

    不能只用 ``glob(f"{skill}-*")`` —— 技能 ``demo`` 的 glob 會一併撈到技能
    ``demo-x`` 的版本目錄，於是 demo 的清理會把 demo-x 的歷史刪掉。
    """
    return re.compile(rf"^{re.escape(skill)}-\d{{8}}T\d{{6}}Z(?:-\d+)?$")


def _skill_versions(versions_root: Path, skill: str) -> list[Path]:
    """回傳這個技能的所有版本目錄。"""
    pattern = _version_re(skill)
    try:
        entries = list(versions_root.iterdir())
    except OSError:
        return []
    return [p for p in entries if pattern.match(p.name) and p.is_dir()]


def _safe_target(root: Path, rel_path: str) -> Path:
    """把相對路徑解析到 ``root`` 底下，拒絕任何逃出去的嘗試。"""
    rel = rel_path.strip()
    if not rel:
        raise PromotionError("產物的路徑是空的")
    if "\x00" in rel:
        raise PromotionError(f"產物路徑含有 NUL 位元組：{rel_path!r}")
    # 絕對路徑一律拒絕，而不是把開頭的 "/" 剝掉當相對路徑用。沙箱回傳的產物
    # 路徑本來就該是相對的（sandbox.py 解 tar 時已經剝過前綴），出現絕對路徑
    # 代表有東西不對勁 —— 那是個訊號，不該被靜默改寫。
    if rel.startswith("/") or PurePosixPath(rel).is_absolute():
        raise PromotionError(f"產物路徑 {rel_path!r} 是絕對路徑")

    target = (root / rel).resolve()
    root_resolved = root.resolve()
    # Python 3.9+ 的 is_relative_to。這是防路徑穿越的關鍵一道 ——
    # 產物路徑由沙箱內執行的任意程式碼決定。
    if not target.is_relative_to(root_resolved):
        raise PromotionError(f"產物路徑 {rel_path!r} 逃出了技能目錄")
    return target


def _write_version(version_dir: Path, artifacts: dict[str, bytes]) -> tuple[int, int]:
    """把產物寫進版本目錄。回傳 (檔案數, 位元組數)。"""
    files = 0
    total = 0
    for rel_path, payload in sorted(artifacts.items()):
        target = _safe_target(version_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        # 技能檔案是要被讀取的，不是要被執行的。runtime 用直譯器載入它們。
        target.chmod(0o644)
        files += 1
        total += len(payload)
    return files, total


def _atomic_symlink_swap(link_path: Path, target: Path) -> None:
    """讓 ``link_path`` 原子性地指向 ``target``。

    ``os.symlink`` 無法覆蓋既有路徑，所以做法是：先建一個名字唯一的暫時
    symlink，再 ``os.replace()`` 把它搬到最終位置。rename 蓋過既有路徑是原子的。
    """
    tmp_link = link_path.parent / f".{link_path.name}.swap-{os.getpid()}-{time.time_ns()}"
    # symlink 內容用相對路徑，這樣整個卷被掛到不同的容器內路徑時（controller
    # 是 /skills，runtime 是 /opt/data/skills）依然能正確解析。這一點是關鍵：
    # 絕對路徑的 symlink 在 runtime 那邊會直接指向不存在的地方。
    relative_target = os.path.relpath(target, link_path.parent)

    try:
        os.symlink(relative_target, tmp_link)
    except OSError as exc:
        raise PromotionError(f"建立暫時 symlink 失敗：{exc}") from exc

    try:
        os.replace(tmp_link, link_path)
    except OSError as exc:
        # 清掉暫時 symlink，別在目錄裡留垃圾。
        try:
            tmp_link.unlink()
        except OSError:
            pass
        if exc.errno == errno.EXDEV:
            # 理論上到不了這裡 —— tmp_link 與 link_path 都在 live_root 底下。
            # 真的發生的話，代表有人在 live_root 內又疊了一層掛載。
            raise PromotionError(
                f"os.replace 因跨檔案系統而失敗（EXDEV）。{link_path.parent} 底下"
                "似乎有巢狀掛載 —— 請檢查 compose 的 volumes 設定。"
            ) from exc
        if exc.errno == errno.EISDIR or exc.errno == errno.ENOTEMPTY:
            raise PromotionError(
                f"{link_path} 是一個真實目錄而不是 symlink。這代表某次晉升被中斷過，"
                "或有人手動改動了技能目錄。請手動移除後重新晉升。"
            ) from exc
        raise PromotionError(f"原子性替換失敗：{exc}") from exc


def _prune_versions(
    versions_root: Path, skill: str, keep: int, *, protect: Path | None = None
) -> list[str]:
    """移除舊版本，保留最近 ``keep`` 個。回傳被刪掉的清單。

    ``protect`` 是目前線上使用中的版本目錄，永遠不會被刪 —— 就算它已經掉出
    保留視窗。少了這道保護，清理有機會把 symlink 剛剛指過去的目錄砍掉，
    留下一個斷掉的線上技能。

    排序用 mtime 而不是名稱。名稱排序看起來可行，實際上不成立：版本代號的
    解析度是秒，同一秒內的多次晉升靠 ``-2``、``-3`` 後綴區分，而後綴的號碼
    在舊版本被清掉之後會被重複使用 —— 於是「名稱最大」不等於「最新」。
    """
    candidates = []
    for path in _skill_versions(versions_root, skill):
        try:
            candidates.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    candidates.sort(reverse=True)

    protected: Path | None = None
    if protect is not None:
        try:
            protected = protect.resolve()
        except OSError:
            protected = None

    pruned: list[str] = []
    for _, _, old in candidates[keep:]:
        if protected is not None:
            try:
                if old.resolve() == protected:
                    continue
            except OSError:
                pass
        try:
            shutil.rmtree(old)
            pruned.append(old.name)
        except OSError as exc:
            log.warning("清除舊版本 %s 失敗：%s", old, exc)
    return pruned


def promote(
    *,
    versions_root: Path,
    live_root: Path,
    skill: str,
    artifacts: dict[str, bytes],
    keep_versions: int = 5,
    metadata: dict[str, Any] | None = None,
) -> PromotionResult:
    """把一組候選技能檔案原子性地晉升上線。

    這個函式要嘛完整成功、要嘛完全不改變線上狀態。中途失敗只會在版本庫底下
    留下一個沒有被引用的版本目錄，下一輪清理會處理掉它。
    """
    skill = validate_skill_name(skill)
    with _skill_lock(skill):
        return _promote_locked(
            versions_root=versions_root,
            live_root=live_root,
            skill=skill,
            artifacts=artifacts,
            keep_versions=keep_versions,
            metadata=metadata,
        )


def _promote_locked(
    *,
    versions_root: Path,
    live_root: Path,
    skill: str,
    artifacts: dict[str, bytes],
    keep_versions: int,
    metadata: dict[str, Any] | None,
) -> PromotionResult:
    if not artifacts:
        raise PromotionError("沒有任何產物可以晉升")

    # 每個技能都必須有 SKILL.md —— 這是上游 hermes 技能格式的規定，同時也
    # 順帶擋掉「一堆散檔被誤認成技能」的情況。
    if not any(Path(p).name == "SKILL.md" for p in artifacts):
        raise PromotionError(
            "候選技能沒有 SKILL.md。hermes 技能必須有這個描述檔。"
        )

    versions_root.mkdir(parents=True, exist_ok=True)
    live_root.mkdir(parents=True, exist_ok=True)

    # 版本代號。同一秒內連續晉升同一個技能時，加上序號避免撞名。
    stamp = time.strftime(_VERSION_FMT, time.gmtime())
    version = f"{skill}-{stamp}"
    version_dir = versions_root / version
    seq = 1
    while version_dir.exists():
        seq += 1
        version = f"{skill}-{stamp}-{seq}"
        version_dir = versions_root / version

    # --- 第 1 階段：完整寫入新版本（此時線上狀態完全沒被碰過）-------------
    try:
        version_dir.mkdir(parents=True)
        files, total = _write_version(version_dir, artifacts)

        if metadata:
            meta_path = version_dir / ".promotion.json"
            meta_path.write_text(
                json.dumps(
                    {**metadata, "skill": skill, "version": version, "files": files},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        # 在切換 symlink 之前先 fsync 目錄樹。少了這一步，機器在切換之後、
        # 資料真正落盤之前斷電，symlink 會指到一個內容不完整的目錄 ——
        # 正是我們一開始要避免的「讀到寫一半的東西」。
        _fsync_tree(version_dir)

    except PromotionError:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise PromotionError(f"寫入版本目錄失敗：{exc}") from exc

    # --- 第 2 階段：原子性切換 ------------------------------------------
    live_path = live_root / skill
    _atomic_symlink_swap(live_path, version_dir)

    # --- 第 3 階段：清理（此時失敗已無所謂，線上狀態是好的）---------------
    # 把剛剛上線的版本傳進去保護。同一秒內多次晉升時，版本代號的序號會在舊
    # 版本被清掉之後被重複使用，剛上線的目錄有機會排在清理名單裡。
    pruned = _prune_versions(versions_root, skill, keep_versions, protect=version_dir)

    log.info(
        "已晉升技能 %s 版本 %s（%d 個檔案、%d 位元組、清除 %d 個舊版本）",
        skill, version, files, total, len(pruned),
    )

    return PromotionResult(
        skill=skill,
        version=version,
        version_path=str(version_dir),
        live_path=str(live_path),
        files=files,
        bytes_written=total,
        pruned_versions=pruned,
    )


def rollback(*, versions_root: Path, live_root: Path, skill: str, version: str) -> PromotionResult:
    """把某個技能切回先前的版本。

    用的是同一套原子性 symlink 替換，所以回滾與晉升具備相同的安全保證。
    """
    skill = validate_skill_name(skill)

    # 用完整的版本代號格式比對，而不是 startswith(f"{skill}-")。後者會讓技能
    # alpha 回滾到技能 alpha-beta 的版本 —— 名稱前綴相同，但那是別人的歷史。
    # 這同時也順帶擋掉了路徑元素（".."、"/" 都不符合格式）。
    if not _version_re(skill).match(version or ""):
        raise PromotionError(f"版本 {version!r} 不屬於技能 {skill!r}，或格式不合法")

    version_dir = versions_root / version
    if not version_dir.is_dir():
        raise PromotionError(f"找不到版本 {version!r}")

    _atomic_symlink_swap(live_root / skill, version_dir)

    files = sum(1 for p in version_dir.rglob("*") if p.is_file())
    log.info("已將技能 %s 回滾至版本 %s", skill, version)

    return PromotionResult(
        skill=skill,
        version=version,
        version_path=str(version_dir),
        live_path=str(live_root / skill),
        files=files,
        bytes_written=0,
        pruned_versions=[],
    )


def list_skills(live_root: Path, versions_root: Path) -> list[dict[str, Any]]:
    """列出目前線上的技能與各自可用的版本。"""
    out: list[dict[str, Any]] = []
    if not live_root.is_dir():
        return out

    for entry in sorted(live_root.iterdir()):
        if not entry.is_symlink() and not entry.is_dir():
            continue
        try:
            current = os.readlink(entry) if entry.is_symlink() else None
        except OSError:
            current = None
        current_version = Path(current).name if current else None

        # 同樣不能用 glob(f"{entry.name}-*")：技能 demo 會撈到技能 demo-x
        # 的版本目錄，列表上就會出現不屬於自己的歷史。
        versions = sorted(
            (p.name for p in _skill_versions(versions_root, entry.name)), reverse=True
        )
        out.append(
            {
                "skill": entry.name,
                "current_version": current_version,
                "is_symlink": entry.is_symlink(),
                "available_versions": versions,
            }
        )
    return out


def _fsync_tree(root: Path) -> None:
    """對目錄樹底下的所有檔案與目錄做 fsync。

    在切換 symlink 之前確保資料真的落到磁碟上。相對於進化任務本身的耗時，
    這點成本可以忽略，但它讓「原子性」在斷電情境下依然成立，而不只是在
    行程崩潰的情境下成立。
    """
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_file():
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            log.debug("fsync %s 失敗：%s", path, exc)

    try:
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        log.debug("fsync 目錄 %s 失敗：%s", root, exc)
