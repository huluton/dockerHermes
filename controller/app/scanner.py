"""晉升前的技能程式碼靜態安全掃描 —— AST 解析加上禁用模式偵測。

**這個模組能提供什麼、不能提供什麼，必須說清楚。**

這是縱深防禦，不是安全邊界。任何以黑名單為基礎的 Python 原始碼分析都可以被
有決心的對手繞過 —— 動態屬性查表、字串拼接匯入、編碼過的 payload，手法無窮。
真正的隔離邊界是沙箱容器本身（卸光 capability、資源上限、任務結束即銷毀）。

這一層擋的是別的東西：**被誤導的 agent 產出意外危險的程式碼**。這是自我進化
系統裡遠遠更常見的失效模式 —— LLM 為了「清理暫存檔」寫出一段 ``shutil.rmtree``，
或是為了偵錯裝了個反向 shell。這類東西，黑名單擋得非常有效。

政策從 YAML 載入，這樣調整規則不必改程式、也不必重建映像。
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# 政策檔讀不到時的內建後備。刻意設得比預期使用的政策更嚴 —— 掃描器如果因為
# 設定檔遺失就默默放行一切，那是最糟的失敗方向。
_FALLBACK_POLICY: dict[str, Any] = {
    "denied_imports": [
        "ctypes", "marshal", "multiprocessing", "pickle", "pty",
        "shelve", "socket", "socketserver", "subprocess", "telnetlib",
    ],
    "denied_calls": [
        "eval", "exec", "compile", "__import__", "breakpoint",
        "os.system", "os.popen", "os.execv", "os.execve", "os.execvp",
        "os.spawnv", "os.spawnve", "os.fork", "os.forkpty",
        "shutil.rmtree", "importlib.import_module",
    ],
    "denied_attributes": [
        "__globals__", "__subclasses__", "__builtins__", "__code__",
        "__bases__", "__mro__", "__reduce__", "__reduce_ex__", "__class__",
    ],
    "denied_patterns": [
        {
            "name": "base64-decoded-exec",
            "regex": r"(?i)b(?:ase)?64[a-z_]*decode\s*\([^)]*\)\s*(?:\)|,)?\s*$",
            "hint": "解碼後的 payload 常被用來夾帶程式碼",
        },
        {
            "name": "reverse-shell",
            "regex": r"(?i)(?:/dev/tcp/|nc\s+-e|bash\s+-i\s*>&)",
            "hint": "反向 shell 的典型特徵",
        },
        {
            "name": "docker-socket",
            "regex": r"/var/run/docker\.sock",
            "hint": "技能程式碼絕對不該直接碰 Docker socket",
        },
        {
            "name": "credential-path",
            "regex": r"(?:\.ssh/id_|\.aws/credentials|/opt/data/\.env|\.docker/config\.json)",
            "hint": "疑似存取憑證檔案",
        },
    ],
    "max_file_bytes": 512 * 1024,
    "scanned_suffixes": [".py"],
}


@dataclass
class Finding:
    """單一項違規。"""

    rule: str          # denied-import | denied-call | denied-attribute | pattern | syntax | size
    detail: str
    path: str
    line: int | None = None
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "detail": self.detail,
            "path": self.path,
            "line": self.line,
            "hint": self.hint,
        }


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "findings": [f.as_dict() for f in self.findings],
        }


def load_policy(path: Path) -> dict[str, Any]:
    """載入掃描政策，讀不到就退回內建的嚴格後備。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("找不到政策檔 %s —— 使用內建的嚴格後備政策", path)
        return dict(_FALLBACK_POLICY)
    except (OSError, yaml.YAMLError) as exc:
        log.error("政策檔 %s 無法解析（%s）—— 使用內建的嚴格後備政策", path, exc)
        return dict(_FALLBACK_POLICY)

    if not isinstance(raw, dict):
        log.error("政策檔 %s 的最外層不是對映 —— 使用後備政策", path)
        return dict(_FALLBACK_POLICY)

    # 與後備政策合併，而不是取代。這樣政策檔漏寫某個鍵時，補上的是嚴格的
    # 預設值，而不是「沒有規則」。
    merged = dict(_FALLBACK_POLICY)
    merged.update(raw)
    return merged


