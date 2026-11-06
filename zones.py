# -*- coding: utf-8 -*-
"""
触发区 / ROI 的几何判断（纯函数，不依赖摄像头，PC 上也能跑）

抽出来单独一个文件的原因：
    "目标是不是进了触发区" 这个判断同时被
       tools/vision_probe.py（看触发区画得对不对）、
       food_vision.py（粮量判断）
    用到，放在 food_vision.py 里会让想用它的模块被迫 import 一堆图像处理。

    （注意：k230_trigger.py 是单文件版，触发区判断内联在里面，不 import 本文件。）
"""

import config as C


def center(box):
    return (box[0] + box[2] * 0.5, box[1] + box[3] * 0.5)


def in_zone(box, zone=None):
    """box 的中心点是否落在 zone=(x, y, w, h) 里"""
    z = zone if zone else C.TRIGGER_ZONE
    cx, cy = center(box)
    return (z[0] <= cx <= z[0] + z[2]) and (z[1] <= cy <= z[1] + z[3])


def any_in_zone(dets, zone=None):
    for d in dets:
        if in_zone(d, zone):
            return True
    return False


def best_in_zone(dets, zone=None):
    """触发区里置信度最高的那个检测框，没有就返回 None"""
    best = None
    for d in dets:
        if not in_zone(d, zone):
            continue
        if best is None or d[5] > best[5]:
            best = d
    return best


def overlap_with_roi(box, roi=None):
    """检测框与 ROI 的交集 / ROI 面积（0~1）"""
    r = roi if roi else C.BOWL_ROI
    rx2 = r[0] + r[2]
    ry2 = r[1] + r[3]
    bx2 = box[0] + box[2]
    by2 = box[1] + box[3]
    ix = min(rx2, bx2) - max(r[0], box[0])
    iy = min(ry2, by2) - max(r[1], box[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    area = r[2] * r[3]
    if area <= 0:
        return 0.0
    return (ix * iy) / float(area)


def is_occluded(dets, roi=None):
    for d in dets:
        if overlap_with_roi(d, roi) >= C.ROI_OCCLUDE_OVERLAP:
            return True
    return False


def crop_rect(box, pad=None, size=None):
    """把"外扩 pad 之后的裁剪矩形"算出来并夹在画面内，返回 (x, y, w, h)。

    拍照上传时用：只传动物那一块，PC 端认个体更准，流量也小。
    """
    if pad is None:
        pad = C.CROP_PAD
    if size is None:
        size = C.RGB888P_SIZE
    W, H = size

    x, y, w, h = box[0], box[1], box[2], box[3]
    # 方框往外扩，单位是框自身尺寸的比例
    x -= w * pad
    y -= h * pad
    w *= (1.0 + 2 * pad)
    h *= (1.0 + 2 * pad)

    x = int(max(0, min(x, W - 8)))
    y = int(max(0, min(y, H - 8)))
    w = int(min(w, W - x))
    h = int(min(h, H - y))
    if w < 8:
        w = 8
    if h < 8:
        h = 8
    return (x, y, w, h)
