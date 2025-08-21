# 智能流量控制脚本

**智能流量控制脚本** 是一个用于监控和管理VPS/服务器流量使用的自动化工具。它能够在流量接近配额时自动限速，在流量即将耗尽时执行熔断保护，有效防止超额使用导致的额外费用或服务中断。

## 🚀 功能亮点

- **实时流量监控**：精确统计多网络接口流量使用情况
- **动态智能限速**：在流量达到阈值时自动调整带宽
- **熔断保护**：流量耗尽前自动阻断网络连接
- **多接口支持**：自动检测并管理所有网络接口
- **Telegram通知**：关键事件实时推送通知
- **每日报告**：自动生成详细流量使用报告
- **状态持久化**：重启后自动恢复控制状态
- **自定义计费周期**：支持任意起始日的计费周期

## 🛠 工作原理

脚本通过以下方式实现智能流量控制：

1. **流量统计**：
   - 使用 `vnstat` 获取精确流量数据
   - 回退到 `sysfs` 实时统计作为备选方案
   - 支持出站或双向流量统计

2. **智能决策**：
   - 达到90%阈值时启用动态限速
   - 剩余流量≤1GB时执行网络熔断
   - 新计费周期自动重置计数器

3. **流量控制**：
   - 使用 `tc` (Traffic Control) 实现动态限速
   - 使用 `iptables` 或 `ip link down` 实现网络熔断
   - 支持IPv4和IPv6双栈环境

4. **通知系统**：
   - Telegram实时通知关键事件
   - 每日自动发送流量报告
   - 系统异常告警

## ⚙️ 安装与配置

### 依赖安装
```bash
sudo apt update
sudo apt install python3 python3-pip vnstat
sudo pip3 install requests
```

### 快速开始

1. 下载脚本：
```bash
sudo wget -O /root/traffic-control.py https://raw.githubusercontent.com/honorcnboy/traffic_control/main/traffic-control.py
```

2. 修改配置：
```python
===== 全局配置常量 =====
VPS_NAME = "My Production Server" # 服务器标识
TOTAL_QUOTA_GB = 2000 # 每月流量配额(GB)
TELEGRAM_ENABLED = True # 启用Telegram通知
TELEGRAM_BOT_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID = "your_chat_id"

... 其他配置 ...
```

3. 运行脚本：
```bash
sudo python3 /root/traffic-control.py
```
### 系统服务安装
```bash
sudo nano /etc/systemd/system/traffic-control.service
```
添加以下内容：
```ini
[Unit]
Description=Traffic Control Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/traffic-control.py
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```
启用并启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable traffic-control
sudo systemctl start traffic-control
```

## 📊 配置选项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `TOTAL_QUOTA_GB` | 1000 | 每月流量配额(GB) |
| `THRESHOLD_PERCENT` | 90 | 限速触发阈值(%) |
| `HARD_LIMIT_GB` | 1.0 | 熔断剩余流量(GB) |
| `CYCLE_START_DAY` | 1 | 计费周期起始日(1-31) |
| `TRAFFIC_DIRECTION` | "out" | 流量统计方向("out"或"both") |
| `USE_UTC_TIME` | True | 使用UTC时间 |
| `UPDATE_INTERVAL_SEC` | 300 | 检测间隔(秒) |
| `DAILY_REPORT_HOUR` | 8 | 每日报告时间(24小时制) |
| `TELEGRAM_ENABLED` | False | 启用Telegram通知 |

## 🛠 最佳配置策略
**1. 保守型（严格防护）**：
BUFFER_PERCENT = 2      # 2%缓冲
SAFETY_FACTOR = 0.97    # 97%安全系数
HARD_LIMIT_GB = 1.0     # 1GB熔断

•​​适用​​：高流量费用环境
•效果​​：提前限速，避免接近熔断

**2. 平衡型（推荐）**：
BUFFER_PERCENT = 1      # 1%缓冲
SAFETY_FACTOR = 0.99    # 99%安全系数
HARD_LIMIT_GB = 0.5     # 0.5GB熔断
•适用​​：大多数场景
•效果​​：平衡安全性与可用性

**3. 宽松型（高利用率）**：
BUFFER_PERCENT = 0.5    # 0.5%缓冲
SAFETY_FACTOR = 0.995   # 99.5%安全系数
HARD_LIMIT_GB = 0.1     # 0.1GB熔断
•​​适用​​：流量费用低的场景
•效果​​：最大化流量利用率


## 🌟 核心优势

1. **精准控制**：
   - 动态计算安全余量
   - 基于剩余时间和流量智能调整限速值
   - 避免过早或过晚触发控制

2. **全面保护**：
   - 多级防护（限速+熔断）
   - 支持IPv4/IPv6双栈
   - 自动处理接口变化

3. **灵活配置**：
   - 自定义计费周期
   - 可调安全参数
   - 支持多种通知方式

4. **健壮可靠**：
   - 状态持久化
   - 错误处理和指数退避
   - 连续失败保护机制

5. **低资源占用**：
   - 高效算法设计
   - 轻量级实现
   - 最小化系统影响

## 📈 使用场景

- **云服务器管理**：防止突发流量导致超额费用
- **VPS流量监控**：精确控制月流量使用
- **网络中转服务器**：避免中转流量超标
- **家庭服务器**：管理有限带宽资源
- **IoT设备**：控制联网设备流量消耗

## 🧪 测试与验证
```bash
测试Telegram通知
sudo python3 traffic-control.py --test-telegram

重置所有限制
sudo python3 traffic-control.py --force-reset

查看实时日志
sudo tail -f /var/log/traffic-control.log
```

## 🤝 贡献指南

我们欢迎任何形式的贡献！请遵循以下步骤：

1. Fork 项目仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开 Pull Request


## ✉️ 联系方式

如有任何问题或建议，请联系：
- GitHub Issues：[https://github.com/honorcnboy/traffic_control/issues](https://github.com/honorcnboy/traffic_control/issues)
---

**让流量控制更智能，让服务器管理更轻松！** 🚀
