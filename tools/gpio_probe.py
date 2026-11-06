# -*- coding: utf-8 -*-
"""
K230 GPIO 探测 —— 用 LED / 万用表找出哪根排针真的能用

为什么要这个：
    K230 上并不是每个 GPIO 号都引到排针、也不是每个都空着。
    相机、LCD、WiFi、Flash、调试串口会占掉一部分，还有些是启动脚
    （上电瞬间的电平决定从哪启动，乱拉可能导致板子起不来）。

怎么用：
    1. 拿一个 LED + 1kΩ 电阻（或者一个万用表，红笔接引脚、黑笔接 GND）
    2. CanMV IDE 运行本文件
    3. 串口会打印"现在轮到 GPIO32"，此时去量排针，看到 LED 亮 / 3.3V 就是它
    4. 记下编号，填进 k230_trigger.py 的 TRIG_PIN

判断成功的标志：
    每 1 秒翻转一次，LED 会"闪"，万用表在 0V / 3.3V 之间跳。

⚠️ 安全提示：
    - 如果轮到某个脚的时候板子重启 / 卡住 / 屏幕黑掉，说明那个脚是启动脚或被占用，
      记下来以后别再碰它。
    - 不要把这几个脚直接接到 Arduino 或任何外部电路上，先用 LED 确认。
    - 探测顺序是"最不容易冲突 -> 最容易冲突"，所以 26 放最后。

已联网核对（亚博官方文档「2.基础例程 / 1.FPIOA引脚分配」）：
    亚博 K230 只引出 7 个 GPIO，全在散热片旁 12pin 排针上，候选就是这 7 个：
      排针1  GPIO42  默认普通 GPIO            ← 最安全
      排针2  GPIO43  默认普通 GPIO
      排针8  GPIO34  IIC1_SCL / IIS_D_IN0  / PDM_IN3
      排针10 GPIO35  IIC1_SDA / IIS_D_OUT0 / PDM_IN1
      排针3  GPIO33  IIC0_SDA / IIS_WS     / UART3_RXD
      排针5  GPIO32  IIC0_SCL / IIS_CLK    / UART3_TXD
      排针6  GPIO26  MMC1_CLK / PDM_CLK    ← 最危险，见下
    另外 4pin 通讯口引的是 GPIO9/10（UART1_TXD/RXD），给串口版用。

    ⚠️ GPIO26 = MMC1_CLK，就是 SDIO 时钟。板载 SDIO WiFi 占用 MMC1，
       所以「要用 WiFi 传图」的话千万不要把 26 当触发脚，否则 WiFi 直接不通。
"""

import time

from machine import Pin

# 你说的这批候选，按"越靠前越安全"排。想加/减直接改这个列表。
# 42/43 默认就是普通 GPIO；32/33/34/35 平时是闲置的 I2S/PDM 脚；
# 26 是 SDIO 时钟，动了可能掉 WiFi，所以排最后。
CANDIDATES = [42, 43, 34, 35, 33, 32, 26]

# 每个 GPIO 对应的排针脚号，打印出来方便对着板子找
HEADER_PIN = {42: 1, 43: 2, 33: 3, 32: 5, 26: 6, 34: 8, 35: 10}

SECONDS_PER_PIN = 6        # 每个脚占用多久（含打印时间）
FLIP_MS = 500              # 每 500ms 翻转一次（2Hz 闪烁，肉眼很清楚）


def try_mux(num):
    """老固件需要先做 IOMUX 复用；新固件不需要，失败就算了。"""
    try:
        from machine import FPIOA
        fpioa = FPIOA()
        fn = getattr(FPIOA, "GPIO%d" % num, None)
        if fn is None:
            return "no GPIOnum in FPIOA"
        fpioa.set_function(num, fn)
        return "fpioa ok"
    except Exception as e:
        return "no fpioa (%s)" % e


def probe(num):
    print("")
    print("-" * 46)
    print(">>> 现在轮到 GPIO%d  （排针第 %s 脚）  <<<"
          % (num, HEADER_PIN.get(num, "?")))
    status = try_mux(num)
    print("    mux: %s" % status)

    try:
        p = Pin(num, Pin.OUT)
    except Exception as e:
        print("    !! Pin(%d, OUT) 失败: %s —— 这个脚用不了，跳过" % (num, e))
        return False

    # 先拉低，让万用表有个基准
    p.value(0)
    time.sleep_ms(200)

    t0 = time.ticks_ms()
    level = 0
    while time.ticks_diff(time.ticks_ms(), t0) < SECONDS_PER_PIN * 1000:
        level ^= 1
        p.value(level)
        left = SECONDS_PER_PIN - time.ticks_diff(time.ticks_ms(), t0) / 1000.0
        print("    GPIO%-3d -> %s   剩余 %.0fs"
              % (num, "HIGH 3.3V" if level else "LOW  0V", left))
        time.sleep_ms(FLIP_MS)

    # 结束一定要拉低，免得留个高电平在线上
    p.value(0)
    print("    GPIO%d 探测结束（已拉低）" % num)
    return True


def main():
    print("=" * 46)
    print(" K230 GPIO 探测")
    print(" 候选: %s" % ", ".join(str(n) for n in CANDIDATES))
    print(" 每个脚 %d 秒，LED 会闪 / 万用表会在 0~3.3V 跳" % SECONDS_PER_PIN)
    print(" 看到哪个排针在闪，就记下当时打印的编号")
    print("=" * 46)

    good = []
    for num in CANDIDATES:
        try:
            if probe(num):
                good.append(num)
        except Exception as e:
            print("    !! GPIO%d 探测期间异常: %s" % (num, e))

    print("")
    print("=" * 46)
    print(" 探测完毕。能正常配置的脚: %s" % (good if good else "（一个都没有，检查固件）"))
    print(" 把闪光的那根填进 k230_trigger.py:  TRIG_PIN = ?")
    print("=" * 46)


if __name__ == "__main__":
    main()
