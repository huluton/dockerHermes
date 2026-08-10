# syntax=docker/dockerfile:1
#
# hermes-runtime — 正式環境的 Hermes agent 容器（Dashboard + CLI）。
#
# 這一層刻意極薄。上游 NousResearch/hermes-agent 的官方映像已經處理完所有
# 困難的部分：s6-overlay 監督樹（/init 是 PID 1）、封存的 venv（/opt/hermes
# 為 root 所有且對 agent 唯讀）、修補過 WAL bug 的 SQLite、Node 22、
# Playwright chromium，並且已發佈 linux/amd64 與 linux/arm64 兩種架構。
#
# 我們在這裡做的只有「設定」，外加一份在建置階段就固定下來的 MCP filesystem
# server（見下面的 mcp stage）—— 執行期不安裝任何東西，這正是「絕不在
# Runtime 容器內執行 pip install」這條原則。所有需要安裝依賴的進化工作都發生
# 在 hermes-sandbox 這個短暫容器裡。
#
# 版本預設釘住而非用 :latest：自我進化的 agent 已經有夠多變動來源了，底層
# runtime 不該是其中之一。要更新到最新版：改 .env 的 HERMES_VERSION（填
# 具體標籤或 latest 都可以）再 `make update`，細節見 README 的
# 「6. 更新到最新版」。
ARG HERMES_VERSION=v2026.7.20

# MCP filesystem server 的版本，同樣釘住。查最新版：
#     npm view @modelcontextprotocol/server-filesystem version
ARG MCP_FS_VERSION=2026.7.10

# ---------------------------------------------------------------------------
# mcp stage —— 把 MCP filesystem server 抓下來，之後整包複製進 runtime
# ---------------------------------------------------------------------------
# 為什麼不在 runtime 裡直接 `npx -y @modelcontextprotocol/server-filesystem`：
# npx 會在「agent 第一次呼叫工具」的當下連上 npm registry 下載套件 —— 那是
# 執行期安裝，離線環境直接失敗，而且每次拉到的內容不保證一樣。改成建置階段
# 裝好、版本釘死，執行期只是啟動一個本機子行程。
#
# 純 JavaScript，沒有原生模組，所以這一層在 amd64 與 arm64 上內容相同 ——
# 因此固定用 $BUILDPLATFORM（建置機器自己的架構）跑 npm，交叉建置時不必經過
# QEMU 模擬。產物照樣能在目標架構的 node 上跑。
FROM --platform=$BUILDPLATFORM node:22-alpine AS mcp
ARG MCP_FS_VERSION
WORKDIR /opt/mcp
RUN npm install --omit=dev --no-audit --no-fund \
        "@modelcontextprotocol/server-filesystem@${MCP_FS_VERSION}" \
    && test -x /opt/mcp/node_modules/.bin/mcp-server-filesystem

# ---------------------------------------------------------------------------
FROM nousresearch/hermes-agent:${HERMES_VERSION}

# ARG 在 FROM 之後要重新宣告才看得到。拿來打 label，讓 `make version` 能問出
# 「這個映像到底是用哪個上游版本建的」，而不是只能相信 .env 現在的內容。
ARG HERMES_VERSION
ARG MCP_FS_VERSION
LABEL org.opencontainers.image.base.name="nousresearch/hermes-agent:${HERMES_VERSION}" \
      hermes.upstream.version="${HERMES_VERSION}" \
      hermes.mcp.filesystem.version="${MCP_FS_VERSION}"

# ---------------------------------------------------------------------------
# 寫入沙箱範圍
# ---------------------------------------------------------------------------
# 維持上游預設 HERMES_WRITE_SAFE_ROOT=/opt/data —— agent 原生的 write_file /
# patch 工具碰不到資料卷以外的路徑。外部資料夾的存取改由 MCP filesystem
# server 負責（見下一段），那是規格指定的路徑。
#
# 這個變數支援用 os.pathsep（Linux 上是 ":"）分隔多個根目錄。要恢復「原生
# 工具也能寫 /workspace」的舊行為，在 .env 裡設
# HERMES_WRITE_SAFE_ROOT=/opt/data:/workspace，compose 會覆寫這個預設值。
ENV HERMES_WRITE_SAFE_ROOT=/opt/data

