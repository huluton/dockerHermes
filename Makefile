# Hermes 自我進化 Agent —— 操作入口
#
#     make help
#
# 所有目標都尊重 .env 裡的 INSTANCE，所以在複製出去的第二套 stack 目錄裡
# 執行同樣的指令，操作的就是那一套。

SHELL := /bin/bash
.DEFAULT_GOAL := help

# 從 .env 讀 INSTANCE（沒有 .env 就用 default）。這裡不用 `include .env`，
# 因為那會把整份 .env 都變成 make 變數，含密碼，容易在 make 的除錯輸出裡外洩。
INSTANCE := $(shell sed -n 's/^INSTANCE=//p' .env 2>/dev/null | head -1)
INSTANCE := $(if $(INSTANCE),$(INSTANCE),default)

# 同樣從 .env 讀。一次性工具容器（seed-config / edit-config）不經過 compose，
# 拿不到 environment: 區塊，所以得在這裡讀出來自己傳進去 —— 否則設了
# HERMES_UID=1000 的人會拿到一個 owner 是 10000 的 config.yaml。
HERMES_UID := $(shell sed -n 's/^HERMES_UID=//p' .env 2>/dev/null | head -1)
HERMES_UID := $(if $(HERMES_UID),$(HERMES_UID),10000)
HERMES_GID := $(shell sed -n 's/^HERMES_GID=//p' .env 2>/dev/null | head -1)
HERMES_GID := $(if $(HERMES_GID),$(HERMES_GID),10000)

COMPOSE := docker compose
SANDBOX_IMAGE := hermes-sandbox:$(INSTANCE)
PLATFORMS := linux/amd64,linux/arm64

# sandbox 容器不是 compose 服務（由 controller 動態建立），所以用 label 找。
SANDBOX_FILTER := --filter label=hermes.instance=$(INSTANCE) --filter label=hermes.role=sandbox

.PHONY: help
help: ## 顯示這份說明
	@echo "Hermes 自我進化 Agent（實例：$(INSTANCE)）"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "首次啟動："
	@echo "  cp .env.example .env && \$$EDITOR .env   # 填 HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"
	@echo "  make build && make seed-config && make edit-config && make up"
	@echo
	@echo "改用 Vertex AI（GCP）而不是地端 vLLM："
	@echo "  make seed-config TEMPLATE=config.vertex.yaml   # 加 FORCE=1 覆蓋既有設定"

# ---------------------------------------------------------------------------
# 前置檢查
# ---------------------------------------------------------------------------

.PHONY: check-env
check-env:
	@test -f .env || { \
		echo "錯誤：找不到 .env。先執行 cp .env.example .env 並填入密碼。" >&2; \
		exit 1; }
	@grep -qE '^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=.+' .env || { \
		echo "錯誤：.env 裡的 HERMES_DASHBOARD_BASIC_AUTH_PASSWORD 是空的。" >&2; \
		echo "      dashboard 綁在非 loopback 位址時，沒有 auth provider 會 fail closed。" >&2; \
		echo "      產生一組：openssl rand -base64 24" >&2; \
		exit 1; }

# ---------------------------------------------------------------------------
# 建置
# ---------------------------------------------------------------------------

.PHONY: build
build: ## 建置三個映像（runtime / controller / sandbox）
	$(COMPOSE) build hermes-runtime hermes-controller
	@# sandbox 不是 compose 服務 —— 它是 controller 動態建立的短暫容器，
	@# 但映像還是得先存在。
	docker build -t $(SANDBOX_IMAGE) ./sandbox

.PHONY: pull-browser
pull-browser: ## 拉 camofox 映像（上游有發佈，不用自建；約 3.5GB）
	$(COMPOSE) --profile browser pull camofox

.PHONY: buildx
buildx: ## 驗證三個映像都能建出 amd64 + arm64
	@# 多架構的結果沒辦法 --load 進本機 daemon，所以這裡只驗證「建得起來」。
	@# 要推到 registry 的話，把 --platform 那行後面加 --push 與 -t <registry>/...
	docker buildx build --platform $(PLATFORMS) ./runtime
	docker buildx build --platform $(PLATFORMS) ./controller
	docker buildx build --platform $(PLATFORMS) ./sandbox
	@echo "✓ 三個映像在 $(PLATFORMS) 都建置成功"

