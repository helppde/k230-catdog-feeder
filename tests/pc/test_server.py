# -*- coding: utf-8 -*-
"""
PC 服务端端到端测试（不需要 K230，也不需要真的拍照）

做法：起一个真服务（随机端口），用 http.client 当 K230 往 /api/v1/visit 传图，
然后检查网页用的 /api/v1/overview 数字对不对，以及改名/合并/取图能不能用。

为了快且可复现，这里固定用"手工特征"后端（不加载 torch）。

运行（在项目根目录）：
    python tests/pc/test_server.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.client import HTTPConnection

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PC_DIR = os.path.join(ROOT, "pc")
sys.path.insert(0, PC_DIR)

import config as PC          # noqa: E402

# ---- 测试用配置：临时目录 + 手工特征（快、确定性好）----
TMP = tempfile.mkdtemp(prefix="catdog_pc_")
PC.DATA_DIR = TMP
PC.DB_PATH = os.path.join(TMP, "test.db")
PC.IMG_DIR = os.path.join(TMP, "images")
PC.EMBED_BACKEND = "handcrafted"
PC.MATCH_THRESHOLD = 0.90
PC.API_TOKEN = ""

import server                # noqa: E402
import identity              # noqa: E402


# ================================================================
# 造几张"猫片"：颜色分布明显不同，手工特征能分开
# ================================================================
def make_cat_a(shift=0, dim=1.0):
    img = Image.new("RGB", (240, 200), (int(210 * dim), int(120 * dim), int(40 * dim)))
    px = img.load()
    for y in range(200):
        for x in range(240):
            if ((x + shift) // 12 + y // 24) % 2 == 0:
                r, g, b = px[x, y]
                px[x, y] = (min(255, r + 40), min(255, g + 45), min(255, b + 35))
    return img


def make_cat_b():
    img = Image.new("RGB", (240, 200), (85, 88, 92))
    px = img.load()
    for y in range(200):
        for x in range(240):
            if x // 20 % 2 == 0:
                px[x, y] = (120, 122, 128)
    return img


def make_dog():
    img = Image.new("RGB", (240, 200), (30, 80, 200))
    px = img.load()
    for y in range(200):
        for x in range(240):
            if y // 25 % 2 == 0:
                px[x, y] = (220, 220, 240)
    return img


def jpeg_bytes(img, q=90):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


# ================================================================
# HTTP 小工具
# ================================================================
class Client(object):
    def __init__(self, port):
        self.port = port

    def req(self, method, path, body=None, headers=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Accept": "application/json"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r.getheader("Content-Type") or ""

    def upload(self, img, species, conf=0.9, device="k230-test", box="0,0,240,200"):
        q = ("/api/v1/visit?device=%s&species=%s&conf=%s&box=%s&ws=1280&hs=720"
             % (device, species, conf, box))
        st, data, _ = self.req("POST", q, jpeg_bytes(img),
                               {"Content-Type": "image/jpeg"})
        return st, (json.loads(data.decode()) if data else {})

    def overview(self):
        st, data, _ = self.req("GET", "/api/v1/overview")
        assert st == 200, "overview 返回 %d" % st
        return json.loads(data.decode())

    def post_json(self, path, obj):
        st, data, _ = self.req("POST", path, json.dumps(obj).encode(),
                               {"Content-Type": "application/json"})
        return st, data


def main():
    srv = server.create_server("127.0.0.1", 0, PC.DB_PATH)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    c = Client(port)

    ok = 0
    total = 0

    def check(cond, what):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print("  [ ok ] %s" % what)
        else:
            print("  [FAIL] %s" % what)

    try:
        print("\n--- 1. 健康检查 ---")
        st, data, _ = c.req("GET", "/api/v1/health")
        health = json.loads(data.decode())
        check(st == 200 and health["ok"], "GET /api/v1/health -> 200")
        check(health["backend"] in ("handcrafted", "torch", "loading"),
              "特征后端 = %s" % health["backend"])

        print("\n--- 2. 上传照片（同一只猫来两次）---")
        st, r1 = c.upload(make_cat_a(), "cat")
        check(st == 200 and r1.get("ok"), "第 1 张猫上传成功")
        check(r1.get("is_new") is True, "第 1 张 -> 新个体 %s" % r1.get("name"))
        first_id = r1.get("individual_id")

        st, r2 = c.upload(make_cat_a(shift=3, dim=0.97), "cat")
        check(st == 200 and r2.get("ok"), "第 2 张猫上传成功")
        check(r2.get("individual_id") == first_id,
              "同一只猫认成同一个体（sim=%.3f）" % (r2.get("sim") or 0))
        check(r2.get("visits") == 2, "这个个体累计 2 次（实际 %s）" % r2.get("visits"))

        print("\n--- 3. 另外两只动物 ---")
        st, r3 = c.upload(make_cat_b(), "cat")
        check(r3.get("is_new") is True, "另一只猫 -> 新个体 %s" % r3.get("name"))
        second_id = r3.get("individual_id")

        st, r4 = c.upload(make_dog(), "dog")
        check(r4.get("is_new") is True, "狗 -> 新个体 %s" % r4.get("name"))
        dog_id = r4.get("individual_id")

        st, r5 = c.upload(make_dog(), "dog")
        check(r5.get("individual_id") == dog_id, "第二张狗 -> 同一只狗")

        print("\n--- 4. 统计接口 ---")
        ov = c.overview()
        check(ov["all_time"] == 5, "总到访 = 5（实际 %s）" % ov["all_time"])
        check(ov["species"]["cat"] == 3 and ov["species"]["dog"] == 2,
              "猫 3 / 狗 2（实际 %s）" % ov["species"])
        check(ov["individuals_by_species"]["cat"] == 2,
              "猫独立个体 = 2（实际 %s）" % ov["individuals_by_species"]["cat"])
        check(len(ov["individuals"]) == 3, "个体列表 3 个")
        check(sum(ov["hourly"]["cat"]) == 3, "24 小时分布里猫有 3 次")
        check(len(ov["periods"]) == len(PC.PERIODS), "时段划分 %d 段" % len(PC.PERIODS))
        check(sum(p["cat"] + p["dog"] for p in ov["periods"]) == 5,
              "时段汇总加起来 = 5")
        check(len(ov["recent"]) == 5, "最近到访 5 条")

        print("\n--- 5. 取图 ---")
        thumb = ov["recent"][0]["thumb"]
        st, data, ctype = c.req("GET", "/img/" + thumb)
        check(st == 200 and data[:2] == b"\xff\xd8",
              "GET /img/<thumb> -> JPEG (%d bytes)" % len(data))
        st, _, _ = c.req("GET", "/img/../../etc/passwd")
        check(st == 404, "路径穿越被挡 -> 404")

        print("\n--- 6. 改名 ---")
        st, _ = c.post_json("/api/v1/individual/%d/rename" % first_id,
                            {"name": "橘猫老大"})
        check(st == 200, "rename -> 200")
        ov = c.overview()
        names = {i["id"]: i["name"] for i in ov["individuals"]}
        check(names.get(first_id) == "橘猫老大", "名字已改：%s" % names.get(first_id))

        print("\n--- 7. 合并（人工纠正认错的个体）---")
        st, r = c.post_json("/api/v1/individual/%d/merge" % second_id,
                            {"into": first_id})
        check(st == 200, "merge -> 200")
        ov = c.overview()
        check(ov["individuals_by_species"]["cat"] == 1, "合并后猫个体 = 1")
        merged = [i for i in ov["individuals"] if i["id"] == first_id][0]
        check(merged["visits"] == 3, "合并后该个体 3 次（实际 %s）" % merged["visits"])

        print("\n--- 8. 错误处理 ---")
        st, data, _ = c.req("POST", "/api/v1/visit?species=cat", b"not an image",
                            {"Content-Type": "image/jpeg"})
        check(st == 400, "非图片 -> 400")
        st, data, _ = c.req("POST", "/api/v1/visit?species=cat", b"",
                            {"Content-Type": "image/jpeg"})
        check(st == 400, "空 body -> 400")

        # ---- 9. 同一张照片重传不能变成多条记录 ----
        #
        # 现场现象：K230 等响应超时（网络慢 + 服务端要做特征提取），
        #           它会重传同一张图。以前每传一次就多一条记录，
        #           一张照片在网页上出现 3 次，统计全乱。
        print("\n--- 9. uid 去重（重传只算一次） ---")
        before = c.overview()["all_time"]
        img = make_dog()
        uid = "k230-test-dup-001"
        ids = []
        for i in range(3):
            q = ("/api/v1/visit?device=k230-test&species=dog&conf=0.9"
                 "&ws=1280&hs=720&uid=" + uid)
            st, data, _ = c.req("POST", q, jpeg_bytes(img),
                                {"Content-Type": "image/jpeg"})
            j = json.loads(data.decode())
            ids.append(j.get("visit_id"))
            if i > 0:
                check(j.get("duplicate") is True,
                      "第 %d 次重传被认成重复（duplicate=True）" % (i + 1))
        check(len(set(ids)) == 1, "三次重传都指向同一条记录 %s" % ids)
        after = c.overview()["all_time"]
        check(after - before == 1,
              "库里只多了 1 条（实际 +%d）" % (after - before))

        # 换 uid 就是真的新到访
        q = ("/api/v1/visit?device=k230-test&species=dog&conf=0.9"
             "&ws=1280&hs=720&uid=k230-test-dup-002")
        st, data, _ = c.req("POST", q, jpeg_bytes(img),
                            {"Content-Type": "image/jpeg"})
        j = json.loads(data.decode())
        check(j.get("duplicate") is not True, "换 uid -> 当成新的到访")
        check(j["visit_id"] != ids[0], "换 uid -> 新记录 id 不同")

        # 不带 uid（老固件/curl 手测）照旧能用
        st, data, _ = c.req("POST", "/api/v1/visit?species=cat&conf=0.9",
                            jpeg_bytes(make_cat_b()),
                            {"Content-Type": "image/jpeg"})
        check(st == 200, "不带 uid 的老请求仍然正常（兼容）")

        # ---- 10. 补传（bf=1）用拍照时刻入库 ----
        #
        # 现场现象：SD 卡里躺着几天前的照片，补传时服务端按"收到时刻"入库，
        #           今天的统计突然多出几次到访 —— 看上去像"网页对不上串口"。
        print("\n--- 10. 补传按拍照时刻入库 ---")
        old_ts = time.time() - 86400 * 3          # 三天前拍的
        q = ("/api/v1/visit?device=k230-test&species=cat&conf=0.9"
             "&ws=1280&hs=720&bf=1&ts=%d&uid=k230-test-bf-001" % int(old_ts))
        st, data, _ = c.req("POST", q, jpeg_bytes(make_cat_a()),
                            {"Content-Type": "image/jpeg"})
        check(st == 200, "补传请求返回 200")
        j = json.loads(data.decode())
        check(abs(j["ts"] - old_ts) < 5,
              "补传记录的时间是三天前，不是现在（实际差 %.0f 秒）"
              % (j["ts"] - old_ts))

        # 板子没 RTC 时 ts 会是 ticks 这种垃圾值，必须忽略
        q = ("/api/v1/visit?device=k230-test&species=cat&conf=0.9"
             "&ws=1280&hs=720&bf=1&ts=396239&uid=k230-test-bf-002")
        st, data, _ = c.req("POST", q, jpeg_bytes(make_cat_b()),
                            {"Content-Type": "image/jpeg"})
        j = json.loads(data.decode())
        check(j["ts"] > 1600000000,
              "垃圾时间戳（ticks=396239）被忽略，仍用现在")

        print("\n--- 11. 首页 ---")
        st, data, ctype = c.req("GET", "/")
        check(st == 200 and b"<!DOCTYPE html>" in data, "GET / -> 网页")
        st, data, _ = c.req("GET", "/nope.html")
        check(st == 404, "不存在的静态文件 -> 404")

    finally:
        srv.shutdown()
        srv.server_close()
        if server.DB:
            server.DB.close()
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n=== %s (%d/%d) ===" % ("ALL PASS" if ok == total else "PROBLEMS FOUND",
                                    ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
