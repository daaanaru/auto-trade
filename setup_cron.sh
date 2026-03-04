#!/bin/zsh
#
# setup_cron.sh — Mac mini M4 24時間監視のセットアップ
#
# auto-tradeの全監視スクリプトをlaunchd plistとして登録する。
# macOSではcronよりlaunchdが推奨されるため、plist方式を採用。
#
# 登録されるジョブ:
#   1. crypto-monitor       毎時0分: BTC/JPY監視+ペーパートレード
#   2. crypto-full-report   毎日9:00: フルレポート（LLM分析+卒業チェック）
#   3. signal-monitor       毎日8:50: 日本株シグナル監視（東証寄り付き前）
#   4. paper-trade          毎日9:00: ペーパートレード単独実行
#
# 使い方:
#   ./setup_cron.sh install              # plist生成+登録
#   ./setup_cron.sh uninstall            # 全plist解除+削除
#   ./setup_cron.sh status               # 登録状況確認
#   ./setup_cron.sh install /custom/path # パス指定
#

set -euo pipefail

# ==============================================================
# 設定
# ==============================================================

AUTO_TRADE_DIR="${2:-$(cd "$(dirname "$0")" && pwd)}"
PYTHON="/usr/bin/python3"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$AUTO_TRADE_DIR/logs"
PREFIX="com.danaru.autotrade"

# ジョブ名一覧
JOB_NAMES=("crypto-monitor" "crypto-full-report" "signal-monitor" "paper-trade")

# ==============================================================
# ヘルパー
# ==============================================================

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*"; }
error() { echo "[ERROR] $*" >&2; }

check_prerequisites() {
    if [ ! -x "$PYTHON" ]; then
        error "Python not found at $PYTHON"
        exit 1
    fi
    if [ ! -f "$AUTO_TRADE_DIR/crypto_monitor.py" ]; then
        error "crypto_monitor.py not found in $AUTO_TRADE_DIR"
        error "Usage: setup_cron.sh install [/path/to/auto-trade]"
        exit 1
    fi
    mkdir -p "$LAUNCH_AGENTS_DIR"
    mkdir -p "$LOG_DIR"
}

is_loaded() {
    launchctl list 2>/dev/null | grep -q "$1" && return 0 || return 1
}

# ==============================================================
# plist生成
# ==============================================================

generate_plist() {
    local job_name="$1"
    local label="${PREFIX}.${job_name}"
    local plist_path="${LAUNCH_AGENTS_DIR}/${label}.plist"

    if [ -f "$plist_path" ]; then
        warn "Already exists: $(basename "$plist_path") (skipping)"
        return 1
    fi

    local args_xml=""
    local schedule_xml=""
    local log_name="$job_name"

    case "$job_name" in
        crypto-monitor)
            args_xml="        <string>$PYTHON</string>
        <string>$AUTO_TRADE_DIR/crypto_monitor.py</string>"
            # 毎時0分
            schedule_xml="    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>0</integer>
    </dict>"
            ;;
        crypto-full-report)
            args_xml="        <string>$PYTHON</string>
        <string>$AUTO_TRADE_DIR/crypto_monitor.py</string>
        <string>--full</string>"
            # 毎日9:00
            schedule_xml="    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>"
            ;;
        signal-monitor)
            args_xml="        <string>$PYTHON</string>
        <string>$AUTO_TRADE_DIR/signal_monitor.py</string>
        <string>--watchlist</string>"
            # 毎朝8:50（東証寄り付き前）
            schedule_xml="    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>50</integer>
    </dict>"
            ;;
        paper-trade)
            args_xml="        <string>$PYTHON</string>
        <string>$AUTO_TRADE_DIR/paper_trade.py</string>"
            # 毎日9:00
            schedule_xml="    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>"
            ;;
    esac

    cat > "$plist_path" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
$args_xml
    </array>

    <key>WorkingDirectory</key>
    <string>$AUTO_TRADE_DIR</string>

    $schedule_xml

    <key>StandardOutPath</key>
    <string>$LOG_DIR/${log_name}.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/${log_name}_error.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLISTEOF
    return 0
}

# ==============================================================
# コマンド: install
# ==============================================================

