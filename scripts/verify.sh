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
MCP_PORT="$(envget MCP_FS_PORT)"; MCP_PORT="${MCP_PORT:-9300}"
MCP_PATH="$(envget MCP_FS_PATH)"; MCP_PATH="${MCP_PATH:-/mcp}"
FULL="${FULL:-0}"

COMPOSE="docker compose"
RUNTIME="hermes-runtime"
CONTROLLER="hermes-controller"
MCPFS="hermes-mcp-fs"

# 在 runtime / controller / mcp-fs 容器裡執行一段 shell。
in_runtime()    { $COMPOSE exec -T "$RUNTIME" sh -c "$1" 2>&1; }
in_controller() { $COMPOSE exec -T "$CONTROLLER" sh -c "$1" 2>&1; }
in_mcpfs()      { $COMPOSE exec -T "$MCPFS" sh -c "$1" 2>&1; }

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

for svc in "$RUNTIME" "$MCPFS" "$CONTROLLER" docker-socket-proxy; do
    state="$($COMPOSE ps --format '{{.State}}' "$svc" 2>/dev/null | head -1)"
    case "$state" in
        running) ok "$svc 執行中" ;;
        "")      bad "$svc 沒有在執行" "先執行 make up" ;;
        *)       bad "$svc 狀態是 $state" ;;
    esac
done

for c in runtime mcp-fs; do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "hermes-${INSTANCE}-${c}" 2>/dev/null)"
    case "$health" in
        healthy)  ok "$c healthcheck 通過" ;;
        starting) soft "$c 還在 starting" "剛啟動的話等 30 秒再跑一次" ;;
        none)     soft "$c 沒有 healthcheck" ;;
        *)        bad "$c healthcheck 是 ${health:-（查不到容器）}" ;;
    esac
done

# 沒有 runtime 就沒必要往下跑了。
if [ "$($COMPOSE ps --format '{{.State}}' "$RUNTIME" 2>/dev/null | head -1)" != "running" ]; then
    printf '\n%s stack 沒有在跑，後續檢查全部跳過。\n' "$(red '✗')"
    exit 1
fi

# ---------------------------------------------------------------------------
head_ "2. Dashboard 與遠端存取"
# ---------------------------------------------------------------------------
# 先確認 runtime 真的接在 hermes-net 上。這不是形式檢查：runtime 同時接兩個
# 網路，而 mcp-net 是 internal —— 如果 hermes-net 那一條沒接上（compose 中途
# 被打斷、或網路是別的專案留下來的殘骸），Docker 就無法發佈 9119，宿主機上
# 根本不會有 listener。症狀是 dashboard 完全連不上，但容器全部 healthy。
nets="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
    "hermes-${INSTANCE}-runtime" 2>/dev/null)"
if printf '%s' "$nets" | grep -qw "hermes-${INSTANCE}-net"; then
    ok "runtime 接在 hermes-net 上"
else
    bad "runtime 沒有接上 hermes-${INSTANCE}-net（目前只在：${nets:-（查不到）}）" \
        "只剩 internal 網路的話 9119 發佈不出去。修：docker network connect hermes-${INSTANCE}-net hermes-${INSTANCE}-runtime，或 docker compose up -d --force-recreate hermes-runtime"
fi

published="$(docker port "hermes-${INSTANCE}-runtime" "${DASH_PORT}/tcp" 2>/dev/null)"
if [ -n "$published" ]; then
    ok "9119 有發佈到宿主機（$published）"
else
    bad "runtime 的 ${DASH_PORT}/tcp 沒有對應的宿主機埠" \
        "docker ps 只會顯示 ${DASH_PORT}/tcp 而沒有 -> 箭號。多半是上面那條網路沒接上"
fi

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
head_ "7. 透過 MCP 操作外部資料夾（獨立容器）"
# ---------------------------------------------------------------------------
# 這一節要證明的是「容器邊界」，不只是「工具設定」：外部資料夾只存在於
# hermes-mcp-fs 裡，runtime 唯一的路徑是 mcp-net 上的 MCP over HTTP。

# 邊界本身。runtime 裡不該有這個掛載點 —— 沒有掛載點，terminal 也拿不到檔案。
out="$(in_runtime 'test -e /workspace && echo PRESENT || echo ABSENT')"
if printf '%s' "$out" | grep -q ABSENT; then
    ok "runtime 容器內沒有 /workspace（外部資料夾不在這個容器裡）"
