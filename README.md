# Hermes 自我進化 Agent —— Docker 化部署

一套完整可跑的 stack。一行 `docker compose up` 起得來，
包含一個可從遠端連線的 Dashboard、一個管理進化生命週期的 Controller，以及
一層把 Docker API 收斂到白名單的最小特權閘道。

```
        ┌─────────────────────── hermes-net（應用層）───────────────────────┐
        │                                                                   │
        │   hermes-runtime  ◄────────►  hermes-controller  ◄────►  sandbox  │
        │   :9119 Dashboard             :9200（不對外）           （短暫）  │
        │   + MCP filesystem                    │                           │
        └───────────────────────────────────────┼───────────────────────────┘
                                                │
        ┌───────────────────────────────────────▼───────────────────────────┐
        │  docker-control-net（internal: true —— 完全沒有對外路由）         │
        │                                                                   │
        │              docker-socket-proxy ──► /var/run/docker.sock         │
        └───────────────────────────────────────────────────────────────────┘
```

`hermes-controller` 是唯一同時連上兩個網路的服務。Runtime 永遠碰不到 Docker
控制層 —— 這是拓撲層級的保證，不是設定層級的約定。

---

## 快速開始

```bash
cp .env.example .env
nano .env                     # 至少要填 HERMES_DASHBOARD_BASIC_AUTH_PASSWORD

make build                    # runtime / controller / sandbox 三個映像
make seed-config              # 把模型設定種進資料卷（首次啟動前）
make edit-config              # 填 base_url 與模型名稱（接地端 vLLM 必做）
make up

make verify                   # 對跑起來的 stack 跑一遍安全與拓撲檢查
```

Dashboard：`http://<你的主機>:9119`，帳號預設 `admin`，密碼是 `.env` 裡設的那個。

密碼是**必填**的。上游在 2026 年 6 月的安全強化之後，只要 dashboard 綁的不是
loopback，就強制要求一個 auth provider，否則 fail closed。`.env` 沒填的話
`docker compose up` 會在當下就明確失敗，而不是等你連不上 dashboard 才去翻 log。
（`HERMES_DASHBOARD_INSECURE` 已經是個只會印警告、沒有作用的舊變數。）

```bash
openssl rand -base64 24       # 產一組
```

`make help` 列出所有可用的指令。

---

## 目錄結構

```
.
├── spec.md                    架構規格
├── docker-compose.yml         三個服務、兩個網路、四個卷
├── .env.example               所有可調參數
├── Makefile                   操作入口
├── runtime/Dockerfile         FROM 上游官方映像；設定 + 內建 MCP filesystem server
├── controller/                進化生命週期管理（本專案自己寫的部分）
│   ├── Dockerfile
│   ├── entrypoint.sh          root 起 → chown 卷 → setpriv 降權
│   ├── policy.yaml            靜態掃描政策（唯讀掛載，改完重啟即可）
│   ├── app/
│   │   ├── main.py            FastAPI 端點
│   │   ├── config.py          環境變數 → 設定，含啟動時的健全性檢查
│   │   ├── docker_client.py   經 socket-proxy 存取 Docker，含孤兒回收器
│   │   ├── sandbox.py         每個任務一個容器：建立、執行、強制移除
│   │   ├── scanner.py         AST 靜態掃描
│   │   ├── promote.py         原子性晉升與回滾
│   │   └── lifecycle.py       串起整條流程，任務狀態存 SQLite
│   └── tests/                 scanner / promote / sandbox 設定的單元測試
├── sandbox/                   唯一允許自由 pip install 的地方
├── examples/
│   ├── config.vllm.yaml       接地端 vLLM 的設定範本
│   ├── evolve.hello-skill.json     最小可跑的進化任務
│   └── evolve.rejected-skill.json  刻意該被掃描器擋下的任務
└── scripts/verify.sh          make verify 的實作
```

---

## 設計原則

