# -*- coding: utf-8 -*-
"""
识别探测 —— 定位"模型在跑，但就是不识别猫狗"

这个工具只做一件事：把模型**看到的东西**原样打出来，不消抖、不过滤、
不写 GPIO、不上传。这样你能一眼看出问题出在哪一层：

    A) 一个框都没有，labels 也没东西     -> 模型没加载 / kmodel 不匹配
    B) 有一堆框，但全是 person/car/...   -> LABELS 或类别顺序不对，或者场景里真没猫狗
    C) 有 cat/dog，但置信度很低(0.2~0.4)  -> 阈值太高 / 画面太暗 / 目标太小 / 宽高比被拉伸
    D) 有 cat/dog，置信度也高，但位置在触发区外 -> TRIGGER_ZONE 设错了
    E) 都正常，只是偶尔漏帧            -> 正常，主程序有消抖，不用管

怎么用（CanMV IDE 里打开本文件直接运行）：
    det_probe.py          连续跑，每 2 秒打一行统计
    det_probe.py 30       只跑 30 秒后退出

输出示例：
    [probe] #042  帧率 11.2  框数 1
             cat 0.87  box=(212,140,168,190)  中心(296,235) 在区内
    [probe] 近 2s 类别汇总: cat(0.87) person(0.55)

⚠️ 这个文件是**独立脚本**，不 import k230_trigger，避免改坏主程序。
   但下面的配置默认从 `config.py` 读（存在的话），读不到才用这里的兜底值。
这样改了 `config.py` 的 KMODEL_PATH 就不用再来改这里了。
"""

import sys
import time

# ============================================================
# 配置：优先用 config.py（单一事实来源），没有才用兜底值
# ============================================================
RGB888P_SIZE = (640, 480)
DISPLAY_SIZE = (640, 480)
DISPLAY_MODE = "lcd"
KMODEL_PATH = "/sdcard/kmodel/yolov8n_320.kmodel"
MODEL_INPUT_SIZE = (320, 320)
CONF_THRESH = 0.40          # 模型自己的门槛（放低一点才能看到"低分候选"）
NMS_THRESH = 0.45
RESIZE_MODE = "padding"
TRIGGER_ZONE = (0, 0, 640, 480)

# 探测时故意把门槛降到很低 —— 目的是看到"模型其实有反应，只是分不够"，
# 而不是判断"合不合格"。低分候选全都会打印出来，带 [LOW] 标记。
PROBE_CONF = 0.15

LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


def _add_import_paths():
    """让 config.py 无论在当前目录、上一层、还是本文件旁边都能被 import。

    CanMV 上所有 .py 平铺在 /sdcard，"." 就够了；
    在 PC 上从项目根跑 tools/xxx.py 时 config.py 在根目录（= "."），
    从 tools/ 里跑时在上一层（= ".."）。
    全部用 append，不动 sys.path[0]，免得顶掉固件自己的库。
    """
    cands = [".", ".."]
    try:
        here = __file__.replace("\\", "/").rsplit("/", 1)[0]
        cands.append(here)
        cands.append(here + "/..")
    except Exception:
        pass
    for p in cands:
        try:
            if p not in sys.path:
                sys.path.append(p)
        except Exception:
            pass


def _apply_config():
    """把 config.py 里的值搬过来。缺项就用上面的兜底值，绝不因此崩掉。"""
    global RGB888P_SIZE, DISPLAY_SIZE, DISPLAY_MODE, KMODEL_PATH
    global MODEL_INPUT_SIZE, LABELS, RESIZE_MODE, TRIGGER_ZONE
    _add_import_paths()
    try:
        import config as C
    except Exception as e:
        print("[probe] 没读到 config.py（%s），用本文件内的兜底配置" % e)
        return
    for name in ("RGB888P_SIZE", "DISPLAY_SIZE", "DISPLAY_MODE", "KMODEL_PATH",
                 "MODEL_INPUT_SIZE", "LABELS", "RESIZE_MODE", "TRIGGER_ZONE"):
        if hasattr(C, name):
            globals()[name] = getattr(C, name)
    print("[probe] 已从 config.py 读取: KMODEL=%s 输入=%s"
          % (KMODEL_PATH, tuple(MODEL_INPUT_SIZE)))


