#!/usr/bin/env python3
"""
智能流量控制脚本 - 最终完整版
功能：
1. 多接口流量监控
2. 动态限速（基于总流量）
3. 网络熔断（剩余流量过低）
4. 自定义计费周期
5. Telegram通知
6. 每日流量报告
7. 状态持久化
8. 错误处理和健壮性设计
"""

import os
import sys
import json
import time
import math
import re
import shutil
import logging
import argparse
import calendar
import requests
import threading
import subprocess
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# ===== 全局配置常量 =====
# 基础设置
VPS_NAME = "My VPS"              # VPS标识名称（重要！用于区分不同服务器）
DEBUG_MODE = False               # 调试模式开关

# 流量控制参数
TOTAL_QUOTA_GB = 1000            # 每月流量配额(GB)
THRESHOLD_PERCENT = 90           # 限速触发阈值(%)
BUFFER_PERCENT = 1               # 安全缓冲(%)
SAFETY_FACTOR = 0.99             # 额外安全因子
HARD_LIMIT_GB = 1.0              # 熔断剩余流量(GB)
UPDATE_INTERVAL_SEC = 300         # 检测间隔(秒)
MIN_RATE_KBPS = 256               # 最低限速值(Kbps)
USE_IPTABLES = True              # 使用iptables替代ip down
CYCLE_START_DAY = 1              # 计费周期起始日(1-31)
TRAFFIC_DIRECTION = "out"        # 流量统计方向: "out"(仅出站), "both"(双向)
USE_UTC_TIME = True              # 使用UTC时间

# 通知间隔设置
EVENT_INTERVAL = 600             # 关键事件通知间隔(秒)
STATUS_INTERVAL = 3600           # 状态更新通知间隔(秒)

# 日志设置
LOG_FILE = "/var/log/traffic_control.log"  # 日志文件
LOG_MAX_BYTES = 100 * 1024 * 1024  # 100MB
LOG_BACKUP_COUNT = 3              # 保留3个备份

# 报告设置
DAILY_REPORT_HOUR = 8            # 每日报告时间(24小时制)

# 状态存储
STATE_DIR = "/etc/traffic_control"  # 状态存储目录

# Telegram通知设置
TELEGRAM_ENABLED = False          # 是否启用Telegram通知
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"  # Telegram机器人令牌
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"  # Telegram聊天ID

# 高级设置
VNSTAT_FAILURE_LIMIT = 5          # vnstat连续失败次数限制
SYSFS_FAILURE_LIMIT = 3           # sysfs连续失败次数限制
FAILURE_NOTIFY_INTERVAL = 86400   # 失败告警间隔(秒)
MAX_CONSECUTIVE_FAILURES = 10     # 最大连续失败次数

# ===== 脚本主体 =====
logger = None