# ---------------------------------------------------------------------------
# 生命週期
# ---------------------------------------------------------------------------

.PHONY: up
up: check-env ## 啟動 stack
	@mkdir -p "$$(sed -n 's/^WORKSPACE_DIR=//p' .env | head -1 || echo ./workspace)" 2>/dev/null || true
	$(COMPOSE) up -d
	@echo
	@echo "Dashboard：http://localhost:$$(sed -n 's/^DASHBOARD_PORT=//p' .env | head -1 || echo 9119)"
	@echo "帳號：$$(sed -n 's/^HERMES_DASHBOARD_BASIC_AUTH_USERNAME=//p' .env | head -1 || echo admin)"

.PHONY: up-browser
up-browser: check-env ## 啟動 stack，含 camofox
	$(COMPOSE) --profile browser up -d

.PHONY: down
down: ## 停止 stack（保留卷）
	$(COMPOSE) --profile browser down
	@$(MAKE) --no-print-directory reap

.PHONY: destroy
destroy: ## 停止 stack 並刪除所有卷（技能、狀態、資料全部消失）
	@echo "這會刪除實例 '$(INSTANCE)' 的所有卷：資料、技能、版本庫、任務資料庫。"
	@read -p "輸入 $(INSTANCE) 確認：" c && [ "$$c" = "$(INSTANCE)" ] || { echo "已取消"; exit 1; }
	$(COMPOSE) --profile browser down -v
	@$(MAKE) --no-print-directory reap

.PHONY: restart
restart: ## 重啟 runtime 與 controller
	$(COMPOSE) restart hermes-runtime hermes-controller

.PHONY: ps
ps: ## 顯示 stack 狀態，含動態沙箱
	@$(COMPOSE) ps
	@echo
	@echo "沙箱容器："
	@docker ps -a $(SANDBOX_FILTER) --format 'table {{.Names}}\t{{.Status}}\t{{.Label "hermes.task"}}' \
		| grep -v '^$$' || echo "  （無）"

.PHONY: logs
logs: ## 追蹤所有服務的 log
	$(COMPOSE) logs -f --tail=100

.PHONY: logs-controller
logs-controller: ## 只追蹤 controller 的 log
	$(COMPOSE) logs -f --tail=200 hermes-controller

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# 要種哪一份範本。examples/ 底下的檔名。
#     make seed-config                              # 地端 vLLM
#     make seed-config TEMPLATE=config.vertex.yaml  # Vertex AI / GCP
TEMPLATE ?= config.vllm.yaml

.PHONY: seed-config
seed-config: ## 把 examples/ 的設定範本種進資料卷（TEMPLATE= 選範本，FORCE=1 覆蓋）
	@# 用 runtime 映像當工具容器，不必額外拉 alpine。要求 make build 先跑過。
	@test -f "$(CURDIR)/examples/$(TEMPLATE)" || { \
		echo "錯誤：找不到範本 examples/$(TEMPLATE)。可用的有：" >&2; \
		ls -1 "$(CURDIR)"/examples/config.*.yaml 2>/dev/null | xargs -n1 basename | sed 's/^/      /' >&2; \
		exit 1; }
	@docker image inspect hermes-runtime:$(INSTANCE) >/dev/null 2>&1 || { \
		echo "錯誤：找不到映像 hermes-runtime:$(INSTANCE)，先執行 make build。" >&2; exit 1; }
	@# /opt/data 是「具名卷 hermes-$(INSTANCE)-data 的掛載點」，不是宿主機路徑。
	@# 在宿主機上找不到這個檔案是正常的 —— 要看內容用 make show-config。
	@docker run --rm \
		-v hermes-$(INSTANCE)-data:/opt/data \
		-v "$(CURDIR)/examples:/seed:ro" \
		-e FORCE='$(FORCE)' \
		-e TEMPLATE='$(TEMPLATE)' \
		-e VOLNAME='hermes-$(INSTANCE)-data' \
		--entrypoint sh hermes-runtime:$(INSTANCE) -c '\
			if [ -f /opt/data/config.yaml ] && [ -z "$$FORCE" ]; then \
				echo "卷 $$VOLNAME 裡的 /opt/data/config.yaml 已存在，未覆蓋。"; \
				echo "  看目前內容：make show-config"; \
				echo "  修改：      make edit-config"; \
				echo "  換成 $$TEMPLATE：make seed-config TEMPLATE=$$TEMPLATE FORCE=1（會先備份）"; \
			else \
				if [ -f /opt/data/config.yaml ]; then \
					bak="/opt/data/config.yaml.bak-$$(date -u +%Y%m%dT%H%M%SZ)"; \
					cp -p /opt/data/config.yaml "$$bak"; \
					echo "已備份舊檔到 $$bak"; \
				fi; \
				cp "/seed/$$TEMPLATE" /opt/data/config.yaml; \
				chown $(HERMES_UID):$(HERMES_GID) /opt/data/config.yaml; \
				chmod 640 /opt/data/config.yaml; \
				echo "已把 $$TEMPLATE 種入 /opt/data/config.yaml —— 請用 make edit-config 依實際情況修改。"; \
			fi'

