#!/bin/sh
# hermes-controller 的進入點。
#
# 這支腳本以 root 起，只做一件 root 才做得到的事 —— 把具名卷的擁有者調整成
# 與 runtime 的 hermes 使用者相同 —— 然後立刻降權執行應用程式。
#
# --- 為什麼 UID 必須對齊 --------------------------------------------------
# 兩個技能卷同時掛在 controller 與 runtime 上（controller rw、runtime ro），
# 而且掛在完全相同的絕對路徑。Docker 具名卷只記錄數字 uid/gid，不管使用者
# 名稱。上游 hermes 映像的 hermes 使用者是 uid 10000（Dockerfile 裡的
# `useradd -u 10000`），所以 controller 晉升出來的檔案必須也是 10000 所有，
# 否則 runtime 讀不到自己被進化出來的技能。
#
# HERMES_UID / HERMES_GID 兩個變數存在，是為了讓上游哪天改了 uid 時，這裡
# 不用重建映像就能跟上。

set -eu

APP_UID="${HERMES_UID:-10000}"
APP_GID="${HERMES_GID:-10000}"
LIVE_SKILLS_DIR="${LIVE_SKILLS_DIR:-/opt/data/skills/evolved}"
SKILL_VERSIONS_DIR="${SKILL_VERSIONS_DIR:-/opt/data/skill-versions}"
STATE_DIR="${STATE_DIR:-/state}"
DEPS_DIR="${DEPS_DIR:-/deps}"
BIND_HOST="${CONTROLLER_HOST:-0.0.0.0}"
BIND_PORT="${CONTROLLER_PORT:-9200}"

log() {
    echo "[entrypoint] $*"
}

die() {
    echo "[entrypoint] 錯誤：$*" >&2
    exit 78  # EX_CONFIG
}

# --- 準備資料卷 -----------------------------------------------------------
#
# 只在 root 身分下做。有人用 `user:` 覆寫 compose 設定、或在 rootless Docker
# 下跑的時候，這一段會整段跳過 —— 那種情況下卷的擁有者本來就已經對了。
if [ "$(id -u)" = "0" ]; then
    for d in "${LIVE_SKILLS_DIR}" "${SKILL_VERSIONS_DIR}" "${STATE_DIR}" "${DEPS_DIR}"; do
        mkdir -p "${d}" || die "無法建立 ${d}"

        # 只有在擁有者不對的時候才 chown。技能卷累積數百個版本之後，
        # 每次重啟都無條件遞迴 chown 會拖慢啟動，而且完全沒有必要。
        current_uid="$(stat -c '%u' "${d}")"
        current_gid="$(stat -c '%g' "${d}")"
        if [ "${current_uid}" != "${APP_UID}" ] || [ "${current_gid}" != "${APP_GID}" ]; then
            log "調整 ${d} 擁有者 ${current_uid}:${current_gid} → ${APP_UID}:${APP_GID}"
            chown -R "${APP_UID}:${APP_GID}" "${d}" \
                || die "無法變更 ${d} 的擁有者。若這是 bind mount，請先在宿主機端把目錄擁有者設成 ${APP_UID}:${APP_GID}"
        fi
    done

    log "以 uid=${APP_UID} gid=${APP_GID} 降權執行"
    exec setpriv \
        --reuid="${APP_UID}" \
        --regid="${APP_GID}" \
        --clear-groups \
        --inh-caps=-all \
        --no-new-privs \
        uvicorn app.main:app \
            --host "${BIND_HOST}" \
            --port "${BIND_PORT}" \
            --no-server-header \
            --log-level "$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
fi

log "已經是非 root（uid=$(id -u)），跳過卷的擁有者調整"
exec uvicorn app.main:app \
    --host "${BIND_HOST}" \
    --port "${BIND_PORT}" \
    --no-server-header \
    --log-level "$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