| 原則 | 落實方式 | 怎麼驗 |
|---|---|---|
| **正式環境穩定** | 技能卷與版本庫對 runtime 以 `:ro` 掛載；runtime 層只 `FROM` 上游映像做設定，執行期不安裝任何東西（MCP server 是建置階段裝好、版本釘死的） | `make verify` 第 3、7 節 |
| **沙箱隔離** | 每個任務一個獨立容器，`CapDrop: ALL`、`no-new-privileges`、記憶體/CPU/PID 上限、`/tmp` 用 tmpfs、逾時強制 kill、`finally` 明確移除，另有孤兒回收器兜底 | `make test`（15 個 sandbox 測試）＋ `make verify FULL=1` |
| **最小特權** | Controller 沒有 `docker.sock`，所有 Docker 存取都經過 socket-proxy 的白名單；Swarm / Network / Secret 一律 `0` | `make verify` 第 5 節 |
| **原子性晉升** | 新版本完整寫進版本庫 → `fsync` → `os.replace()` 換 symlink。Runtime 看到的永遠是「完整的舊版」或「完整的新版」 | `make test`（39 個 promote 測試） |
| **靜態安全掃描** | `ast.parse` 走訪，攔截 `eval`/`exec`/`subprocess`/`ctypes`/dunder 爬鏈等；外加樣式比對抓反向 shell 與編碼過的 payload | `make test`（27 個 scanner 測試）＋ `make verify FULL=1` 會提交一個必須被拒的技能 |

### 為什麼 symlink，而不是直接 `os.replace` 整個目錄

`os.replace()` 對單一檔案是原子的，但**沒辦法原子替換非空目錄** —— 底層的
`rename(2)` 會回 `ENOTEMPTY`。而一個技能是一整個目錄（`SKILL.md` 加上支援
檔案），必須整組一起換。所以做法是：把新版本完整寫進
`/opt/data/skill-versions/<skill>-<timestamp>/`，建一個指向它的暫時 symlink，
再 `os.replace(暫時symlink, /opt/data/skills/evolved/<skill>)`。rename 蓋過既有
symlink 是原子的。

### 為什麼版本庫是獨立的一個卷

上游的技能掃描器（`agent/skill_utils.py` 的 `iter_skill_index_files()`）用
`os.walk(skills_dir, followlinks=True)` 走遍整棵樹，只排除一組固定的目錄名稱
（`.git`、`.archive`、`node_modules` 等）—— **不包含** `.versions` 這類名字。

版本庫如果放在 `/opt/data/skills` 底下，`KEEP_VERSIONS=5` 就代表 agent 會同時
看到同一個技能的五個歷史世代，而且是**靜默**發生，不會有任何錯誤。所以版本庫
掛在 `/opt/data/skill-versions`，掃描樹之外。`config.py` 在啟動時會檢查這件事，
設錯會直接拒絕啟動。

兩個卷在 controller 與 runtime 內掛在**完全相同的絕對路徑**上 —— symlink 的
內容是相對路徑（`../../skill-versions/demo-...`），兩邊的相對關係必須對得上。

---

## 特色

### 1. 多個獨立的 Hermes agents

每一套 stack 的容器名、卷名、網路名、compose 專案名稱全部由 `.env` 的
`INSTANCE` 推導。開第二個 agent：

```bash
cp -r hermes_1 hermes_2 && cd hermes_2
sed -i 's/^INSTANCE=.*/INSTANCE=research/' .env
sed -i 's/^DASHBOARD_PORT=.*/DASHBOARD_PORT=9120/' .env
make build && make up
```

兩套完全不共用任何卷或網路。Controller 的孤兒容器回收器依
`hermes.instance` label 過濾，所以 A 的清理絕不會砍掉 B 正在跑的沙箱。

唯一共用的是宿主機的 Docker daemon 與埠號空間 —— 記得把**所有**對外發佈的埠
都改掉。目前只有 Dashboard 這一個（`DASHBOARD_PORT`）；controller 刻意不發佈
埠，MCP filesystem server 走 stdio 也不佔埠。

`WORKSPACE_DIR` 預設是相對路徑 `./workspace`，兩套各自解析到自己的目錄，
不會撞在一起。要讓兩套共用同一個外部資料夾就把它指到同一個絕對路徑。

### 2. 外接地端 vLLM

⚠️ **模型與 `base_url` 設定在 `/opt/data/config.yaml`，不是環境變數。**

```bash
make seed-config     # 把 examples/config.vllm.yaml 種進資料卷（已存在則不覆蓋）
make edit-config     # 改 base_url 與模型名稱（用 nano 開啟）
make show-config     # 印出目前生效的內容
make restart
```

