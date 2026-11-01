#!/bin/bash
set -e

echo "=== 更新 IPTSD 触控采样灵敏度 (/etc/iptsd.conf) ==="
# 调低触控激活阈值：从默认 24 降到 16，轻点也能瞬时响应；释放阈值降到 12
sed -i 's/^# *ActivationThreshold = .*/ActivationThreshold = 16/' /etc/iptsd.conf
sed -i 's/^# *DeactivationThreshold = .*/DeactivationThreshold = 12/' /etc/iptsd.conf
# 降低微小移动被判定的门槛：提升跟手性
sed -i 's/^# *PositionThresholdMin = .*/PositionThresholdMin = 0.02/' /etc/iptsd.conf

systemctl restart system-iptsd.slice || true
echo " -> IPTSD 触控灵敏度已提升并重新加载！"
