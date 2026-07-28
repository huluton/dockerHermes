#!/usr/bin/env bash
#
# 對執行中的 stack 跑一遍安全與拓撲檢查。
#
#     make verify              # 不會改動任何狀態的檢查
#     make verify FULL=1       # 額外跑一次真正的進化任務（會建立沙箱容器）
#
# 這份腳本檢查的是「架構有沒有真的照設計落地」，而不是「程式碼有沒有
# 語法錯誤」。單元測試（make test）管後者。
#
# 刻意不用 set -e —— 一次跑完所有檢查再一起報告，比在第一個失敗就中斷有用。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PASS=0
FAIL=0
WARN=0

green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }
amber() { printf '\033[33m%s\033[0m' "$1"; }

ok()   { PASS=$((PASS + 1)); printf '  %s %s\n' "$(green '✓')" "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  %s %s\n' "$(red '✗')" "$1"; [ $# -gt 1 ] && printf '      %s\n' "$2"; }
soft() { WARN=$((WARN + 1)); printf '  %s %s\n' "$(amber '!')" "$1"; [ $# -gt 1 ] && printf '      %s\n' "$2"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

envget() { sed -n "s/^$1=//p" .env 2>/dev/null | head -1; }

# ---------------------------------------------------------------------------
# 前置
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
    printf '%s 找不到 .env。先執行 cp .env.example .env 並填入密碼。\n' "$(red '✗')"
    exit 1
fi

INSTANCE="$(envget INSTANCE)"; INSTANCE="${INSTANCE:-default}"
DASH_PORT="$(envget DASHBOARD_PORT)"; DASH_PORT="${DASH_PORT:-9119}"
DASH_USER="$(envget HERMES_DASHBOARD_BASIC_AUTH_USERNAME)"; DASH_USER="${DASH_USER:-admin}"
DASH_PASS="$(envget HERMES_DASHBOARD_BASIC_AUTH_PASSWORD)"
WORKSPACE="$(envget WORKSPACE_DIR)"; WORKSPACE="${WORKSPACE:-./workspace}"
FULL="${FULL:-0}"

COMPOSE="docker compose"
RUNTIME="hermes-runtime"
CONTROLLER="hermes-controller"

# 在 runtime / controller 容器裡執行一段 shell。
in_runtime()    { $COMPOSE exec -T "$RUNTIME" sh -c "$1" 2>&1; }
in_controller() { $COMPOSE exec -T "$CONTROLLER" sh -c "$1" 2>&1; }

# 從 controller 打 socket-proxy，回傳 HTTP 狀態碼。
proxy_code() {
    in_controller "curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        http://docker-socket-proxy:2375$1" | tr -d '\r\n '
}

printf '\033[1mHermes stack 驗證 —— 實例 %s\033[0m\n' "$INSTANCE"

# ---------------------------------------------------------------------------
head_ "1. Compose 設定與容器狀態"
# ---------------------------------------------------------------------------
if $COMPOSE config -q 2>/dev/null; then
    ok "docker compose config 解析成功"
else
    bad "docker compose config 解析失敗" "$($COMPOSE config -q 2>&1 | head -3)"
fi

for svc in "$RUNTIME" "$CONTROLLER" docker-socket-proxy; do
    state="$($COMPOSE ps --format '{{.State}}' "$svc" 2>/dev/null | head -1)"
    case "$state" in
        running) ok "$svc 執行中" ;;
        "")      bad "$svc 沒有在執行" "先執行 make up" ;;
        *)       bad "$svc 狀態是 $state" ;;
    esac
done

health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "hermes-${INSTANCE}-runtime" 2>/dev/null)"
case "$health" in
    healthy)  ok "runtime healthcheck 通過" ;;
    starting) soft "runtime 還在 starting" "剛啟動的話等 30 秒再跑一次" ;;
    none)     soft "runtime 沒有 healthcheck" ;;
    *)        bad "runtime healthcheck 是 $health" ;;
esac

# 沒有 runtime 就沒必要往下跑了。
if [ "$($COMPOSE ps --format '{{.State}}' "$RUNTIME" 2>/dev/null | head -1)" != "running" ]; then
    printf '\n%s stack 沒有在跑，後續檢查全部跳過。\n' "$(red '✗')"
    exit 1
fi

# ---------------------------------------------------------------------------
head_ "2. Dashboard 與遠端存取"
# ---------------------------------------------------------------------------
# /api/status 是上游 PUBLIC_API_PATHS 裡標為 liveness probe 的端點，未驗證
# 也回得了 200。不要換成 /api/health —— v2026.7.20 沒有那個路由。
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "http://127.0.0.1:${DASH_PORT}/api/status" 2>/dev/null)"
if [ "$code" = "200" ]; then
    ok "Dashboard 在宿主機 127.0.0.1:${DASH_PORT} 上可達"
