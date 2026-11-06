# -*- coding: utf-8 -*-
"""
在真实板子上跑一遍官方例程 —— 一条命令，不需要开 CanMV IDE。

用途：分清"硬件/模型有问题"和"我们自己的逻辑有问题"。
    这是两个完全不同的方向，混在一起查会白折腾很久。

用法（Windows，板子插 USB）：
    python tools/run_official_example.py                # 自动找串口，推送并运行
    python tools/run_official_example.py --port COM23   # 指定串口
    python tools/run_official_example.py --show-log     # 只读上次跑留下的日志
    python tools/run_official_example.py --virt         # 用 virt 模式跑（排除屏幕）
    python tools/run_official_example.py --deploy       # 只推文件，不运行

⚠️ CanMV IDE 开着会占住串口。先点 IDE 的「断开连接」，或者直接关掉 IDE。
   这个脚本不需要 IDE，全部走串口 raw REPL。
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("缺 pyserial：python -m pip install pyserial")

from push_to_k230 import Board          # 复用已验证的传输层

LOCAL_PROBE = os.path.join(HERE, "board_official_probe.py")
REMOTE_PROBE = "/sdcard/official_probe.py"
REMOTE_LOG = "/sdcard/official_probe.log"


def find_board_port():
    """在系统串口里找 K230。

    判据按可靠程度排：OpenMV/CanMV 的描述 > VID 1209 > VID 303A。
    找不到就列出所有口，让人自己指定 —— 别瞎猜一个口往上写，
    万一那口是别的设备，会把它搞乱。
    """
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None, ports
    for p in ports:
        desc = (p.description or "").lower()
        if "openmv" in desc or "canmv" in desc or "k230" in desc:
            return p.device, ports
    for p in ports:
        hwid = (p.hwid or "").upper()
        if "VID:1209" in hwid or "VID_1209" in hwid:
            return p.device, ports
    for p in ports:
        hwid = (p.hwid or "").upper()
        if "VID:303A" in hwid or "VID_303A" in hwid:
            return p.device, ports
    return None, ports


def show_ports(ports):
    if not ports:
        print("!! 系统里一个串口都没有 —— 板子没插，或者 USB 线只供电不能传数据。")
        return
    print("--- 现有串口 ---")
    for p in ports:
        print("   %-8s %s   %s" % (p.device, p.description, p.hwid))
    print("用 --port 指定其中一个。")


def patch_mode(virt):
    """把板端脚本里的 MODE 改掉，不用手工编辑文件。"""
    f = open(LOCAL_PROBE, "r", encoding="utf-8")
    src = f.read()
    f.close()
    if not virt:
        return src
    new = src.replace('MODE = "lcd"', 'MODE = "virt"')
    if new == src:
        print("!! 没能把 MODE 改成 virt（脚本里那行变了？）继续用原样")
    return new


def run(port, virt=False, timeout=180):
    board = Board(port, timeout=0.3)
    try:
        print("[board] 连接 %s ..." % port)
        board.enter_raw()
        print("[board] raw REPL OK")

        src = patch_mode(virt)
        print("[board] 上传体检脚本 -> %s" % REMOTE_PROBE)
        # 先写成本地临时文件，复用 Board.put（它带大小校验）
        tmp = os.path.join(HERE, "_probe_upload.tmp")
        f = open(tmp, "w", encoding="utf-8")
        f.write(src)
        f.close()
        try:
            board.put(tmp, REMOTE_PROBE)
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

        print("[board] 开始运行（最多等 %d 秒）...\n" % timeout)
        print("-" * 60)
        code = ("exec(open(" + repr(REMOTE_PROBE) + ").read())\n")
        out, err = board.exec(code, timeout=timeout)
        print(out)
        if err.strip():
            print("--- stderr ---")
            print(err)
        print("-" * 60)
        return out, err
    finally:
        board.close()


def show_log(port):
    board = Board(port, timeout=0.3)
    try:
        board.enter_raw()
        code = (
            "try:\n"
            "    f = open(" + repr(REMOTE_LOG) + ", 'r')\n"
            "    print(f.read())\n"
            "    f.close()\n"
            "except Exception as e:\n"
            "    print('读不到日志: ' + str(e))\n"
        )
        out, err = board.exec(code, timeout=20)
        print(out)
        if err.strip():
            print("--- stderr ---")
            print(err)
    finally:
        board.close()


def main():
    ap = argparse.ArgumentParser(description="在 K230 上跑官方例程体检")
    ap.add_argument("--port", help="串口号，例如 COM23；不填则自动找")
    ap.add_argument("--virt", action="store_true",
                    help='用 display_mode="virt" 跑，排除屏幕因素')
    ap.add_argument("--show-log", action="store_true",
                    help="只读上次运行留下的 /sdcard/official_probe.log")
    ap.add_argument("--deploy", action="store_true", help="只推文件，不运行")
    ap.add_argument("--timeout", type=int, default=180, help="运行超时秒数")
    args = ap.parse_args()

    port = args.port
    if not port:
        port, ports = find_board_port()
        if not port:
            show_ports(ports)
            sys.exit(1)
        print("[board] 自动识别到 %s" % port)

    if args.show_log:
        show_log(port)
        return

    if args.deploy:
        board = Board(port, timeout=0.3)
        try:
            board.enter_raw()
            tmp = os.path.join(HERE, "_probe_upload.tmp")
            f = open(tmp, "w", encoding="utf-8")
            f.write(patch_mode(args.virt))
            f.close()
            try:
                board.put(tmp, REMOTE_PROBE)
            finally:
                os.remove(tmp)
            print("[board] 已推送 %s" % REMOTE_PROBE)
            print("        在 IDE 里跑：exec(open('%s').read())" % REMOTE_PROBE)
        finally:
            board.close()
        return

    run(port, virt=args.virt, timeout=args.timeout)


if __name__ == "__main__":
    main()
