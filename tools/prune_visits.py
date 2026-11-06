"""清掉"网页数据和串口对不上"的两类脏数据。

两类问题：

1. **测试数据混进生产库**
   `tools/sim_k230_upload.py` 发的是随机噪声图（`noisy_jpeg`），
   device 写的是 `k230-sim`。这些记录在网页上就是一块灰噪点，
   而且串口上根本没有对应的识别 —— 看起来像"网页凭空多出来几次到访"。

2. **同一张照片存成多条**
   板子读响应超时后会重传同一张图，早先没有 uid 去重，一张照片存了 N 条。

用法：
    python tools/prune_visits.py                    # 只看，不动
    python tools/prune_visits.py --apply            # 真删
    python tools/prune_visits.py --devices k230-sim,selftest --apply
"""
import hashlib
import os
import sqlite3
import sys
from collections import defaultdict

DB = r"pc\data\visits.db"
IMG = os.path.join(os.path.dirname(os.path.abspath(DB)), "images")
REAL_DEVICE = "k230-01"          # 板子自己的 device id
TEST_DEVICES = ("k230-sim", "selftest", "test", "k230-test")
APPLY = "--apply" in sys.argv


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def sha1_file(path):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 16), b""):
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def drop_rows(c, rows, why):
    files = 0
    for r in rows:
        for rel in (r["image"], r["thumb"]):
            if not rel:
                continue
            p = os.path.join(IMG, rel.replace("/", os.sep))
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    files += 1
                except OSError as e:
                    print("   删文件失败 %s: %s" % (p, e))
        c.execute("DELETE FROM visits WHERE id=?", (r["id"],))
    print("  %s：删 %d 条记录、%d 个文件" % (why, len(rows), files))
    print("    ids = %s" % [r["id"] for r in rows])


def main():
    devs = _arg("--devices")
    test_devs = tuple(d.strip() for d in devs.split(",")) if devs else TEST_DEVICES

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM visits ORDER BY id").fetchall()
    print("总记录 %d 条" % len(rows))

    # ---- 1. 按设备统计 ----
    by_dev = defaultdict(list)
    for r in rows:
        by_dev[r["device"] or "(空)"].append(r)
    print("\n按设备：")
    for dev, items in sorted(by_dev.items(), key=lambda kv: -len(kv[1])):
        mark = "  <- 真实板子" if dev == REAL_DEVICE else (
            "  <- 测试数据" if dev in test_devs else "")
        print("  %-12s %3d 条%s" % (dev, len(items), mark))

    junk = [r for r in rows if (r["device"] or "") in test_devs]
    # ---- 2. 内容完全相同的重复 ----
    groups = defaultdict(list)
    for r in rows:
        if r["device"] in test_devs or not r["image"]:
            continue
        p = os.path.join(IMG, r["image"].replace("/", os.sep))
        h = sha1_file(p)
        if h:
            groups[h].append(r)
    dups = []
    dup_groups = []
    for h, items in groups.items():
        if len(items) > 1:
            dup_groups.append(items)
            kept = items[0]
            extra = items[1:]
            print("\n  内容相同的 %d 条（sha=%s…）：留 #%s，删 %s"
                  % (len(items), h[:8], kept["id"], [e["id"] for e in extra]))
            dups.extend(extra)

    # ---- 3. 没有到访的孤儿个体 ----
    # 测试数据被删之后，它们留下的"个体"还在网页上列表里挂着（0 次），
    # 而且它们的特征向量是从噪声图算出来的 —— 留着会让真正的猫狗匹配错。
    orphans = c.execute(
        """SELECT * FROM individuals i
           WHERE NOT EXISTS (SELECT 1 FROM visits v WHERE v.individual_id=i.id)
           ORDER BY i.id""").fetchall()
    if orphans:
        print("\n没有到访的个体（会一起删，连带它们的特征向量）：")
        for r in orphans:
            n_emb = c.execute(
                "SELECT COUNT(*) FROM embeddings WHERE individual_id=?",
                (r["id"],)).fetchone()[0]
            print("  id=%-3s %-4s name=%r  向量 %d 条"
                  % (r["id"], r["species"], r["name"], n_emb))

    print("\n计划：删测试数据 %d 条，删重复 %d 条，删孤儿个体 %d 个，共 %d 条记录"
          % (len(junk), len(dups), len(orphans), len(junk) + len(dups)))
    if not (junk or dups or orphans):
        print("没有需要清理的。")
        return 0
    if not APPLY:
        print("（只预览；加 --apply 才真删）")
        return 0

    if junk:
        drop_rows(c, junk, "测试数据")
    for items in dup_groups:
        drop_rows(c, items[1:], "重复照片")
    for r in orphans:
        c.execute("DELETE FROM embeddings WHERE individual_id=?", (r["id"],))
        c.execute("DELETE FROM individuals WHERE id=?", (r["id"],))
    if orphans:
        print("  孤儿个体：删 %d 个" % len(orphans))
    c.commit()
    print("\n清理完成，剩 %d 条"
          % c.execute("SELECT COUNT(*) FROM visits").fetchone()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