else
    bad "runtime 容器內看得到 /workspace" \
        "compose 把外部資料夾掛回 runtime 了 —— 那樣 terminal 工具就繞得過 MCP"
fi

# mcp-fs 這一側：bind mount 真的通到宿主機。
token="verify-$$-$(date -u +%s)"
in_mcpfs "printf '%s' '$token' > /workspace/.verify-probe" >/dev/null
if [ -f "${WORKSPACE}/.verify-probe" ] && [ "$(cat "${WORKSPACE}/.verify-probe")" = "$token" ]; then
    ok "mcp-fs 的 /workspace 通到宿主機的 ${WORKSPACE}"
else
    bad "workspace 沒有寫穿到宿主機" "檢查 .env 的 WORKSPACE_DIR 與 mcp-fs 的 bind mount"
fi

# 網路拓撲：runtime 到得了，sandbox（與 controller 同在 hermes-net）到不了。
out="$(in_runtime 'getent hosts hermes-mcp-fs >/dev/null 2>&1 && echo RESOLVED || echo NXDOMAIN')"
if printf '%s' "$out" | grep -q RESOLVED; then
    ok "runtime 解析得到 hermes-mcp-fs（兩邊都在 mcp-net 上）"
else
    bad "runtime 解析不到 hermes-mcp-fs" "runtime 沒有加進 mcp-net —— MCP 工具會整組消失"
fi

out="$(in_controller 'getent hosts hermes-mcp-fs >/dev/null 2>&1 && echo RESOLVED || echo NXDOMAIN')"
if printf '%s' "$out" | grep -q NXDOMAIN; then
    ok "controller / sandbox 所在的網路解析不到 hermes-mcp-fs"
else
    bad "hermes-net 上解析得到 hermes-mcp-fs" \
        "還沒審查過的進化產物就能直接打 MCP 端點 —— 那個端點沒有身分驗證"
fi

net_internal="$(docker network inspect -f '{{.Internal}}' "hermes-${INSTANCE}-mcp" 2>/dev/null)"
if [ "$net_internal" = "true" ]; then
    ok "mcp-net 是 internal（掛著使用者資料夾的容器沒有對外路由）"
else
    bad "mcp-net 的 Internal=${net_internal:-?}" "預期 true"
fi

# 端到端：從 runtime 用 HTTP 講一次完整的 MCP —— initialize、讀探針檔、
# 再要求一個清單外的路徑。這證明的是 agent 真的走得通這條路，而不只是
# 「容器在跑」。
#
# 用 sh -s 餵整段腳本進去，而不是塞成一行 sh -c —— session id 要在多次
# curl 之間傳遞，那在單行字串裡會變成一團跳脫字元。
mcp_out="$($COMPOSE exec -T "$RUNTIME" sh -s \
    "http://hermes-mcp-fs:${MCP_PORT}${MCP_PATH}" <<'MCP_PROBE' 2>&1
url="$1"
ct='Content-Type: application/json'
acc='Accept: application/json, text/event-stream'
hdr=/tmp/.verify-mcp-hdr
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hermes-verify","version":"1"}}}'

printf 'INIT %s\n' "$(curl -s -m 20 -D "$hdr" -H "$ct" -H "$acc" -d "$init" "$url" | tr -d '\r\n')"
sid="$(tr -d '\r' < "$hdr" | sed -n 's/^[Mm][Cc][Pp]-[Ss]ession-[Ii][Dd]: *//p' | head -1)"
rm -f "$hdr"
[ -n "$sid" ] || { echo 'NOSESSION'; exit 0; }

call() {
    curl -s -m 20 -H "$ct" -H "$acc" -H "mcp-session-id: $sid" -d "$1" "$url" | tr -d '\r\n'
}
call '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
printf 'READ %s\n' "$(call '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_text_file","arguments":{"path":"/workspace/.verify-probe"}}}')"
printf 'DENY %s\n' "$(call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_text_file","arguments":{"path":"/etc/passwd"}}}')"
curl -s -m 10 -X DELETE -H "mcp-session-id: $sid" "$url" >/dev/null
MCP_PROBE
)"

init_line="$(printf '%s' "$mcp_out" | grep '^INIT ' | head -1)"
read_line="$(printf '%s' "$mcp_out" | grep '^READ ' | head -1)"
deny_line="$(printf '%s' "$mcp_out" | grep '^DENY ' | head -1)"