.PHONY: edit-config
edit-config: ## 用你自己的編輯器改 config.yaml（stack 沒起來也能用）
	@# 為什麼不在容器裡開編輯器：上游映像裡一個都沒有 —— vi / vim / nano /
	@# ed 全都不存在，映像刻意做得瘦。舊版這裡寫 `compose exec ... vi`，
	@# 那有兩個問題：vi 根本找不到；而且首次啟動的順序是
	@# build → seed-config → up，走到這一步時 stack 通常還沒起來。
	@#
	@# 改成把檔案拉到宿主機、用宿主機的 $$EDITOR 編輯、再寫回卷裡。順帶
	@# 讓你用自己慣用的編輯器，不必忍受映像裡剛好有的那一個。
	@docker image inspect hermes-runtime:$(INSTANCE) >/dev/null 2>&1 || { \
		echo "錯誤：找不到映像 hermes-runtime:$(INSTANCE)，先執行 make build。" >&2; exit 1; }
	@ed="$${VISUAL:-$${EDITOR:-}}"; \
	if [ -z "$$ed" ]; then \
		for c in nano vim vi; do \
			command -v $$c >/dev/null 2>&1 && { ed=$$c; break; }; \
		done; \
	fi; \
	if [ -z "$$ed" ]; then \
		echo "錯誤：找不到編輯器。指定一個：EDITOR=nano make edit-config" >&2; exit 1; \
	fi; \
	tmp="$$(mktemp)"; trap 'rm -f "$$tmp"' EXIT INT TERM; chmod 600 "$$tmp"; \
	docker run --rm -v hermes-$(INSTANCE)-data:/opt/data:ro \
		--entrypoint sh hermes-runtime:$(INSTANCE) \
		-c 'cat /opt/data/config.yaml' > "$$tmp" 2>/dev/null || { \
		echo "錯誤：讀不到 /opt/data/config.yaml，先執行 make seed-config。" >&2; exit 1; }; \
	before="$$(cksum < "$$tmp")"; \
	$$ed "$$tmp" || { echo "編輯器結束於非 0，未寫回。" >&2; exit 1; }; \
	if [ "$$(cksum < "$$tmp")" = "$$before" ]; then \
		echo "內容沒有變更，未寫回。"; exit 0; \
	fi; \
	docker run --rm -i -v hermes-$(INSTANCE)-data:/opt/data \
		--entrypoint sh hermes-runtime:$(INSTANCE) -c '\
			cat > /opt/data/.config.yaml.new \
			&& chown $(HERMES_UID):$(HERMES_GID) /opt/data/.config.yaml.new \
			&& chmod 640 /opt/data/.config.yaml.new \
			&& mv /opt/data/.config.yaml.new /opt/data/config.yaml' < "$$tmp" || { \
		echo "錯誤：寫回失敗。原本的 config.yaml 沒有被動到。" >&2; exit 1; }; \
	echo "已更新 /opt/data/config.yaml。"; \
	if [ -n "$$($(COMPOSE) ps -q hermes-runtime 2>/dev/null)" ]; then \
		echo "重啟以套用：make restart"; \
	else \
		echo "接下來：make up"; \
	fi