`/opt/data` 是**具名卷 `hermes-<INSTANCE>-data` 的掛載點，不是宿主機路徑** ——
在宿主機上 `ls` 找不到 `config.yaml` 是正常的，用 `make show-config` 看內容。
已經有設定檔而想換一份範本，加 `FORCE=1`（會先備份成 `config.yaml.bak-<時間戳>`）。

這三個都直接對資料卷操作，stack 沒起來也能用 —— 首次啟動的順序
（`build` → `seed-config` → `edit-config` → `up`）本來就是在 stack 還沒起來的
狀態下設定模型。`edit-config` 會用**宿主機**上的 nano 開啟；上游映像裡沒有裝
任何編輯器（`vi`、`vim`、`nano`、`ed` 一個都沒有），所以不在容器內編輯。

要用別的編輯器就設 `EDITOR`（有設就以它為準，沒設才用 nano）：

```bash
EDITOR=vim make edit-config
```

`base_url` 依 vLLM 跑在哪裡而不同：

| vLLM 在哪 | `base_url` |
|---|---|
| 同一份 compose 裡的另一個服務 | `http://vllm:8000/v1` |
| 宿主機（`vllm serve ...`） | `http://host.docker.internal:8000/v1` |
| 區域網路上的另一台機器 | `http://192.168.1.50:8000/v1` |

`host.docker.internal` 在 Linux 上原本不存在，compose 裡的
`extra_hosts: host.docker.internal:host-gateway` 補上了它。

vLLM 沒開驗證的話 `api_key` 填 `"none"` —— 不能留空，OpenAI 相容的客戶端會
拒絕空字串。`context_length` 要對齊 vLLM 啟動時的 `--max-model-len`，設得比它大
會在推論到一半才炸。

不想自己跑模型的話，另一條路是走 Google Cloud 的 Vertex AI ——
見下面的「用 Vertex AI（GCP）當推論後端」。

### 3. 透過 MCP 操作外部資料夾

宿主機的 `WORKSPACE_DIR`（預設 `./workspace`，可以指到任何路徑）以 rw 掛在
容器內的 `/workspace`。Agent 操作它的方式是 **MCP**：runtime 映像裡裝了一個
MCP filesystem server，由 `config.yaml` 的 `mcp_servers` 接起來。

```yaml
mcp_servers:
  workspace-fs:
    command: "/opt/mcp/node_modules/.bin/mcp-server-filesystem"
    args:
      - "/workspace"
    connect_timeout: 60
    timeout: 120
    tools:
      prompts: false
      resources: false
```

`make seed-config` 種進去的範本已經含這一段，不用自己加。工具會以
`mcp__workspace-fs__read_text_file`、`mcp__workspace-fs__write_file`、
`mcp__workspace-fs__list_directory`… 的名稱出現在 agent 的工具清單裡。

**`args` 列出的目錄就是允許清單。** server 拒絕清單以外的任何路徑（含用
symlink 繞出去），這是 MCP 這條路徑上的授權邊界。`make verify` 第 7 節會實際
跑一次 handshake，再要求它讀 `/etc/passwd` 來確認拒絕真的有效。

改完 `config.yaml` 不用重啟容器 —— 在對話裡下 `/reload-mcp`，hermes 會重新
連線並更新工具清單。

**開放第二個資料夾**：加一個 bind，再把容器內路徑補進 `args`。

```yaml
# docker-compose.override.yml
services:
  hermes-runtime:
    volumes:
      - /srv/datasets:/datasets
```

```bash
make edit-config     # 在 mcp_servers.workspace-fs.args 底下加一行 - "/datasets"
make restart
```

**為什麼是 build 階段裝，而不是 `npx -y`。** 上游文件示範的
`command: "npx"` 會在 agent 第一次呼叫工具的當下連 npm registry 下載套件 ——
那是執行期安裝，離線環境直接失敗，而且每次拉到的內容不保證一樣。這裡改成在
`runtime/Dockerfile` 的一個 `node:22-alpine` build stage 裝好、版本由
`MCP_FS_VERSION` 釘死，執行期只是啟動一個本機子行程。

**原生檔案工具預設碰不到 `/workspace`。** `HERMES_WRITE_SAFE_ROOT` 維持上游
預設的 `/opt/data`，也就是 `write_file` / `patch` 只能寫資料卷 —— 外部資料夾
統一走 MCP。要回到舊行為（原生工具也能直接寫）就在 `.env` 設：

