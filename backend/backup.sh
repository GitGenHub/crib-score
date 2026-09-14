#!/usr/bin/env bash
set -euo pipefail

API_URL="http://127.0.0.1:8002/api/backups/create"
LOG_FILE="/opt/crib-score/backend/data/backup.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Creating backup..." | tee -a "$LOG_FILE"
RESPONSE=$(curl -s -X POST "$API_URL" --max-time 30)
echo "$RESPONSE" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
