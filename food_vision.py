# -*- coding: utf-8 -*-
"""
没有重量传感器时，怎么知道"吃完了"？

思路（三层判据，任何一层成立都能收盆，任何一层失效都还有兜底）：

  判据① 行为层：YOLO 框和饭盆 ROI 不再重叠，并且连续 T_LEAVE_CONFIRM_MS
                都检测不到动物 —— 动物走了 = 这一顿结束。
                （最简单也最可靠，主要判据）

  判据② 视觉层：在"盆没被挡住"的时刻，统计 BOWL_ROI 内的灰度标准差。
                盆里装满粮 -> 颗粒明暗交错，标准差大；
                盆被吃空   -> 盆底平整，标准差小。
                ratio = 当前标准差 / 本次推出时的"满盆基准"
                ratio < FOOD_EMPTY_RATIO 且持续 T_EMPTY_CONFIRM_MS => 盆空
                （能识别"还在吃但已经没粮了，动物在舔空盆"）

  判据③ 时间层：T_MAX_EAT_MS 兜底强制收回；推出去 T_WAIT_ANIMAL_MS 没人来也收回。

为什么用"标准差"而不是颜色阈值：
    粮的颜色、光照、盆的颜色每个场景都不一样，硬编码颜色阈值换地方就废。
    标准差衡量的是"纹理/起伏"，盆空时纹理必然塌下去，对光照的适应性更好。
    以后如果发现不稳，把它换成"饱和度覆盖面积"或"YOLO 粮粒检测"都可以，
    只要保证 measure() 还是返回 0~1 的残食率。

关键点：饭盆是被推杆推出去的，位置每次都不一样（±几毫米），
所以每推出去一次就重新采一次"满盆基准"，而不是用写死的常数。
"""

import time

import config as C


def log(msg):
    if C.VERBOSE:
        print("[food] " + msg)


def _stdev_of(s):
    """兼容不同固件 get_statistics() 的返回形式。"""
    if s is None:
        return None
    # OpenMV 风格对象
    try:
        v = s.l_stdev
        v = v() if callable(v) else v
        return float(v)
    except Exception:
        pass
    # 元组风格：(mean, median, mode, stdev, lmean, lmedian, lmode, lstdev, ...)
    if isinstance(s, (tuple, list)):
        n = len(s)
        if n >= 8:
            return float(s[7])
        if n >= 4:
            return float(s[3])
    return None


def _median(vals):
    v = sorted(vals)
    n = len(v)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return v[n // 2]
    return (v[n // 2 - 1] + v[n // 2]) / 2.0


class FoodVision(object):

    def __init__(self, roi=None, log_fn=log):
        self.roi = roi if roi else C.BOWL_ROI
        self.log = log_fn

        self.baseline = None       # 满盆基准（标准差）
        self.ratio = 1.0           # 残食率 1.0=满盆
        self.valid = False         # 本帧数值可信吗
        self.stdev = 0.0

        self.calibrating = False
        self._samples = []
        self._calib_t0 = 0

        self.empty_since = None

    # ------------------------------------------------ 基础
    def _roi_px(self, img):
        x, y, w, h = self.roi
        W, H = img.width(), img.height()
        x = max(0, min(int(x), W - 4))
        y = max(0, min(int(y), H - 4))
        w = max(4, min(int(w), W - x))
        h = max(4, min(int(h), H - y))
        return (x, y, w, h)

    def _stdev(self, img):
        roi = self._roi_px(img)
        s = None
        try:
            s = img.get_statistics(roi=roi)
        except Exception:
            try:
                s = img.get_statistics(roi)
            except Exception:
                return None
        return _stdev_of(s)

    # ------------------------------------------------ 每轮投喂开始时调用
    def begin_cycle(self, now=None):
        if now is None:
            now = time.ticks_ms()
        self.ratio = 1.0
        self.valid = False
        self.empty_since = None
        self.calibrating = True
        self._samples = []
        self._calib_t0 = now
        log("开始采集满盆基准...")

    # ------------------------------------------------ 每帧调用
    def update(self, img, occluded, now=None):
        """occluded: 是否有动物（检测框）压在饭盆上，压住时数值不可信"""
        if now is None:
            now = time.ticks_ms()

        st = self._stdev(img)
        if st is None:
            self.valid = False
            return
        self.stdev = st

        if occluded:
            self.valid = False
            return

        if self.calibrating:
            self._samples.append(st)
            if len(self._samples) >= C.BASELINE_SAMPLES:
                self._commit_baseline()
            elif time.ticks_diff(now, self._calib_t0) >= C.T_BASELINE_TIMEOUT_MS:
                if len(self._samples) >= 3:
                    self._commit_baseline()
                else:
                    self.calibrating = False
                    self.valid = False
                    log("采不到干净画面，沿用上次基准: %s" %
                        ("None" if self.baseline is None else "%.1f" % self.baseline))
            self.valid = False
            return

        if self.baseline is None or self.baseline <= 1.0:
            # 没有基准就没法判断残食，只能靠行为/时间判据
            self.valid = False
            return

        r = st / self.baseline
        if r > C.FOOD_OCCLUDE_RATIO:
            # 纹理量暴涨：多半是动物身体/影子进入 ROI，或曝光突变
            self.valid = False
            return

        if r < 0.0:
            r = 0.0
        elif r > 1.0:
            r = 1.0
        self.ratio = r
        self.valid = True

    def _commit_baseline(self):
        newb = _median(self._samples)
        # 基准不能太小，否则轻微噪声就判空
        if newb < 3.0:
            newb = 3.0
        self.baseline = newb
        self._samples = []
        self.calibrating = False
        log("满盆基准 = %.1f" % newb)

    # ------------------------------------------------ 判空
    def is_empty(self, now=None):
        """判据②：盆里已经没东西了（需要连续满足）"""
        if now is None:
            now = time.ticks_ms()
        if self.valid and self.ratio <= C.FOOD_EMPTY_RATIO:
            if self.empty_since is None:
                self.empty_since = now
        else:
            self.empty_since = None

        if self.empty_since is None:
            return False
        return time.ticks_diff(now, self.empty_since) >= C.T_EMPTY_CONFIRM_MS

    # ------------------------------------------------ 给日志用
    def describe(self):
        if self.baseline is None:
            return "res=-- (无基准)"
        if not self.valid:
            return "res=-- (遮挡/异常) raw=%.1f" % self.stdev
        return "res=%d%% raw=%.1f base=%.1f" % (
            int(self.ratio * 100), self.stdev, self.baseline)


# ---------------------------------------------------- 检测框与 ROI 遮挡关系
# 几何判断统一放在 zones.py，这里只做转发，老代码 `from food_vision import ...` 不用改
from zones import (overlap_with_roi, is_occluded,  # noqa: F401,E402
                   in_zone, any_in_zone)
