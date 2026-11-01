#!/bin/bash
set -e

echo "=== 1. 修复 Windows Hello 红外人脸识别 (Howdy + ov7251 ALS 毫勒克斯单位 Bug) ==="
if [ -f /usr/local/bin/ir_bridge_run.sh ]; then
    cp /usr/local/bin/ir_bridge_run.sh /usr/local/bin/ir_bridge_run.sh.bak
    cat > /usr/local/bin/ir_bridge_run.sh << 'EOF'
#!/bin/bash
export PATH=/usr/local/bin:/usr/bin:/bin

# Surface Pro 6 环境光传感器 (in_illuminance_raw) 的 scale 为 0.001 (单位：毫勒克斯 milli-lux)
# 20 lux = 20000, 400 lux (室内正常照明) = 400000
ALS=$(cat /sys/bus/iio/devices/iio:device*/in_illuminance_raw 2>/dev/null | head -1)
ALS=${ALS:-50000}
if [ "$ALS" -lt 25000 ]; then
	GAIN=128; EXP=1500
elif [ "$ALS" -lt 450000 ]; then
	GAIN=108; EXP=1320
else
	GAIN=48;  EXP=750
fi

for dev in /dev/media*; do
	if media-ctl -d "$dev" -p 2>/dev/null | grep -q "ov7251"; then
		media-ctl -d "$dev" -l '"ov7251 3-0060":0->"ipu3-csi2 2":0[1]' 2>/dev/null
		media-ctl -d "$dev" -V '"ipu3-csi2 2":0 [fmt:Y10_1X10/640x480 field:none]' 2>/dev/null
		media-ctl -d "$dev" -V '"ipu3-csi2 2":1 [fmt:Y10_1X10/640x480 field:none]' 2>/dev/null
		for s in /sys/class/video4linux/v4l-subdev*; do
			if grep -q "ov7251" "$s/name" 2>/dev/null; then
				v4l2-ctl -d "/dev/$(basename "$s")" --set-ctrl exposure=$EXP,analogue_gain=$GAIN 2>/dev/null
				break
			fi
		done
		V=$(media-ctl -d "$dev" -e "ipu3-cio2 2")
		v4l2-ctl -d /dev/video42 -c keep_format=1 2>/dev/null || true
		exec /usr/local/bin/ir_bridge "$V"
	fi
done
exit 1
EOF
    chmod 755 /usr/local/bin/ir_bridge_run.sh
    echo " -> 已更新 /usr/local/bin/ir_bridge_run.sh"
fi

if [ -f /usr/local/etc/howdy/config.ini ]; then
    sed -i 's/^dark_threshold = .*/dark_threshold = 95/' /usr/local/etc/howdy/config.ini
    echo " -> 已放宽 /usr/local/etc/howdy/config.ini dark_threshold = 95"
fi

echo "=== 2. 启用 IPTSD 底层手掌防误触与笔尖倾角补偿 (/etc/iptsd.conf) ==="
if [ -f /etc/iptsd.conf ]; then
    cp /etc/iptsd.conf /etc/iptsd.conf.bak
    sed -i 's/^# *DisableOnPalm = false/DisableOnPalm = true/g' /etc/iptsd.conf
    sed -i 's/^# *DisableOnStylus = false/DisableOnStylus = true/g' /etc/iptsd.conf
    sed -i 's/^# *TipDistance = 0/TipDistance = 0.25/g' /etc/iptsd.conf
    systemctl restart system-iptsd.slice || true
    echo " -> 已更新 /etc/iptsd.conf 并重启 iptsd 服务"
fi

echo "=== 3. 禁用内置 USB3.0 MicroSD 读卡器 (045e:0306) 自动休眠防掉盘 ==="
cat > /etc/udev/rules.d/99-surface-sd-reader.rules << 'EOF'
# Keep Surface Pro 6 built-in USB3.0 MicroSD Card Reader (045e:0306) awake when card is inserted
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="045e", ATTR{idProduct}=="0306", ATTR{power/control}="on"
EOF
udevadm control --reload-rules && udevadm trigger --subsystem-match=usb || true
echo " -> 已写入 /etc/udev/rules.d/99-surface-sd-reader.rules"

echo "=== 4. 安装 Intel UHD 620 满血非自由 VA-API 编解码驱动 ==="
DEBIAN_FRONTEND=noninteractive apt-get install -y intel-media-va-driver-non-free || true

echo "=== 全部系统级硬件优化已完成！ ==="
