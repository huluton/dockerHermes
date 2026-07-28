#!/bin/sh
# hermes-sandbox 進入點。
#
# 刻意保持極薄：真正的工作在 run_task.py。這一層只負責在把控制權交給 Python
# 之前，先確認執行環境的前提條件成立，並在前提不成立時給出「人看得懂的錯誤」
# 而不是一個裸的 traceback — 沙箱容器跑完就被銷毀，事後沒得再進去看，所以
# 失敗訊息必須在當下就講清楚。
set -eu

WORK_DIR="${SANDBOX_WORK_DIR:-/work}"
TASK_FILE="${WORK_DIR}/task.json"

if [ ! -d "${WORK_DIR}" ]; then
    echo "sandbox: 工作目錄 ${WORK_DIR} 不存在 — controller 沒有掛載交換卷" >&2
    exit 78   # EX_CONFIG
fi

if [ ! -r "${TASK_FILE}" ]; then
    echo "sandbox: 讀不到 ${TASK_FILE} — controller 沒有寫入任務定義" >&2
    exit 78
fi

if [ ! -w "${WORK_DIR}" ]; then
    echo "sandbox: ${WORK_DIR} 不可寫（uid=$(id -u)）— 無法寫回 result.json" >&2
    exit 78
fi

mkdir -p "${WORK_DIR}/out"

# exec 讓 run_task.py 成為 PID 1，這樣 controller 送來的 SIGTERM 會直接到達
# 它（Python 端有註冊處理器做善後），而不是被一層 shell 吃掉。
exec python /opt/sandbox/run_task.py "${TASK_FILE}"
