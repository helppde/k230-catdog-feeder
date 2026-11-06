# -*- coding: utf-8 -*-
"""
相机通道探测 —— 定位 "sensor(0) snapshot chn(2) failed(3)"

背景：
    CanMV 的 PipeLine 会开两个相机通道：
        chn0 -> 屏幕（DISPLAY_SIZE）
        chn2 -> 给模型用的 RGB888P 帧（RGB888P_SIZE）
    chn2 出不了帧时，get_frame() 永远返回 None，主程序就会静默空转，
    看起来跟死机一样。串口那行 snapshot chn(2) failed 就是它。

怎么用（CanMV IDE 里运行）：
    直接运行            -> 打印所有待试组合
    camera_probe.py 0   -> 只试第 0 组

⚠️ 一次只试一组。试完**断电重上电**再试下一组 ——
    相机通道在同一个进程里反复 create/destroy 很容易把状态搞乱，
    那样测出来的结果不可信（本来好的组合也可能报错）。

判读：
    "拿到 N 帧"        -> 这组能用，把参数填回 k230_trigger.py 顶部
    "0 帧"             -> 这组不行，换下一组
    "create() 就失败"  -> 更基础的问题（排线 / 屏幕分辨率对不上 / 固件）
"""

import sys
import time

# (RGB888P_SIZE, DISPLAY_SIZE, DISPLAY_MODE, 说明)
#
# 亚博 K230 = GC2093 摄像头（1920x1080）+ 2.4 寸 MIPI 屏 640x480。
# 所以正确的组合只有第一组，其余是给"换了屏 / 没接屏"的情况准备的。
COMBOS = [
    ((640, 480),  (640, 480),   "lcd",  "★ 亚博 K230 正确配置（640x480 屏）"),
    ((640, 480),  (640, 480),   "virt", "同分辨率但不点屏，走 IDE 显示"),
    ((1280, 960), (640, 480),   "lcd",  "4:3 高清，内存够的话更清楚"),
    ((1280, 720), (640, 480),   "lcd",  "16:9 AI帧 + 4:3 屏（画面会压扁，仅对比）"),
    ((640, 480),  (800, 480),   "lcd",  "接 3.5 寸屏（800x480）时用"),
    ((640, 480),  (1920, 1080), "hdmi", "走 HDMI 直出"),
    ((640, 480),  (640, 480),   "auto", "让固件按板型自己选屏驱动"),
]


def show_list():
    print("=" * 56)
    print(" 相机组合探测")
    print("=" * 56)
    for i, (rp, ds, mode, desc) in enumerate(COMBOS):
        print("  [%d] %-30s" % (i, desc))
        print("      RGB888P %sx%s   DISPLAY %sx%s   mode=%s"
              % (rp[0], rp[1], ds[0], ds[1], mode))
    print("-" * 56)
    print(" 一次试一组，试完断电重上电：")
    print("     import camera_probe; camera_probe.run(0)")
    print("  或者把本文件存成 /sdcard/camera_probe.py 后：")
    print("     exec(open('/sdcard/camera_probe.py').read())")
    print("     run(0)")
    print("=" * 56)


def run(idx):
    if idx < 0 or idx >= len(COMBOS):
        print("组合编号要在 0..%d 之间" % (len(COMBOS) - 1))
        return

    rp, ds, mode, desc = COMBOS[idx]
    print("")
    print("=" * 56)
    print(" 组合 [%d] %s" % (idx, desc))
    print(" RGB888P %sx%s   DISPLAY %sx%s   mode=%s"
          % (rp[0], rp[1], ds[0], ds[1], mode))
    print("=" * 56)

    try:
        from libs.PipeLine import PipeLine
    except Exception as e:
        print("[FAIL] 连 PipeLine 都 import 不了: %s" % e)
        print("       固件不对？这个板子要跑 CanMV 的 K230 固件。")
        return

    try:
        pl = PipeLine(rgb888p_size=list(rp),
                      display_size=list(ds),
                      display_mode=mode)
        pl.create()
        print("[ok] create() 成功")
    except Exception as e:
        print("[FAIL] create() 失败: %s" % e)
        print("       这一组直接跳过。屏幕分辨率对不上时就会这样。")
        return

    ok = 0
    bad = 0
    first = None
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 3000:
        try:
            img = pl.get_frame()
        except Exception as e:
            print("[FAIL] get_frame() 抛异常: %s" % e)
            break
        if img is None:
            bad += 1
            time.sleep_ms(20)
            continue
        ok += 1
        if first is None:
            try:
                first = "%dx%d" % (img.width(), img.height())
            except Exception:
                first = "?"
        time.sleep_ms(20)

    print("-" * 56)
    print(" 3 秒内：拿到 %d 帧，空帧 %d 次" % (ok, bad))
    if ok > 0:
        print(" [OK] 这一组可用，首帧 = %s" % first)
        print("")
        print(" 把这三行填回 k230_trigger.py 顶部：")
        print("     RGB888P_SIZE = (%d, %d)" % (rp[0], rp[1]))
        print("     DISPLAY_SIZE = (%d, %d)" % (ds[0], ds[1]))
        print("     DISPLAY_MODE = \"%s\"" % mode)
    else:
        print(" [NO ] 一帧都没有 —— 这组不行，换下一组。")
        print("       如果所有组合都是 0 帧，重点查摄像头排线和固件版本，")
        print("       先用官方 camera 例程确认相机本身是好的。")
    print("=" * 56)
    print("")

    try:
        pl.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_list()
    else:
        try:
            run(int(sys.argv[1]))
        except KeyboardInterrupt:
            print("\n停止")
