# -*- coding: utf-8 -*-
"""
在 K230 上**按官方例程原样**跑一遍，把每一步的真实情况打出来。

这个文件不是给主程序用的，是给 tools/run_official_example.py 推上板子执行的。
也可以直接拷到 /sdcard 后在 CanMV IDE 里跑。

它只回答四个问题，不做任何判断、不碰 GPIO、不上传：
    Q1  相机的 chn2（AI 通道）到底出不出帧？出多少？
    Q2  model.run(img) 返回的**是什么**（原始张量？还是已经后处理好的框？）
    Q3  postprocess 的返回值长什么样？
    Q4  官方写法 draw_result(...) 能不能正常画、画完之后框有几个？

为什么要单独做这件事：
    主程序里检测、消抖、GPIO、上传搅在一起，出问题时很难判断是哪一环。
    这里只留"相机 -> 模型 -> 画框"三件事，跑通了就说明硬件和模型都好，
    剩下的一定是我们自己的逻辑问题 —— 这个分界线非常值钱。

逐阶段都写日志到 /sdcard/official_probe.log。
**卡住时也能拿到证据**：如果串口没反应了，重新插 USB，再跑
    tools/run_official_example.py --show-log
就能看到最后走到哪一步。
"""

import gc
import os
import sys
import time

LOG = "/sdcard/official_probe.log"
KMODEL = "/sdcard/kmodel/yolov8n_320.kmodel"
FRAME = (640, 480)          # AI 帧（chn2）
DISP = (640, 480)           # 板载屏
MODE = "lcd"                # 想排除屏幕就改 "virt"
NFRAMES = 40                # 跑多少帧

# 80 类 COCO，顺序必须和 kmodel 一致。这里只是为了 len(labels) 和屏幕上
# 显示名字，判断用的是序号，不依赖这个列表。
COCO = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def log(msg):
    """同时打到串口和 SD 卡。串口断了也能从文件里查到最后一步。"""
    print(msg)
    try:
        f = open(LOG, "a")
        f.write("[%8d] %s\n" % (time.ticks_ms(), msg))
        f.close()
    except Exception:
        pass


def describe(o, depth=0):
    """尽可能不含糊地描述一个对象：类型、长度、**第一个元素长什么样**。

    为什么一定要说清第一个元素：
        (框列表, 序号列表, 分数列表) 和 [张量, 张量, 张量] 在最外层都是
        "len=3"，光看长度根本分不出来 —— 而这正是我们要判断的东西。
        多打一层 first=... 就一眼能分：
            三元组 -> tuple(len=3) first=list(len=1)
            张量   -> list(len=3)  first=Tensor(len=1)
        再往下看框里面：
            list(len=1) first=tuple(len=4)   <- 这才是 x,y,w,h
    """
    try:
        n = len(o)
    except Exception:
        return "%s(%r)" % (type(o).__name__, o)
    name = type(o).__name__
    if n == 0:
        return "%s(len=0)" % name

    head = ""
    if depth < 2:
        try:
            e = o[0]
            el = _safe_len(e)
            if el is not None:
                head = " first=%s(len=%d)" % (type(e).__name__, el)
            else:
                head = " first=%s(%r)" % (type(e).__name__, e)
        except Exception:
            pass
    return "%s(len=%d)%s" % (name, n, head)


def peek(seq, k=6):
    """取前 k 个值，取不到就跳过。"""
    vals = []
    for i in range(k):
        try:
            vals.append("%.4g" % float(seq[i]))
        except Exception:
            try:
                sub = seq[i]
                try:
                    vals.append("%s(len=%d)" % (type(sub).__name__, len(sub)))
                except Exception:
                    vals.append(type(sub).__name__)
            except Exception:
                break
    return vals


