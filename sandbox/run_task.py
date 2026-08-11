#!/usr/bin/env python3
"""在沙箱容器內執行一次進化任務。

契約
----
輸入  ``/work/task.json``：

    {
      "id": "evo-20260727-abc123",
      "name": "csv-summariser",
      "env": {"FOO": "bar"},
      "timeout_sec": 900,
      "steps": [
        {"name": "install", "run": ["pip", "install", "pandas"], "timeout_sec": 300},
        {"name": "generate", "run": ["bash", "-lc", "..."], "allow_failure": false},
        {"name": "test", "run": ["pytest", "-q", "out/"]}
      ]
    }

輸出  ``/work/result.json``（永遠會被寫出來，連 crash 的情況也是），
候選技能檔案則放在 ``/work/out/``。

result.json 裡的 ``installed`` 欄位記錄「這次任務在沙箱裡多裝了什麼」。沙箱
跑完就被銷毀，裝出來的檔案跟著消失 —— 能搬到 runtime 的只有這份清單，而且
它不會自動生效，見 controller/app/deps.py 與 README 的「讓 agent 自由裝套件」。

設計上的重點：**這個腳本絕不能不留結果就死掉。** 容器一結束就會被銷毀，
如果 result.json 沒寫出來，controller 就只剩一個裸退出碼可看，任何診斷資訊
都沒了。所以主要邏輯整個包在 try/finally 裡，finally 一定寫檔。
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 每個步驟保留的 stdout/stderr 位元組上限。超過就從中間截斷、頭尾都留 —
# 錯誤訊息通常在尾端（traceback），而失敗的成因常在頭端（設定、版本），
# 只留其中一邊都會把診斷資訊丟掉。
_OUTPUT_CAP_BYTES = 64 * 1024

# 沒指定時的預設值。
_DEFAULT_TASK_TIMEOUT = 900
_DEFAULT_STEP_TIMEOUT = 600

WORK_DIR = Path(os.environ.get("SANDBOX_WORK_DIR", "/work"))
OUT_DIR = WORK_DIR / "out"
RESULT_FILE = WORK_DIR / "result.json"

# 收到 SIGTERM 時翻起來的旗標。controller 逾時會先送 SIGTERM 再送 SIGKILL，
# 這中間的空檔正好夠我們把已完成步驟的結果寫出去。
_terminated = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _on_sigterm(signum: int, frame: Any) -> None:  # noqa: ARG001
    global _terminated
    _terminated = True
    raise KeyboardInterrupt("收到 SIGTERM")


def _truncate(raw: bytes) -> tuple[str, bool]:
    """把輸出解碼並在需要時做頭尾保留式截斷。"""
    if len(raw) <= _OUTPUT_CAP_BYTES:
        return raw.decode("utf-8", errors="replace"), False

    half = _OUTPUT_CAP_BYTES // 2
    head = raw[:half].decode("utf-8", errors="replace")
    tail = raw[-half:].decode("utf-8", errors="replace")
    omitted = len(raw) - _OUTPUT_CAP_BYTES
    return f"{head}\n\n... [略過 {omitted} 位元組] ...\n\n{tail}", True


def _run_step(step: dict[str, Any], env: dict[str, str], budget_left: float) -> dict[str, Any]:
    """執行單一步驟並回傳它的結果紀錄。"""
    name = step.get("name") or "unnamed"
    cmd = step.get("run")

    if not cmd:
        return {
            "name": name,
            "status": "error",
            "exit_code": None,
            "error": "步驟缺少 'run' 欄位",
        }

    # 允許字串形式（走 shell）與陣列形式（不走 shell）。陣列形式是預設也是
    # 建議用法 — 沒有 shell 介入就沒有引號被重新解讀的問題。
    use_shell = isinstance(cmd, str)
    display = cmd if use_shell else shlex.join(cmd)

    # 步驟自己的逾時不能超過整個任務剩下的預算，否則單一步驟就能拖垮
    # 整體上限，讓 controller 端的逾時失去意義。
    step_timeout = min(
        float(step.get("timeout_sec", _DEFAULT_STEP_TIMEOUT)),
        max(budget_left, 1.0),
    )

    started = time.monotonic()
    record: dict[str, Any] = {
        "name": name,
        "command": display,
        "timeout_sec": step_timeout,
    }

    try:
        proc = subprocess.run(  # noqa: S603 — 執行任意命令正是本沙箱的用途
            cmd,
            shell=use_shell,
            cwd=str(WORK_DIR),
            env=env,
            capture_output=True,
            timeout=step_timeout,
        )
        stdout, out_trunc = _truncate(proc.stdout)
        stderr, err_trunc = _truncate(proc.stderr)
        record.update(
            {
                "status": "success" if proc.returncode == 0 else "failed",
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": out_trunc or err_trunc,
            }
        )
    except subprocess.TimeoutExpired as exc:
        stdout, _ = _truncate(exc.stdout or b"")
        stderr, _ = _truncate(exc.stderr or b"")
        record.update(
            {
                "status": "timeout",
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "error": f"步驟超過 {step_timeout:.0f} 秒逾時上限",
            }
        )
    except FileNotFoundError as exc:
        record.update(
            {
                "status": "error",
                "exit_code": None,
                "error": f"找不到執行檔：{exc}",
            }
        )
    except OSError as exc:
        record.update({"status": "error", "exit_code": None, "error": str(exc)})

    record["duration_sec"] = round(time.monotonic() - started, 3)
    return record


def _collect_artifacts() -> list[str]:
    """列出候選技能檔案，路徑相對於 /work。

    只掃 out/。任務在 /work 其他地方留下的暫存檔（venv、clone 下來的
    儲存庫、pip 快取）都不是產物，不該被送去晉升。
    """
    if not OUT_DIR.is_dir():
        return []
    return sorted(
        str(p.relative_to(WORK_DIR))
        for p in OUT_DIR.rglob("*")
        if p.is_file()
    )


# --- 套件快照 --------------------------------------------------------------
#
# 這裡的每一個函式都遵守同一條規則：**失敗就回 None，絕不拋例外。** 快照是
# 附加資訊，不是任務的目的；一個 dpkg-query 打嗝不該讓一次跑了十分鐘的進化
# 任務變成失敗。


def _query(cmd: list[str], timeout: float = 60.0) -> str | None:
    try:
        proc = subprocess.run(  # noqa: S603 — 固定的命令，不含任務輸入
            cmd, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _apt_snapshot() -> dict[str, str] | None:
    """已安裝的 Debian 套件 -> 版本。

    用 dpkg-query 而不是 `apt list --installed`：前者的輸出格式由 -f 完全指定，
    不會因為 apt 的版本或語系而變動，也不會夾雜「WARNING: apt does not have a
    stable CLI interface」那行。
    """
    raw = _query(["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"])
    if raw is None:
        return None
    out: dict[str, str] = {}
    for line in raw.splitlines():
        name, _, version = line.partition("\t")
        if name and version:
            out[name] = version
    return out


def _pip_snapshot() -> dict[str, str] | None:
    """venv 裡已安裝的 Python 套件 -> 版本。"""
    raw = _query(
        [sys.executable, "-m", "pip", "list", "--format=json",
         "--disable-pip-version-check"]
    )
    if raw is None:
        return None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    return {
        str(e["name"]): str(e["version"])
        for e in entries
        if isinstance(e, dict) and e.get("name") and e.get("version")
    }


def _snapshot() -> dict[str, dict[str, str] | None]:
    return {"apt": _apt_snapshot(), "pip": _pip_snapshot()}


def _diff_one(
    before: dict[str, str] | None, after: dict[str, str] | None
) -> list[dict[str, str]] | None:
    """算出「多出來或版本變了」的套件。回傳 None 代表這一類沒抓到快照。"""
    if before is None or after is None:
        return None
    changed: list[dict[str, str]] = []
    for name, version in sorted(after.items()):
        previous = before.get(name)
        if previous == version:
            continue
        entry = {"name": name, "version": version}
        if previous is not None:
            entry["previous"] = previous
        changed.append(entry)
    return changed


def _diff_snapshots(
    before: dict[str, dict[str, str] | None],
    after: dict[str, dict[str, str] | None],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    unavailable: list[str] = []
    for kind in ("apt", "pip"):
        diff = _diff_one(before.get(kind), after.get(kind))
        if diff is None:
            unavailable.append(kind)
            out[kind] = []
        else:
            out[kind] = diff
    if unavailable:
        # 明講「這一類沒抓到」而不是靜靜回空陣列。空陣列的意思是「什麼都沒
        # 裝」，跟「量不到」是完全不同的兩件事，混在一起會讓人以為套件沒裝成。
        out["unavailable"] = unavailable
    return out


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)

    task_path = Path(argv[1]) if len(argv) > 1 else WORK_DIR / "task.json"

    result: dict[str, Any] = {
        "task_id": None,
        "status": "error",
        "started_at": _now(),
        "finished_at": None,
        "duration_sec": 0.0,
        "steps": [],
        "artifacts": [],
        "installed": None,
        "privileged": os.environ.get("SANDBOX_PRIVILEGED") == "1",
        "error": None,
    }
    started = time.monotonic()
    before: dict[str, dict[str, str] | None] | None = None

    try:
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result["error"] = f"讀不到或解析不了 {task_path}：{exc}"
            return 78  # EX_CONFIG

        result["task_id"] = task.get("id")
        steps = task.get("steps") or []
        if not steps:
            result["error"] = "任務沒有定義任何步驟"
            return 78

        # 以容器自身環境為基底，再疊上任務指定的變數。刻意不從乾淨環境開始：
        # PATH、VIRTUAL_ENV、HOME 都是映像建好的，砍掉會讓每個任務都得自己
        # 重建一遍。
        env = {**os.environ, **{str(k): str(v) for k, v in (task.get("env") or {}).items()}}

        budget = float(task.get("timeout_sec", _DEFAULT_TASK_TIMEOUT))
        OUT_DIR.mkdir(parents=True, exist_ok=True)

        # 在跑任何步驟「之前」拍第一張快照。這一次的耗時算進總預算裡是刻意
        # 的 —— 它跟步驟一樣會佔用容器的生命週期，藏起來只會讓逾時難以解釋。
        before = _snapshot()

        overall = "success"
        for step in steps:
            elapsed = time.monotonic() - started
            budget_left = budget - elapsed
            if budget_left <= 0:
                result["steps"].append(
                    {
                        "name": step.get("name") or "unnamed",
                        "status": "skipped",
                        "error": f"任務總預算 {budget:.0f} 秒已用盡",
                    }
                )
                overall = "timeout"
                continue

            record = _run_step(step, env, budget_left)
            result["steps"].append(record)

            if record["status"] != "success" and not step.get("allow_failure", False):
                # 步驟之間是有序且相依的（安裝 → 產生 → 測試）。第一步失敗
                # 之後還硬跑下去，只會製造一堆掩蓋真正成因的雜訊失敗。
                overall = record["status"]
                break

        result["status"] = overall
        return 0 if overall == "success" else 1

    except KeyboardInterrupt:
        result["status"] = "terminated"
        result["error"] = "收到 SIGTERM（多半是 controller 端逾時）"
        return 143  # 128 + SIGTERM
    except Exception as exc:  # noqa: BLE001 — 最後一道防線，必須留下結果
        result["status"] = "error"
        result["error"] = f"沙箱執行器發生未預期例外：{exc!r}"
        return 70  # EX_SOFTWARE
    finally:
        result["finished_at"] = _now()
        result["duration_sec"] = round(time.monotonic() - started, 3)
        try:
            result["artifacts"] = _collect_artifacts()
        except OSError as exc:
            result["error"] = f"{result.get('error') or ''} (產物列舉失敗：{exc})".strip()

        # 第二張快照放在 finally 裡，逾時與 SIGTERM 的路徑也照樣拍得到 ——
        # 「裝到一半被砍掉」正是最需要知道當時裝了什麼的情況。
        if before is not None:
            try:
                result["installed"] = _diff_snapshots(before, _snapshot())
            except Exception as exc:  # noqa: BLE001 — 快照壞掉不能蓋掉真正的結果
                print(f"sandbox: 套件快照失敗：{exc!r}", file=sys.stderr)

        # 這是整支程式最重要的一次寫入。先寫暫存檔再 os.replace，避免
        # controller 讀到寫到一半的 JSON — 與晉升流程用的是同一個原子性原則。
        try:
            tmp = RESULT_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, RESULT_FILE)
        except OSError as exc:
            print(f"sandbox: 寫入 {RESULT_FILE} 失敗：{exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
