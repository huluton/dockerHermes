# syntax=docker/dockerfile:1
#
# hermes-runtime — 正式環境的 Hermes agent 容器（Dashboard + CLI）。
#
# 這一層刻意極薄。上游 NousResearch/hermes-agent 的官方映像已經處理完所有
# 困難的部分：s6-overlay 監督樹（/init 是 PID 1）、封存的 venv（/opt/hermes
# 為 root 所有且對 agent 唯讀）、修補過 WAL bug 的 SQLite、Node 22、
# Playwright chromium，並且已發佈 linux/amd64 與 linux/arm64 兩種架構。
#
# 我們在這裡做的只有「設定」，不安裝任何套件 — 這正是「絕不在 Runtime
# 容器內執行 pip install」這條原則。所有需要安裝依賴的
# 進化工作都發生在 hermes-sandbox 這個短暫容器裡。
#
# 版本刻意釘住而非用 :latest：自我進化的 agent 已經有夠多變動來源了，
# 底層 runtime 不該是其中之一。升級請改 .env 的 HERMES_VERSION。
ARG HERMES_VERSION=v2026.7.20
FROM nousresearch/hermes-agent:${HERMES_VERSION}

# ---------------------------------------------------------------------------
# 寫入沙箱範圍
# ---------------------------------------------------------------------------
# 上游映像預設 HERMES_WRITE_SAFE_ROOT=/opt/data，也就是 agent 的 write_file /
# patch 工具完全無法碰到資料卷以外的路徑。但 runtime 要能用
# terminal 操作外部掛進來的 /workspace，所以這裡把 /workspace 加進安全根。
#
# 這個變數支援用 os.pathsep（Linux 上是 ":"）分隔多個根目錄。列出的根之外
# 一律硬性拒絕，不會跳核准提示。憑證類路徑（~/.ssh/id_rsa 之類）即使在安全
# 根內部仍然被上游的 denylist 擋住，這點不受影響。
ENV HERMES_WRITE_SAFE_ROOT=/opt/data:/workspace

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
# camofox 瀏覽器 sidecar
# ---------------------------------------------------------------------------
# 指向同一個 hermes-net 上的服務名稱。camofox 沒起來時上游會退回其他瀏覽器
# 後端，所以即使沒啟用 browser profile 這個設定也無害。
ENV CAMOFOX_URL=http://camofox:9377

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