```bash
HERMES_WRITE_SAFE_ROOT=/opt/data:/workspace
```

⚠️ 誠實揭露：這是**工具層**的區隔，不是容器邊界。`/workspace` 是同一個容器裡
的一個掛載點，terminal 工具在 OS 層還是碰得到它。要真正的隔離，得把資料夾從
runtime 拿掉、改成獨立容器裡的 remote MCP server —— 那超出本專案目前的範圍。

想讓容器寫出來的檔案在宿主機上屬於你自己：

```bash
echo "HERMES_UID=$(id -u)" >> .env
echo "HERMES_GID=$(id -g)" >> .env
```

Runtime 與 Controller 必須用**同一組** UID/GID —— 否則 Controller 晉升出來的
技能檔案，Runtime 讀不到。

### 4. Dashboard 可從遠端連接

Dashboard 綁 `0.0.0.0:9119` 並由 compose 發佈到宿主機。只想開放本機的話，把
`.env` 的 `DASHBOARD_BIND` 改成 `127.0.0.1`，再用 SSH tunnel 連進來：

```bash
ssh -L 9119:127.0.0.1:9119 user@host
```

**這裡沒有 TLS。** basic auth 的密碼是以 base64 明文傳送的 —— 在信任的網路
之外，一定要放在反向代理（Caddy / nginx / Traefik）後面終結 TLS，或走 SSH
tunnel / VPN。

### 5. amd64 與 ARM

所有映像都確認過是多架構的（`docker manifest inspect` 實測 amd64 + arm64
皆存在）：`nousresearch/hermes-agent`、`python:3.13-slim`、
`tecnativa/docker-socket-proxy:v0.4.2`、`node:22-alpine`（只出現在 runtime 的
build stage 裡）。MCP filesystem server 是純 JavaScript，沒有原生模組，兩個
架構拿到的內容一樣。

```bash
make buildx     # 驗證自建的三個映像都能建出 linux/amd64 + linux/arm64
```

**跨架構建置需要先裝 QEMU binfmt handler。** 在 amd64 主機上建 arm64 映像
時，`RUN` 那幾行是真的在跑 arm64 執行檔 —— 核心得知道要交給誰執行。沒裝的話
會看到：

```
exec /bin/sh: exec format error
```

那不是 Dockerfile 的問題。裝法（Docker 官方維護的映像，一行搞定）：

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx inspect default | grep Platforms   # 應該要看得到 linux/arm64
```

它註冊的是宿主機核心的 `binfmt_misc`，重開機後失效；要立刻還原就把
`--install` 換成 `--uninstall`。Docker Desktop 通常已經內建，WSL2 上的
原生 daemon 則多半沒有。

在原生的 arm64 機器（Apple silicon、樹莓派、Graviton）上部署不需要這一步 ——
直接 `make build` 就好，那是原生建置。

### 6. 更新到最新版

```bash
make update-check     # 列出上游 nousresearch/hermes-agent 最近發佈的標籤
$EDITOR .env          # 改 HERMES_VERSION
make update           # 重建（--pull）+ 滾動重啟，卷完全不動
make version          # .env 要求的版本 / 映像實際建出來的版本 / 執行中的容器
make verify           # 確認架構在新版上依然成立
```

`HERMES_VERSION` 填具體標籤（`v2026.7.20`）或 `latest` 都可以。預設是釘住的
具體標籤 —— 一個會自我進化的 agent 已經有夠多變動來源，底層 runtime 不該
在你沒按下按鈕的時候自己換版。填 `latest` 的話，每次 `make update` 都會跟到
上游當下的最新映像。

`make update` 做的事：

| 步驟 | 為什麼 |
|---|---|
| `compose build --pull` | `--pull` 是關鍵 —— 沒有它，`latest` 或被重推過的標籤會沿用本機快取的舊 base image，看起來像「更新沒生效」 |
| `docker build --pull` sandbox | sandbox 不是 compose 服務，得單獨建 |
| `compose up -d --remove-orphans` | 只重建映像變了的容器；順手清掉舊版本留下、現在已經不在 compose 檔裡的容器 |

**資料不會受影響。** 技能、版本庫、`config.yaml`、sessions、memory、任務
資料庫全部在具名卷裡（`hermes-<INSTANCE>-{data,skills,versions,state}`），
更新只換映像。

**退版**：把 `HERMES_VERSION` 改回舊標籤，再跑一次 `make update`。唯一要留意
的是**資料卷的 schema 不會自動退回** —— 新版若動過 SQLite schema，退版前先備份：

```bash
docker run --rm -v hermes-default-data:/d -v "$PWD:/b" alpine \
    tar czf /b/hermes-data-backup.tar.gz -C /d .
