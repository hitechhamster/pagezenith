#!/usr/bin/env bash
# PageZenith 部署：拉代码 → 等正在跑的任务归零 → 重启。
# 为什么要等：uvicorn 收到 stop 会立刻停止监听、然后等在跑的文章跑完（可长达几分钟），
# 期间所有新请求 502。2026-09-05 一次部署正好砸在用户写正文中间，润色请求就这么 502 了。
# 用法（服务器上）：bash /srv/pagezenith/ops/deploy.sh        # 最多等 10 分钟
#                  bash /srv/pagezenith/ops/deploy.sh --now   # 不等，立刻重启
set -euo pipefail
cd /srv/pagezenith
git pull -q --ff-only
echo "代码已更新到 $(git log --oneline -1)"

if [[ "${1:-}" != "--now" ]]; then
  deadline=$(( $(date +%s) + 600 ))
  while :; do
    n=$(curl -s --max-time 3 http://127.0.0.1:8012/api/ops/inflight | sed -n 's/.*"inflight": *\([0-9]*\).*/\1/p')
    n=${n:-0}
    [[ "$n" == "0" ]] && break
    if (( $(date +%s) > deadline )); then echo "等了 10 分钟还有 $n 个任务在跑，照常重启"; break; fi
    echo "还有 $n 个任务在跑，30 秒后再看…"; sleep 30
  done
fi

sudo systemctl restart pagezenith
for i in $(seq 1 20); do
  sleep 1
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8012/api/seo-writer/health || true)
  [[ "$code" == "200" ]] && { echo "重启完成，health $code（${i}s）"; exit 0; }
done
echo "重启后 20 秒 health 仍不是 200，去看 journalctl -u pagezenith" >&2; exit 1