# ---------------------------------------------------------------------------
# MCP filesystem server（操作外部資料夾的路徑）
# ---------------------------------------------------------------------------
# 從 mcp stage 整包搬過來。stdio 傳輸 —— hermes 需要時把它當子行程啟動，
# 講完 MCP handshake 就註冊出 mcp__workspace-fs__* 這組工具。
#
# 沒有對外開埠、沒有常駐行程：不用的時候它根本沒在跑。
#
# 實際要開放哪些目錄，是由 /opt/data/config.yaml 的 mcp_servers 區塊傳給它的
# 位置參數決定（範本見 examples/config.vllm.yaml）。server 只接受那份清單裡
# 的路徑，其餘一律拒絕 —— 這是 MCP 這條路徑上的授權邊界。
#
# 0755 / root 所有：agent（hermes 使用者）執行得了，但改不動 server 自己的
# 程式碼。
COPY --from=mcp --chown=root:root --chmod=0755 /opt/mcp /opt/mcp
ENV HERMES_MCP_FS_BIN=/opt/mcp/node_modules/.bin/mcp-server-filesystem

# ---------------------------------------------------------------------------
# Dashboard（可從遠端連接）
# ---------------------------------------------------------------------------
# HERMES_DASHBOARD=1 會讓上游的 s6-rc dashboard 服務起來（預設只註冊不啟動）。
# 綁 0.0.0.0 才能讓 compose 發佈的 -p 9119:9119 從宿主機外部連得到。
#
# 安全性注意：上游在 2026 年 6 月的強化之後，只要綁定位址不是 loopback，
# dashboard 就會強制要求一個 auth provider，否則擋掉所有路由。
# docker-compose.yml 因此把 basic auth 的帳密設成必填變數。
ENV HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=0.0.0.0 \
    HERMES_DASHBOARD_PORT=9119

# ---------------------------------------------------------------------------
# 掛載點
# ---------------------------------------------------------------------------
# 先把目錄造出來，這樣 compose 的 bind / volume 掛上去時就不會出現由 Docker
# 代為建立、屬主為 root 的空目錄。
#
# /opt/data/skills/evolved 是 controller 晉升技能的落點，在 runtime 這邊會被
# 以 :ro 覆蓋掛載 — runtime 只讀技能，永遠不寫。
#
# /opt/data/skill-versions 是技能版本庫，同樣以 :ro 掛進來。它裡面存的是每個
# 技能的完整歷史版本，而 evolved/<skill> 只是指向其中一個版本的 symlink ——
# 沒有把版本庫掛進 runtime 的話，那些 symlink 全部會斷掉。
#
# 版本庫刻意放在 /opt/data/skills 之外：上游的技能掃描器會用
# os.walk(followlinks=True) 走遍整個 skills 目錄，版本庫若在掃描範圍內，
# 保留的每一個歷史版本都會被當成一個獨立的線上技能。
#
# 屬主設成 10000:10000（映像內建的 hermes 使用者）。若透過 HERMES_UID /
# HERMES_GID 改用其他 UID，上游的 stage2-hook 開機時會重新 chown。
RUN mkdir -p /workspace /opt/data/skills/evolved /opt/data/skill-versions \
    && chown -R 10000:10000 /workspace /opt/data/skills /opt/data/skill-versions

# ---------------------------------------------------------------------------
# 健康檢查
# ---------------------------------------------------------------------------
# 打 /api/status。這是上游在 dashboard_auth/public_paths.py 裡明確標為
# 「Liveness probe target」的端點，也是 PUBLIC_API_PATHS 這個 frozenset 裡
# 唯一適合當健康檢查的一項 —— auth gate 會直接放行，不必帶憑證。
#
# 不要改成 /api/health：v2026.7.20 沒有這個路由。未驗證時它回 401（auth gate
# 在 routing 之前就先擋下來），帶著 session cookie 才看得到真相是 404。
# 這個組合很容易讓人誤判成「驗證設錯了」，而實際上是端點根本不存在。
#
# start-period 給得寬鬆：冷啟動時 stage2-hook 要做 UID 重映射、資料卷 chown、
# 設定檔種入與技能同步，在較慢的機器或 ARM SBC 上可能要花上一分鐘。
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://127.0.0.1:9119/api/status >/dev/null || exit 1

# ---------------------------------------------------------------------------
# 不要覆寫 ENTRYPOINT
# ---------------------------------------------------------------------------
# 繼承上游的 ["/init", "/opt/hermes/docker/main-wrapper.sh"]。/init 是
# s6-overlay 的 PID 1，負責跑 /etc/cont-init.d/*（UID 重映射、資料卷 chown、
# 設定種入、profile 重建）並建立監督樹，然後才啟動服務。
#
# 換掉它 = 上面那些全部不會發生，gateway 會以各種難以診斷的方式壞掉。
# 實際的 CMD（gateway run）由 docker-compose.yml 提供。
