#!/usr/bin/env bash
#
# 依賴晉升 —— 把沙箱裡驗過的套件併進 runtime 的建置清單。
#
#     scripts/deps.sh list                 列出待審清單
#     scripts/deps.sh show  <task-id>      看某一份的完整內容
#     scripts/deps.sh accept <task-id>     併進 runtime/deps/*.txt
#     scripts/deps.sh reject <task-id>     否決（檔案移到 rejected/ 保留）
#
# 由 Makefile 的 deps-* 目標呼叫，也可以直接跑。
#
# --- 為什麼這一段跑在宿主機上，而不是 controller 裡 ------------------------
#
# controller 已經能建容器、能把程式碼晉升到線上技能目錄。再讓它決定「正式映像
# 裝什麼」，自我進化的迴圈就完全閉合了 —— 一個被誤導的 agent 可以自己把套件
# 寫進下一次 build。所以那一步刻意留在這裡：檔案由你在宿主機上寫、由 git 記錄、
# 由你決定什麼時候 make build。
#
# 這支腳本只用 docker + coreutils，不需要 jq —— 解析 JSON 的部分借 controller
# 映像裡的 python 做，而且只讓它輸出「kind<TAB>name<TAB>version」這種扁平文字，
# 寫檔的動作全部在宿主機這一側。這樣產生的檔案屬於你，不是 root。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APT_FILE="${REPO_ROOT}/runtime/deps/apt.txt"
PIP_FILE="${REPO_ROOT}/runtime/deps/pip.txt"

INSTANCE="$(sed -n 's/^INSTANCE=//p' "${REPO_ROOT}/.env" 2>/dev/null | head -1)"
INSTANCE="${INSTANCE:-default}"
DEPS_VOLUME="hermes-${INSTANCE}-deps"
CONTROLLER_IMAGE="hermes-controller:${INSTANCE}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
[ -t 1 ] || { RED=""; GREEN=""; YELLOW=""; DIM=""; RESET=""; }

die() { echo "${RED}錯誤：${RESET}$*" >&2; exit 1; }

# 在一個一次性容器裡跑 python，把 deps 卷唯讀掛進去。
#
# 唯讀是刻意的：這支腳本只讀清單，「把 pending 移到 accepted」那一步走
# controller 的 HTTP API（見 mark_resolved），因為那是 controller 自己的狀態，
# 讓兩邊都能寫同一個目錄只會製造出誰覆蓋誰的問題。
deps_python() {
    docker image inspect "${CONTROLLER_IMAGE}" >/dev/null 2>&1 \
        || die "找不到映像 ${CONTROLLER_IMAGE}，先執行 make build。"
    docker run --rm -i \
        -v "${DEPS_VOLUME}:/deps:ro" \
        --entrypoint python3 "${CONTROLLER_IMAGE}" -
}

volume_exists() {
    docker volume inspect "${DEPS_VOLUME}" >/dev/null 2>&1
}

# --- list -------------------------------------------------------------------

cmd_list() {
    volume_exists || { echo "還沒有 ${DEPS_VOLUME} 這個卷 —— 這套 stack 從來沒跑過進化任務。"; return 0; }
    deps_python <<'PY'
import json, pathlib, sys

pending = sorted(pathlib.Path("/deps/pending").glob("*.json"), reverse=True)
if not pending:
    print("沒有待審的依賴清單。")
    sys.exit(0)

print(f"{len(pending)} 份待審清單：\n")
for path in pending:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  {path.name}  （讀不了：{exc}）")
        continue
    pkgs = d.get("packages", {})
    apt, pip = pkgs.get("apt", []), pkgs.get("pip", [])
    flag = " [特權沙箱]" if d.get("privileged") else ""
    print(f"  {d.get('task_id')}  技能={d.get('skill')}  {d.get('recorded_at', '')}{flag}")
    print(f"      apt {len(apt)} 個、pip {len(pip)} 個")
    preview = [p["name"] for p in (apt + pip)[:8]]
    if preview:
        more = "…" if len(apt) + len(pip) > 8 else ""
        print(f"      {', '.join(preview)}{more}")
    if d.get("rejected"):
        print(f"      ⚠ 有 {len(d['rejected'])} 項被消毒規則丟掉，用 show 看細節")
    print()
print("看細節：make deps-show TASK=<task-id>")
print("接受：  make deps-accept TASK=<task-id>")
PY
}

# --- show -------------------------------------------------------------------