cmd_install() {
    info "Installing auto-trade launchd jobs..."
    info "Auto-trade dir: $AUTO_TRADE_DIR"
    info "Log dir: $LOG_DIR"
    echo

    check_prerequisites

    local installed=0
    local descriptions=(
        "crypto-monitor:every hour at :00"
        "crypto-full-report:daily at 09:00 (LLM report)"
        "signal-monitor:daily at 08:50 (JP stocks)"
        "paper-trade:daily at 09:00 (BTC/JPY)"
    )

    for entry in "${descriptions[@]}"; do
        local job_name="${entry%%:*}"
        local desc="${entry#*:}"
        local plist_path="${LAUNCH_AGENTS_DIR}/${PREFIX}.${job_name}.plist"
        if generate_plist "$job_name"; then
            launchctl load "$plist_path"
            info "Installed: $job_name ($desc)"
            installed=$((installed + 1))
        fi
    done

    echo
    if [ $installed -gt 0 ]; then
        info "$installed job(s) installed successfully."
    else
        info "No new jobs installed (all already exist)."
    fi
    echo
    cmd_status
}

# ==============================================================
# コマンド: uninstall
# ==============================================================

cmd_uninstall() {
    info "Uninstalling auto-trade launchd jobs..."
    echo

    local removed=0
    for job_name in "${JOB_NAMES[@]}"; do
        local plist_path="${LAUNCH_AGENTS_DIR}/${PREFIX}.${job_name}.plist"

        if [ -f "$plist_path" ]; then
            launchctl unload "$plist_path" 2>/dev/null || true
            rm "$plist_path"
            info "Removed: $job_name"
            removed=$((removed + 1))
        fi
    done

    echo
    if [ $removed -gt 0 ]; then
        info "$removed job(s) removed."
    else
        info "No jobs found to remove."
    fi
}

# ==============================================================
# コマンド: status
# ==============================================================

cmd_status() {
    echo "=========================================="
    echo "  AUTO-TRADE LAUNCHD STATUS"
    echo "=========================================="

    local schedules=(
        "crypto-monitor:hourly at :00"
        "crypto-full-report:daily 09:00"
        "signal-monitor:daily 08:50"
        "paper-trade:daily 09:00"
    )

    for entry in "${schedules[@]}"; do
        local job_name="${entry%%:*}"
        local sched="${entry#*:}"
        local label="${PREFIX}.${job_name}"
        local plist_path="${LAUNCH_AGENTS_DIR}/${label}.plist"
        local job_status="NOT INSTALLED"

        if [ -f "$plist_path" ]; then
            if is_loaded "$label"; then
                job_status="ACTIVE"
            else
                job_status="INSTALLED (not loaded)"
            fi
        fi

        printf "  %-24s %-14s %s\n" "$job_name" "[$job_status]" "$sched"
    done

    echo "=========================================="
    echo "  Log dir: $LOG_DIR/"
    echo "=========================================="
    echo

    # ログファイルの最終更新を表示
    if [ -d "$LOG_DIR" ] && ls "$LOG_DIR"/*.log 1>/dev/null 2>&1; then
        echo "  Recent log activity:"
        for f in $(ls -t "$LOG_DIR"/*.log 2>/dev/null | head -4); do
            local mod
            mod=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$f" 2>/dev/null || echo "unknown")
            local size
            size=$(wc -c < "$f" 2>/dev/null | tr -d ' ')
            printf "    %-35s %s (%s bytes)\n" "$(basename "$f")" "$mod" "$size"
        done
        echo
    fi
}

# ==============================================================
# メイン
# ==============================================================

show_usage() {
    cat << EOF
Usage: setup_cron.sh {install|uninstall|status} [/path/to/auto-trade]

Commands:
  install     Generate launchd plist files and register them
  uninstall   Unload and remove all auto-trade plist files
  status      Show registration status of all jobs

Options:
  /path/to/auto-trade  Override the auto-trade directory
                       (default: directory containing this script)

Jobs:
  crypto-monitor       Every hour at :00     BTC/JPY monitoring + paper trade
  crypto-full-report   Daily at 09:00        LLM analysis + graduation check
  signal-monitor       Daily at 08:50        JP stock signals (pre-market)
  paper-trade          Daily at 09:00        BTC/JPY paper trade execution
EOF
}

case "${1:-}" in
    install|--install)     cmd_install ;;
    uninstall|--uninstall) cmd_uninstall ;;
    status|--status)       cmd_status ;;
    *)                     show_usage; exit 1 ;;
esac