else
    bad "GET /api/status 回 $code（預期 200）" "檢查 docker compose logs hermes-runtime"
fi

status_json="$(curl -s --max-time 10 "http://127.0.0.1:${DASH_PORT}/api/status" 2>/dev/null)"
if printf '%s' "$status_json" | grep -q '"auth_required"[[:space:]]*:[[:space:]]*true'; then
    ok "Dashboard 回報 auth_required=true（非 loopback 綁定強制驗證）"
elif printf '%s' "$status_json" | grep -q '"auth_required"'; then
    bad "Dashboard 回報 auth_required=false" \
        "綁在 0.0.0.0 卻沒有 auth provider —— 檢查 .env 的密碼有沒有真的傳進容器"
else
    soft "解析不出 /api/status 的 auth_required" "回應：$(printf '%s' "$status_json" | head -c 120)"
fi

if printf '%s' "$status_json" | grep -q '"auth_providers":\[[^]]*"basic"'; then
    ok "auth provider 是 basic（密碼有傳進容器）"
else
    bad "status 裡沒有 basic auth provider" \
        "$(printf '%s' "$status_json" | sed -n 's/.*\("auth_providers":\[[^]]*\]\).*/\1/p')"
fi

# 登入不是 HTTP basic auth。dashboard 用的是 cookie session：POST 憑證到
# /auth/password-login 換一組 session cookie，未登入的頁面請求會 302 導到
# /login。所以 `curl -u` 一定失敗，那不是設定錯誤。
if [ -n "$DASH_PASS" ]; then
    jar="$(mktemp)"
    body="$(mktemp)"
    login_code="$(curl -s -o "$body" -w '%{http_code}' --max-time 10 -c "$jar" \
        -X POST "http://127.0.0.1:${DASH_PORT}/auth/password-login" \
        -H 'Content-Type: application/json' \
        --data-binary "{\"provider\":\"basic\",\"username\":\"${DASH_USER}\",\"password\":\"${DASH_PASS}\"}" \
        2>/dev/null)"
    bad_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        -X POST "http://127.0.0.1:${DASH_PORT}/auth/password-login" \
        -H 'Content-Type: application/json' \
        --data-binary "{\"provider\":\"basic\",\"username\":\"${DASH_USER}\",\"password\":\"definitely-not-it\"}" \
        2>/dev/null)"

    if [ "$login_code" = "200" ] && grep -q '"ok":[[:space:]]*true' "$body"; then
        ok "用 .env 裡的密碼登入成功"
    else
        bad "登入回 $login_code" "$(head -c 160 "$body")"
    fi

    case "$bad_code" in
        401) ok "錯誤密碼被拒（401）" ;;
        429) soft "錯誤密碼回 429（觸發登入速率限制）" "重跑前先等一下" ;;
        *)   bad "錯誤密碼回 $bad_code（預期 401）" "dashboard 的驗證沒有生效" ;;
    esac

    page_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -b "$jar" \
        "http://127.0.0.1:${DASH_PORT}/" 2>/dev/null)"
    anon_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        "http://127.0.0.1:${DASH_PORT}/" 2>/dev/null)"
    if [ "$page_code" = "200" ] && [ "$anon_code" = "302" ]; then
        ok "帶 session cookie 可讀 dashboard（200），未登入被導向登入頁（302）"
    else
        bad "已登入=$page_code、未登入=$anon_code（預期 200 / 302）"
    fi
    rm -f "$jar" "$body"
else
    soft "跳過登入檢查" ".env 裡沒有 HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
fi

# ---------------------------------------------------------------------------
head_ "3. 正式環境穩定：技能對 Runtime 唯讀"
# ---------------------------------------------------------------------------
out="$(in_runtime 'touch /opt/data/skills/evolved/.verify-probe 2>&1; echo "rc=$?"')"
if printf '%s' "$out" | grep -qi 'read-only file system'; then
    ok "runtime 寫不進 /opt/data/skills/evolved（Read-only file system）"
elif printf '%s' "$out" | grep -q 'rc=0'; then
    bad "runtime 竟然寫得進技能目錄" "compose 的 hermes-skills 掛載少了 :ro"
    in_runtime 'rm -f /opt/data/skills/evolved/.verify-probe' >/dev/null
else
    soft "技能目錄寫入被擋，但錯誤訊息不是預期的" "$(printf '%s' "$out" | head -1)"
