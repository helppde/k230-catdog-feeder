# -*- coding: utf-8 -*-
"""
K230 识别链路分段自检 —— 一次只测一件事，卡在哪一步立刻看得出来

为什么要这个文件：
    主程序卡住的时候，可能是相机、模型加载、推理、后处理、屏幕，任何一个环节。
    全塞在一个 while True 里，只能看到"卡住了"，看不出卡在哪。
    这个脚本把整条链路拆成 5 段，每段一个 ["ok"] 标记 + 计时：

        1. import        —— 固件里有没有 libs.YOLO
        2. load_kmodel   —— 模型文件在不在、能不能加载
        3. config_pre    —— 预处理配置
        4. run x N       —— 真正的推理（最可能卡死的地方）
        5. postprocess   —— 后处理出的框对不对

用法：
    CanMV IDE 里直接打开运行。**不要同时开主程序**（抢同一个 KPU/相机）。

怎么读结果：
    最后一行停在哪一步，问题就在下一步。
    · 停在第 4 步（run 无输出）  -> nncase/KPU 卡死，多半是输入尺寸和 kmodel 不匹配
    · 第 4 步有输出但很慢        -> 正常，K230 上 320x320 大约 20~50ms
    · 框数永远是 0               -> 黑图本来就该是 0 框，属于正常；换真图再测

⚠️ 这个脚本**不初始化摄像头**，所以即使相机坏了它也照样能跑完。
"""

import time

# ---- 时间兼容层 ----
# K230 的 MicroPython 有 time.ticks_ms，PC 上的 CPython 没有。
# 加这一层是为了能在电脑上先跑一遍（确认没有语法/逻辑错误，
# 而不是等烧到板子上才发现某一行拼错了）。
try:
    _ticks = time.ticks_ms
    _diff = time.ticks_diff
except AttributeError:
    def _ticks():
        return int(time.time() * 1000)

    def _diff(a, b):
        return a - b

# ---- 和主程序保持一致的参数（改主程序记得同步这里）----
KMODEL_CANDIDATES = [
    "/sdcard/kmodel/yolov8n_320.kmodel",
    "/sdcard/examples/kmodel/yolov8n_320.kmodel",
    "/sdcard/models/yolov8n_320.kmodel",
]
MODEL_INPUT_SIZE = (320, 320)
AI_FRAME_SIZE = (640, 480)
CONF_THRESH = 0.30
NMS_THRESH = 0.45
RUN_TIMES = 5

# ---- 复用主程序的判定逻辑 ----
# 这两个函数是"识别不到"排查的核心（形状像框但值是垃圾），
# 必须和主程序完全一致。所以优先从 k230_trigger 里拿，
# 拿不到才用本地副本（这个文件要能在没有主程序的情况下单独跑）。
try:
    from k230_trigger import _looks_like_dets, _dets_are_sane
except Exception:
    def _looks_like_dets(seq):
        try:
            if len(seq) == 0:
                return True
            first = seq[0]
            if len(first) != 6:
                return False
            for v in first:
                float(v)
            return True
        except Exception:
            return False

    def _dets_are_sane(seq, max_check=8):
        try:
            n = len(seq)
        except Exception:
            return False
        if n == 0:
            return True
        for i in range(min(n, max_check)):
            try:
                r = seq[i]
                if len(r) != 6:
                    return False
                x = float(r[0]); y = float(r[1])
                w = float(r[2]); h = float(r[3])
                cid = float(r[4]); conf = float(r[5])
            except Exception:
                return False
            if not (0.0 <= conf <= 1.0):
                return False
            if w <= 0 or h <= 0:
                return False
            if cid < 0 or cid > 1000:
                return False
            if x < -4096 or y < -4096 or x > 65535 or y > 65535:
                return False
        return True


def mark(step, msg, t0=None):
    """统一的进度打印。带耗时，卡死前最后一行一眼能认出来。"""
    if t0 is None:
        print("[%s] %s" % (step, msg))
    else:
        print("[%s] %s  (%.0f ms)" % (step, msg, (_ticks() - t0)))


def pick_kmodel():
    import os
    for p in KMODEL_CANDIDATES:
        try:
            if os.stat(p):
                return p, int(os.stat(p)[6])
        except Exception:
            continue
    return None, 0