if printf '%s' "$init_line" | grep -q '"serverInfo"'; then
    ok "從 runtime 打 MCP over HTTP 握手成功（server 回了 serverInfo）"
elif printf '%s' "$mcp_out" | grep -q NOSESSION; then
    bad "MCP 握手沒拿到 session id" "$(printf '%s' "$init_line" | head -c 160)"
else
    bad "MCP 握手失敗" "$(printf '%s' "$mcp_out" | head -2 | head -c 200)"
fi

if printf '%s' "$read_line" | grep -q "$token"; then
    ok "MCP 讀得到外部資料夾的內容（read_text_file /workspace/.verify-probe）"
else
    bad "MCP 讀不到 /workspace/.verify-probe" "$(printf '%s' "$read_line" | head -c 200)"
fi
rm -f "${WORKSPACE}/.verify-probe"

# 授權邊界：server 只接受 MCP_FS_ROOTS 列出的根目錄。/etc/passwd 必須被拒。
if printf '%s' "$deny_line" | grep -q 'root:x:0:0'; then
    bad "MCP server 讀得到自己容器裡的 /etc/passwd" "MCP_FS_ROOTS 的限制沒有生效"
elif printf '%s' "$deny_line" | grep -qiE 'outside allowed|not allowed|access denied|"isError":true'; then
    ok "MCP server 拒絕允許清單外的路徑（/etc/passwd）"
else
    soft "分辨不出 MCP server 對 /etc/passwd 的回應" "$(printf '%s' "$deny_line" | head -c 200)"
fi

# hermes 這一側的前提：mcp 是「選用」相依套件，沒裝的話 MCP 探索會被靜默跳過
# （log 只留一行 "MCP SDK not available"），而 url: 這種遠端 server 還額外需要
# mcp.client.streamable_http。兩者缺一，agent 的工具清單就是空的 —— 而上面所有
# 檢查都還是會過，因為 server 本身沒問題。
out="$(in_runtime 'python3 -c "import mcp.client.streamable_http" >/dev/null 2>&1 && echo HAVE || echo MISSING')"
if printf '%s' "$out" | grep -q HAVE; then
    ok "runtime 的 hermes 有 MCP client SDK（含 streamable_http）"
else
    bad "runtime 裡 import 不到 mcp.client.streamable_http" \
        "hermes 的 MCP 探索會被靜默跳過，agent 完全看不到 MCP 工具。上游映像換版時要重新確認這個相依"
fi

# config.yaml 有沒有真的把 server 接起來。沒接的話 agent 那邊看不到 mcp__workspace_fs__* 工具。
out="$(in_runtime 'grep -A12 "^mcp_servers:" /opt/data/config.yaml 2>/dev/null')"
if printf '%s' "$out" | grep -qE '^[[:space:]]*url:'; then
    ok "config.yaml 的 mcp_servers 已指向 remote MCP server"
elif printf '%s' "$out" | grep -q 'mcp-server-filesystem'; then
    bad "config.yaml 還在用舊的 stdio 設定（command: .../mcp-server-filesystem）" \
        "那個 binary 已經不在 runtime 映像裡了。改成 url: http://hermes-mcp-fs:${MCP_PORT}${MCP_PATH}，範本見 examples/config.vllm.yaml"
else
    bad "config.yaml 裡沒有 mcp_servers 設定" \
        "跑 make seed-config（已有設定檔的話手動補上，範本見 examples/config.vllm.yaml）"
fi

# mcp-fs 的根檔案系統唯讀 —— 它唯一該寫的地方是掛進來的資料夾。
out="$(in_mcpfs 'touch /opt/mcp/.verify-probe 2>&1; echo "rc=$?"')"
if printf '%s' "$out" | grep -qi 'read-only file system'; then
    ok "mcp-fs 的根檔案系統唯讀（改不動 server 自己的程式碼）"
elif printf '%s' "$out" | grep -q 'rc=0'; then
    bad "mcp-fs 寫得進 /opt/mcp" "compose 的 read_only: true 沒有生效"
    in_mcpfs 'rm -f /opt/mcp/.verify-probe' >/dev/null
else
    soft "/opt/mcp 寫入被擋，但錯誤訊息不是預期的" "$(printf '%s' "$out" | head -1)"
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