fi

out="$(in_runtime 'touch /opt/data/skill-versions/.verify-probe 2>&1; echo "rc=$?"')"
if printf '%s' "$out" | grep -qi 'read-only file system'; then
    ok "runtime 寫不進 /opt/data/skill-versions"
else
    bad "版本庫對 runtime 可寫" "compose 的 hermes-versions 掛載少了 :ro"
fi

# runtime 自身的狀態目錄必須可寫，否則 hermes 根本起不來。
out="$(in_runtime 'touch /opt/data/.verify-probe && rm -f /opt/data/.verify-probe && echo WRITABLE')"
if printf '%s' "$out" | grep -q WRITABLE; then
    ok "/opt/data 本身可寫（SQLite、sessions、memory 需要）"
else
    bad "/opt/data 不可寫" "hermes 需要這個目錄存放自身狀態"
fi

out="$(in_controller 'touch /opt/data/skills/evolved/.verify-probe \
    && rm -f /opt/data/skills/evolved/.verify-probe && echo WRITABLE')"
if printf '%s' "$out" | grep -q WRITABLE; then
    ok "controller 寫得進技能目錄（晉升需要）"
else
    bad "controller 寫不進技能目錄" "$(printf '%s' "$out" | head -1)"
fi

# GCP 憑證掛載。runtime 只能讀；controller 連掛都沒掛 —— 它動態建立的 sandbox
# 跑的是還沒通過審查的進化產物，憑證離那條路徑愈遠愈好。
out="$(in_runtime 'touch /opt/gcp/.verify-probe 2>&1; echo "rc=$?"')"
if printf '%s' "$out" | grep -qi 'read-only file system'; then
    ok "runtime 寫不進 /opt/gcp（GCP 憑證唯讀）"
elif printf '%s' "$out" | grep -q 'rc=0'; then
    bad "runtime 寫得進 /opt/gcp" "compose 的 GCP_CREDS_DIR 掛載少了 :ro"
    in_runtime 'rm -f /opt/gcp/.verify-probe' >/dev/null
else
    soft "/opt/gcp 寫入被擋，但錯誤訊息不是預期的" "$(printf '%s' "$out" | head -1)"
fi

out="$(in_controller 'test -d /opt/gcp && echo PRESENT || echo ABSENT')"
if printf '%s' "$out" | grep -q ABSENT; then
    ok "controller 沒有掛到 GCP 憑證"
else
    bad "controller 掛到了 /opt/gcp" "憑證不該出現在建立 sandbox 的那個容器裡"
fi

# ---------------------------------------------------------------------------
head_ "4. 兩卷佈局：版本庫在技能掃描樹之外"
# ---------------------------------------------------------------------------
# 版本庫如果落在 /opt/data/skills 底下，上游的 os.walk(followlinks=True) 會把
# 每一個保留的歷史版本都當成一個獨立的線上技能。
for c in "$RUNTIME:in_runtime" "$CONTROLLER:in_controller"; do
    name="${c%%:*}"; fn="${c##*:}"
    out="$($fn 'test -d /opt/data/skill-versions && echo EXISTS')"
    if printf '%s' "$out" | grep -q EXISTS; then
        ok "$name 的 /opt/data/skill-versions 存在且在掃描樹之外"
    else
        bad "$name 沒有 /opt/data/skill-versions" "symlink 會全部斷掉"
    fi
done

# UID 一致性：controller 寫出來的檔案，runtime 必須讀得到。
r_uid="$(in_runtime 'id -u hermes 2>/dev/null' | tr -d '\r\n ')"
c_uid="$(in_controller 'stat -c %u /opt/data/skills/evolved' | tr -d '\r\n ')"
if [ -n "$r_uid" ] && [ "$r_uid" = "$c_uid" ]; then
    ok "UID 對齊（runtime hermes=$r_uid、技能目錄擁有者=$c_uid）"
else
    soft "UID 可能不一致（runtime hermes=${r_uid:-?}、技能目錄=${c_uid:-?}）" \
        "不一致時 runtime 會讀不到晉升出來的技能。檢查 .env 的 HERMES_UID。"
fi

# ---------------------------------------------------------------------------
head_ "5. 最小特權：socket-proxy 白名單"
# ---------------------------------------------------------------------------
code="$(proxy_code /containers/json)"
if [ "$code" = "200" ]; then
    ok "GET /containers/json → 200（controller 管得了沙箱）"
else
    bad "GET /containers/json → $code（預期 200）" "controller 沒辦法管理沙箱"
fi