cmd_show() {
    local task="$1"
    volume_exists || die "還沒有 ${DEPS_VOLUME} 這個卷。"
    docker run --rm -i \
        -v "${DEPS_VOLUME}:/deps:ro" \
        -e "TASK_ID=${task}" \
        --entrypoint python3 "${CONTROLLER_IMAGE}" - <<'PY'
import json, os, pathlib, sys

task = os.environ["TASK_ID"]
path = pathlib.Path("/deps/pending") / f"{task}.json"
if not path.is_file():
    print(f"找不到 {task} 的待審清單。", file=sys.stderr)
    sys.exit(1)
print(path.read_text(encoding="utf-8"))
PY
}

# --- accept -----------------------------------------------------------------

# 把清單攤平成「kind<TAB>name<TAB>version」，寫檔的邏輯留在宿主機上。
emit_flat() {
    local task="$1"
    docker run --rm -i \
        -v "${DEPS_VOLUME}:/deps:ro" \
        -e "TASK_ID=${task}" \
        --entrypoint python3 "${CONTROLLER_IMAGE}" - <<'PY'
import json, os, pathlib, re, sys

task = os.environ["TASK_ID"]
path = pathlib.Path("/deps/pending") / f"{task}.json"
if not path.is_file():
    print(f"找不到 {task} 的待審清單。", file=sys.stderr)
    sys.exit(1)

data = json.loads(path.read_text(encoding="utf-8"))
name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
ver_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:~-]{0,127}$")

# controller 寫檔前已經消毒過一次。這裡再驗一次是刻意的重複 —— 這份 JSON
# 存在一個卷裡，中間隔了一段人為可及的時間，而它的內容馬上要被組成
# `apt-get install` 的參數。重複的成本是十行程式碼。
for kind in ("apt", "pip"):
    for entry in data.get("packages", {}).get(kind, []):
        name, version = entry.get("name", ""), entry.get("version", "")
        if not name_re.match(name) or not ver_re.match(version):
            print(f"清單裡有不合格的項目：{kind} {name!r} {version!r}", file=sys.stderr)
            sys.exit(2)
        print(f"{kind}\t{name}\t{version}")
PY
}

