# -*- coding: utf-8 -*-
"""
把 libs/YOLO.py 的源码和真实行为打印出来 —— 一次运行，全部真相

为什么要这个工具：
    "模型加载成功、fps 正常、但框永远是 0"，或者"一调 postprocess 就整个卡死"，
    这两种情况靠猜固件版本是猜不出来的。libs/YOLO 是板子上的 Python 源码，
    直接读它、直接看它返回什么，比任何推理都可靠。

用法：
    CanMV IDE 里直接打开运行。不需要相机（用全黑图），不占 KPU 很久。
    跑完把输出整段发给我，我就能给出确定的调用方式。

输出四段：
    [1] 库文件在哪、多大
    [2] 源码里的关键函数（run / postprocess / draw_result 的定义行）
    [3] 实际跑一次 run()，返回值的类型、形状、前几个数
    [4] 逐个尝试 postprocess 的调用方式（带"要先做什么"的提示）
"""

import sys

# ---- 时间兼容层：PC 上也能跑，方便先验证脚本本身没写错 ----
try:
    import time
    _ticks = time.ticks_ms
except Exception:
    def _ticks():
        import time as _t
        return int(_t.time() * 1000)

KMODEL_CANDIDATES = [
    "/sdcard/kmodel/yolov8n_320.kmodel",
    "/sdcard/examples/kmodel/yolov8n_320.kmodel",
    "/sdcard/models/yolov8n_320.kmodel",
]
FRAME = (640, 480)
INPUT = (320, 320)


def head(t):
    print("")
    print("=" * 60)
    print(t)
    print("=" * 60)


# ============================================================
# [1] 库文件在哪
# ============================================================
def step1_locate():
    head("[1] 找到 libs/YOLO 的源码")
    path = None
    try:
        import libs.YOLO as Y
        path = getattr(Y, "__file__", None)
        print("      module.__file__ = %s" % path)
    except Exception as e:
        print("      !! import libs.YOLO 失败: %s" % e)
        print("         这个固件没有 YOLO 库，先确认烧的是亚博 K230 出厂固件")
        return None, None

    if not path:
        for p in ("/sdcard/libs/YOLO.py", "/sdcard/libs/YOLO.pyc"):
            try:
                import os
                os.stat(p)
                path = p
                print("      按常见路径找到: %s" % p)
                break
            except Exception:
                continue

    size = 0
    try:
        import os
        st = os.stat(path)
        size = st[6]
        print("      文件大小 = %d 字节" % size)
    except Exception as e:
        print("      !! 读不到文件信息: %s" % e)

    return path, size


# ============================================================
# [2] 源码里的关键函数
# ============================================================
def step2_source(path):
    head("[2] 源码里的关键函数定义")
    if not path:
        print("      跳过（没找到文件）")
        return

    try:
        f = open(path, "r")
        src = f.read()
        f.close()
    except Exception as e:
        print("      !! 读不了源码（可能只有 .pyc）: %s" % e)
        print("         没关系，看后面第 3、4 段也能定位")
        return

    lines = src.split("\n")
    print("      共 %d 行" % len(lines))

    keys = ("def run", "def postprocess", "def draw_result",
            "def config_preprocess", "def preprocess", "def __init__",
            "class ")
    for i, ln in enumerate(lines):
        s = ln.strip()
        for k in keys:
            if s.startswith(k):
                print("      L%-5d %s" % (i + 1, s[:90]))
                break

    # 把 postprocess / draw_result 的完整函数体打出来 —— 这是最关键的两段
    for target in ("def postprocess", "def draw_result"):
        for i, ln in enumerate(lines):
            if ln.strip().startswith(target):
                print("")
                print("      ---- %s 的完整实现 ----" % target)
                base = len(ln) - len(ln.lstrip())
                for j in range(i, min(i + 40, len(lines))):
                    cur = lines[j]
                    # 遇到同缩进的下一个定义就停
                    if j > i and cur.strip() and \
                            (len(cur) - len(cur.lstrip())) <= base and \
                            (cur.strip().startswith("def ") or
                             cur.strip().startswith("class ")):
                        break
                    print("      %s" % cur)
                break


