# hermes-mcp-fs —— 獨立容器裡的 remote MCP filesystem server。
#
# 為什麼是獨立容器：外部資料夾（WORKSPACE_DIR）只掛在這裡，runtime 完全沒有
# 這個掛載點。runtime 上的 terminal 工具、原生 write_file、以及任何被進化出來
# 的程式碼，在 OS 層都碰不到使用者的檔案 —— 唯一的路徑是打到這個容器的
# MCP over Streamable HTTP，而那條路徑上的允許清單由這裡的 args 決定。
# 這是容器邊界，不是工具層的君子協定。
#
# 上游的 @modelcontextprotocol/server-filesystem 只講 stdio，所以用
# supergateway 把它的 stdio 轉成 Streamable HTTP（stateful 模式：每個 MCP
# session 一個子行程，閒置逾時才回收）。
#
# 兩個版本都釘死。查最新版：
#     npm view @modelcontextprotocol/server-filesystem version
#     npm view supergateway version
ARG MCP_FS_VERSION=2026.7.10
ARG SUPERGATEWAY_VERSION=3.4.3

# ---------------------------------------------------------------------------
# 建置階段：把套件裝進 /opt/mcp
# ---------------------------------------------------------------------------
# --platform=$BUILDPLATFORM：兩個套件都是純 JavaScript，沒有原生模組，所以
# 用建置機器自己的架構跑 npm 就好，跨架構建置不必啟 QEMU。
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
ARG MCP_FS_VERSION
ARG SUPERGATEWAY_VERSION
WORKDIR /opt/mcp
RUN npm install --omit=dev --no-audit --no-fund \
        "@modelcontextprotocol/server-filesystem@${MCP_FS_VERSION}" \
        "supergateway@${SUPERGATEWAY_VERSION}" \
    && test -x /opt/mcp/node_modules/.bin/mcp-server-filesystem \
    && test -x /opt/mcp/node_modules/.bin/supergateway

# ---------------------------------------------------------------------------
# 執行階段
# ---------------------------------------------------------------------------
FROM node:22-alpine
ARG MCP_FS_VERSION
ARG SUPERGATEWAY_VERSION

LABEL org.opencontainers.image.title="hermes-mcp-fs" \
      org.opencontainers.image.description="Remote MCP filesystem server (Streamable HTTP) for Hermes" \
      hermes.mcp.filesystem.version="${MCP_FS_VERSION}" \
      hermes.mcp.supergateway.version="${SUPERGATEWAY_VERSION}"

# root 所有、非 root 執行。容器以 compose 的 user: 指定的 UID/GID 跑（要與
# HERMES_UID 一致，寫出去的檔案在宿主機上才屬於使用者），那個 UID 改不動
# /opt/mcp 裡的任何東西。
COPY --from=build --chown=root:root --chmod=0755 /opt/mcp /opt/mcp
COPY --chown=root:root --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

# MCP_FS_ROOTS：允許存取的根目錄，以 : 分隔（對應 compose 裡的 bind mount）。
#   server 拒絕清單以外的任何路徑，含用 symlink 繞出去。
# MCP_FS_SESSION_TIMEOUT_MS：閒置多久回收一個 MCP session 的子行程（毫秒）。
#   設成 0 代表不回收 —— 那樣被遺棄的 session 會一直佔著一個 node 行程。
ENV MCP_FS_ROOTS=/workspace \
    MCP_FS_PORT=9300 \
    MCP_FS_PATH=/mcp \
    MCP_FS_HEALTH_PATH=/healthz \
    MCP_FS_SESSION_TIMEOUT_MS=1800000

EXPOSE 9300

# 純 JS，沒有 shell 相依的初始化 —— 直接 exec 進 supergateway。
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