cmd_accept() {
    local task="$1"
    volume_exists || die "還沒有 ${DEPS_VOLUME} 這個卷。"

    local flat
    flat="$(emit_flat "${task}")" || die "讀不到 ${task} 的清單。"
    [ -n "${flat}" ] || die "${task} 的清單是空的，沒有東西可以併。"

    # --- 撞名護欄 ---------------------------------------------------------
    # pip 套件會經由 PYTHONPATH 掛在 /opt/hermes/.venv 的 site-packages
    # 「前面」，所以只要名稱撞到上游已經有的東西，hermes 自己就會 import 到
    # 我們裝的那一份。這是最難查的一種故障：一切照常啟動，然後某個角落的行為
    # 悄悄變了。寧可在這裡擋下來。
    local venv_pkgs=""
    if docker image inspect "hermes-runtime:${INSTANCE}" >/dev/null 2>&1; then
        # dist-info 目錄名是 `<正規化過的名稱>-<版本>.dist-info`。名稱正規化
        # 的規則（PEP 503/427）會把 . _ - 統一成 -，並轉成小寫，所以兩邊都做
        # 同一套處理才比得出來。
        venv_pkgs="$(docker run --rm --entrypoint sh "hermes-runtime:${INSTANCE}" -c \
            'ls /opt/hermes/.venv/lib/python*/site-packages 2>/dev/null \
             | sed -n "s/\(.*\)-[0-9][^-]*\.dist-info$/\1/p" \
             | tr "[:upper:]_." "[:lower:]--" | sort -u' \
            2>/dev/null || true)"
    else
        echo "${YELLOW}提醒：${RESET}找不到 hermes-runtime:${INSTANCE}，跳過與上游 venv 的撞名檢查。"
    fi

    local clashes="" norm
    while IFS=$'\t' read -r kind name version; do
        [ "${kind}" = "pip" ] || continue
        norm="$(printf '%s' "${name}" | tr '[:upper:]_.' '[:lower:]--')"
        if printf '%s\n' "${venv_pkgs}" | grep -qxF "${norm}"; then
            clashes+="  ${name} (${version})"$'\n'
        fi
    done <<< "${flat}"

    if [ -n "${clashes}" ]; then
        echo "${RED}拒絕併入：${RESET}這些 pip 套件在上游的 venv 裡已經有了 ——"
        printf '%s' "${clashes}"
        echo
        echo "PYTHONPATH 的優先序比 venv 的 site-packages 高，併進去會把 hermes"
        echo "自己用的那一份蓋掉。要嘛從清單裡拿掉它們（make deps-reject 之後手動"
        echo "編輯 runtime/deps/pip.txt 只加需要的），要嘛確認上游那個版本本來就夠用。"
        exit 1
    fi

    # --- 併入 --------------------------------------------------------------
    #
    # 「已經在清單裡了嗎」用 awk 逐行比對，不用 grep 的正則 —— 套件名裡的
    # + . 是正則的元字元（libstdc++6、ca-certificates 都中招），組進 pattern
    # 會比對到錯的東西，或是完全比不到而重複寫入。
    local added_apt=0 added_pip=0 skipped=0
    while IFS=$'\t' read -r kind name version; do
        case "${kind}" in
        apt)
            # apt 不釘版本（Debian mirror 會清掉舊版，釘死會讓 build 在幾週後
            # 憑空壞掉），把沙箱驗到的版本寫成同行註解留存。
            if awk -v n="${name}" '
                    { sub(/#.*/, ""); gsub(/^[ \t]+|[ \t]+$/, "") }
                    $1 == n { found = 1 }
                    END { exit !found }' "${APT_FILE}"; then
                skipped=$((skipped + 1))
            else
                printf '%-32s # 沙箱驗過 %s（%s）\n' "${name}" "${version}" "${task}" >> "${APT_FILE}"
                added_apt=$((added_apt + 1))
            fi
            ;;
        pip)
            # requirements 行可能寫成 name==1.2、name>=1.2、name 三種，統一切到
            # 第一個版本運算子之前再比。
            if awk -v n="${name}" '
                    { sub(/#.*/, ""); gsub(/[ \t]/, "") }
                    { split($0, a, /[=<>~!\[]/); if (tolower(a[1]) == tolower(n)) found = 1 }
                    END { exit !found }' "${PIP_FILE}"; then
                skipped=$((skipped + 1))
            else
                printf '%s==%s  # %s\n' "${name}" "${version}" "${task}" >> "${PIP_FILE}"
                added_pip=$((added_pip + 1))
            fi
            ;;
        esac
    done <<< "${flat}"

    echo "${GREEN}已併入：${RESET}apt ${added_apt} 個、pip ${added_pip} 個（略過已存在的 ${skipped} 個）"

    mark_resolved "${task}" accepted \
        || echo "${YELLOW}提醒：${RESET}清單沒能從 pending/ 移走（controller 可能沒在跑），下次 make deps-list 還會看到它。runtime/deps/*.txt 已經改好了，重跑一次 accept 只會被去重擋掉，不會重複寫入。"

    echo
    echo "接下來 —— ${DIM}這三步是刻意分開的，每一步你都還能反悔${RESET}"
    echo "  1. git diff runtime/deps/        # 看清楚正式映像要多裝什麼"
    echo "  2. git add -A runtime/deps && git commit -m 'deps: ${task}'"
    echo "  3. make build && make up         # 或 make update"
    echo "  4. make verify                   # 第 9 節會確認套件真的裝進去了"
}

# --- reject -----------------------------------------------------------------

# pending/ 是 controller 的狀態，所以移動檔案這件事透過它的 API 做，
# 而不是由這支腳本直接動那個卷（兩邊都能寫同一個目錄只會製造出誰覆蓋誰的
# 問題）。accepted/ 與 rejected/ 都保留檔案，不刪除 —— 「誰在什麼時候決定
# runtime 要裝什麼」屬於稽核軌跡。
mark_resolved() {
    local task="$1" outcome="$2"
    (cd "${REPO_ROOT}" && docker compose exec -T hermes-controller \
        curl -fsS -X POST \
        "http://127.0.0.1:9200/deps/pending/${task}/resolve?outcome=${outcome}") \
        >/dev/null 2>&1
}

cmd_reject() {
    local task="$1"
    if mark_resolved "${task}" rejected; then
        echo "${GREEN}已否決${RESET} ${task}（檔案移到 rejected/ 保留，沒有刪除）"
    else
        die "否決失敗。controller 沒在跑，或找不到 ${task} 的待審清單。"
    fi
}

# --- 進入點 -----------------------------------------------------------------

case "${1:-}" in
list)   cmd_list ;;
show)   [ $# -ge 2 ] || die "用法：scripts/deps.sh show <task-id>";   cmd_show   "$2" ;;
accept) [ $# -ge 2 ] || die "用法：scripts/deps.sh accept <task-id>"; cmd_accept "$2" ;;
reject) [ $# -ge 2 ] || die "用法：scripts/deps.sh reject <task-id>"; cmd_reject "$2" ;;
*)
    cat >&2 <<EOF
用法：scripts/deps.sh <子命令>

  list                列出待審的依賴清單
  show   <task-id>    看某一份的完整 JSON
  accept <task-id>    併進 runtime/deps/*.txt（之後還要 git commit + make build）
  reject <task-id>    否決，檔案移到 rejected/ 保留

實例：${INSTANCE}    卷：${DEPS_VOLUME}
EOF
    exit 64  # EX_USAGE
    ;;
esac