# ============================================================
# [3] 实际跑一次 run()
# ============================================================
def step3_run():
    head("[3] 实际跑一次 run()，看它到底返回什么")
    path = None
    for p in KMODEL_CANDIDATES:
        try:
            import os
            if os.stat(p):
                path = p
                break
        except Exception:
            continue
    if path is None:
        print("      !! 找不到 kmodel，跳过")
        return None

    print("      模型: %s" % path)
    try:
        from libs.YOLO import YOLOv8
    except Exception as e:
        print("      !! libs.YOLO.YOLOv8 不可用: %s" % e)
        return None

    labels = ["c%d" % i for i in range(80)]
    try:
        model = YOLOv8(task_type="detect", mode="video",
                       kmodel_path=path, labels=labels,
                       rgb888p_size=list(FRAME),
                       model_input_size=list(INPUT),
                       display_size=list(FRAME),
                       conf_thresh=0.25, nms_thresh=0.45,
                       debug_mode=0)
        print("      模型构造成功")
    except Exception as e:
        print("      !! 构造失败: %s" % e)
        return None

    try:
        model.config_preprocess()
        print("      config_preprocess 成功")
    except Exception as e:
        print("      !! config_preprocess 失败: %s" % e)
        return model

    try:
        import ulab.numpy as np
        img = np.zeros((3, FRAME[1], FRAME[0]), dtype=np.uint8)
    except Exception as e:
        print("      !! 造不出测试图: %s" % e)
        return model

    t0 = _ticks()
    try:
        res = model.run(img)
    except Exception as e:
        print("      !! run() 抛异常: %s" % e)
        return model
    dt = _ticks() - t0
    print("      run() 用时 %d ms" % dt)

    try:
        n = len(res)
        print("      返回 %d 个对象" % n)
    except Exception:
        print("      返回的不是序列: %s" % type(res))
        return model

    for i in range(min(3, n)):
        o = res[i]
        info = [type(o).__name__]
        for attr in ("shape", "dtype"):
            v = getattr(o, attr, None)
            if v is not None:
                info.append("%s=%s" % (attr, v))
        try:
            info.append("len=%d" % len(o))
        except Exception:
            pass
        print("      out[%d] %s" % (i, " ".join(info)))

        vals = []
        for k in range(8):
            try:
                vals.append("%.4g" % float(o[k]))
                continue
            except Exception:
                pass
            try:
                sub = o[k]
                try:
                    vals.append("%s(len=%d)" % (type(sub).__name__, len(sub)))
                except Exception:
                    vals.append(type(sub).__name__)
            except Exception:
                vals.append("?")
        print("             前 8 个值: %s" % ", ".join(vals))

    print("")
    print("      判读：")
    print("      · out[0] 有 shape=(1,C,H,W) 且 H/W 是 40/20/10")
    print("        -> run() 给的是**原始张量**，必须交给 postprocess 解码")
    print("      · out[0] len=6 且全是普通小数")
    print("        -> run() 已经给的是**框**，直接用，不要再 postprocess")

    return model


