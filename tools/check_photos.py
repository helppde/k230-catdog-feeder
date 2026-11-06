"""检查存下来的照片到底是不是"灰图"。

为什么要这个工具：
    现场出过一次 —— 串口每次都在刷 "[up] 上传成功"，网页记录一条不少、
    图片文件也有几十 KB，看起来全都正常。但把 JPEG 拆开看像素才发现是
    **一片灰**：识别完全正常（模型在平面格式的帧上认得出猫狗），可存图
    那一步把数据读错了。

    这种失败不会报错、不会丢记录，文件大小还很"正常"，肉眼看日志根本
    发现不了。所以干脆给个工具，一眼看穿。

怎么判断：
    正常照片 -> 有明暗结构，8x8 块的块均值方差很大；
    灰图/雪花 -> 块内全是噪声、块均值几乎不变。

用法：
    python tools/check_photos.py             # 检查全部
    python tools/check_photos.py --last 10   # 只看最近 10 张
    python tools/check_photos.py --quiet     # 只打结论

退出码：0 = 全部正常；1 = 有灰图（方便写进脚本/CI）。
"""
import os
import sqlite3
import sys
import time

DB = r"pc\data\visits.db"
IMG = os.path.join("pc", "data", "images")

QUIET = "--quiet" in sys.argv


def _last_n():
    for i, a in enumerate(sys.argv):
        if a == "--last" and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                return None
    return None


def analyse(path):
    """返回 (是否灰图, 说明)。依赖 numpy + PIL（服务端本来就要用）。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception as e:
        return None, "缺依赖（%s），装一下 numpy pillow" % e

    try:
        im = Image.open(path).convert("L")
    except Exception as e:
        return None, "打不开：%s" % e

    a = np.asarray(im).astype(np.float32)
    if a.size == 0:
        return None, "空图"

    h8 = a.shape[0] // 8 * 8
    w8 = a.shape[1] // 8 * 8
    if h8 < 8 or w8 < 8:
        return None, "尺寸太小（%s）" % (im.size,)

    blk = a[:h8, :w8].reshape(h8 // 8, 8, w8 // 8, 8)
    inner = blk.var(axis=(1, 3)).mean()          # 块内方差
    means = blk.mean(axis=(1, 3)).var()          # 块均值的方差

    # 灰图：块内是噪声、块之间没有结构
    is_grey = means < 60 and a.std() < 40
    detail = "块内方差%.0f 块间方差%.0f 标准差%.0f 均值%.0f" % (
        inner, means, a.std(), a.mean())
    return is_grey, detail


def main():
    n_last = _last_n()
    rows = []

    if os.path.isfile(DB):
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        q = "SELECT id, ts, species, image FROM visits ORDER BY id DESC"
        if n_last:
            q += " LIMIT %d" % n_last
        rows = list(c.execute(q))
        rows.reverse()
    else:
        print("没有数据库：%s（直接扫图片目录）" % DB)

    # 数据库里没有的图片文件也一起看
    seen = set()
    for r in rows:
        if r["image"]:
            seen.add(os.path.normpath(os.path.join(IMG, r["image"].replace("/", os.sep))))

    files = []
    for r in rows:
        if not r["image"]:
            continue
        files.append((r["id"], r["ts"], r["species"],
                      os.path.join(IMG, r["image"].replace("/", os.sep))))

    if not rows:
        for root, _dirs, names in os.walk(IMG):
            for n in sorted(names):
                if n.lower().endswith((".jpg", ".jpeg", ".png")) and "_t." not in n:
                    files.append((None, None, None, os.path.join(root, n)))

    if not files:
        print("没找到任何照片。先让板子传一张过来。")
        return 0

    grey = 0
    bad = 0
    if not QUIET:
        print("%-5s %-19s %-5s %-9s %s" % ("id", "时间", "物种", "结论", "细节"))
        print("-" * 78)
    for (vid, ts, sp, path) in files:
        if not os.path.isfile(path):
            bad += 1
            if not QUIET:
                print("%-5s %-19s %-5s %-9s 文件不存在 %s"
                      % (vid, "-", sp or "-", "缺失", path))
            continue
        is_grey, detail = analyse(path)
        if is_grey is None:
            bad += 1
            verdict = "无法判断"
        elif is_grey:
            grey += 1
            verdict = "灰图!!"
        else:
            verdict = "正常"
        if not QUIET:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"
            print("%-5s %-19s %-5s %-9s %s"
                  % (vid if vid is not None else "-", when, sp or "-",
                     verdict, detail))

    print()
    total = len(files)
    print("共 %d 张：正常 %d，灰图 %d，无法判断 %d"
          % (total, total - grey - bad, grey, bad))
    if grey:
        print("")
        print("灰图说明拍照那一步拿到的不是画面内容。检查串口有没有这两行：")
        print("    [up] 拍照走专用通道 chn1（打包 RGB888 ...）")
        print("    [up] 拍照转换 OK：...（抽样 9/9 与源帧一致 ...）")
        print("两行都没有 -> 两条路都没走通，把那几行日志发出来。")
    return 1 if (grey or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
