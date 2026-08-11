# hermes-controller —— 進化生命週期管理。
#
# 這個服務是整套架構裡權限最高的一環：它能建立容器，也能把程式碼晉升到
# 正式環境的技能目錄。映像本身因此刻意保持極小 —— 沒有編譯器、沒有 git、
# 沒有任何進化任務會用到的工具。那些東西屬於 sandbox 映像。
#
# python:3.13-slim 的 amd64 與 arm64 皆已確認存在。

FROM python:3.13-slim

# 與上游 hermes 映像的 hermes 使用者對齊。跨容器共用具名卷時，Docker 只認
# 數字 uid —— 對不上的話，runtime 會讀不到 controller 晉升出來的技能。
ARG HERMES_UID=10000
ARG HERMES_GID=10000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/controller

# ca-certificates —— docker SDK 走 HTTP 連 socket-proxy，但保留憑證庫讓日後
#                    改用 TLS 端點時不必動映像。
# curl            —— 只給 HEALTHCHECK 用。
# util-linux      —— 提供 setpriv。Debian 的 base layer 通常已經有，這裡明確
#                    列出來，避免上游哪次瘦身把它拿掉導致進入點靜默失效。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

# 先裝相依，再複製程式碼 —— 改應用程式邏輯時不會讓 pip 層失效。
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# 建立應用程式使用者。實際降權由 entrypoint 在執行期用數字 uid 完成，所以
# 就算之後用 HERMES_UID 覆寫，這裡建的使用者名稱對不上也不影響運作 ——
# 建它主要是為了讓 `ps` 和錯誤訊息裡看得到有意義的名字。
RUN groupadd --gid "${HERMES_GID}" hermes \
    && useradd --uid "${HERMES_UID}" --gid "${HERMES_GID}" \
        --home-dir /opt/controller --no-create-home --shell /usr/sbin/nologin hermes

WORKDIR /opt/controller

COPY app/ /opt/controller/app/
COPY policy.yaml /opt/controller/policy.yaml
COPY --chmod=0755 entrypoint.sh /opt/controller/entrypoint.sh

# 掛載點。compose 會把具名卷蓋上來；先造出來是為了在沒掛卷的情況下
# （例如手動 `docker run` 除錯）也能正常啟動。
#
# 這兩個技能路徑必須與 runtime 容器裡的一模一樣 —— 線上目錄裡放的是指向
# 版本庫的 symlink，兩邊路徑不一致就會 dangling。
RUN mkdir -p /opt/data/skills/evolved /opt/data/skill-versions /state /deps \
    && chown -R "${HERMES_UID}:${HERMES_GID}" /opt/data /state /deps

# 這些預設值與 app/config.py 裡的一致。集中在 compose 覆寫，這裡只是讓映像
# 在沒有任何環境變數的情況下也能自我描述。
ENV CONTROLLER_HOST=0.0.0.0 \
    CONTROLLER_PORT=9200 \
    LIVE_SKILLS_DIR=/opt/data/skills/evolved \
    SKILL_VERSIONS_DIR=/opt/data/skill-versions \
    STATE_DIR=/state \
    DEPS_DIR=/deps \
    SCANNER_POLICY_FILE=/opt/controller/policy.yaml \
    DOCKER_HOST=tcp://docker-socket-proxy:2375

EXPOSE 9200

# /healthz 刻意不碰 Docker API —— 這樣 socket-proxy 掛掉時，healthcheck 反映的
# 是「controller 自己還活著」，而不是把整個依賴鏈的故障都算到它頭上。
# Docker 的可達性由 /readyz 負責。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9200/healthz >/dev/null || exit 1

# 以 root 進入 —— entrypoint 調整完卷的擁有者後會用 setpriv 自行降權。
# 這裡不設 USER 是刻意的，不是疏漏。
ENTRYPOINT ["/opt/controller/entrypoint.sh"]
