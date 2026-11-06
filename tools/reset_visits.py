"""清空所有到访记录和图片，让"串口 / 后台 / 网页"从 1 开始重新对齐。

做完了会剩一个空库：网页显示 0 条，板子下次到访是第 1 条 —— 这样
"串口第 N 次到访" 和 "网页第 N 条" 就能一一对上地看。

用法：
    python tools/reset_visits.py            # 只预览
    python tools/reset_visits.py --apply    # 真删（先自动备份 db）
"""
import os
import shutil
import sqlite3
import sys
import time

DB = r"pc\data\visits.db"
IMG = os.path.join(os.path.dirname(os.path.abspath(DB)), "images")
APPLY = "--apply" in sys.argv


def main():
    if not os.path.isfile(DB):
        print("没有数据库：%s" % DB)
        return 1

    c = sqlite3.connect(DB)
    n_visit = c.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    n_ind = c.execute("SELECT COUNT(*) FROM individuals").fetchone()[0]
    n_emb = c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    n_img = 0
    for root, _dirs, files in os.walk(IMG):
        n_img += sum(1 for f in files if f.lower().endswith((".jpg", ".jpeg", ".png")))

    print("将清空：")
    print("  到访记录   %d 条" % n_visit)
    print("  个体       %d 个（连带特征向量 %d 条）" % (n_ind, n_emb))
    print("  图片文件   %d 个" % n_img)

    if not APPLY:
        print("\n（只预览；加 --apply 才真删）")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = "%s.bak_%s" % (DB, stamp)
    shutil.copy2(DB, bak)
    print("\n已备份数据库 -> %s" % bak)

    with c:
        c.execute("DELETE FROM visits")
        c.execute("DELETE FROM embeddings")
        c.execute("DELETE FROM individuals")
        # 自增 id 也归零，下次就是 #1
        try:
            c.execute("DELETE FROM sqlite_sequence")
        except sqlite3.OperationalError:
            pass          # 表还没用过，没有这张表

    removed = 0
    for root, _dirs, files in os.walk(IMG):
        for f in files:
            if not f.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            try:
                os.remove(os.path.join(root, f))
                removed += 1
            except OSError as e:
                print("  删文件失败 %s: %s" % (f, e))
    print("删掉 %d 个图片文件" % removed)
    print("库里现在：%d 条到访，%d 个个体"
          % (c.execute("SELECT COUNT(*) FROM visits").fetchone()[0],
             c.execute("SELECT COUNT(*) FROM individuals").fetchone()[0]))
    print("\n需要重启服务（网页才会显示 0）：重启 pc\\run.py 即可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
