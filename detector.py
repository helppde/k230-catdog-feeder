# -*- coding: utf-8 -*-
"""
YOLO 检测封装（K230 CanMV）。

⚠️ 这个文件**不是主程序用的**。
   真正跑的是 k230_trigger.py，它内部自带一份检测实现，配置也写在自己文件开头。
   这里只剩 tools/vision_probe.py 在 import 它的 draw_text()。
   改检测参数（CONF_THRESH / LABELS / MODEL_INPUT_SIZE）请改 k230_trigger.py，
   改这里不会有任何效果。

不同固件版本 libs/YOLO.py 里类名不一样（YOLO11 / YOLOv8 / YOLOv5），
这里做成适配层：能拿到哪个用哪个，拿不到就退化成模拟检测，
保证没有模型的时候也能把机械/逻辑调通。

检测结果统一成：
    [x, y, w, h, class_id, confidence]   # 坐标在 rgb888p 坐标系
"""

import math
import time

import config as C


def log(msg):
    if C.VERBOSE:
        print("[det] " + msg)


# ---------------------------------------------------------------- 适配层
def _load_yolo_cls(kmodel_path=""):
    """按模型文件名挑对应的类。

    三个类前处理相同、**后处理不同**（各自调不同的 aidemo 函数）。
    用错类不报错，只会"永远检不到"，所以必须按模型名对号入座。
    """
    names = ["YOLO11", "YOLOv8", "YOLOv5"]
    hint = str(kmodel_path or "").lower()
    if "yolov8" in hint:
        names = ["YOLOv8"] + [n for n in names if n != "YOLOv8"]
    elif "yolo11" in hint or "yolov11" in hint:
        names = ["YOLO11"] + [n for n in names if n != "YOLO11"]
    elif "yolov5" in hint:
        names = ["YOLOv5"] + [n for n in names if n != "YOLOv5"]

    for n in names:
        try:
            mod = __import__("libs.YOLO", None, None, [n])
            cls = getattr(mod, n, None)
            if cls is not None:
                log("使用 libs.YOLO.%s（按模型名匹配）" % n)
                return cls
        except Exception:
            continue
    return None


def _resolve_kmodel():
    """挑一个真的存在的 kmodel，别让路径写错直接憋死整个识别器。

    出厂固件自带的 80 类 COCO 模型在 /sdcard/kmodel/ 里（不是 models），
    所以这里先试配置值，再试 C.KMODEL_FALLBACKS 里那几个已知位置。
    """
    import os
    cands = [C.KMODEL_PATH] + list(getattr(C, "KMODEL_FALLBACKS", ()))
    seen = []
    for p in cands:
        if p in seen:
            continue
        seen.append(p)
        try:
            if os.stat(p):
                if p != C.KMODEL_PATH:
                    log("配置的 %s 不存在，改用卡上现成的 %s" % (C.KMODEL_PATH, p))
                return p
        except Exception:
            continue
    return C.KMODEL_PATH        # 都没有，原样返回让报错带上它


class _BaseDetector(object):
    def detect(self, img):
        return []

    def deinit(self):
        pass


class YoloDetector(_BaseDetector):

    def __init__(self):
        path = _resolve_kmodel()
        cls = _load_yolo_cls(path)
        if cls is None:
            raise OSError("libs.YOLO 不可用")

        kw = dict(
            kmodel_path=path,
            labels=C.LABELS,
            rgb888p_size=list(C.RGB888P_SIZE),
            display_size=list(C.DISPLAY_SIZE),
            conf_thresh=C.CONF_THRESH,
            nms_thresh=C.NMS_THRESH,
            debug_mode=0,
        )
        try:
            self.model = cls(task_type="detect", mode="video", **kw)
        except TypeError:
            self.model = cls(mode="video", **kw)

        self.model.config_preprocess()
        log("模型加载完成: %s" % kw["kmodel_path"])
        self.fps = 0.0
        self._t = time.ticks_ms()
        self._n = 0

    def detect(self, img):
        res = self.model.run(img)
        out = []
        if not res:
            return out
        for r in res:
            try:
                cid = int(r[4])
                if cid < 0 or cid >= len(C.LABELS):
                    continue
                # COCO 模型会吐出 80 类，只留猫和狗
                if C.KEEP_CLASSES and cid not in C.KEEP_CLASSES:
                    continue
                out.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]),
                            cid, float(r[5])])
            except Exception:
                continue
        self._tick()
        return out

    def _tick(self):
        self._n += 1
        now = time.ticks_ms()
        dt = time.ticks_diff(now, self._t)
        if dt >= 1000:
            self.fps = self._n * 1000.0 / dt
            self._n = 0
            self._t = now

    def deinit(self):
        try:
            self.model.deinit()
        except Exception:
            pass


class SimDetector(_BaseDetector):
    """没有 kmodel 时的假检测：一只猫在门口来回走，周期性离开。
    用来验证 计数/状态机/舵机 逻辑是否正确。
    """

    def __init__(self):
        self.fps = 0.0
        self.t0 = time.ticks_ms()

    def detect(self, img):
        el = time.ticks_diff(time.ticks_ms(), self.t0) / 1000.0
        phase = el % 60.0
        if phase > 45.0:
            return []                      # 后 15s 离开，用来测试"吃完收回"
        W, H = C.RGB888P_SIZE
        x = int(W * 0.30 + W * 0.12 * (1 + math.sin(el)))
        y = int(H * 0.40)
        w = int(W * 0.22)
        h = int(H * 0.42)
        return [[x, y, w, h, C.CLASS_CAT, 0.88]]


class NullDetector(_BaseDetector):
    def detect(self, img):
        return []


def build_detector():
    if C.SIMULATE_DETECTION:
        log("模拟检测模式（不加载模型）")
        return SimDetector()
    try:
        return YoloDetector()
    except Exception as e:
        log("模型加载失败(%s)，回退到模拟检测" % e)
        return SimDetector()


# ---------------------------------------------------------------- 绘图
def _color_of(cid):
    if cid == C.CLASS_CAT:
        return (0, 255, 0)
    if cid == C.CLASS_DOG:
        return (255, 160, 0)
    return (200, 200, 200)


def draw_text(img, x, y, text, color=(255, 255, 255), size=20):
    try:
        img.draw_string_advanced(int(x), int(y), int(size), text, color=color)
        return
    except Exception:
        pass
    try:
        img.draw_string(int(x), int(y), text, scale=2, color=color)
    except Exception:
        pass


def draw_detections(img, dets, sx=1.0, sy=1.0):
    for d in dets:
        x, y, w, h = d[0] * sx, d[1] * sy, d[2] * sx, d[3] * sy
        col = _color_of(d[4])
        try:
            img.draw_rectangle(int(x), int(y), int(w), int(h), color=col, thickness=2)
        except Exception:
            pass
        name = C.LABELS[d[4]] if 0 <= d[4] < len(C.LABELS) else "?"
        draw_text(img, x + 4, y + 2, "%s %.2f" % (name, d[5]), col, 16)