_apply_config()

REPORT_EVERY_MS = 2000


def resolve_classes(labels):
    cat = dog = -1
    for i, name in enumerate(labels):
        n = str(name).strip().lower()
        if n == "cat" and cat < 0:
            cat = i
        elif n == "dog" and dog < 0:
            dog = i
    return cat, dog


CLASS_CAT, CLASS_DOG = resolve_classes(LABELS)


def in_zone(box):
    z = TRIGGER_ZONE
    cx = box[0] + box[2] * 0.5
    cy = box[1] + box[3] * 0.5
    return (z[0] <= cx <= z[0] + z[2]) and (z[1] <= cy <= z[1] + z[3])


def name_of(cid):
    return LABELS[cid] if 0 <= cid < len(LABELS) else "?%d" % cid


# ============================================================
# 加载模型
# ============================================================
def build_model():
    cls = None
    for n in ("YOLO11", "YOLOv8", "YOLOv5"):
        try:
            mod = __import__("libs.YOLO", None, None, [n])
            cls = getattr(mod, n, None)
            if cls is not None:
                print("[probe] 使用 libs.YOLO.%s" % n)
                break
        except Exception:
            continue
    if cls is None:
        print("[probe] !! libs.YOLO 不可用 —— 固件里没有 YOLO 库")
        print("        用亚博出厂固件 CanMV_K230_YAHBOOM_micropython_V1.3.x")
        return None

    base = dict(kmodel_path=KMODEL_PATH, labels=LABELS,
                rgb888p_size=list(RGB888P_SIZE),
                display_size=list(DISPLAY_SIZE),
                conf_thresh=PROBE_CONF, nms_thresh=NMS_THRESH,
                debug_mode=0)
    model = None
    for kw in (dict(model_input_size=list(MODEL_INPUT_SIZE), **base), base):
        try:
            model = cls(task_type="detect", mode="video", **kw)
            break
        except TypeError:
            continue
        except Exception as e:
            print("[probe] !! 模型加载失败: %s" % e)
            print("         检查 kmodel 路径、以及 kmodel 和固件版本是否匹配")
            return None
    if model is None:
        model = cls(mode="video", **base)

    for kw in ({"resize_mode": RESIZE_MODE}, {}):
        try:
            model.config_preprocess(**kw)
            break
        except TypeError:
            continue
        except Exception as e:
            print("[probe] config_preprocess 异常: %s" % e)
            break

    print("[probe] 模型就绪: %s" % KMODEL_PATH)
    print("[probe] 类别映射: cat=%d dog=%d  (LABELS 共 %d 类)"
          % (CLASS_CAT, CLASS_DOG, len(LABELS)))
    if CLASS_CAT < 0 or CLASS_DOG < 0:
        print("[probe] !! LABELS 里找不到 cat/dog，把模型真实类别顺序填进 LABELS")
    return model


