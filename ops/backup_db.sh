#!/usr/bin/env bash
# billing.db 每日备份。README 一直写着"部署时务必放持久盘并每日备份 —— 丢了等于所有卡密
# 余额归零"，但 2026-09-08 审计发现服务器上根本没有备份 cron。
#
# 用 sqlite3 的在线备份 API 而不是 cp：库开着 WAL，直接 cp 主文件会丢掉 -wal 里还没
# checkpoint 的写入（今天实测 -wal 有 4MB，比主文件的增量还大）。
# 保留 14 天；每天一份 1.7MB 量级，磁盘不是问题。
#
# 安装（服务器上，root）：
#   echo "0 3 * * * root bash /srv/pagezenith/ops/backup_db.sh >> /var/log/pagezenith-backup.log 2>&1" \
#     | sudo tee /etc/cron.d/pagezenith-backup
set -euo pipefail
SRC="${BILLING_DB:-/srv/pagezenith/data/billing.db}"
DST_DIR="/srv/pagezenith/backups"
KEEP_DAYS=14
mkdir -p "$DST_DIR"
DST="$DST_DIR/billing-$(date +%F).db"
/srv/pagezenith/.venv/bin/python - "$SRC" "$DST" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)          # 在线备份：连 -wal 里未 checkpoint 的写入一起带上
d.close(); s.close()
n = sqlite3.connect(dst).execute("select count(*) from cards").fetchone()[0]
print(f"backup ok -> {dst}  cards={n}")
PY
find "$DST_DIR" -name 'billing-*.db' -mtime +"$KEEP_DAYS" -delete
