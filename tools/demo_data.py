# -*- coding: utf-8 -*-
"""
往数据库里塞一批"假到访"，用来在硬件还没做好的时候先看网页长什么样。

会真的生成图片文件、真的算特征向量（所以以后真猫来也能对上），
只是时间戳是编的——分布在过去 7 天的不同时段。

用法（在项目根目录）：
    python tools/demo_data.py --yes
    python tools/demo_data.py --yes --days 14 --per-individual 8

⚠️ 默认写进 pc/data（就是真数据目录）。想不污染：
    set CATDOG_DATA_DIR=%TEMP%\catdog_demo
    python tools/demo_data.py --yes
"""

import argparse
import math
import os
import random
import sys
import time

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pc"))

import config as PC          # noqa: E402
import db as dbmod           # noqa: E402
import identity              # noqa: E402


# 5 个"个体"：名字 + 主色 + 花纹
ANIMALS = [
    ("cat", "橘猫老大", (222, 138, 52), "stripe"),
    ("cat", "小黑",     (46, 46, 54),   "solid"),
    ("cat", "三花",     (238, 232, 220), "patch"),
    ("dog", "大黄",     (198, 150, 74), "solid"),
    ("dog", "小白",     (245, 245, 242), "patch"),
]


def make_image(base_rgb, pattern, seed, size=(360, 300)):
    rnd = random.Random(seed)
    img = Image.new("RGB", size, base_rgb)
    d = ImageDraw.Draw(img)

    # 背景渐变，像户外的光
    for y in range(size[1]):
        k = 0.75 + 0.35 * (y / size[1])
        d.line([(0, y), (size[0], y)],
               fill=tuple(min(255, int(c * k)) for c in base_rgb))

    # 主体：一个圆当脑袋 + 两个三角当耳朵
    cx, cy, rr = size[0] // 2, int(size[1] * 0.56), int(size[1] * 0.29)
    body = tuple(min(255, int(c * 1.15) + 12) for c in base_rgb)
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=body)
    d.polygon([(cx - rr, cy - rr + 10), (cx - rr - 26, cy - rr - 42),
               (cx - rr + 34, cy - rr - 6)], fill=body)
    d.polygon([(cx + rr, cy - rr + 10), (cx + rr + 26, cy - rr - 42),
               (cx + rr - 34, cy - rr - 6)], fill=body)

    if pattern == "stripe":
        for i in range(-3, 4):
            x = cx + i * 22
            d.line([(x, cy - rr), (x - 12, cy)], fill=(120, 70, 26), width=7)
    elif pattern == "patch":
        for _ in range(6):
            px = rnd.randint(cx - rr, cx + rr)
            py = rnd.randint(cy - rr, cy + rr)
            pr = rnd.randint(12, 30)
            d.ellipse([px - pr, py - pr, px + pr, py + pr],
                      fill=(90, 66, 52) if base_rgb[0] > 150 else (60, 58, 60))

    # 眼睛
    for ex in (-rr // 2, rr // 2):
        d.ellipse([cx + ex - 9, cy - 12, cx + ex + 9, cy + 6], fill=(24, 24, 24))
        d.ellipse([cx + ex - 4, cy - 8, cx + ex + 2, cy - 2], fill=(240, 240, 240))

    # 一点噪点，让"同一只"的两张图不会完全一样
    px = img.load()
    for _ in range(1200):
        x = rnd.randrange(size[0])
        y = rnd.randrange(size[1])
        r, g, b = px[x, y]
        n = rnd.randint(-14, 14)
        px[x, y] = (min(255, max(0, r + n)), min(255, max(0, g + n)),
                    min(255, max(0, b + n)))
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="确认要往数据目录里写")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--per-individual", type=int, default=6)
    args = ap.parse_args()

    print("数据目录: %s" % PC.DATA_DIR)
    if not args.yes:
        print("\n这会往上面这个目录写演示数据。确认请加 --yes")
        print("想不污染正式数据，先设置环境变量 CATDOG_DATA_DIR 到一个临时目录。")
        return 0

    os.makedirs(PC.IMG_DIR, exist_ok=True)
    db = dbmod.DB(PC.DB_PATH)

    # 特征后端：演示数据用不上 GPU，直接手工特征，快
    if PC.EMBED_BACKEND == "auto":
        identity.EMBEDDER.load()

    now = time.time()
    rnd = random.Random(20261101)
    total = 0

    for species, name, rgb, pattern in ANIMALS:
        iid = db.create_individual(species, name, now=now - args.days * 86400)
        # 给这个个体算一个基准特征
        base = make_image(rgb, pattern, seed=hash(name) & 0xffff)
        vec = identity.EMBEDDER.embed(base)
        if vec is not None:
            db.add_embedding(iid, vec)

        for k in range(args.per_individual):
            day_ago = rnd.uniform(0, args.days)
            # 猫狗大多在清晨和傍晚来
            hour = rnd.choice([5, 6, 6, 7, 17, 18, 18, 19, 20, 2])
            ts = now - day_ago * 86400
            lt = time.localtime(ts)
            ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                              hour, rnd.randint(0, 59), rnd.randint(0, 59),
                              0, 0, -1))
            if ts > now:
                ts = now - rnd.uniform(60, 3600)

            img = make_image(rgb, pattern, seed=rnd.randrange(1 << 30))
            day = time.strftime("%Y%m%d", time.localtime(ts))
            folder = os.path.join(PC.IMG_DIR, day)
            os.makedirs(folder, exist_ok=True)
            fname = "demo_%d_%d" % (iid, k)
            full = os.path.join(folder, fname + ".jpg")
            thumb = os.path.join(folder, fname + "_t.jpg")
            img.save(full, "JPEG", quality=88)
            img.resize((PC.THUMB_WIDTH, int(PC.THUMB_WIDTH * img.height / img.width)),
                       Image.LANCZOS).save(thumb, "JPEG", quality=80)

            db.add_visit(species=species, ts=ts, individual_id=iid,
                         confidence=round(rnd.uniform(0.62, 0.95), 3),
                         box="420,260,300,340",
                         image="%s/%s.jpg" % (day, fname),
                         thumb="%s/%s_t.jpg" % (day, fname),
                         sim=round(rnd.uniform(0.84, 0.97), 3),
                         device="demo", nbytes=os.path.getsize(full))
            total += 1

    ov = db.overview()
    print("\n写入 %d 条演示到访，%d 个个体" % (total, len(ANIMALS)))
    print("猫 %d 次 / 狗 %d 次" % (ov["species"]["cat"], ov["species"]["dog"]))
    print("\n现在启动服务看效果： python pc/run.py")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