def _dotted_name(node: ast.AST) -> str | None:
    """把 ``os.path.join`` 這種 Attribute/Name 串還原成點分字串。"""
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, policy: dict[str, Any], path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        self._denied_imports = {str(m) for m in policy.get("denied_imports", [])}
        self._denied_calls = {str(c) for c in policy.get("denied_calls", [])}
        self._denied_attrs = {str(a) for a in policy.get("denied_attributes", [])}
        # 從被禁的點分呼叫反推出「尾端名稱」，用來抓 `from os import system`
        # 之後直接呼叫 `system(...)` 的迂迴寫法。
        self._denied_bare = {c.rsplit(".", 1)[-1] for c in self._denied_calls if "." in c}

    def _add(self, rule: str, detail: str, node: ast.AST, hint: str | None = None) -> None:
        self.findings.append(
            Finding(
                rule=rule,
                detail=detail,
                path=self.path,
                line=getattr(node, "lineno", None),
                hint=hint,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in self._denied_imports or alias.name in self._denied_imports:
                self._add("denied-import", f"import {alias.name}", node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        root = module.split(".")[0]
        if root in self._denied_imports or module in self._denied_imports:
            self._add("denied-import", f"from {module} import ...", node)
        else:
            # `from os import system` —— 模組本身沒問題，但匯入進來的名字有問題。
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                if full in self._denied_calls:
                    self._add(
                        "denied-import",
                        f"from {module} import {alias.name}",
                        node,
                        hint="被禁的呼叫，改以直接匯入名稱的方式繞過",
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        dotted = _dotted_name(node.func)
        if dotted and dotted in self._denied_calls:
            self._add("denied-call", f"呼叫 {dotted}()", node)
        elif isinstance(node.func, ast.Name):
            name = node.func.id
            if name in self._denied_calls:
                self._add("denied-call", f"呼叫 {name}()", node)
            elif name in self._denied_bare:
                self._add(
                    "denied-call",
                    f"呼叫 {name}()",
                    node,
                    hint="名稱與某個被禁的點分呼叫相符（可能是先 import 進來的）",
                )

        # getattr(obj, <非字面值>) 是「用字串繞過黑名單」的經典手法。
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                self._add(
                    "denied-call",
                    "getattr() 的屬性名稱不是字面常數",
                    node,
                    hint="動態屬性查表會讓靜態分析失效",
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr in self._denied_attrs:
            self._add("denied-attribute", f"存取 .{node.attr}", node)
        self.generic_visit(node)


def scan_source(source: str, path: str, policy: dict[str, Any]) -> list[Finding]:
    """掃描單一 Python 原始碼字串。"""
    findings: list[Finding] = []

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        # 語法錯誤就是硬性拒絕。無法解析的檔案也就無法審查，而一個
        # 「解析不了」的技能對 runtime 來說本來就沒有用處。
        return [
            Finding(
                rule="syntax",
                detail=f"語法錯誤：{exc.msg}",
                path=path,
                line=exc.lineno,
            )
        ]
    except (ValueError, RecursionError) as exc:
        # 深度巢狀的字面值可以讓 ast.parse 遞迴爆掉。這本身就足以構成拒絕。
        return [Finding(rule="syntax", detail=f"無法解析：{exc}", path=path)]

    visitor = _Visitor(policy, path)
    visitor.visit(tree)
    findings.extend(visitor.findings)

    # AST 之外的樣式比對。抓的是 AST 看不見的東西 —— 字串內容、註解、
    # 以及 AST 節點層級無法表達的結構特徵。
    for rule in policy.get("denied_patterns", []):
        if not isinstance(rule, dict):
            continue
        regex = rule.get("regex")
        if not regex:
            continue
        try:
            compiled = re.compile(regex, re.MULTILINE)
        except re.error as exc:
            log.error("政策樣式 %r 不是合法正規表示式：%s", rule.get("name"), exc)
            continue
        for match in compiled.finditer(source):
            findings.append(
                Finding(
                    rule="pattern",
                    detail=f"符合樣式 {rule.get('name', regex)!r}：{match.group(0)[:120]}",
                    path=path,
                    line=source.count("\n", 0, match.start()) + 1,
                    hint=rule.get("hint"),
                )
            )

    return findings


def scan_artifacts(artifacts: dict[str, bytes], policy: dict[str, Any]) -> ScanReport:
    """掃描沙箱回傳的整組候選技能檔案。"""
    report = ScanReport()
    max_bytes = int(policy.get("max_file_bytes", 512 * 1024))
    suffixes = {str(s) for s in policy.get("scanned_suffixes", [".py"])}

    for rel_path, payload in sorted(artifacts.items()):
        suffix = Path(rel_path).suffix
        if suffix not in suffixes:
            # 非程式碼檔（SKILL.md、JSON 設定、測試資料）不做 AST 掃描。
            # 晉升階段仍然會驗證路徑，所以它們沒辦法逃出技能目錄。
            report.files_skipped.append(rel_path)
            continue

        if len(payload) > max_bytes:
            report.findings.append(
                Finding(
                    rule="size",
                    detail=f"檔案 {len(payload)} 位元組超過 {max_bytes} 上限",
                    path=rel_path,
                    hint="超大原始檔通常是塞了資料，而不是真的程式碼",
                )
            )
            continue

        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError:
            report.findings.append(
                Finding(
                    rule="encoding",
                    detail="檔案不是合法的 UTF-8",
                    path=rel_path,
                    hint="Python 原始檔應為 UTF-8；二進位內容偽裝成 .py 很可疑",
                )
            )
            continue

        report.files_scanned += 1
        report.findings.extend(scan_source(source, rel_path, policy))

    return report
