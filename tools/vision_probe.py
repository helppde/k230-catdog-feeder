# -*- coding: utf-8 -*-
"""
饭盆 ROI 标定 + 残食率实测工具。

作用：
  1) 在屏幕上把 BOWL_ROI 画出来，肉眼确认框只框住盆里的食物；
  2) 装满时采一次基准，然后实时打印 ratio，
     你就能看到"满 / 吃到一半 / 吃空"分别对应多少，据此定 FOOD_EMPTY_RATIO。

用法：
  把饭盆推到最外面（就是喂食时的位置），装满粮，运行本文件，回车开始。
  然后人工把粮一点点拿走，观察 ratio 变化。
"""

import sys
sys.path.append("..")
sys.path.append("/sdcard")
sys.path.append(".")

import time
import config as C
from libs.PipeLine import PipeLine
from food_vision import FoodVision
from detector import draw_text


def rect(img, x, y, w, h, color, t=2):
    try:
        img.draw_rectangle(int(x), int(y), int(w), int(h), color=color, thickness=t)
    except Exception:
        pass


def main():
    pl = PipeLine(rgb888p_size=list(C.RGB888P_SIZE),
                  display_size=list(C.DISPLAY_SIZE),
                  display_mode=C.DISPLAY_MODE)
    pl.create()

    W, H = C.RGB888P_SIZE
    DW, DH = C.DISPLAY_SIZE
    sx, sy = float(DW) / W, float(DH) / H

    fv = FoodVision(C.BOWL_ROI)

    print("当前 BOWL_ROI =", C.BOWL_ROI, " 画面", W, "x", H)
    input("请把饭盆推到喂食位置并装满，然后回车采集满盆基准...")

    fv.begin_cycle()
    t0 = time.ticks_ms()
    while fv.calibrating and time.ticks_diff(time.ticks_ms(), t0) < 8000:
        img = pl.get_frame()
        if img is None:
            continue
        fv.update(img, False)
        time.sleep_ms(30)
    print("基准 =", fv.baseline)

    print("\n开始实时输出（Ctrl-C 结束）。慢慢把粮拿走，看 res 的变化。")
    last = 0
    while True:
        img = pl.get_frame()
        if img is None:
            continue
        fv.update(img, False, time.ticks_ms())

        osd = getattr(pl, "osd_img", None)
        canvas = osd if osd is not None else img
        try:
            canvas.clear()
        except Exception:
            pass
        r = C.BOWL_ROI
        rect(canvas, r[0] * sx, r[1] * sy, r[2] * sx, r[3] * sy, (255, 0, 255), 2)
        draw_text(canvas, 8, 6, "ROI %d,%d %dx%d" % (r[0], r[1], r[2], r[3]),
                  (255, 255, 255), 20)
        draw_text(canvas, 8, 34, fv.describe(), (0, 255, 0), 22)
        draw_text(canvas, 8, 64, "empty<%.2f" % C.FOOD_EMPTY_RATIO,
                  (255, 255, 0), 18)
        try:
            pl.show_image()
        except Exception:
            pass

        now = time.ticks_ms()
        if time.ticks_diff(now, last) > 500:
            last = now
            print(fv.describe())


try:
    main()
except KeyboardInterrupt:
    print("\n停止")
