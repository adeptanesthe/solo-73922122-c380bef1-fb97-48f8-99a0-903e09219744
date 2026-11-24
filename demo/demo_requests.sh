#!/usr/bin/env bash
# 提花织带纹版修复 API 演示请求。
#
# 前置：
#   1. docker compose up --build -d   （或本地：uvicorn app.main:app --port 8000）
#   2. python demo/make_demo_data.py  （仓库已附带生成好的 PNG，可跳过）
#
# 用法：bash demo/demo_requests.sh [BASE_URL]
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${DEMO_DIR}/out"
mkdir -p "${OUT_DIR}"

echo "== 0. 健康检查 =="
curl -sS "${BASE_URL}/health"
echo

echo "== 1. 竖条纹残片（6×6 重复单元，期望已证最优）=="
create_resp=$(curl -sS -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_stripes.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_stripes.png;type=image/png" \
    -F "unit_w=6" -F "unit_h=6")
echo "${create_resp}" | python3 -m json.tool
draft_id=$(echo "${create_resp}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('draft_id',''))")
test -n "${draft_id}" || { echo "未获得草案编号，终止"; exit 1; }

echo "== 2. 查看草案 ${draft_id} =="
curl -sS "${BASE_URL}/api/drafts/${draft_id}" | python3 -m json.tool

echo "== 3. 在缺损处补录一个完好色点（格坐标 (4,4)=红）=="
curl -sS -X POST "${BASE_URL}/api/drafts/${draft_id}/points" \
    -F "cell_x=4" -F "cell_y=4" -F "color=220,60,60" | python3 -m json.tool

echo "== 4. 同格异色补录（期望 409 冲突，草案保留）=="
curl -sS -o "${OUT_DIR}/conflict.json" -w "HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts/${draft_id}/points" \
    -F "cell_x=4" -F "cell_y=4" -F "color=60,90,220"
cat "${OUT_DIR}/conflict.json" | python3 -m json.tool

echo "== 5. 领取正式纹版 PNG（已证最优才放行，响应头带纹版标识）=="
curl -sS -D "${OUT_DIR}/pattern_headers.txt" -o "${OUT_DIR}/pattern_${draft_id}.png" \
    "${BASE_URL}/api/drafts/${draft_id}/pattern.png?scale=8"
grep -i "X-Pattern-Id" "${OUT_DIR}/pattern_headers.txt"
pattern_id=$(grep -i "X-Pattern-Id" "${OUT_DIR}/pattern_headers.txt" \
    | sed 's/.*: //' | tr -d '\r')
test -n "${pattern_id}" || { echo "未拿到纹版标识，终止"; exit 1; }
file "${OUT_DIR}/pattern_${draft_id}.png" || true

echo "== 5b. 放大下载（scale=16）必须是同一纹版标识 =="
curl -sS -D - -o "${OUT_DIR}/pattern_${draft_id}_x16.png" \
    "${BASE_URL}/api/drafts/${draft_id}/pattern.png?scale=16" \
    | grep -i "X-Pattern-Id"

echo "== 5d. 为试织验收建一个 4×4 横向条纹草案（相邻纹版行异色）=="
trial_resp=$(curl -sS -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_hstripes.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_hstripes.png;type=image/png" \
    -F "unit_w=4" -F "unit_h=4")
echo "${trial_resp}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('状态:', d['status'], '草案:', d['draft_id'])"
trial_draft=$(echo "${trial_resp}" | python3 -c "import sys,json; print(json.load(sys.stdin)['draft_id'])")
trial_pid=$(curl -sS -D - -o /dev/null \
    "${BASE_URL}/api/drafts/${trial_draft}/pattern.png" \
    | grep -i "X-Pattern-Id" | sed 's/.*: //' | tr -d '\r')
echo "试织验收草案 #${trial_draft}，标识 ${trial_pid}"

echo "== 5e. 完美试织带验收（按纹版前两行织造，最低分 0，期望放行）=="
curl -sS -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${DEMO_DIR}/trial_perfect.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=0" -F "max_missing_weft=0" | python3 -m json.tool

echo "== 5f. 含透明缺损像素的试织带（透明像素被忽略，仍放行）=="
curl -sS -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${DEMO_DIR}/trial_with_hole.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=1" -F "max_missing_weft=1" | python3 -m json.tool

echo "== 5g. 重纬试织带：零容忍上限期望返工，放宽上限后期望放行 =="
curl -sS -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${DEMO_DIR}/trial_double_weft.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=0" -F "max_missing_weft=0" | python3 -m json.tool
curl -sS -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${DEMO_DIR}/trial_double_weft.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=2" -F "max_missing_weft=2" | python3 -m json.tool

echo "== 5h. 现场生成复查场景试织带（同一夹具、不同上限演示三种判定）=="
python3 "${DEMO_DIR}/make_trial_png.py" "${BASE_URL}" "${trial_draft}" "${OUT_DIR}"
for limits in "1 1" "1 0" "0 1" "0 0"; do
    set -- ${limits}
    echo "-- 上限 重纬=$1 漏纬=$2 --"
    curl -sS -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
        -F "trial=@${OUT_DIR}/trial_review.png;type=image/png" \
        -F "pattern_id=${trial_pid}" \
        -F "max_double_weft=$1" -F "max_missing_weft=$2" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['verdict'], d['verdict_label'], 'min=', d['min_total_score'])"
done

echo "== 5i. 非法试织图（全透明/宽度不符/负上限）期望 400 =="
python3 - "${OUT_DIR}" <<'PY'
import sys
from pathlib import Path
from PIL import Image
out = Path(sys.argv[1])
Image.new("RGBA", (4, 2), (0, 0, 0, 0)).save(out / "all_transparent.png")
Image.new("RGBA", (3, 2), (220, 60, 60, 255)).save(out / "wrong_width.png")
PY
curl -sS -o "${OUT_DIR}/transparent.json" -w "全透明 HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${OUT_DIR}/all_transparent.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=0" -F "max_missing_weft=0"
curl -sS -o "${OUT_DIR}/wrong_width.json" -w "宽度不符 HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${OUT_DIR}/wrong_width.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=0" -F "max_missing_weft=0"
curl -sS -o "${OUT_DIR}/negative.json" -w "负上限 HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts/${trial_draft}/acceptance" \
    -F "trial=@${DEMO_DIR}/trial_perfect.png;type=image/png" \
    -F "pattern_id=${trial_pid}" \
    -F "max_double_weft=-1" -F "max_missing_weft=0"
cat "${OUT_DIR}/negative.json" | python3 -m json.tool

echo "== 6. 初始残片同格异色（期望 409，返回两处原坐标）=="
curl -sS -o "${OUT_DIR}/initial_conflict.json" -w "HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_conflict.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_conflict.png;type=image/png" \
    -F "unit_w=4" -F "unit_h=4"
cat "${OUT_DIR}/initial_conflict.json" | python3 -m json.tool

echo "== 7. 棋盘格残片（4×4 单元）=="
curl -sS -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_checker.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_checker.png;type=image/png" \
    -F "unit_w=4" -F "unit_h=4" | python3 -m json.tool

echo
echo "演示完成，输出目录：${OUT_DIR}"
