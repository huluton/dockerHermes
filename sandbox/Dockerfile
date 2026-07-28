# syntax=docker/dockerfile:1
#
# hermes-sandbox — 短暫的進化任務容器。
#
# 每一次進化任務都會由 hermes-controller 從這個映像開一個全新容器，任務結束
# 後強制移除 —— 每次進化任務都是一個獨立容器，完成後不留痕跡。
#
# 這是整套系統裡「唯一」允許自由安裝套件的地方。Runtime 的 venv 是封存的，
# 候選技能要編譯 wheel、拉依賴、跑測試，全部發生在這裡；跑完連容器一起丟掉，
# 所以再髒也不會污染正式環境。
#
# python:3.13-slim 官方映像同時提供 linux/amd64 與 linux/arm64。
FROM python:3.13-slim

# build-essential 佔掉相當一部分映像體積（約 250MB），但沒有它，任何缺少
# 對應平台 wheel 的套件都會在 pip install 階段掛掉 — 在 ARM 上尤其常見，
# 而 ARM 正是本專案必須支援的架構之一。對一個目的就是安裝任意依賴的
# 沙箱來說，這個交換划算。
#
# git 是給會 clone 儲存庫的任務用的；ca-certificates 給 HTTPS。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# 非 root 使用者。UID 刻意選 10001，與 runtime 的 hermes（10000）錯開，這樣
# 沙箱寫出來的檔案在共用卷上一眼就能認出來源。
RUN useradd --create-home --uid 10001 --shell /bin/bash sandbox

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/sandbox \
    VIRTUAL_ENV=/home/sandbox/.venv \
    PATH=/home/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin

# 預先建好 venv 並裝入測試工具。任務通常需要跑候選技能的測試，先裝好 pytest
# 可以省下每個沙箱容器都重抓一次的時間；同時也代表「網路完全隔離」的任務
# 至少還有個能用的測試框架。
RUN python -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir pytest \
    && chown -R sandbox:sandbox /home/sandbox

# 任務交換目錄。controller 會把一個卷掛在這裡，寫入 task.json，任務結束後
# 讀回 result.json 與 out/ 底下的候選技能。
RUN mkdir -p /work /work/out && chown -R sandbox:sandbox /work

COPY --chmod=0755 entrypoint.sh /opt/sandbox/entrypoint.sh
COPY --chmod=0755 run_task.py   /opt/sandbox/run_task.py

USER sandbox
WORKDIR /work

# 沒有 HEALTHCHECK：這是個一次性的批次容器，不是常駐服務。它的健康狀態就是
# 退出碼，由 controller 負責收。
ENTRYPOINT ["/opt/sandbox/entrypoint.sh"]