# ============================================================
# 主循环
# ============================================================
def main():
    dur = 0
    if len(sys.argv) > 1:
        try:
            dur = int(sys.argv[1])
        except ValueError:
            dur = 0

    print("=" * 60)
    print(" 识别探测（只看不打）")
    print("=" * 60)
    print(" AI帧 %dx%d   模型输入 %dx%d   resize=%s"
          % (RGB888P_SIZE[0], RGB888P_SIZE[1],
             MODEL_INPUT_SIZE[0], MODEL_INPUT_SIZE[1], RESIZE_MODE))
    print(" 判定门槛 conf>=%.2f   探测门槛 conf>=%.2f（故意调低）"
          % (CONF_THRESH, PROBE_CONF))
    print(" 触发区 %s" % (TRIGGER_ZONE,))
    print("-" * 60)

    from libs.PipeLine import PipeLine
    pl = PipeLine(rgb888p_size=list(RGB888P_SIZE),
                  display_size=list(DISPLAY_SIZE),
                  display_mode=DISPLAY_MODE)
    pl.create()

    model = build_model()
    if model is None:
        pl.destroy()
        return

    W, H = RGB888P_SIZE
    t_start = time.ticks_ms()
    last_report = t_start
    frames = 0
    cat_frames = 0
    dog_frames = 0
    total_frames = 0

    try:
        while True:
            img = pl.get_frame()
            if img is None:
                time.sleep_ms(5)
                continue
            total_frames += 1

            res = model.run(img) or []
            frames += 1

            now = time.ticks_ms()
            if time.ticks_diff(now, last_report) < REPORT_EVERY_MS:
                continue
            dt = time.ticks_diff(now, last_report) / 1000.0
            fps = frames / dt if dt > 0 else 0
            last_report = now
            frames = 0

            if res:
                print("[probe] #%03d  帧率 %.1f  框数 %d"
                      % (total_frames, fps, len(res)))
            else:
                print("[probe] #%03d  帧率 %.1f  框数 0  <- 这一帧什么都没看到"
                      % (total_frames, fps))

            seen = {}
            for r in res:
                try:
                    cid = int(r[4])
                    conf = float(r[5])
                    box = (r[0], r[1], r[2], r[3])
                except Exception:
                    continue
                seen[cid] = max(seen.get(cid, 0.0), conf)

                is_cat = (cid == CLASS_CAT)
                is_dog = (cid == CLASS_DOG)
                if is_cat:
                    cat_frames += 1
                elif is_dog:
                    dog_frames += 1

                mark = ""
                if not (is_cat or is_dog):
                    mark = "  <- 不是猫狗，会被丢掉"
                elif conf < CONF_THRESH:
                    mark = "  <- [LOW] 低于判定门槛 %.2f" % CONF_THRESH
                elif not in_zone(box):
                    mark = "  <- 中心在触发区外"
                else:
                    mark = "  <- OK 会触发"

                print("         %-8s %.2f  box=(%d,%d,%d,%d)  中心(%d,%d)%s"
                      % (name_of(cid), conf,
                         box[0], box[1], box[2], box[3],
                         box[0] + box[2] * 0.5, box[1] + box[3] * 0.5, mark))

            # 每 10 秒给一次结论，省得自己翻日志
            if total_frames % 50 == 0 and seen:
                if CLASS_CAT not in seen and CLASS_DOG not in seen:
                    print("[probe] >> 只有非猫狗类别：LABELS 顺序可能和 kmodel 不一致")
                elif max(seen.get(CLASS_CAT, 0), seen.get(CLASS_DOG, 0)) < CONF_THRESH:
                    print("[probe] >> 认出猫狗了但分数偏低：试试把 CONF_THRESH 降到 0.30，"
                          "或改善光照 / 让摄像头靠近一点")

            if dur > 0 and time.ticks_diff(now, t_start) >= dur * 1000:
                break
    except KeyboardInterrupt:
        print("\n[probe] 手动停止")
    finally:
        try:
            model.deinit()
        except Exception:
            pass
        try:
            pl.destroy()
        except Exception:
            pass
        print("-" * 60)
        print("[probe] 共 %d 帧，其中检测到猫 %d 帧、狗 %d 帧"
              % (total_frames, cat_frames, dog_frames))
        if cat_frames == 0 and dog_frames == 0:
            print("[probe] 一次猫狗都没识别到。按顺序查：")
            print("  1) 上面有没有打出 LABELS / 模型就绪？没有 -> 模型没加载")
            print("  2) 有框但全是别的类别 -> LABELS 和 kmodel 的类别顺序对不上")
            print("  3) 完全没有框 -> 镜头盖/太暗/模型输入尺寸填错")
            print("  4) 分数都在 0.2~0.4 -> 把 CONF_THRESH 降到 0.30 试")
        else:
            print("[probe] 识别正常。如果主程序还是不开饭，检查 TRIGGER_ZONE。")


if __name__ == "__main__":
    main()