# 必須擋掉的 API。
for path in /networks /secrets /swarm /configs /nodes /services /volumes; do
    code="$(proxy_code "$path")"
    if [ "$code" = "403" ]; then
        ok "GET $path → 403（已封鎖）"
    else
        bad "GET $path → $code（預期 403）" "socket-proxy 的白名單有漏"
    fi
done

# controller 絕不能直接碰到 socket。
out="$(in_controller 'test -S /var/run/docker.sock && echo MOUNTED || echo ABSENT')"
if printf '%s' "$out" | grep -q ABSENT; then
    ok "controller 內沒有 /var/run/docker.sock"
else
    bad "controller 直接掛到了 docker.sock" "整個 socket-proxy 層形同虛設"
fi

# ---------------------------------------------------------------------------
head_ "6. 網路隔離：runtime 碰不到控制層"
# ---------------------------------------------------------------------------
out="$(in_runtime 'getent hosts docker-socket-proxy >/dev/null 2>&1 && echo RESOLVED || echo NXDOMAIN')"
if printf '%s' "$out" | grep -q NXDOMAIN; then
    ok "runtime 解析不到 docker-socket-proxy（不在 control net 上）"
else
    bad "runtime 解析得到 docker-socket-proxy" "runtime 被錯誤地加進了 docker-control-net"
fi

# internal: true 的意義：控制層沒有對外路由。
net_internal="$(docker network inspect -f '{{.Internal}}' "hermes-${INSTANCE}-control" 2>/dev/null)"
if [ "$net_internal" = "true" ]; then
    ok "docker-control-net 是 internal（無對外路由）"
else
    bad "docker-control-net 的 Internal=${net_internal:-?}" "預期 true"
fi

# ---------------------------------------------------------------------------
head_ "7. 外部 workspace"
# ---------------------------------------------------------------------------
token="verify-$$-$(date -u +%s)"
in_runtime "printf '%s' '$token' > /workspace/.verify-probe" >/dev/null
if [ -f "${WORKSPACE}/.verify-probe" ] && [ "$(cat "${WORKSPACE}/.verify-probe")" = "$token" ]; then
    ok "runtime 寫入 /workspace，宿主機的 ${WORKSPACE} 看得到"
    rm -f "${WORKSPACE}/.verify-probe"
else
    bad "workspace 沒有寫穿到宿主機" "檢查 .env 的 WORKSPACE_DIR 與 compose 的 bind mount"
fi

out="$(in_runtime 'echo $HERMES_WRITE_SAFE_ROOT')"
if printf '%s' "$out" | grep -q '/workspace'; then
    ok "HERMES_WRITE_SAFE_ROOT 含 /workspace（terminal 工具寫得進去）"
else
    bad "HERMES_WRITE_SAFE_ROOT 不含 /workspace" "目前值：$(printf '%s' "$out" | head -1)"
fi

# ---------------------------------------------------------------------------
head_ "8. Controller API 與沙箱衛生"
# ---------------------------------------------------------------------------
out="$(in_controller 'curl -sS --max-time 10 http://127.0.0.1:9200/healthz')"
if printf '%s' "$out" | grep -q '"ok"'; then
    ok "controller /healthz 正常"
else
    bad "controller /healthz 異常" "$(printf '%s' "$out" | head -1)"
fi

if docker image inspect "hermes-sandbox:${INSTANCE}" >/dev/null 2>&1; then
    ok "沙箱映像 hermes-sandbox:${INSTANCE} 存在"
else
    bad "找不到沙箱映像 hermes-sandbox:${INSTANCE}" "執行 make build"
fi

leftovers="$(docker ps -aq \
    --filter "label=hermes.instance=${INSTANCE}" \
    --filter label=hermes.role=sandbox 2>/dev/null | wc -l | tr -d ' ')"
if [ "$leftovers" = "0" ]; then
    ok "沒有殘留的沙箱容器"
else
    soft "有 $leftovers 個殘留的沙箱容器" "任務進行中屬正常；否則執行 make reap"
fi

# ---------------------------------------------------------------------------
head_ "9. 端到端進化（FULL=1 才跑）"
# ---------------------------------------------------------------------------
if [ "$FULL" != "1" ]; then
    printf '  （跳過。要跑真正的進化任務：make verify FULL=1）\n'