def main():
    try:
        os.remove(LOG)
    except Exception:
        pass

    log("=" * 56)
    log(" 官方例程体检")
    log("=" * 56)
    try:
        log(" 固件: %s / %s" % (sys.implementation[0], sys.version))
    except Exception:
        pass

    # ---------- Q1: 相机 ----------
    log("")
    log("[1] PipeLine   AI帧 %dx%d   屏 %dx%d   mode=%s"
        % (FRAME[0], FRAME[1], DISP[0], DISP[1], MODE))
    from libs.PipeLine import PipeLine

    try:
        pl = PipeLine(rgb888p_size=list(FRAME),
                      display_size=list(DISP),
                      display_mode=MODE)
        log("    对象创建 OK")
    except Exception as e:
        log("!!  PipeLine() 构造失败: %s" % e)
        return

    t0 = time.ticks_ms()
    try:
        pl.create()
    except Exception as e:
        log("!!  create() 抛异常: %s" % e)
        return
    log("    create() 返回，用时 %d ms" % time.ticks_diff(time.ticks_ms(), t0))
    try:
        log("    屏幕实测 %s" % (pl.get_display_size(),))
    except Exception:
        pass

    # ---------- 取帧 ----------
    log("")
    log("[2] 连续取 100 次 get_frame()，看 chn2 到底出不出帧")
    got = 0
    miss = 0
    img = None
    for i in range(100):
        f = pl.get_frame()
        if f is None:
            miss += 1
            time.sleep_ms(10)
        else:
            got += 1
            if img is None:
                img = f
    log("    出帧 %d 次 / 失败 %d 次" % (got, miss))
    if got:
        log("    帧类型: %s   shape=%s"
            % (type(img).__name__, getattr(img, "shape", "?")))
    else:
        log("!!  一帧都没拿到 —— 就是串口刷的 snapshot chn(2) failed")
        log("    后面两步跳过。先解决 chn2，跟模型无关。")
        log("")
        log("=" * 56)
        log(" 结论：相机 chn2 不出帧，模型没问题也发挥不了")
        log("=" * 56)
        return

    # ---------- Q2/Q3: 模型 ----------
    log("")
    log("[3] YOLOv8   %s" % KMODEL)
    try:
        os.stat(KMODEL)
    except Exception:
        log("!!  模型文件不存在: %s" % KMODEL)
        return
    from libs.YOLO import YOLOv8

    try:
        model = YOLOv8(task_type="detect", mode="video",
                       kmodel_path=KMODEL, labels=COCO,
                       rgb888p_size=list(FRAME),
                       model_input_size=[320, 320],
                       display_size=list(DISP),
                       conf_thresh=0.25, nms_thresh=0.45,
                       debug_mode=0)
        log("    构造 OK")
    except Exception as e:
        log("!!  构造失败: %s" % e)
        return
    try:
        model.config_preprocess()
        log("    config_preprocess OK")
    except Exception as e:
        log("!!  config_preprocess 失败: %s" % e)

    # ---------- 官方主循环 ----------
    log("")
    log("[4] 按官方例程跑 %d 帧" % NFRAMES)
    log("    官方写法: res = model.run(img); model.draw_result(res, pl.osd_img)")
    ok = 0
    noneCnt = 0
    drew = 0
    ppInfo = 0
    classes = {}
    firstShown = False

    for i in range(NFRAMES):
        try:
            img = pl.get_frame()
        except Exception as e:
            log("!!  第 %d 帧 get_frame 异常: %s" % (i, e))
            break
        if img is None:
            noneCnt += 1
            time.sleep_ms(10)
            continue
        ok += 1

        t1 = time.ticks_ms()
        try:
            res = model.run(img)
        except Exception as e:
            log("!!  run() 异常: %s" % e)
            break
        tRun = time.ticks_diff(time.ticks_ms(), t1)

        # ---- 只看第一帧的结构，后面的会刷屏 ----
        if not firstShown:
            firstShown = True
            log("")
            log("    ---- 第 1 帧 run() 的返回值 ----")
            log("    类型 %s   len=%s" % (type(res).__name__,
                                         _safe_len(res)))
            if _safe_len(res) is not None:
                for j in range(min(4, _safe_len(res))):
                    log("      res[%d] = %s" % (j, describe(res[j])))
            log("    前几个值: %s" % (peek(res),))

        # ---- 官方画框写法 ----
        try:
            model.draw_result(res, pl.osd_img)
            pl.show_image()
            drew += 1
        except Exception as e:
            if drew == 0:
                log("!!  draw_result(res, osd_img) 失败: %s" % e)
                log("    说明 run() 的返回值不是 draw_result 想要的格式")

        # ---- 单独试一次 postprocess，确认它的结构 ----
        if ppInfo < 1:
            ppInfo += 1
            try:
                out = model.postprocess(res)
                log("")
                log("    ---- postprocess(run 结果) ----")
                log("    类型 %s   len=%s"
                    % (type(out).__name__, _safe_len(out)))
                if _safe_len(out) is not None:
                    for j in range(min(4, _safe_len(out))):
                        log("      out[%d] = %s" % (j, describe(out[j])))
                # 如果是 (框,序号,分数) 结构，把序号和分数说出来
                try:
                    if _safe_len(out) == 3:
                        ids = out[1]
                        confs = out[2]
                        n = _safe_len(ids) or 0
                        log("      检测到 %d 个目标" % n)
                        for j in range(min(8, n)):
                            try:
                                cid = int(ids[j])
                                nm = COCO[cid] if 0 <= cid < len(COCO) else "?"
                                classes[cid] = max(
                                    classes.get(cid, 0.0), float(confs[j]))
                                log("        序号 %d (%s)  conf=%.3f"
                                    % (cid, nm, float(confs[j])))
                            except Exception:
                                pass
                except Exception as e:
                    log("    解析序号失败: %s" % e)
            except Exception as e:
                log("    postprocess 异常: %s" % e)

        gc.collect()
        if ok % 10 == 0:
            log("    ... 已跑 %d 帧，单帧 run() %d ms" % (ok, tRun))

    # ---------- 汇总 ----------
    log("")
    log("[5] 汇总")
    log("    取到帧 %d 次，None %d 次" % (ok, noneCnt))
    log("    run() 走通 %d 次，draw_result 成功 %d 次" % (ok, drew))
    if classes:
        items = []
        for cid in classes:
            nm = COCO[cid] if 0 <= cid < len(COCO) else "?"
            items.append("%d(%s)=%.2f" % (cid, nm, classes[cid]))
        log("    第一帧看到的类别: %s" % ", ".join(items))
    else:
        log("    第一帧没有任何类别（可能是空画面，正常）")

    log("")
    log("=" * 56)
    if ok and drew:
        log(" 结论：相机出帧、模型推理、官方画框 —— 全都正常")
        log("       如果主程序还是不识别，问题一定在解析/消抖/触发区")
    elif ok and not drew:
        log(" 结论：相机和模型都跑起来了，但 draw_result 不认 run() 的返回值")
        log("       看上面 [4] 打印的结构，就能确定 run() 给的是张量还是框")
    else:
        log(" 结论：相机没出帧，先解决 chn2")
    log("=" * 56)

    try:
        model.deinit()
    except Exception:
        pass
    try:
        pl.destroy()
    except Exception:
        pass


def _safe_len(o):
    try:
        return len(o)
    except Exception:
        return None


main()
