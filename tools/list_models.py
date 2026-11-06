# -*- coding: utf-8 -*-
"""
K230 模型文件体检 —— 找出卡上到底有哪些 .kmodel

为什么要这个：
    k230_trigger.py 报「Kmodel file not exist.」的时候，你其实不知道
    卡上到底有没有模型、叫什么名、在哪个目录。以前只能靠猜路径，
    或者把卡拔下来插电脑翻。
    这个脚本 3 秒就能回答："卡上有哪些模型 / 我配的那个在不在"。

怎么用：
    1. CanMV IDE 打开本文件，直接运行（不需要改任何东西）
    2. 看串口输出：
       - 有列出文件 -> 把 KMODEL_PATH 改成列表里真正存在的那个路径
       - 一个都没有 -> 模型确实没放上去，见下面"模型从哪来"
    3. 改完 config.py 的 KMODEL_PATH 后再跑一遍，看到"✓ 配置的路径存在"

⚠️ 这个脚本**只查文件，不加载模型**。
   文件在、但加载仍然报错，那是参数/版本问题，去跑 tools/det_probe.py。
   （两者分工：本脚本查"有没有"，det_probe 查"能不能跑"）

模型从哪来：
    A. 自己做：train/README.md 有从标注到 nncase 转 kmodel 的全流程
    B. 板子自带：亚博出厂的 /sdcard/examples/ 里常带 yolov8n.kmodel 之类的例程模型，
       直接拿来做流程联调够用（类别是 COCO 80 类，cat=15 / dog=16）
"""

import os
import sys

# 想查别的目录就改这个。默认 /sdcard —— K230 上 TF 卡挂在 /sdcard。
ROOT = "/sdcard"
MAX_DEPTH = 4        # 递归深度。太深会慢，而且例程目录一般不超过 3 层
MAX_FILES = 30       # 最多列这么多，防止 /sdcard 被塞满图片时刷屏


def human(n):
    """字节数转成好读的大小。"""
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def scan(root=ROOT, max_depth=MAX_DEPTH):
    """递归找 .kmodel，返回 [(path, size)]。

    MicroPython 没有 os.walk，用 os.ilistdir 自己递归。
    ilistdir 每项是 (name, type[, inode, size])，type 的 0x4000 位是目录。
    任何一步失败都跳过 —— 没权限的目录不应该让整个检查挂掉。
    """
    out = []

    def walk(d, depth):
        if depth > max_depth or len(out) >= MAX_FILES:
            return
        try:
            # 注意：MicroPython 的 ilistdir 是**生成器**，目录不存在时
            # OSError 是在迭代时才抛出来的，不在调用这一行。
            # 所以 for 必须一起待在 try 里，否则 /sdcard 下某个目录没权限
            # 就会把整个体检炸掉。
            for ent in os.ilistdir(d):
                try:
                    name = ent[0]
                    typ = int(ent[1])
                except Exception:
                    continue
                if name in (".", ".."):
                    continue
                p = d.rstrip("/") + "/" + name
                if typ & 0x4000:
                    walk(p, depth + 1)
                elif str(name).lower().endswith(".kmodel"):
                    size = 0
                    try:
                        size = int(os.stat(p)[6])
                    except Exception:
                        pass
                    out.append((p, size))
                    if len(out) >= MAX_FILES:
                        return
        except Exception as e:
            print("  (跳过 %s: %s)" % (d, e))

    walk(root, 0)
    out.sort()
    return out


def _add_import_paths():
    """让 config.py 无论在当前目录、上一层、还是本文件旁边都能被 import。"""
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


def configured_path():
    """尽量从 config.py 读出 KMODEL_PATH；读不到就返回 None。"""
    _add_import_paths()
    try:
        import config as C
        return getattr(C, "KMODEL_PATH", None), getattr(C, "MODEL_INPUT_SIZE", None)
    except Exception as e:
        print("(读不到 config.py: %s —— 只做文件扫描)" % e)
        return None, None


def main():
    print("=" * 56)
    print("K230 模型文件体检")
    print("=" * 56)

    print("\n[1] 扫描 %s 下的 .kmodel（最多 %d 层）" % (ROOT, MAX_DEPTH))
    found = scan()
    if found:
        print("    找到 %d 个：" % len(found))
        for p, sz in found:
            print("      %-46s %s" % (p, human(sz)))
    else:
        print("    一个都没有。")

    cfg_path, cfg_size = configured_path()
    print("\n[2] 检查配置里的 KMODEL_PATH")
    if cfg_path is None:
        print("    没读到配置，跳过。手动确认 KMODEL_PATH 有没有写错。")
    else:
        print("    config.py -> %s" % cfg_path)
        exists = False
        try:
            exists = os.stat(cfg_path) is not None
        except Exception:
            # 老 MicroPython 的 stat 失败会抛 OSError，而不是返回 None
            exists = False
        if exists:
            try:
                sz = int(os.stat(cfg_path)[6])
            except Exception:
                sz = 0
            print("    ✓ 配置的路径存在，大小 %s" % human(sz))
            if sz == 0:
                print("    ⚠️  大小是 0 字节 —— 拷贝中断了，重新拷一次")
        else:
            print("    ✗ 配置的路径**不存在**，这就是报错的原因")
            if found:
                print("    -> 把 KMODEL_PATH 改成上面 [1] 里真实存在的那个路径")
            else:
                print("    -> 卡上没有任何 kmodel，需要先放一个上去（见文件头说明）")

    print("\n[3] 输入尺寸提示")
    if cfg_size is None:
        print("    没读到 MODEL_INPUT_SIZE，跳过。")
    else:
        print("    config.py -> MODEL_INPUT_SIZE = %s" % (tuple(cfg_size),))
        hint = "%dx%d" % (cfg_size[0], cfg_size[1])
        warn = False
        for p, _sz in found:
            for tag in ("320", "416", "512", "640"):
                if tag in p and tag != str(cfg_size[0]):
                    print("    ⚠️  %s 的名字里带 %s，而配置是 %s" % (p, tag, hint))
                    warn = True
                    break
        if not warn:
            print("    没看出文件名和尺寸冲突（文件名不一定带尺寸，以训练时为准）")

    print("\n" + "=" * 56)
    if found and cfg_path:
        print("下一步：文件对得上但加载还报错 -> 跑 tools/det_probe.py")
        print("        它会真的把模型跑起来，把每个框为什么不达标打出来")
    print("=" * 56)


if __name__ == "__main__":
    main()