```

同時也可以更新 MCP filesystem server（`.env` 的 `MCP_FS_VERSION`）—— 它是在
runtime 映像的 build stage 裡裝的，一樣走 `make update`。

---

## 用 Vertex AI（GCP）當推論後端

地端 vLLM 的替代方案：用 Google Cloud 的 Vertex AI 跑 Gemini。這是選用的，
兩者擇一（`config.yaml` 一次只能有一個 `model.provider`）。

先講清楚 **Vertex 和 Gemini 是兩個不同的 provider**：

| | `gemini` | `vertex` |
|---|---|---|
| 從哪拿 | Google AI Studio | GCP 專案 |
| 認證 | 靜態 API key | service account + OAuth2 |
| 計費 | 個人帳號 | GCP 帳單 |
| 配額 | 一般 | 企業級 |

下面講的是 `vertex`。

### 1. GCP 這邊

```bash
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT

gcloud iam service-accounts create hermes-vertex \
    --display-name="Hermes agent — Vertex AI only" --project=YOUR_PROJECT

# ⚠️ 只給這一個角色。理由見本節最後的「憑證的暴露面」。
gcloud projects add-iam-policy-binding YOUR_PROJECT \
    --member="serviceAccount:hermes-vertex@YOUR_PROJECT.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"

gcloud iam service-accounts keys create ./secrets/gcp-sa.json \
    --iam-account="hermes-vertex@YOUR_PROJECT.iam.gserviceaccount.com"
```

`secrets/` 目錄已經在版控裡（只有一個 `.gitkeep`），內容則被 `.gitignore` 排除。

### 2. Stack 這邊

`.env` 的預設值就對應上面的路徑，通常不用改：

```bash
GCP_CREDS_DIR=./secrets      # 以 :ro 掛到容器內的 /opt/gcp
VERTEX_SA_FILE=gcp-sa.json
VERTEX_PROJECT_ID=           # 留空 → 用 JSON 內嵌的專案
VERTEX_REGION=               # 留空 → config.yaml 的 region（預設 global）
```

然後種設定檔並啟動：

```bash
make seed-config TEMPLATE=config.vertex.yaml   # 已有設定檔就再加 FORCE=1
make edit-config                               # 挑模型；project_id 通常可留空
make up
```

### 3. 挑模型

`config.yaml` 的 `model.default` 填下列其中之一（前面的 `google/` 不能省）：

| 模型 | 說明 |
|---|---|
| `google/gemini-3.1-pro-preview` | 最強，最貴 |
| `google/gemini-3-pro-preview` | |
| `google/gemini-3-flash-preview` | 範本的預設，速度與成本的平衡點 |
| `google/gemini-3.1-flash-lite-preview` | 最便宜 |
| `google/gemini-2.5-pro` | 已 GA，不是 preview |
| `google/gemini-2.5-flash` | 已 GA，不是 preview |

### 兩個會讓人卡很久的陷阱

**不要填 `model.base_url`。** Vertex 的端點是執行期依 `project_id` + `region`
算出來的：

```
https://{host}/v1beta1/projects/{project}/locations/{region}/endpoints/openapi
```

自己填一個上去只會蓋掉正確的值，然後拿到 404。範本裡刻意沒有這個鍵。

**`region` 保持 `global`。** Gemini 3.x 的 preview 模型只在 global endpoint 上。
改成 `us-central1` 之類的區域值不會噴錯，而是**靜默 404** —— 症狀是「模型不存在」，
看起來像打錯名字。只有在用 GA 模型（`gemini-2.5-*`）而且有資料落地需求時才改。

### 驗證

```bash
docker compose logs hermes-runtime | grep -i vertex
```

沒有輸出就是正常的。看到下面這些代表憑證沒接上：

| log 訊息 | 意思 |
|---|---|
| `no GCP credentials found` | `/opt/gcp/gcp-sa.json` 不存在或讀不到 |
| `could not mint token` | 憑證讀到了但換 token 失敗（SA 被停用 / 權限不足 / 時鐘偏移） |
| `google-auth package not installed` | 不該發生 —— 上游映像已內建 |

檔案在不在容器裡：

```bash
docker compose exec hermes-runtime ls -l /opt/gcp/
```

### 備選：不用 service account，改用 ADC

`gcloud auth application-default login` 產生的憑證也能用，但在容器裡比較彆扭 ——
`hermes` 使用者的 HOME 是 `/opt/data`（那是資料卷），得把
`~/.config/gcloud` 掛到 `/opt/data/.config/gcloud:ro`，而且 ADC 的 refresh token
會過期。伺服器部署建議還是用 service account。

### 憑證的暴露面

**掛進 runtime 的 GCP 憑證，agent 讀得到。** 上游的敏感路徑規則
（`tools/file_tools.py` 的 `_SENSITIVE_PATH_PREFIXES`、`tools/approval.py` 的
那組樣式）擋的是**寫入**，不擋讀取；`HERMES_WRITE_SAFE_ROOT` 同理，它限制的是
寫入範圍。也就是說一個被 prompt injection 誘導的 agent，是有可能把
`/opt/gcp/gcp-sa.json` 的內容讀出來並送走的。

這件事沒有乾淨的技術解 —— agent 要能用憑證，就得能讀憑證。能做的是把爆炸半徑壓小：

- 一個**專用**的 service account，只綁一個專案，只給 `roles/aiplatform.user`
- 不要重複使用其他用途的 SA
- 在 GCP 那邊設預算警示與配額上限
- 不用 Vertex 的期間，把 JSON 從 `secrets/` 拿掉

憑證沒有掛進 controller，也沒有掛進 sandbox（`make verify` 會檢查這兩件事）。

---

## 進化流程

```
POST /evolve
    │
    ▼