else
    req="examples/evolve.hello-skill.json"
    if [ ! -f "$req" ]; then
        bad "找不到 $req"
    else
        resp="$($COMPOSE exec -T "$CONTROLLER" \
            curl -sS --max-time 30 -X POST http://127.0.0.1:9200/evolve \
                -H 'Content-Type: application/json' --data-binary @- < "$req")"
        task_id="$(printf '%s' "$resp" | sed -n 's/.*"task_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
        if [ -z "$task_id" ]; then
            bad "提交進化任務失敗" "$(printf '%s' "$resp" | head -c 200)"
        else
            ok "已提交進化任務 $task_id"
            # 用容器裡的 python 解 JSON，不要在宿主機端 sed 整個 body。
            # /tasks/{id} 的回應裡不只一個 "status" —— 巢狀的 spec 與沙箱結果
            # 各自也有。sed 的 .* 是貪婪比對，會抓到最後一個，於是任務明明是
            # succeeded 卻被讀成沙箱那層的 success。
            state=""
            for _ in $(seq 1 60); do
                sleep 5
                state="$(in_controller "python -c \"
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:9200/tasks/$task_id', timeout=10) as r:
    print(json.load(r)['status'])
\"" 2>/dev/null | tr -d '\r')"
                case "$state" in succeeded|failed|rejected|error|timeout) break ;; esac
            done
            case "$state" in
                succeeded) ok "任務完成（status=succeeded）" ;;
                "")        bad "查不到任務狀態" ;;
                *)         bad "任務結束於 status=$state" "make task ID=$task_id 看細節" ;;
            esac

            if in_runtime 'test -e /opt/data/skills/evolved/hello-skill/SKILL.md && echo FOUND' \
                | grep -q FOUND; then
                ok "runtime 看得到晉升上線的技能 hello-skill"
            else
                bad "runtime 看不到 hello-skill" "symlink 斷了，或 UID 不對"
            fi

            # --- 反向案例：掃描器必須擋下惡意產物 -------------------
            # 只驗證「好的會過」是不夠的 —— 一個永遠回 clean 的掃描器也能
            # 讓上面那幾項全綠。這一段提交一個一定該被拒的技能。
            evil="examples/evolve.rejected-skill.json"
            if [ ! -f "$evil" ]; then
                soft "找不到 $evil" "跳過掃描器的反向驗證"
            else
                resp="$($COMPOSE exec -T "$CONTROLLER" \
                    curl -sS --max-time 30 -X POST http://127.0.0.1:9200/evolve \
                        -H 'Content-Type: application/json' --data-binary @- < "$evil")"
                evil_id="$(printf '%s' "$resp" | sed -n 's/.*"task_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
                if [ -z "$evil_id" ]; then
                    bad "提交惡意範例失敗" "$(printf '%s' "$resp" | head -c 200)"
                else
                    evil_state=""; evil_phase=""
                    for _ in $(seq 1 60); do
                        sleep 5
                        evil_state="$(in_controller "python -c \"
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:9200/tasks/$evil_id', timeout=10) as r:
    d = json.load(r)
print(d['status'], d.get('phase'))
\"" 2>/dev/null | tr -d '\r')"
                        evil_phase="${evil_state#* }"
                        evil_state="${evil_state%% *}"
                        case "$evil_state" in succeeded|failed|rejected|error|timeout) break ;; esac
                    done
                    if [ "$evil_state" = "failed" ] && [ "$evil_phase" = "scan" ]; then
                        ok "掃描器擋下惡意技能（status=failed、phase=scan）"
                    else
                        bad "惡意技能結束於 status=$evil_state、phase=$evil_phase（預期 failed/scan）" \
                            "make task ID=$evil_id 看細節；SCANNER_ENFORCE 是不是被關掉了？"
                    fi
                fi

                if in_runtime 'test -e /opt/data/skills/evolved/rejected-skill && echo LEAKED' \
                    | grep -q LEAKED; then
                    bad "被拒絕的技能仍然出現在線上目錄" "晉升在掃描失敗後還是執行了"
                else
                    ok "被拒絕的技能沒有進到線上目錄"
                fi
            fi

            sleep 5
            n="$(docker ps -aq --filter "label=hermes.instance=${INSTANCE}" \
                --filter label=hermes.role=sandbox 2>/dev/null | wc -l | tr -d ' ')"
            if [ "$n" = "0" ]; then
                ok "任務結束後沙箱已被強制移除"
            else
                bad "還剩 $n 個沙箱容器" "生命週期的清理沒有執行到"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m結果：\033[0m %s 通過、%s 失敗、%s 警告\n' \
    "$(green "$PASS")" "$(red "$FAIL")" "$(amber "$WARN")"

if [ "$FAIL" -gt 0 ]; then
    printf '%s 有檢查沒過。上面每一項都附了對應的檢查方向。\n' "$(red '✗')"
    exit 1
fi
printf '%s 全部通過。\n' "$(green '✓')"