# ============================================================
# [4] 怎么调 postprocess
# ============================================================
def step4_postprocess(model):
    head("[4] postprocess 的调用方式")
    if model is None:
        print("      跳过（模型没起来）")
        return

    res = getattr(model, "results", None)
    if res is None:
        print("      !! model.results 是空的，先确认 run() 成功")
        res = []

    fn = getattr(model, "postprocess", None)
    if fn is None:
        print("      !! 这个模型根本没有 postprocess() 方法")
        print("         那就只能靠 draw_result() —— 见下面 [5]")
        return

    print("      方法存在: %s" % fn)
    try:
        print("      __doc__: %s" % (fn.__doc__ or "（没有）"))
    except Exception:
        pass

    # 依次试各种参数组合。每个都在 try 里，失败继续下一个。
    # ⚠️ 注意：如果这里卡住，说明这个签名会死循环 —— 直接重插 USB，
    #    然后告诉我"卡在第几个"，我就知道该避开哪个。
    attempts = [
        ("postprocess(results)", lambda: model.postprocess(res)),
        ("postprocess(results, 0.45)", lambda: model.postprocess(res, 0.45)),
        ("postprocess(results, 0.25, 0.45)",
         lambda: model.postprocess(res, 0.25, 0.45)),
        ("postprocess()", lambda: model.postprocess()),
    ]
    for idx, (desc, call) in enumerate(attempts):
        print("")
        print("      [%d] 试 %s ..." % (idx, desc))
        sys.stdout.flush()
        try:
            out = call()
        except TypeError as e:
            print("          -> TypeError（参数个数不对）: %s" % e)
            continue
        except Exception as e:
            print("          -> 异常: %s" % e)
            continue

        try:
            n = len(out) if out else 0
        except Exception:
            print("          -> 返回了不可遍历的东西: %s" % type(out))
            continue
        print("          -> OK，返回 %d 项" % n)

        if n:
            for i in range(min(3, n)):
                d = out[i]
                try:
                    print("             框[%d] = %s" % (i, [float(v) for v in d]))
                except Exception:
                    print("             框[%d] = %s" % (i, d))
            # 打印类别序号 —— 用户要的就是这个
            print("")
            print("             类别序号一览：")
            seen = {}
            for d in out:
                try:
                    cid = int(d[4])
                    conf = float(d[5])
                    if cid not in seen or conf > seen[cid]:
                        seen[cid] = conf
                except Exception:
                    continue
            labels = ["c%d" % i for i in range(80)]
            for cid in sorted(seen):
                nm = labels[cid] if 0 <= cid < len(labels) else "?"
                print("               序号 %d  conf=%.2f" % (cid, seen[cid]))
            print("")
            print("            ★ 哪个序号是猫/狗：看你自己喂的图。")
            print("              COCO 80 类（0 开始）里 cat=15、dog=16。")
            print("              如果这里打印的序号和它差 1，说明模型是 1 开始的。")
        else:
            print("              （0 项 = 黑图里没有目标，这是正常的）")
        print("")
        print("      ★ 这个签名可用：%s" % desc)
        return desc

    print("")
    print("      !! 所有签名都失败了")
    print("         看 [2] 里 postprocess 的源码，把它的 def 行发给我")


# ============================================================
# [5] draw_result（官方例程用的就是它）
# ============================================================
def step5_draw(model):
    head("[5] draw_result —— 官方例程就是这么用的")
    if model is None:
        print("      跳过")
        return
    dr = getattr(model, "draw_result", None)
    if dr is None:
        print("      !! 没有 draw_result 方法")
        return
    print("      存在: %s" % dr)
    print("      官方例程的写法：")
    print("          res = model.run(img)")
    print("          model.draw_result(res, pl.osd_img)")
    print("      -> 说明**库自己在 draw_result 内部做后处理**。")
    print("         如果 postprocess 直接调会卡，那就别直接调它，")
    print("         改成先 draw_result，再从库里找结果（见下）。")

    # draw_result 可能把结果缓存到某个属性里，这里列一下候选
    res = getattr(model, "results", None)
    print("")
    print("      调用前 model.results 的类型: %s" % type(res).__name__)
    for name in ("dets", "boxes", "detections", "output", "bboxes"):
        v = getattr(model, name, None)
        if v is not None:
            print("      发现属性 model.%s = %s" % (name, type(v).__name__))


def main():
    print("=" * 60)
    print(" libs/YOLO 体检 —— 搞清 run() 和 postprocess() 到底怎么用")
    print("=" * 60)

    path, size = step1_locate()
    step2_source(path)
    model = step3_run()
    step4_postprocess(model)
    step5_draw(model)

    print("")
    print("=" * 60)
    print(" 跑完了。把上面整段输出发我，我给出确定的写法。")
    print(" ⚠️ 如果卡在 [4] 的某一步不动了：直接拔 USB 重插，")
    print("    然后告诉我卡在 [4] 的第几个，我就能避开那个签名。")
    print("=" * 60)

    if model is not None:
        try:
            model.deinit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