def setup_logger():
    """初始化日志系统"""
    log = logging.getLogger('traffic-control')
    log.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    
    # 日志格式
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    
    # 文件日志（轮转）
    file_handler = RotatingFileHandler(
        LOG_FILE, 
        maxBytes=LOG_MAX_BYTES, 
        backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(formatter)
    
    # 控制台日志
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    log.addHandler(file_handler)
    log.addHandler(console_handler)
    return log

def generate_notification(title, message, interface=None):
    """生成格式化的通知消息"""
    notification = f"🚨 <b>[{VPS_NAME}] {title}</b>\n"
    if interface:
        notification += f"<b>接口:</b> {interface}\n"
    notification += message
    return notification

def send_telegram(message):
    """发送Telegram通知（统一使用线程）"""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    # 统一使用线程发送
    thread = threading.Thread(target=_send_telegram_request, args=(message,))
    thread.daemon = True
    thread.start()
    return True

def _send_telegram_request(message):
    """实际的Telegram请求发送函数"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception:
        return False

def get_all_interfaces():
    """获取所有非回环网络接口"""
    try:
        net_dir = "/sys/class/net"
        return [name for name in os.listdir(net_dir) 
                if name != "lo" and os.path.isdir(os.path.join(net_dir, name))]
    except Exception:
        return ["eth0"]  # 默认值

def get_default_interface():
    """自动检测默认网络接口"""
    interfaces = get_all_interfaces()
    
    try:
        # 查找默认路由的接口
        result = subprocess.run(
            ["ip", "route", "show", "default"], 
            capture_output=True, 
            text=True,
            check=False
        )
        
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                if "dev" in line:
                    parts = line.split()
                    iface = parts[parts.index("dev") + 1]
                    if iface in interfaces:
                        return iface
        
        # 返回第一个接口
        if interfaces:
            return interfaces[0]
    except Exception:
        pass
    
    return "eth0"  # 默认值

def get_interface_speed(interface):
    """获取网卡实际速率（统一返回Kbps）"""
    try:
        # 检查是否为虚拟接口
        if ":" in interface or interface.startswith("veth") or interface.startswith("venet"):
            return 1000000  # 虚拟接口默认1Gbps = 1,000,000 Kbps
        
        result = subprocess.run(
            ["ethtool", interface], 
            capture_output=True, 
            text=True,
            check=False
        )
        if result.returncode == 0:
            # 使用正则表达式匹配速度行
            pattern = r"Speed:\s*(\d+)\s*(Mb/s|Gb/s)"
            match = re.search(pattern, result.stdout)
            if match:
                speed_value = int(match.group(1))
                unit = match.group(2)
                
                if unit == "Mb/s":
                    return speed_value * 1000  # Mbps → Kbps
                elif unit == "Gb/s":
                    return speed_value * 1000000  # Gbps → Kbps
        return 1000000  # 默认1Gbps = 1,000,000 Kbps
    except Exception:
        return 1000000  # 默认1Gbps = 1,000,000 Kbps

def get_vnstat_data(interface):
    """使用vnstat获取本月流量数据"""
    try:
        cmd = ["vnstat", "--json", "m"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        data = json.loads(result.stdout)
        
        # 查找指定接口的本月数据
        for iface_data in data.get("interfaces", []):
            if iface_data["name"] == interface:
                for month_data in iface_data.get("traffic", {}).get("months", []):
                    current_time = datetime.utcnow() if USE_UTC_TIME else datetime.now()
                    if (month_data["date"]["year"] == current_time.year and 
                        month_data["date"]["month"] == current_time.month):
                        if TRAFFIC_DIRECTION == "both":
                            return (month_data["tx"] + month_data["rx"]) * 1024
                        else:
                            return month_data["tx"] * 1024
        return 0
    except Exception:
        return None

def get_sysfs_data(interface):
    """从sysfs获取实时流量数据"""
    try:
        tx_bytes = 0
        rx_bytes = 0
        
        with open(f"/sys/class/net/{interface}/statistics/tx_bytes", "r") as f:
            tx_bytes = int(f.read().strip())
        
        if TRAFFIC_DIRECTION == "both":
            with open(f"/sys/class/net/{interface}/statistics/rx_bytes", "r") as f:
                rx_bytes = int(f.read().strip())
            return tx_bytes + rx_bytes
        else:
            return tx_bytes
    except Exception:
        return None

def get_current_bytes(interface, state):
    """获取当前周期的流量数据"""
    # 优先使用vnstat
    if shutil.which("vnstat"):
        data = get_vnstat_data(interface)
        if data is not None:
            state["vnstat_failures"] = 0
            return data
        else:
            state["vnstat_failures"] = state.get("vnstat_failures", 0) + 1
    else:
        state["vnstat_failures"] = state.get("vnstat_failures", 0) + 1
    
    # 回退到sysfs
    data = get_sysfs_data(interface)
    if data is not None:
        state["sysfs_failures"] = 0
        return data
    else:
        state["sysfs_failures"] = state.get("sysfs_failures", 0) + 1
        return 0

def get_cycle_timestamps():
    """计算当前计费周期开始和结束时间戳"""
    now = datetime.utcnow() if USE_UTC_TIME else datetime.now()
    year = now.year
    month = now.month
    
    # 处理无效起始日（大于31或小于1）
    start_day = max(1, min(31, CYCLE_START_DAY))
    
    # 计算当前周期的开始日期
    if now.day >= start_day:
        # 本月开始
        try:
            start_date = datetime(year, month, start_day)
        except ValueError:
            # 如果日期无效（如2月30日），使用当月最后一天
            _, last_day = calendar.monthrange(year, month)
            start_date = datetime(year, month, last_day)
    else:
        # 上月开始
        prev_month = month - 1
        prev_year = year
        if prev_month == 0:
            prev_month = 12
            prev_year = year - 1
        
        try:
            start_date = datetime(prev_year, prev_month, start_day)
        except ValueError:
            # 如果日期无效（如2月30日），使用上月最后一天
            _, last_day = calendar.monthrange(prev_year, prev_month)
            start_date = datetime(prev_year, prev_month, last_day)
    
    # 计算结束日期（下个周期开始的前一天）
    next_start = start_date + timedelta(days=32)  # 确保进入下个月
    try:
        next_start = next_start.replace(day=start_day)
    except ValueError:
        # 处理无效日期
        _, last_day = calendar.monthrange(next_start.year, next_start.month)
        next_start = next_start.replace(day=last_day)
    
    end_date = next_start - timedelta(seconds=1)  # 23:59:59
    
    return {
        "start": int(start_date.timestamp()),
        "end": int(end_date.timestamp())
    }

def setup_tc(interface):
    """初始化tc流量控制结构"""
    try:
        subprocess.run(["tc", "qdisc", "del", "dev", interface, "root"], 
                       stderr=subprocess.DEVNULL,
                       check=False)
        
        subprocess.run([
            "tc", "qdisc", "add", "dev", interface, 
            "root", "handle", "1:", "htb"
        ], check=False)
        
        subprocess.run([
            "tc", "class", "add", "dev", interface, 
            "parent", "1:", "classid", "1:1", "htb", "rate", "1gbit", "ceil", "1gbit"
        ], check=False)
        
        subprocess.run([
            "tc", "filter", "add", "dev", interface, 
            "protocol", "ip", "parent", "1:", "prio", "1", 
            "u32", "match", "ip", "dst", "0.0.0.0/0", "flowid", "1:1"
        ], check=False)
        
        return True
    except subprocess.CalledProcessError:
        return False

def update_tc_limit(interface, rate_kbps):
    """更新tc限速值"""
    try:
        rate_str = f"{int(rate_kbps)}kbit"
        subprocess.run([
            "tc", "class", "change", "dev", interface,
            "parent", "1:", "classid", "1:1", "htb", 
            "rate", rate_str, "ceil", rate_str
        ], check=False)
        return True
    except subprocess.CalledProcessError:
        return False

def disable_tc_limit(interface):
    """禁用tc限速"""
    try:
        speed = get_interface_speed(interface)
        rate_str = f"{int(speed)}kbit"
        subprocess.run([
            "tc", "class", "change", "dev", interface,
            "parent", "1:", "classid", "1:1", "htb", 
            "rate", rate_str, "ceil", rate_str
        ], check=False)
        return True
    except subprocess.CalledProcessError:
        return False

def block_network(interface):
    """执行网络熔断"""
    if USE_IPTABLES:
        try:
            # 检查INPUT规则是否已存在
            check_input = subprocess.run(
                ["iptables", "-C", "INPUT", "-i", interface, "-j", "DROP"],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                check=False
            )
            if check_input.returncode != 0:
                # 规则不存在，添加新规则
                subprocess.run([
                    "iptables", "-A", "INPUT", "-i", interface, "-j", "DROP"
                ], check=False)
            
            # 检查OUTPUT规则是否已存在
            check_output = subprocess.run(
                ["iptables", "-C", "OUTPUT", "-o", interface, "-j", "DROP"],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                check=False
            )
            if check_output.returncode != 0:
                # 规则不存在，添加新规则
                subprocess.run([
                    "iptables", "-A", "OUTPUT", "-o", interface, "-j", "DROP"
                ], check=False)
            
            return True
        except Exception as e:
            logger.error(f"iptables blocking failed: {e}")
            return False
    else:
        try:
            logger.warning("Blocking network via ip link down - SSH may be lost!")
            subprocess.run(["ip", "link", "set", "dev", interface, "down"], check=False)
            return True
        except Exception as e:
            logger.error(f"Interface disable failed: {e}")
            return False

def unblock_network(interface):
    """恢复网络连接"""
    if USE_IPTABLES:
        try:
            # 删除INPUT规则
            while True:
                result = subprocess.run(
                    ["iptables", "-D", "INPUT", "-i", interface, "-j", "DROP"],
                    stderr=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    check=False
                )
                if result.returncode != 0:
                    break
            
            # 删除OUTPUT规则
            while True:
                result = subprocess.run(
                    ["iptables", "-D", "OUTPUT", "-o", interface, "-j", "DROP"],
                    stderr=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    check=False
                )
                if result.returncode != 0:
                    break
            return True
        except Exception as e:
            logger.error(f"iptables unblock failed: {e}")
            return False
    else:
        try:
            subprocess.run(["ip", "link", "set", "dev", interface, "up"], check=False)
            return True
        except Exception as e:
            logger.error(f"Interface enable failed: {e}")
            return False

def generate_report(gb_used, interface=None):
    """生成流量报告"""
    remaining_gb = TOTAL_QUOTA_GB - gb_used
    used_percent = (gb_used / TOTAL_QUOTA_GB) * 100
    
    # 获取周期信息
    cycle = get_cycle_timestamps()
    
    # 计算剩余时间
    current_time = int(time.time())
    time_remaining = max(0, cycle["end"] - current_time)
    
    # 使用timedelta格式化时间显示
    time_display = str(timedelta(seconds=time_remaining))
    
    # 计算平均日用量
    start_date = datetime.fromtimestamp(cycle["start"])
    now = datetime.utcnow() if USE_UTC_TIME else datetime.now()
    elapsed_days = (now - start_date).days + 1
    elapsed_days = max(1, elapsed_days)
    avg_daily = gb_used / elapsed_days if elapsed_days > 0 else 0
    
    # 计算剩余流量日平均值
    daily_avg_needed = remaining_gb / (time_remaining / 86400 or 1)
    
    # 格式化数据
    report = f"<b>已用流量:</b> {gb_used:.2f} GB ({used_percent:.1f}%)\n"
    report += f"<b>剩余流量:</b> {remaining_gb:.2f} GB\n\n"
    report += f"<b>剩余时间:</b> {time_display}\n"
    report += f"<b>日均用量:</b> {avg_daily:.2f} GB/day\n"
    report += f"<b>可用日均:</b> {daily_avg_needed:.2f} GB/day\n\n"
    report += f"<b>安全阈值:</b> {HARD_LIMIT_GB} GB"
    
    return report

def should_send_daily_report(state):
    """检查是否应该发送每日报告"""
    if USE_UTC_TIME:
        now = datetime.utcnow()
    else:
        now = datetime.now()
    
    current_hour = now.hour
    last_report = state.get("last_daily_report", 0)
    time_diff = time.time() - last_report
    
    # 检查是否在报告时间
    if current_hour == DAILY_REPORT_HOUR:
        if time_diff > 23 * 3600:
            return True
    elif time_diff > 23 * 3600 and current_hour >= DAILY_REPORT_HOUR:
        return True
    
    return False

def load_state():
    """加载保存的状态"""
    state_file = os.path.join(STATE_DIR, "state.json")
    default_state = {
        "version": 1,
        "cycle_start_ts": 0,
        "is_limiting": False,
        "is_blocked": False,
        "last_rate_kbps": 0,
        "last_daily_report": 0,
        "last_event_notification": 0,
        "last_status_notification": 0,
        "last_failure_notification": 0,
        "detected_interfaces": [],
        "vnstat_failures": 0,
        "sysfs_failures": 0
    }
    
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except Exception:
            return default_state
    return default_state

def save_state(state):
    """保存当前状态"""
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, "state.json")
    
    try:
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"State save failed: {e}")

def main():
    global logger
    logger = setup_logger()
    
    logger.info("===== Starting Traffic Control Script =====")
    logger.info(f"VPS Name: {VPS_NAME}")
    logger.info(f"Total Quota: {TOTAL_QUOTA_GB}GB")
    logger.info(f"Threshold: {THRESHOLD_PERCENT}%")
    logger.info(f"Cycle Start Day: {CYCLE_START_DAY}")
    logger.info(f"Traffic Direction: {TRAFFIC_DIRECTION}")
    logger.info(f"Use UTC Time: {USE_UTC_TIME}")
    
    # 初始化状态
    state = load_state()
    
    # 确保状态目录存在
    os.makedirs(STATE_DIR, exist_ok=True)
    
    # 检测默认网络接口
    main_interface = get_default_interface()
    logger.info(f"Detected main interface: {main_interface}")
    
    # 初始化接口列表
    if not state["detected_interfaces"]:
        state["detected_interfaces"] = [main_interface]
    
    # 初始化tc
    tc_initialized = {}
    for interface in state["detected_interfaces"]:
        if shutil.which("tc"):
            if setup_tc(interface):
                tc_initialized[interface] = True
                logger.info(f"Traffic control initialized for {interface}")
            else:
                tc_initialized[interface] = False
                logger.warning(f"Failed to initialize TC for {interface}")
        else:
            tc_initialized[interface] = False
            logger.warning("tc command not found! Dynamic rate limiting disabled")
    
    # Telegram启动通知
    if TELEGRAM_ENABLED:
        title = "流量监控已启动"
        message = f"监控接口: {main_interface}\n总流量限额: {TOTAL_QUOTA_GB}GB"
        send_telegram(generate_notification(title, message))
    
    # 主循环
    consecutive_failures = 0
    
    while True:
        try:
            # 动态检测新接口
            current_interfaces = get_all_interfaces()
            for iface in current_interfaces:
                if iface not in state["detected_interfaces"]:
                    logger.info(f"Detected new interface: {iface}")
                    state["detected_interfaces"].append(iface)
                    
                    # 初始化新接口的tc
                    if shutil.which("tc"):
                        if setup_tc(iface):
                            tc_initialized[iface] = True
                            logger.info(f"Initialized TC for new interface {iface}")
                        else:
                            tc_initialized[iface] = False
                    else:
                        tc_initialized[iface] = False
            
            # 获取当前周期信息
            cycle = get_cycle_timestamps()
            current_time = int(time.time())
            time_remaining_sec = max(1, cycle["end"] - current_time)
            
            # 检查新周期开始
            if current_time > cycle["end"] or state["cycle_start_ts"] != cycle["start"]:
                logger.info("New billing cycle detected! Resetting...")
                
                # 解除限制
                for interface in state["detected_interfaces"]:
                    if state["is_blocked"]:
                        unblock_network(interface)
                    if state["is_limiting"] and tc_initialized.get(interface, False):
                        disable_tc_limit(interface)
                
                # 新周期通知
                if TELEGRAM_ENABLED:
                    title = "新计费周期开始"
                    message = f"周期起始日: {CYCLE_START_DAY}\n流量计数器已重置"
                    send_telegram(generate_notification(title, message))
                
                # 重置状态
                state = {
                    "version": state.get("version", 1),
                    "cycle_start_ts": cycle["start"],
                    "is_limiting": False,
                    "is_blocked": False,
                    "last_rate_kbps": 0,
                    "last_daily_report": state.get("last_daily_report", 0),
                    "last_event_notification": state.get("last_event_notification", 0),
                    "last_status_notification": state.get("last_status_notification", 0),
                    "last_failure_notification": state.get("last_failure_notification", 0),
                    "detected_interfaces": state["detected_interfaces"],
                    "vnstat_failures": 0,
                    "sysfs_failures": 0
                }
            
            # 每日报告
            if should_send_daily_report(state):
                total_gb_used = 0
                report_content = ""
                
                for interface in state["detected_interfaces"]:
                    bytes_used = get_current_bytes(interface, state)
                    gb_used = bytes_used / (1024 ** 3)
                    total_gb_used += gb_used
                    
                    if report_content:
                        report_content += "\n\n"
                    report_content += generate_report(gb_used, interface)
                
                if len(state["detected_interfaces"]) > 1:
                    report_content = f"<b>总用量:</b> {total_gb_used:.2f}GB\n" + report_content
                
                title = f"流量报告 - {datetime.utcnow().strftime('%Y-%m-%d') if USE_UTC_TIME else datetime.now().strftime('%Y-%m-%d')}"
                report_message = generate_notification(title, report_content)
                
                if send_telegram(report_message):
                    state["last_daily_report"] = time.time()
            
            # ===== 核心流量控制逻辑 =====
            total_bytes_used = 0
            per_iface_bytes = {}
            
            # 先获取所有接口流量
            for interface in state["detected_interfaces"]:
                bytes_used = get_current_bytes(interface, state)
                per_iface_bytes[interface] = bytes_used
                total_bytes_used += bytes_used
            
            total_gb_used = total_bytes_used / (1024 ** 3)
            used_percent = (total_gb_used / TOTAL_QUOTA_GB) * 100
            remaining_gb = TOTAL_QUOTA_GB - total_gb_used
            
            logger.info(f"[ALL] Usage: {used_percent:.2f}% ({total_gb_used:.2f}GB of {TOTAL_QUOTA_GB}GB)")
            
            # 熔断检查（按总量）
            if not state["is_blocked"] and remaining_gb <= HARD_LIMIT_GB:
                state["is_blocked"] = True
                # 对所有接口下发熔断规则
                for interface in state["detected_interfaces"]:
                    block_network(interface)
                title = "网络熔断激活(全局)"
                message = f"总已用: {total_gb_used:.2f}GB ({used_percent:.1f}%)\n"
                message += f"剩余流量: {remaining_gb:.2f}GB ≤ {HARD_LIMIT_GB}GB"
                send_telegram(generate_notification(title, message))
            
            # 如果处于熔断状态，跳过限速逻辑
            if state["is_blocked"]:
                save_state(state)
                time.sleep(UPDATE_INTERVAL_SEC)
                consecutive_failures = 0
                continue
            
            # 限速决策（按总量）
            if not state["is_limiting"] and used_percent >= THRESHOLD_PERCENT:
                state["is_limiting"] = True
                title = "流量超阈值(全局)"
                message = f"总已用: {total_gb_used:.2f}GB ({used_percent:.1f}%)\n"
                message += "已启用动态限速(所有接口)"
                send_telegram(generate_notification(title, message))
            
            # 执行/解除全局限速
            if state["is_limiting"]:
                # 计算安全余量（实际可用流量减去缓冲）
                safe_remaining_gb = max(0, remaining_gb - (TOTAL_QUOTA_GB * BUFFER_PERCENT / 100))
                safe_remaining_bytes = safe_remaining_gb * (1024 ** 3)
                
                # 计算目标速率 (bps)
                target_rate_bps = (safe_remaining_bytes * 8 * SAFETY_FACTOR) / time_remaining_sec
                target_rate_kbps = max(MIN_RATE_KBPS, target_rate_bps / 1000)
                
                logger.info(f"[ALL] Limiting: Remaining {safe_remaining_gb:.2f}GB safe, {time_remaining_sec/3600:.2f}h left → {target_rate_kbps:.2f}Kbps")
                
                # 下发到所有已初始化 tc 的接口
                for interface in state["detected_interfaces"]:
                    if tc_initialized.get(interface, False):
                        if update_tc_limit(interface, target_rate_kbps):
                            state["last_rate_kbps"] = target_rate_kbps
                        else:
                            logger.error(f"Failed to update TC limit for {interface}")
                
                # 如果使用百分比低于阈值，移除限速
                if used_percent < THRESHOLD_PERCENT:
                    for interface in state["detected_interfaces"]:
                        if tc_initialized.get(interface, False):
                            disable_tc_limit(interface)
                    state["is_limiting"] = False
                    title = "限速解除(全局)"
                    message = f"总已用: {total_gb_used:.2f}GB ({used_percent:.1f}%)\n"
                    message += "已低于阈值，移除限速"
                    send_telegram(generate_notification(title, message))
            else:
                # 非限速状态确保各接口恢复到物理速率
                for interface in state["detected_interfaces"]:
                    if tc_initialized.get(interface, False):
                        disable_tc_limit(interface)
            # ===== 核心流量控制逻辑结束 =====
            
            # 保存状态并等待
            save_state(state)
            time.sleep(UPDATE_INTERVAL_SEC)
            
            # 重置失败计数器
            consecutive_failures = 0
            
        except KeyboardInterrupt:
            logger.info("Script terminated by user")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            consecutive_failures += 1
            
            # 每3次失败发送一次通知
            if consecutive_failures % 3 == 0 and TELEGRAM_ENABLED:
                title = "脚本运行异常"
                message = f"连续失败次数: {consecutive_failures}\n错误信息: {str(e)}"
                send_telegram(generate_notification(title, message))
            
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                msg = generate_notification("严重错误", f"脚本连续{MAX_CONSECUTIVE_FAILURES}次失败，即将退出")
                send_telegram(msg)
                logger.critical("Too many consecutive failures, exiting")
                sys.exit(1)
            
            # 指数退避等待
            sleep_time = min(600, 2 ** consecutive_failures)
            time.sleep(sleep_time)

if __name__ == "__main__":
    # 检查root权限
    if os.getuid() != 0:
        print("ERROR: This script requires root privileges!")
        sys.exit(1)
    
    parser = argparse.ArgumentParser(description="高级流量控制系统")
    parser.add_argument("--force-reset", action="store_true", help="重置所有限制并恢复网络")
    parser.add_argument("--test-telegram", action="store_true", help="测试Telegram通知")
    args = parser.parse_args()
    
    if args.test_telegram:
        print("Sending Telegram test message...")
        if TELEGRAM_ENABLED:
            title = "流量监控测试通知"
            message = "This is a test message from the traffic control system."
            test_msg = generate_notification(title, message)
            if send_telegram(test_msg):
                print("Telegram notification sent successfully!")
            else:
                print("Failed to send Telegram notification.")
        else:
            print("Telegram notifications are disabled")
        sys.exit(0)
    
    if args.force_reset:
        print("Performing force reset...")
        if os.path.exists(STATE_DIR):
            try:
                shutil.rmtree(STATE_DIR)
                print("State directory removed")
            except Exception as e:
                print(f"Reset failed: {e}")
        
        # 恢复所有可能的接口
        possible_interfaces = ["eth0", "ens3", "enp0s3", "wlan0", "eth1"]
        for iface in possible_interfaces:
            if USE_IPTABLES:
                # 删除INPUT和OUTPUT规则
                while True:
                    result = subprocess.run(
                        ["iptables", "-D", "INPUT", "-i", iface, "-j", "DROP"],
                        stderr=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        check=False
                    )
                    if result.returncode != 0:
                        break
                    
                    result = subprocess.run(
                        ["iptables", "-D", "OUTPUT", "-o", iface, "-j", "DROP"],
                        stderr=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        check=False
                    )
                    if result.returncode != 0:
                        break
            else:
                subprocess.run(["ip", "link", "set", "dev", iface, "up"], 
                              stderr=subprocess.DEVNULL,
                              check=False)
            
            if shutil.which("tc"):
                subprocess.run(["tc", "qdisc", "del", "dev", iface, "root"], 
                              stderr=subprocess.DEVNULL,
                              check=False)
        
        print("Reset complete. Exiting.")
        sys.exit(0)
    
    main()