def pick_class(path):
    """按模型名挑 YOLO11/YOLOv8/YOLOv5 —— 后处理函数不同，用错不报错只出不了框。"""
    hint = str(path or "").lower()
    order = ["YOLO11", "YOLOv8", "YOLOv5"]
    if "yolov8" in hint:
        order = ["YOLOv8"] + [n for n in order if n != "YOLOv8"]
    elif "yolo11" in hint or "yolov11" in hint:
        order = ["YOLO11"] + [n for n in order if n != "YOLO11"]
    elif "yolov5" in hint:
        order = ["YOLOv5"] + [n for n in order if n != "YOLOv5"]

    for name in order:
        try:
            mod = __import__("libs.YOLO", None, None, [name])
            cls = getattr(mod, name, None)
            if cls is not None:
                return cls, name
        except Exception:
            continue
    return None, None


def main():
    print("=" * 56)
    print("K230 识别链路分段自检")
    print("=" * 56)

    # ---------- 1. import ----------
    t0 = _ticks()
    cls, cls_name = pick_class(KMODEL_CANDIDATES[0])
    if cls is None:
        mark("1/5", "!! libs.YOLO 不可用 —— 固件里没有 YOLO 库")
        print("        需要亚博出厂固件 CanMV_K230_YAHBOOM_micropython")
        return
    mark("1/5", "libs.YOLO.%s 可用" % cls_name, t0)

    # ---------- 2. 模型文件 + 加载 ----------
    path, size = pick_kmodel()
    if path is None:
        mark("2/5", "!! 一个 kmodel 都找不到，试过：")
        for p in KMODEL_CANDIDATES:
            print("        " + p)
        return
    mark("2/5", "模型 %s  %.1f MB" % (path, size / 1048576.0))

    # 用真实模型名重新挑一次类（pick_class 一开始拿的是候选里第一个）
    cls2, cls_name2 = pick_class(path)
    if cls2 is not None:
        cls, cls_name = cls2, cls_name2

    labels = ["c%d" % i for i in range(80)]
    t0 = _ticks()
    try:
        model = cls(task_type="detect", mode="video",
                    kmodel_path=path,
                    labels=labels,
                    rgb888p_size=list(AI_FRAME_SIZE),
                    model_input_size=list(MODEL_INPUT_SIZE),
                    display_size=list(AI_FRAME_SIZE),
                    conf_thresh=CONF_THRESH,
                    nms_thresh=NMS_THRESH,
                    debug_mode=0)
        mark("2/5", "load_kmodel 成功（用 %s 类）" % cls_name, t0)
    except Exception as e:
        mark("2/5", "!! 加载失败: %s" % e, t0)
        print("        文件在但加载不了 = kmodel 和固件版本不匹配（nncase 版本）")
        return

    # ---------- 3. 预处理 ----------
    t0 = _ticks()
    try:
        model.config_preprocess()
        mark("3/5", "config_preprocess 成功（detect 分支自带 letterbox）", t0)
    except Exception as e:
        mark("3/5", "!! 预处理配置失败: %s" % e, t0)
        return

    # ---------- 4. 推理（最可能卡住的地方）----------
    # 造一张全黑图。不接相机，所以相机坏没坏不影响这一步。
    try:
        import ulab.numpy as np
        w, h = AI_FRAME_SIZE
        black = np.zeros((3, h, w), dtype=np.uint8)
    except Exception as e:
        mark("4/5", "!! 造不出测试图: %s" % e)
        return

    print("[4/5] 开始推理 %d 次（如果停在这里不出下一行，就是 KPU 卡死）..." % RUN_TIMES)
    res = None
    for i in range(RUN_TIMES):
        t0 = _ticks()
        try:
            print("      run #%d ..." % (i + 1))
            res = model.run(black)
            n = len(res) if res else 0
            mark("4/5", "run #%d 返回 %d 个输出张量" % (i + 1, n), t0)
        except Exception as e:
            mark("4/5", "!! run 异常: %s" % e, t0)
            print("        黑图不该抛异常。多半是输入尺寸/格式和 kmodel 对不上。")
            break

    # ---------- 4b. run() 返回值结构（决定后处理能不能对上）----------
    # 为什么单独打这一段：
    #   现场出现过"形状像框、数值是垃圾"的情况（conf=2.8e38）。
    #   那是把没写过的内存按 float32 读出来的结果（0x7F7F7F7F ≈ 3.4e38）。
    #   只有把「类型 / 形状 / 前几个值」都打出来，才能判断 run() 到底给的是什么。
    print("[4b] run() 返回值结构（排查全靠这一段）：")
    try:
        if getattr(model, "results", None):
            res = model.results
    except Exception:
        pass
    try:
        print("      共 %d 个" % len(res))
    except Exception:
        print("      !! 不是序列: %s" % type(res))
        res = None

    if res:
        for i in range(min(3, len(res))):
            o = res[i]
            bits = [type(o).__name__]
            for attr in ("shape", "dtype"):
                v = getattr(o, attr, None)
                if v is not None:
                    bits.append("%s=%s" % (attr, v))
            try:
                bits.append("len=%d" % len(o))
            except Exception:
                pass
            print("      out[%d] %s" % (i, " ".join(bits)))
            vals = []
            for k in range(6):
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
            print("             前 6 个值: %s" % ", ".join(vals))

    # 值合理性判定 —— 和主程序同一套规则
    print("[4c] 这批返回值能不能直接当框用：")
    try:
        like = _looks_like_dets(res) if res else True
        sane = _dets_are_sane(res) if res else True
        print("      形状像框: %s   数值合理: %s" % (like, sane))
        if like and not sane:
            print("      -> 形状像但值是垃圾：这是**原始张量被误当成框**，")
            print("         必须走 postprocess，不能直接用")
        elif not like:
            print("      -> 不是框的结构，交给 postprocess 解")
        else:
            print("      -> 已经是合法的框，可以直接用")
    except Exception as e:
        print("      判定异常: %s" % e)

    # 顺便看看这个库有没有 draw_result（有的话说明 run() 按官方约定返回框）
    try:
        print("[4d] 库自带的辅助方法：")
        for name in ("draw_result", "postprocess", "config_preprocess"):
            print("      %-18s %s" % (name, hasattr(model, name)))
    except Exception:
        pass

    # ---------- 4e. 直接问 KPU 要输出张量（最权威的形状/dtype）----------
    # 前面 4b 看的是 run() 包装后的东西；这里绕过包装，直接看底层 tensor。
    # 两者对不上，就说明库在把 tensor 转成 Python 对象时出了问题
    # （比如量化模型按 float32 读 —— 会读出 3.4e38 这种数）。
    print("[4e] 底层 KPU 输出（读不到就说明这个固件不开放）：")
    try:
        kpu = getattr(model, "kpu", None)
        if kpu is None:
            print("      没有 model.kpu")
        else:
            n = kpu.outputs_size()
            print("      outputs_size = %d" % n)
            for i in range(n):
                t = kpu.get_output_tensor(i)
                arr = None
                for m in ("to_numpy", "numpy"):
                    fn = getattr(t, m, None)
                    if fn is not None:
                        try:
                            arr = fn()
                            break
                        except Exception:
                            pass
                if arr is None:
                    print("      out[%d] 拿不到 numpy: %s" % (i, type(t).__name__))
                    continue
                shape = getattr(arr, "shape", None)
                dtype = getattr(arr, "dtype", None)
                print("      out[%d] shape=%s dtype=%s" % (i, shape, dtype))
                # 前 6 个值 + 最大最小值，量化模型一眼能看出来
                vals = []
                for k in range(6):
                    try:
                        vals.append("%.4g" % float(arr[k]))
                    except Exception:
                        vals.append("?")
                print("             前 6 个值: %s" % ", ".join(vals))
                del arr
                del t
    except Exception as e:
        print("      读不到: %s" % e)

    # ---------- 5. 后处理 ----------
    print("[5/5] 黑图的后处理结果应该是 0 个框，这是正常的：")
    try:
        det = model.postprocess(model.results)
        print("      框数 = %d" % (len(det) if det else 0))
        if det:
            for d in list(det)[:3]:
                print("      %s" % (list(d),))
    except Exception as e:
        print("      !! 后处理异常: %s" % e)

    try:
        model.deinit()
    except Exception:
        pass

    print("=" * 56)
    print("自检完成。全 5 步都有输出 = 模型链路没问题，")
    print("那主程序卡住就在相机/屏幕那一侧，去跑 tools/camera_probe.py。")
    print("=" * 56)


if __name__ == "__main__":
    main()

