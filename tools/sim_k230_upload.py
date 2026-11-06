# -*- coding: utf-8 -*-
"""
模拟 K230 的裸 socket 上传，验证服务端在各种情况下都给出可读的响应。

为什么需要这个：
    板子上出问题时只能看到一行 "ECONNRESET"，什么原因都读不出来。
    这个脚本用**和板子一模一样的写法**（HTTP/1.0 + 裸 socket + 一次性发完）
    在 PC 上复现同一件事，就能分清是"服务端问题"还是"板子/网络问题"。

用法：
    python tools/sim_k230_upload.py
    python tools/sim_k230_upload.py --host 192.168.43.154 --port 8000
"""

import argparse
import io
import json
import random
import socket
import sys
import urllib.request

try:
    from PIL import Image
except ImportError:
    sys.exit("缺 Pillow：python -m pip install pillow")


def health_db_path(host, port, timeout=4):
    """问服务端它在用哪个数据库文件；问不到就返回 None。"""
    url = "http://%s:%d/api/v1/health" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("db") or None
    except Exception:
        return None


def noisy_jpeg(w=640, h=480, quality=82):
    """造一张噪声图 —— 最吃压缩，用来模拟"照片特别大"的情况。"""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (random.randint(0, 255),
                        random.randint(0, 255),
                        random.randint(0, 255))
    b = io.BytesIO()
    img.save(b, "JPEG", quality=quality)
    return b.getvalue()


def post(data, host, port, query_extra="", timeout=25):
    """完全按板子的方式发：HTTP/1.0、Connection: close、裸 socket。

    返回 (状态行, send 期间的异常, 收到的字节数)。
    """
    qs = ("?device=k230-sim&species=dog&conf=0.590&ws=640&hs=480"
          + query_extra)
    head = ("POST /api/v1/visit%s HTTP/1.0\r\n"
            "Host: %s:%d\r\n"
            "Content-Type: image/jpeg\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n\r\n"
            % (qs, host, port, len(data)))
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((host, port))

    send_err = None
    try:
        s.sendall(head.encode())
        s.sendall(data)
    except Exception as e:
        # 服务端提前关连接就会在这里报 ECONNRESET。
        # 关键点：它其实已经把原因发出去了，所以要继续读。
        send_err = e

    buf = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except Exception:
        pass
    s.close()

    status = buf.split(b"\r\n", 1)[0] if buf else b"(no response)"
    return status.decode("latin-1"), send_err, len(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.43.154")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--allow-prod", action="store_true",
                    help="允许往生产库写测试数据（会留下噪声图片，慎用）")
    args = ap.parse_args()

    print("=" * 60)
    print(" 模拟 K230 裸 socket 上传   %s:%d" % (args.host, args.port))
    print("=" * 60)

    # ---- 生产库保护 ----
    # 这个脚本发的 device 是 k230-sim，照片是随机噪声图。以前不设防，跑一次
    # 生产库就多几条"狗"的到访 —— 网页上点开是一块灰噪点，串口却什么都没发生，
    # 也就是"网页数据和串口对不上"。真正的压测请在测试端口上做。
    if not args.allow_prod:
        prod = health_db_path(args.host, args.port)
        if prod and "visits.db" in prod.replace("\\", "/") and \
                "test" not in prod.lower() and "tmp" not in prod.lower():
            print("\n!! 拒绝执行：%s:%d 用的是生产库" % (args.host, args.port))
            print("   %s" % prod)
            print("   这个脚本会往库里塞随机噪声图，网页上看起来像真到访。")
            print("")
            print("   要么换个测试端口（CATDOG_PORT=8098 CATDOG_DATA_DIR=<临时目录>）")
            print("   要么确实想压测生产库，加 --allow-prod")
            print("   已经写进去了的话：python tools/prune_visits.py --apply")
            return 2

    cases = []

    # ① 正常大小（板子降到 q65 左右的水平）
    cases.append(("普通大小 ~60KB", noisy_jpeg(quality=60), ""))

    # ② 板子日志里那种大图（360KB）—— 这是最容易出问题的一档
    cases.append(("大图 ~300KB", noisy_jpeg(quality=82), ""))

    # ③ token 校验：故意带错误 token，看能不能拿到可读的 403
    cases.append(("错误 token", noisy_jpeg(quality=60), "&token=wrong-on-purpose"))

    ok = 0
    for name, data, extra in cases:
        status, err, n = post(data, args.host, args.port, extra)
        print("")
        print("  %-16s %6d 字节 (%3d KB)" % (name, len(data), len(data) // 1024))
        print("    响应      : %s" % status)
        print("    send 异常 : %s" % (err if err else "无"))
        print("    收到      : %d 字节" % n)

        if "403" in status:
            # 403 只有两种情况：①token 对不上（本用例故意）②服务端设了 token
            print("    >> 拿到可读的 403，说明服务端没再用 RST 掐断连接")
            ok += 1
        elif "200" in status:
            ok += 1
        else:
            print("    >> !! 响应不可读，客户端只会看到 ECONNRESET")

    print("")
    print("=" * 60)
    print(" %d/%d 个用例拿到了可读响应" % (ok, len(cases)))
    if ok < len(cases):
        print(" 有不可读的情况 -> 服务端仍在提前关连接，检查 pc/server.py 的 _drain_body")
    print("=" * 60)


if __name__ == "__main__":
    main()
