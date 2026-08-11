# syntax=docker/dockerfile:1
#
# hermes-runtime — 正式環境的 Hermes agent 容器（Dashboard + CLI）。
#
# 這一層刻意極薄。上游 NousResearch/hermes-agent 的官方映像已經處理完所有
# 困難的部分：s6-overlay 監督樹（/init 是 PID 1）、封存的 venv（/opt/hermes
# 為 root 所有且對 agent 唯讀）、修補過 WAL bug 的 SQLite、Node 22、
# Playwright chromium，並且已發佈 linux/amd64 與 linux/arm64 兩種架構。
#
# 我們在這裡做的只有「設定」—— 執行期不安裝任何東西，這正是「絕不在 Runtime
# 容器內執行 pip install」這條原則。所有需要安裝依賴的進化工作都發生在
# hermes-sandbox 這個短暫容器裡。
#
# 這個容器裡「沒有」使用者的外部資料夾。存取外部資料夾的唯一路徑是打到
# hermes-mcp-fs 那個獨立容器的 MCP over HTTP —— 見 mcp-fs/Dockerfile 與
# README 的「3. 透過 MCP 操作外部資料夾」。
#
# 版本預設釘住而非用 :latest：自我進化的 agent 已經有夠多變動來源了，底層
# runtime 不該是其中之一。要更新到最新版：改 .env 的 HERMES_VERSION（填
# 具體標籤或 latest 都可以）再 `make update`，細節見 README 的
# 「6. 更新到最新版」。
ARG HERMES_VERSION=v2026.7.20

# ---------------------------------------------------------------------------
FROM nousresearch/hermes-agent:${HERMES_VERSION}

# ARG 在 FROM 之後要重新宣告才看得到。拿來打 label，讓 `make version` 能問出
# 「這個映像到底是用哪個上游版本建的」，而不是只能相信 .env 現在的內容。
ARG HERMES_VERSION
LABEL org.opencontainers.image.base.name="nousresearch/hermes-agent:${HERMES_VERSION}" \
      hermes.upstream.version="${HERMES_VERSION}"

# ---------------------------------------------------------------------------
# 寫入沙箱範圍
# ---------------------------------------------------------------------------
# 維持上游預設 HERMES_WRITE_SAFE_ROOT=/opt/data —— agent 原生的 write_file /
# patch 工具碰不到資料卷以外的路徑。
#
# 外部資料夾的存取走 hermes-mcp-fs（獨立容器裡的 remote MCP server）。這裡
# 沒有 /workspace 可以寫，不是因為政策設定，而是因為那個掛載點根本不在這個
# 容器裡 —— 把 /workspace 加進這個變數也變不出檔案來。
ENV HERMES_WRITE_SAFE_ROOT=/opt/data

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
# 這裡刻意「沒有」/workspace —— 外部資料夾只掛在 hermes-mcp-fs 容器裡。
#
# 屬主設成 10000:10000（映像內建的 hermes 使用者）。若透過 HERMES_UID /
# HERMES_GID 改用其他 UID，上游的 stage2-hook 開機時會重新 chown。
RUN mkdir -p /opt/data/skills/evolved /opt/data/skill-versions \
    && chown -R 10000:10000 /opt/data/skills /opt/data/skill-versions

# ---------------------------------------------------------------------------
# 額外依賴（沙箱裡驗過、經人工確認後才進得來的東西）
# ---------------------------------------------------------------------------
# 這兩個清單是「讓 agent 自由裝套件」那條路徑的終點：agent 在短暫的
# hermes-sandbox 容器裡愛裝什麼裝什麼 → 沙箱記下裝了什麼 → controller 寫成待審
# 清單 → 人在宿主機上 `make deps-accept` 併進這裡 → git commit → make build。
#
# 關鍵在於安裝發生在 **build 時**，不是執行期。「絕不在 Runtime 容器內執行
# pip install」這條原則沒有被放寬：agent 在跑起來的容器裡依然是 uid 10000、
# 沒有 sudo、/usr 唯讀，裝不了任何東西。它能影響的只有「下一次 build 裝什麼」，
# 而那中間隔著一次人工審查與一次 git commit。
#
# 為什麼用 COPY deps/ 而不是 COPY deps/apt.txt deps/pip.txt：整個目錄複製進來，
# 映像裡就留著一份「這個映像到底多裝了什麼」的自我描述，事後在容器裡
# `cat /opt/deps/apt.txt` 就能對答案，不必回頭翻 repo。
COPY deps/ /opt/deps/

# apt 套件。清單全是註解或空的時候整層是 no-op（連 apt-get update 都不會跑）。
#
# sed 把 # 之後的內容砍掉，讓「套件名 # 沙箱驗過的版本」這種寫法能用。
RUN set -eu; \
    pkgs="$(sed 's/#.*//' /opt/deps/apt.txt | tr -s '[:space:]' '\n' | grep -v '^$' || true)"; \
    if [ -n "${pkgs}" ]; then \
        echo "[deps] 安裝 apt 套件：${pkgs}"; \
        apt-get update; \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ${pkgs}; \
        rm -rf /var/lib/apt/lists/*; \
    else \
        echo "[deps] apt.txt 沒有套件，跳過"; \
    fi

# Python 套件。刻意「不」裝進 /opt/hermes/.venv —— 那個 venv 是上游封存的
# （root 所有、對 agent 唯讀、連 pip 模組都沒有），裝進去會在 make update
# 換掉上游映像時留下難以追蹤的殘骸。改成獨立目錄 + PYTHONPATH。
#
# 用 uv 而不是 pip：上游映像的 venv 裡沒有 pip，但 /usr/local/bin/uv 有。
# --python 指向 venv 的直譯器，讓相依解析對到正確的 Python 版本與平台標記。
RUN set -eu; \
    mkdir -p /opt/extra/site-packages; \
    if grep -qvE '^[[:space:]]*(#|$)' /opt/deps/pip.txt 2>/dev/null; then \
        echo "[deps] 安裝 pip 套件："; \
        grep -vE '^[[:space:]]*(#|$)' /opt/deps/pip.txt; \
        uv pip install \
            --python /opt/hermes/.venv/bin/python \
            --target /opt/extra/site-packages \
            -r /opt/deps/pip.txt; \
    else \
        echo "[deps] pip.txt 沒有套件，跳過"; \
    fi; \
    chmod -R a+rX /opt/extra

# 目錄空的時候這行是無害的 no-op。設在這裡而不是條件式設定，是為了讓
# 「有沒有額外套件」不會改變映像的環境變數形狀 —— 兩種情況下的容器長得一樣，
# 少一個排查時的變因。
ENV PYTHONPATH=/opt/extra/site-packages

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
