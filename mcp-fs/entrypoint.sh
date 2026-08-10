#!/bin/sh
#
# 把 MCP_FS_ROOTS（以 : 分隔）展開成 filesystem server 的允許清單，
# 再用 supergateway 把它的 stdio 轉成 Streamable HTTP。
#
# 這支腳本刻意很薄 —— 唯一的邏輯是「環境變數 → 命令列參數」。真正的授權
# 判斷在 filesystem server 自己身上（它只接受下面列出的根目錄）。
set -eu

FS_BIN=/opt/mcp/node_modules/.bin/mcp-server-filesystem
GATEWAY_BIN=/opt/mcp/node_modules/.bin/supergateway

: "${MCP_FS_ROOTS:=/workspace}"
: "${MCP_FS_PORT:=9300}"
: "${MCP_FS_PATH:=/mcp}"
: "${MCP_FS_HEALTH_PATH:=/healthz}"
: "${MCP_FS_SESSION_TIMEOUT_MS:=1800000}"
: "${MCP_FS_LOG_LEVEL:=info}"

# supergateway 是用 `spawn(cmd, { shell: true })` 起子行程的，所以 --stdio
# 收到的是一個 shell 字串。路徑用單引號包起來，含空白也不會被拆開。
# 單引號本身不做跳脫 —— 目錄名有單引號的話這裡會壞掉，那不是要支援的情境。
stdio_cmd="$FS_BIN"
root_count=0

# 這裡的 IFS 只影響 for 的展開（清單在進迴圈前就切好了），迴圈內用的都是
# 有加引號的變數，不受影響。
IFS=:
for root in $MCP_FS_ROOTS; do
    [ -n "$root" ] || continue

    if [ ! -d "$root" ]; then
        # 不直接失敗：少掛一個資料夾時，其他資料夾應該照常可用。
        echo "mcp-fs: 警告 —— 允許清單裡的 $root 不存在（compose 少了對應的 bind mount？）" >&2
        continue
    fi

    stdio_cmd="$stdio_cmd '$root'"
    root_count=$((root_count + 1))
done
unset IFS

if [ "$root_count" -eq 0 ]; then
    echo "mcp-fs: MCP_FS_ROOTS ($MCP_FS_ROOTS) 裡沒有任何存在的目錄，拒絕啟動。" >&2
    echo "mcp-fs: 沒有允許清單的 filesystem server 沒有意義 —— 檢查 WORKSPACE_DIR 與 bind mount。" >&2
    exit 1
fi

echo "mcp-fs: 允許 $root_count 個根目錄；監聽 0.0.0.0:${MCP_FS_PORT}${MCP_FS_PATH}"

# --stateful：每個 MCP session 一個子行程。stateless 模式是「每個 HTTP POST
# 重新 spawn 一次 server 並重跑 initialize」，對長時間的對話又慢又浪費。
set -- \
    --stdio "$stdio_cmd" \
    --outputTransport streamableHttp \
    --stateful \
    --port "$MCP_FS_PORT" \
    --streamableHttpPath "$MCP_FS_PATH" \
    --healthEndpoint "$MCP_FS_HEALTH_PATH" \
    --logLevel "$MCP_FS_LOG_LEVEL"

# 不給 --sessionTimeout 的語意是「只有客戶端明確終止才刪 session」，
# 也就是被遺棄的 session 會一直佔著一個 node 行程。設成 0 就是選這個行為。
if [ "$MCP_FS_SESSION_TIMEOUT_MS" -gt 0 ] 2>/dev/null; then
    set -- "$@" --sessionTimeout "$MCP_FS_SESSION_TIMEOUT_MS"
fi

exec node "$GATEWAY_BIN" "$@"