.PHONY: show-config
show-config: ## 印出目前的 config.yaml
	@docker run --rm -v hermes-$(INSTANCE)-data:/opt/data:ro \
		--entrypoint sh hermes-runtime:$(INSTANCE) -c 'cat /opt/data/config.yaml'

# ---------------------------------------------------------------------------
# 進化
# ---------------------------------------------------------------------------

.PHONY: evolve
evolve: ## 提交進化任務。用法：make evolve REQ=examples/evolve.hello-skill.json
	@test -n "$(REQ)" || { echo "用法：make evolve REQ=<request.json>"; exit 1; }
	@test -f "$(REQ)" || { echo "錯誤：找不到 $(REQ)"; exit 1; }
	@$(COMPOSE) exec -T hermes-controller \
		curl -sS -X POST http://127.0.0.1:9200/evolve \
			-H 'Content-Type: application/json' --data-binary @- < "$(REQ)"
	@echo

.PHONY: tasks
tasks: ## 列出進化任務
	@$(COMPOSE) exec -T hermes-controller curl -sS http://127.0.0.1:9200/tasks
	@echo

.PHONY: task
task: ## 查詢單一任務。用法：make task ID=evo-...
	@test -n "$(ID)" || { echo "用法：make task ID=<task-id>"; exit 1; }
	@$(COMPOSE) exec -T hermes-controller curl -sS http://127.0.0.1:9200/tasks/$(ID)
	@echo

.PHONY: skills
skills: ## 列出線上技能與可用版本
	@$(COMPOSE) exec -T hermes-controller curl -sS http://127.0.0.1:9200/skills
	@echo

.PHONY: rollback
rollback: ## 回滾技能。用法：make rollback SKILL=foo VERSION=foo-20260727T...
	@test -n "$(SKILL)" -a -n "$(VERSION)" || { \
		echo "用法：make rollback SKILL=<name> VERSION=<version>"; exit 1; }
	@$(COMPOSE) exec -T hermes-controller \
		curl -sS -X POST http://127.0.0.1:9200/skills/$(SKILL)/rollback \
			-H 'Content-Type: application/json' -d '{"version":"$(VERSION)"}'
	@echo

.PHONY: status
status: ## controller 的完整狀態
	@$(COMPOSE) exec -T hermes-controller curl -sS http://127.0.0.1:9200/status
	@echo

.PHONY: reap
reap: ## 強制移除這個實例殘留的沙箱容器
	@ids=$$(docker ps -aq $(SANDBOX_FILTER)); \
	if [ -n "$$ids" ]; then \
		echo "移除殘留沙箱：$$ids"; \
		docker rm -f $$ids; \
	else \
		echo "沒有殘留的沙箱容器。"; \
	fi

# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------

.PHONY: test
test: ## 跑 controller 的單元測試（scanner / promote / sandbox 設定）
	@# 原始碼以 :ro 掛進去 —— 測試不該有能力改到自己。連帶要關掉 pytest 的
	@# 快取外掛，否則它會為了寫不進 .pytest_cache 而印一段無關的警告。
	docker run --rm -v "$(CURDIR)/controller:/opt/controller:ro" \
		-e PYTHONPATH=/opt/controller -w /opt/controller \
		--entrypoint sh hermes-controller:$(INSTANCE) \
		-c 'pip install --quiet --no-cache-dir pytest \
		    && python -m pytest tests -q -p no:cacheprovider'

.PHONY: verify
verify: ## 對執行中的 stack 跑安全與拓撲檢查（FULL=1 會額外跑一次真的進化任務）
	@FULL=$(if $(FULL),$(FULL),0) bash scripts/verify.sh

.PHONY: config
config: ## 印出解析後的 compose 設定
	$(COMPOSE) config