起一個沙箱容器（CapDrop ALL、資源上限、逾時）
    │
    ▼
依序執行 steps；技能檔案寫到 /work/out/
    │
    ▼
用 get_archive 把 /work/out/ 收回 controller（沒有共用卷）
    │
    ▼
AST 靜態掃描 —— 沒過就到此為止，線上狀態完全沒被碰過
    │
    ▼
原子性晉升：寫版本庫 → fsync → 換 symlink
    │
    ▼
finally：強制移除沙箱容器
```

提交一個任務：

```bash
make evolve REQ=examples/evolve.hello-skill.json
make tasks                        # 列出所有任務
make task ID=evo-...              # 查單一任務（含每個步驟的 stdout/stderr）
make skills                       # 線上技能與可用版本
make rollback SKILL=foo VERSION=foo-20260727T120000Z
```

想親眼看掃描器擋東西的話，提交反向範例：

```bash
make evolve REQ=examples/evolve.rejected-skill.json
```

它會以 `status=failed`、`phase=scan` 收場，錯誤訊息列出四項發現
（`import socket`、`import subprocess`、`exec()`、base64 編碼過的 payload），
而且 `/opt/data/skills/evolved/` 底下不會出現 `rejected-skill` —— 被拒絕的
產物連版本庫都不會寫進去。

請求格式：

```json
{
  "skill": "csv-summariser",
  "timeout_sec": 900,
  "env": {"SOME_FLAG": "1"},
  "steps": [
    {"name": "deps",  "run": ["pip", "install", "--quiet", "pandas"], "timeout_sec": 300},
    {"name": "write", "run": "mkdir -p out/scripts && cat > out/SKILL.md <<'EOF'\n...\nEOF"},
    {"name": "test",  "run": ["python", "out/scripts/main.py", "--self-test"]}
  ]
}
```

- `run` 是**字串**就走 shell，是**陣列**就不走 shell。要用 heredoc、管線、
  重導向就用字串；其他情況用陣列比較安全。
- 技能檔案必須寫到 `/work/out/`（步驟的工作目錄是 `/work`，所以相對路徑
  `out/...` 就對了）。其他地方留下的暫存檔（venv、clone 下來的 repo）不會被收走。
- `out/SKILL.md` 是必要的 —— 這是上游 hermes 的技能格式規定。
- `env` 不能覆寫 `PATH`、`PYTHONPATH`、`LD_PRELOAD`、`LD_LIBRARY_PATH`、
  `VIRTUAL_ENV`，那些會改變「什麼程式碼會被執行」。

Controller 的 API（`hermes-net` 內的 `http://hermes-controller:9200`，
**刻意不對宿主機發佈**）：

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/healthz` | 存活探測，不碰 Docker |
| GET | `/readyz` | 就緒探測，會確認 Docker 可達 |
| GET | `/status` | 完整狀態（沙箱設定、掃描器、技能路徑） |
| POST | `/evolve` | 提交任務，回 202 + `task_id` |
| GET | `/tasks?limit=&status=` | 列出任務 |
| GET | `/tasks/{id}` | 單一任務的完整記錄 |
| GET | `/skills` | 線上技能與可用版本 |
| POST | `/skills/{skill}/rollback` | 回滾到指定版本 |

這個 API 沒有身分驗證 —— **網路拓撲就是它的邊界**。它只在 `hermes-net` 上，
沒有發佈任何埠。要從宿主機打就用 `docker compose exec`（`make` 的那些目標就是
這樣做的）。

### 調整掃描政策

`controller/policy.yaml` 以唯讀掛載進 controller，改完 `make restart` 就生效，
不用重建映像。唯讀是刻意的：controller 改不了自己的安全政策。

被誤殺的話，先確認那真的是誤報 —— `controller/tests/test_scanner.py` 裡有一個
`test_ordinary_skill_code_is_clean`，就是為了守住「不能太吵」這條線。一個到處
誤報的掃描器，最後的下場是被人設成 `SCANNER_ENFORCE=0`，等於整層防禦消失。

---

## 「唯讀」怎麼詮釋（明確聲明）

「Runtime 所有磁碟區皆以唯讀掛載」這條，**字面照做會讓 hermes 開不起來**
—— `/opt/data` 存放 SQLite state、sessions、memory 與 `.env`，必須可寫。

本專案採用的詮釋是：**「會進化的程式碼」對 Runtime 唯讀**。

| 路徑 | Runtime | Controller |
|---|---|---|
| `/opt/hermes`（程式碼 + venv） | ro（上游已 root-owned） | 無 |
| `/opt/data/skills/evolved`（線上技能） | **ro** | rw |
| `/opt/data/skill-versions`（版本庫） | **ro** | rw |
| `/opt/data`（自身狀態） | rw | 無 |
| `/workspace`（外部資料夾，走 MCP） | rw | 無 |
| `/opt/mcp`（MCP filesystem server） | ro（root 所有） | 無 |

`make verify` 的第 3 節就是在驗這張表。

### 開機時會看到一行 warning，那是預期的

```
[stage2] Warning: chown /opt/data/skills failed
```

上游的 `docker/stage2-hook.sh` 會想 chown 整棵 skills 樹，撞到我們的唯讀掛載。
它把 chown 失敗吞掉只印 warning（`chown_hermes_tree()`），開機流程不受影響。
**這不是故障。**

---

## 誠實揭露的殘留風險

### socket-proxy 不是完整的授權邊界

一旦放行 `CONTAINERS=1` + `POST=1`（controller 要建沙箱，非放不可），取得
controller 執行權的攻擊者就能建立 privileged 容器、或掛載宿主機路徑，藉此逃逸
到宿主機。

它確實擋掉了 Swarm / Secrets / Networks / Configs / Volumes / Build（符合
白名單之外一律拒絕），也把攻擊面從「完整的 Docker API」收斂到「容器 CRUD」，
但**不該被當成「即使 controller 被攻陷也安全」的保證**。

要再往下收，得換成一個能檢查請求 body 的授權代理（例如自己寫一層，拒絕
`HostConfig.Privileged`、`Binds`、`CapAdd`）—— 那超出了本專案的範圍。

### AST 靜態掃描是縱深防禦，不是沙箱

任何以黑名單為基礎的 Python 原始碼分析都能被繞過。`policy.yaml` 開頭就寫了
這件事：它防的是「被誤導的 agent 產出意外危險的程式碼」（LLM 為了清暫存檔寫出
`shutil.rmtree`、為了偵錯留下反向 shell），不是「有決心的對手」。

真正的隔離邊界是 sandbox 容器本身。

### Agent 讀得到掛進 runtime 的憑證

`HERMES_WRITE_SAFE_ROOT` 與上游的敏感路徑規則限制的都是**寫入**，讀取不在管制
範圍內。所以 GCP service account JSON（`/opt/gcp/`）這類掛進 runtime 的憑證，
agent 本身讀得到 —— 被 prompt injection 誘導時就有外洩的可能。

這沒有乾淨的技術解：agent 要能用憑證，就得能讀憑證。緩解只能靠壓小爆炸半徑 ——
專用的 service account、單一專案、只給 `roles/aiplatform.user`、設預算警示。
詳見「用 Vertex AI（GCP）當推論後端」的最後一小節。

### 其他

- **Dashboard 沒有 TLS。** basic auth 的密碼以 base64 明文傳送。
- **Controller API 沒有身分驗證。** 保護它的是「不發佈埠 + 只在應用網路上」。
  任何能在 `hermes-net` 上執行程式碼的東西都能呼叫它 —— 包括沙箱容器本身。
- **沙箱可以連外網。** 進化任務通常要 `pip install`。要斷網的話，在
  `sandbox.py` 裡把 `NetworkMode` 改成 `none`，但那樣裝不了套件。

---

## 疑難排解

**`docker compose up` 直接失敗，說 `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` 必須設定**
— 這是刻意的。`.env` 裡填一組密碼（`openssl rand -base64 24`）。

**Dashboard 連得上但所有頁面都回錯誤** — auth provider 沒生效。
`docker compose exec hermes-runtime env | grep DASHBOARD` 確認密碼真的傳進去了。

**Agent 看不到晉升上線的技能** — 九成是 UID 不一致。`make verify` 第 4 節會
檢查；`.env` 的 `HERMES_UID` 改了以後，既有卷裡的檔案屬主不會自動跟著變。

**Agent 看到同一個技能的好幾個版本** — 版本庫被掛進了掃描樹裡。確認
`SKILL_VERSIONS_DIR` 在 `/opt/data/skills` 之外。`config.py` 啟動時會擋這種設定。

**Agent 的工具清單裡沒有 `mcp__workspace-fs__*`** — `config.yaml` 裡缺
`mcp_servers` 區塊。既有的資料卷不會因為範本更新而自動跟著改：用
`make edit-config` 手動補上（內容抄 `examples/config.vllm.yaml`），或
`make seed-config FORCE=1` 重種一份（會先備份）。補完在對話裡下 `/reload-mcp`。
`make verify` 第 7 節會檢查這件事。

**MCP 工具回「path outside allowed directories」** — 目標路徑不在
`mcp_servers.workspace-fs.args` 的清單裡。那是預期的拒絕，不是故障；要開放
就加 bind 再加一行 `args`。

**`make evolve` 回 503** — 併發任務數滿了（`MAX_CONCURRENT_TASKS`），或
controller 連不上 socket-proxy。`make status` 看細節。

**沙箱容器留下來沒被清掉** — 正常路徑上 controller 會在 `finally` 移除，孤兒
回收器（`REAPER_INTERVAL_SEC`）兜底處理 controller 被 SIGKILL 的情況。
手動清：`make reap`。

**任務逾時但步驟看起來很快** — `timeout_sec` 是**整個任務**的上限，步驟各自
還有自己的 `timeout_sec`。兩個都要夠。

---

## 開發

```bash
make test        # controller 的單元測試（在 controller 映像裡跑）
make verify      # 對執行中的 stack 跑安全與拓撲檢查
make verify FULL=1   # 額外跑一次真的進化任務，驗端到端流程與沙箱清理
make config      # 印出解析後的 compose 設定
make logs        # 追蹤所有服務的 log
```

本機直接跑測試（不進容器）：

```bash
cd controller
python3 -m venv /tmp/hv && /tmp/hv/bin/pip install -q pytest pyyaml
PYTHONPATH=. /tmp/hv/bin/python -m pytest tests -q
```

升級上游 hermes：`make update-check` → 改 `.env` 的 `HERMES_VERSION` →
`make update`。技能與狀態都在具名卷裡，不會受影響。詳見「6. 更新到最新版」。
