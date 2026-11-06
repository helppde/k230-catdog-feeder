# -*- coding: utf-8 -*-
"""
k230_trigger.py（GPIO 触发）的逻辑测试 —— 在 PC 上跑，不需要 K230

测什么：
  1. Trigger：拉高 800ms 再拉低，退出时不会把信号线留在高电平
  2. FeedGate：一只动物只开一次饭；蹲着不走不重复；走了再来才算下一次
  3. 锁定期内即使满足条件也不开饭（Arduino 还在跑）
  4. 接了 BUSY 线时，提前解锁但 animal 还在 -> 仍然不开第二次

运行（项目根目录）：
    python tests/k230_trigger_sim.py
"""

import os
import sys
import time as _time
import types

# ---------------- 假时钟 ----------------
_clock = [0]


_time.ticks_ms = lambda: _clock[0]
_time.ticks_diff = lambda a, b: a - b
_time.ticks_add = lambda a, b: a + b
_time.sleep_ms = lambda ms: _clock.__setitem__(0, _clock[0] + int(ms))
_time.sleep = lambda s: _clock.__setitem__(0, _clock[0] + int(s * 1000))


# ---------------- 假 machine 模块 ----------------
class FakePin(object):
    OUT = 1
    IN = 0
    PULL_UP = 2
    PULL_DOWN = 3

    def __init__(self, num, mode=None, pull=None):
        self.num = num
        self.mode = mode
        self.level = 0
        FakePin.last[num] = self

    def value(self, v=None):
        if v is None:
            return self.level
        self.level = 1 if v else 0
        return self.level


FakePin.last = {}

_machine = types.ModuleType("machine")
_machine.Pin = FakePin
_machine.FPIOA = None          # 模拟"不需要做 IOMUX 复用"的新固件
sys.modules["machine"] = _machine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import k230_trigger as K       # noqa: E402

K.VERBOSE = False
K.PRINT_PIN_INFO = False
# 测试里默认走**同步**上传：结果立刻可见，断言才能直接检查。
# 后台线程的行为由 case_upload_background 单独测（那里会临时打开）。
K.UPLOAD_IN_BACKGROUND = False

CAT = [400.0, 300.0, 300.0, 350.0, K.CLASS_CAT, 0.90]
DOG = [420.0, 320.0, 320.0, 300.0, K.CLASS_DOG, 0.85]


def check(cond, what):
    global _ok, _total
    _total += 1
    if cond:
        _ok += 1
        print("  [ ok ] %s" % what)
    else:
        print("  [FAIL] %s" % what)


_ok = 0
_total = 0


# ================================================================
# 1. 触发信号：持续电平（不再是脉冲）
# ================================================================
def case_trigger_level():
    print("\n--- 1. 触发信号是持续电平 ---")
    _clock[0] = 0
    FakePin.last = {}
    trig = K.Trigger(K.TRIG_PIN)

    p = FakePin.last.get(K.TRIG_PIN)
    check(p is not None, "GPIO%d 被配置成输出" % K.TRIG_PIN)
    check(p.mode == FakePin.OUT, "方向是 OUT")
    check(p.level == 0, "初始化后是低电平（信号线不会一直吊着）")

    # 有动物 -> 拉高，并且**一直保持**
    trig.set_level(True)
    check(p.level == 1, "检测到动物 -> 高电平")
    for _ in range(50):
        _clock[0] += 100
    check(p.level == 1, "过了 5 秒还是高电平（这就是'一直高电平'）")
    check(_clock[0] > 0, "期间没有 sleep 卡住主循环")

    # 重复设同一个值不应该反复写引脚（否则串口刷满、还掩盖真实跳变）
    writes = []
    orig = FakePin.value

    def spy(self, v=None):
        if v is not None:
            writes.append(1 if v else 0)
        return orig(self, v)

    FakePin.value = spy
    try:
        trig.set_level(True)
        trig.set_level(True)
    finally:
        FakePin.value = orig
    check(len(writes) == 0, "状态没变时不再重复写引脚（写了 %d 次）" % len(writes))

    # 动物走了 -> 拉低
    trig.set_level(False)
    check(p.level == 0, "动物离开 -> 低电平")

    # 退出时强制拉低，别把线留在高电平
    trig.set_level(True)
    trig.force_low()
    check(p.level == 0, "force_low() 保证退出时是低电平")


# ================================================================
# 2. 存在判定：一只动物只喂一次
# ================================================================
def _drive(g, n, det, step=100):
    """连续喂 n 帧，返回 (present, 这段里开饭次数)。"""
    feeds = 0
    present = g.present
    for _ in range(n):
        _clock[0] += step
        present, feed_now, _d = g.update(_clock[0], det)
        if feed_now:
            feeds += 1
    return present, feeds


def case_one_feed_per_visit():
    print("\n--- 2. 一只动物只开一次饭 ---")
    _clock[0] = 0
    g = K.PresenceGate()

    present, feeds = _drive(g, 3 * 100, CAT)     # 20 秒
    check(present is True, "连续多帧后 present=True")
    check(feeds == 1, "第 1 次到访开 1 次饭（实际 %d 次）" % feeds)

    present, feeds2 = _drive(g, 1800, CAT)       # 再 3 分钟
    check(feeds2 == 0, "蹲着 3 分钟不再开饭（实际 %d 次）" % feeds2)
    check(present is True, "全程保持 present=True -> GPIO 一直高")
    check(g.visits == 1, "只算 1 次到访")


# ================================================================
# 3. 消抖：单帧漏检不能让电平抖
# ================================================================
def case_debounce():
    print("\n--- 3. 消抖（识别不准时最容易出事的地方）---")
    _clock[0] = 0
    g = K.PresenceGate()

    # 单帧命中：不够 CONF/不够帧数，不能立刻拉高
    _clock[0] += 100
    present, feed_now, _ = g.update(_clock[0], CAT)
    check(present is False, "只命中 1 帧不拉高（要 %d 帧）" % K.PRESENCE_ON_FRAMES)
    check(feed_now is False, "单帧不开饭")

    # 低置信度的框也不算数
    g2 = K.PresenceGate()
    _clock[0] = 0
    weak = [400.0, 300.0, 300.0, 350.0, K.CLASS_CAT, 0.30]
    present2, feed2 = _drive(g2, 20, weak)
    check(present2 is False, "conf=0.30 太低，不认为有动物")
    check(feed2 == 0, "低置信度不开饭")

    # 连续命中 -> 拉高
    present, feeds = _drive(g, K.PRESENCE_ON_FRAMES, CAT)
    check(present is True, "连续 %d 帧 -> 拉高" % K.PRESENCE_ON_FRAMES)
    check(feeds == 1, "开饭 1 次")

    # 中间丢几帧（猫低头吃东西）-> 不能判定离开
    mid = K.PRESENCE_OFF_MS // 2
    present, _ = _drive(g, mid // 100, None)
    check(present is True,
          "丢掉 %dms 检测仍算有动物（阈值 %dms）" % (mid, K.PRESENCE_OFF_MS))

    # 继续丢到超过阈值 -> 才拉低
    present, _ = _drive(g, K.PRESENCE_OFF_MS // 100, None)
    check(present is False, "连续 %dms 无检测 -> 拉低" % K.PRESENCE_OFF_MS)


# ================================================================
# 4. 走了再回来才开第二次
# ================================================================
def case_leave_and_return():
    print("\n--- 4. 走了再回来才算下一次 ---")
    _clock[0] = 0
    g = K.PresenceGate()

    present, feeds = _drive(g, K.PRESENCE_ON_FRAMES, CAT)
    check(feeds == 1, "第一次来，开饭")

    # 走了一会儿再回来，但间隔 < VISIT_MIN_GAP_MS
    _drive(g, K.PRESENCE_OFF_MS // 100 + 5, None)
    check(g.present is False, "确实判定为离开了")
    _, feeds = _drive(g, K.PRESENCE_ON_FRAMES, CAT)
    check(feeds == 0,
          "离开不到 %ds 就回来，不开饭（防抖）" % (K.VISIT_MIN_GAP_MS // 1000))

    # 隔够久了再走再回 -> 第 2 次
    _drive(g, K.PRESENCE_OFF_MS // 100 + 5, None)
    _clock[0] += K.VISIT_MIN_GAP_MS
    _, feeds = _drive(g, K.PRESENCE_ON_FRAMES, DOG)
    check(feeds == 1, "离开够久后狗来了 -> 开第 2 次饭")
    check(g.visits == 3, "到访计数 = 3（第 2 次被防抖挡掉也算一次到访）")


# ================================================================
# 5. GPIO 电平跟着 present 走（端到端串起来看）
# ================================================================
def case_pin_follows_presence():
    print("\n--- 5. GPIO 电平跟着存在状态走 ---")
    _clock[0] = 0
    FakePin.last = {}
    trig = K.Trigger(K.TRIG_PIN)
    p = FakePin.last[K.TRIG_PIN]
    g = K.PresenceGate()

    levels = []

    def tick(det):
        _clock[0] += 100
        present, feed_now, _d = g.update(_clock[0], det)
        trig.set_level(present)
        levels.append(p.level)

    for _ in range(10):
        tick(CAT)
    check(levels[-1] == 1, "有动物时引脚是高")

    for _ in range(K.PRESENCE_OFF_MS // 100 + 5):
        tick(None)
    check(levels[-1] == 0, "动物走后引脚拉低")

    # ---- 关键：漏检几帧不能产生假边沿 ----
    # 执行板是靠上升沿触发的，多一个假边沿就多开一次饭。
    _clock[0] = 0
    g = K.PresenceGate()
    levels = []
    for _ in range(6):
        tick(CAT)                 # 确认期：前 2 帧还是低，第 3 帧起高
    established = levels.count(1)
    check(established == 4, "确认期是 2 帧低 + 4 帧高（实际 4 帧高）")

    for _ in range(5):            # 漏 500ms（猫低头吃东西）
        tick(None)
    for _ in range(5):
        tick(CAT)

    # 重新数上升沿：整段只能有 1 次
    edits = sum(1 for i in range(1, len(levels)) if levels[i] > levels[i - 1])
    check(edits == 1,
          "整段只有 1 次上升沿（漏 500ms 检测没造出假边沿，实际 %d 次）" % edits)
    check(0 not in levels[2:], "确认之后电平全程没掉下来")


# ================================================================
# 6. 触发区判断
# ================================================================
def case_zone():
    print("\n--- 6. 触发区 ---")
    old = K.TRIGGER_ZONE
    try:
        K.TRIGGER_ZONE = (400, 200, 400, 400)     # 中间一块
        inside = [450.0, 250.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
        outside = [10.0, 10.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
        check(K.best_in_zone([outside]) is None, "区外的目标不算")
        check(K.best_in_zone([inside]) is not None, "区内目标算")
        both = [outside, inside]
        check(K.best_in_zone(both) is inside, "多个目标取区内的那个")
        hi = [450.0, 250.0, 100.0, 120.0, K.CLASS_CAT, 0.99]
        check(K.best_in_zone([inside, hi]) is hi, "多个都在区内时取置信度高的")
    finally:
        K.TRIGGER_ZONE = old


# ================================================================
# 7. 服务端地址解析（云端部署的关键）
# ================================================================
def case_parse_url():
    print("\n--- 7. SERVER_URL 解析 ---")
    old = K.SERVER_URL
    try:
        cases = [
            # (填的 SERVER_URL,      期望 scheme, host,                 port, path)
            ("http://192.168.1.100:8000", "http", "192.168.1.100", 8000, ""),
            ("192.168.1.100:8000", "http", "192.168.1.100", 8000, ""),
            ("https://catdog.example.com", "https", "catdog.example.com", 443, ""),
            ("https://catdog.example.com/", "https", "catdog.example.com", 443, ""),
            ("http://1.2.3.4", "http", "1.2.3.4", 80, ""),
            ("https://api.example.com:8443", "https", "api.example.com", 8443, ""),
            # 后台挂在子路径下（nginx 反代常见）
            ("https://example.com/catdog", "https", "example.com", 443, "/catdog"),
            ("https://example.com/catdog/", "https", "example.com", 443, "/catdog"),
            # 前后空格是手改配置最容易带进来的
            ("  https://example.com  ", "https", "example.com", 443, ""),
        ]
        for url, es, eh, ep, epath in cases:
            K.SERVER_URL = url
            s, h, p, path = K.parse_server_url()
            ok = (s == es and h == eh and p == ep and path == epath)
            check(ok, "%-40s -> %s %s:%d%s" % (url, s, h, p, path))

        # 没写 scheme 必须默认 http，绝不能默认成 https
        # （否则局域网直连会莫名其妙去套 TLS）
        K.SERVER_URL = "192.168.1.5:8000"
        check(K.parse_server_url()[0] == "http", "没写 scheme 默认 http，不是 https")
    finally:
        K.SERVER_URL = old

    # ---- 7b. 内网地址识别（开机自检靠它判断"要不要告警"）----
    print("\n--- 7b. 内网地址判定 ---")
    private = ["192.168.1.100", "192.168.43.170", "10.0.0.5", "172.16.3.9",
               "172.31.255.1", "127.0.0.1", "localhost", ""]
    public = ["catdog.example.com", "8.8.8.8", "1.2.3.4",
              "172.32.0.1", "172.15.0.1", "11.0.0.1"]
    for h in private:
        check(K._is_private_host(h), "%-22s -> 内网" % (h or "(空)"))
    for h in public:
        check(not K._is_private_host(h), "%-22s -> 公网/域名" % h)

    # 空的必须当内网，否则没配地址的时候会误报"公网没设 token"
    check(K._is_private_host(""), "空地址不报公网告警")


# ================================================================
# 8. 鉴权 token 真的进了请求
# ================================================================
def case_token_in_request():
    print("\n--- 8. token 进请求 ---")
    old_tok, old_url = K.API_TOKEN, K.SERVER_URL
    sent = []

    class FakeSock(object):
        def settimeout(self, t):
            pass

        def connect(self, addr):
            self.addr = addr

        def send(self, data):
            sent.append(data)
            return len(data)

        def recv(self, n):
            return b"HTTP/1.0 200 OK\r\n\r\n"

        def close(self):
            pass

    class FakeSocketMod(object):
        def socket(self):
            return FakeSock()

    try:
        K.SERVER_URL = "http://10.0.0.2:8000"
        K.API_TOKEN = "s3cret"
        # 借 sys.modules 让 import socket 拿到假模块
        saved = sys.modules.get("socket")
        sys.modules["socket"] = FakeSocketMod()
        try:
            up = K.Uploader()
            det = [10.0, 10.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
            ok = up._post(b"\xff\xd8jpeg", "cat", det)
        finally:
            if saved is not None:
                sys.modules["socket"] = saved
            else:
                sys.modules.pop("socket", None)

        blob = b"".join(sent)
        check(ok, "200 被正确识别为成功")
        check(b"token=s3cret" in blob, "请求里带了 token=s3cret")
        # 服务端校验的是 query 里的 token，必须拼在 URL 上而不是只放 header
        check(blob.split(b"\r\n")[0].find(b"token=") > 0, "token 在请求行(query)里")
    finally:
        K.API_TOKEN, K.SERVER_URL = old_tok, old_url


# ================================================================
# 9. 403 不重试，避免无意义地反复打云端
# ================================================================
def case_403_no_retry():
    print("\n--- 9. 403 不重试 ---")
    old_tok, old_url = K.API_TOKEN, K.SERVER_URL
    tries = []

    class FakeSock(object):
        def settimeout(self, t):
            pass

        def connect(self, addr):
            tries.append(addr)

        def send(self, data):
            return len(data)

        def recv(self, n):
            return b"HTTP/1.0 403 Forbidden\r\n\r\n"

        def close(self):
            pass

    class FakeSocketMod(object):
        def socket(self):
            return FakeSock()

    try:
        K.SERVER_URL = "https://cloud.example.com"
        K.API_TOKEN = "wrong"
        saved = sys.modules.get("socket")
        sys.modules["socket"] = FakeSocketMod()
        try:
            up = K.Uploader()
            det = [10.0, 10.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
            ok = up._post(b"\xff\xd8jpeg", "cat", det)
        finally:
            if saved is not None:
                sys.modules["socket"] = saved
            else:
                sys.modules.pop("socket", None)
        check(not ok, "403 判为失败")
        check(len(tries) == 1, "403 只试了 1 次（重试也没用，别打云端）")
    finally:
        K.API_TOKEN, K.SERVER_URL = old_tok, old_url


# ================================================================
# 10. 断网补传（云端部署的核心：公网断线是常态）
# ================================================================
def case_backfill():
    print("\n--- 10. 断网补传 ---")
    import tempfile
    import shutil

    tmp = tempfile.mkdtemp()
    old_dir = K.SAVE_LOCAL_DIR
    old_ready = None
    try:
        K.SAVE_LOCAL_DIR = tmp
        up = K.Uploader()
        up.ready = True                     # 假装 WiFi 已连上

        det = [0.0, 0.0, 100.0, 100.0, K.CLASS_CAT, 0.913]
        up._save(b"\xff\xd8" + b"x" * 100, "cat", det)
        up._save(b"\xff\xd8" + b"y" * 100, "dog",
                 [0.0, 0.0, 100.0, 100.0, K.CLASS_DOG, 0.755])

        names = sorted(os.listdir(tmp))
        check(len(names) == 2, "两张都存下来了")

        # 文件名必须自带元数据，否则补传时不知道是猫是狗、置信度多少
        check(any(n.startswith("cat_913_") for n in names), "cat 文件名带 conf=0.913")
        check(any(n.startswith("dog_755_") for n in names), "dog 文件名带 conf=0.755")

        sp, conf = K._parse_saved_name("cat_913_12345.jpg")
        check(sp == "cat" and abs(conf - 0.913) < 1e-6, "_parse_saved_name 还原 cat/0.913")

        # 旧版本留下的两段式文件名：后一段是 ticks，不能当成 conf=12.345
        sp, conf = K._parse_saved_name("cat_12345.jpg")
        check(sp == "cat" and conf == 0.5, "旧格式文件名不会解析出 conf>1")

        # ---- 上传成功 -> 文件要删掉 ----
        sent = []
        def ok_post(jpeg, species, d, ts=None, uid=None, backfill=False):
            sent.append((species, d[5], uid, backfill))
            return True
        up._post = ok_post
        n = up.retry_pending(limit=2)
        check(n == 2, "两张都补传成功")
        check(len(os.listdir(tmp)) == 0, "补传成功后文件被删掉了，不会重复上传")
        check({s for s, _, _, _ in sent} == {"cat", "dog"}, "补传时 species 没搞错")
        check(all(u for _, _, u, _ in sent), "补传也带 uid（否则重传会变成新记录）")
        check(all(bf for _, _, _, bf in sent),
              "补传带 bf=1（服务端才知道这是旧照片，不是刚来的）")

        # ---- 上传失败 -> 文件必须留着，下次再试 ----
        up._save(b"\xff\xd8zzz", "cat", det)
        tries = []
        def bad_post(jpeg, species, d, ts=None, uid=None, backfill=False):
            tries.append(1)
            return False
        up._post = bad_post
        n = up.retry_pending(limit=2)
        check(n == 0, "补传失败返回 0")
        check(len(tries) == 1, "失败后立刻停手，不挨个等超时（只试了 1 次）")
        check(len(os.listdir(tmp)) == 1, "失败的照片还在 SD 卡上，没丢")

        # ---- 还没连上网就不该去试 ----
        up.ready = False
        check(up.retry_pending(2) == 0, "没联网时直接返回，不发请求")

        # ---- trim 要按 ticks 删最旧的，而不是按字母序 ----
        for i in range(5):
            with open(os.path.join(tmp, "cat_500_%05d.jpg" % i), "wb") as f:
                f.write(b"z")
        old_max = K.SAVE_LOCAL_MAX
        try:
            K.SAVE_LOCAL_MAX = 3
            up._trim()
            left = sorted(os.listdir(tmp))
            check(len(left) == 3, "trim 后只剩 3 张")
            check(all(int(n[:-4].split("_")[-1]) >= 2 for n in left),
                  "留下的是 ticks 最大的 3 张（最旧的被删）")
        finally:
            K.SAVE_LOCAL_MAX = old_max
    finally:
        K.SAVE_LOCAL_DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================
# 11. 大图分片发送（MicroPython 的 send 不是 sendall）
# ================================================================
def case_partial_send():
    print("\n--- 11. 分片发送 ---")

    class TrickleSock(object):
        """每次只发 7 字节 —— 模拟窄带/WAN 上 send() 只发一部分的情况。"""
        def __init__(self):
            self.got = []

        def send(self, data):
            piece = bytes(data[:7])
            self.got.append(piece)
            return len(piece)

    payload = b"\xff\xd8" + bytes(range(256)) * 4      # 1026 字节
    s = TrickleSock()
    K._send_all(s, payload)
    joined = b"".join(s.got)
    check(joined == payload, "全部 %d 字节都发出去了，没截断" % len(payload))
    check(len(s.got) > 1, "确实是分了很多次发的（%d 次）" % len(s.got))

    # 发到一半连接断了，要报错而不是静默成功
    class DeadSock(object):
        def __init__(self):
            self.n = 0

        def send(self, data):
            self.n += 1
            if self.n > 2:
                return 0
            return len(bytes(data[:7]))

    try:
        K._send_all(DeadSock(), payload)
        check(False, "连接断了应该抛异常")
    except Exception:
        check(True, "连接断了会抛异常，不会当成发完了")


# ================================================================
# 12. 类别映射（换模型后"识别不到猫狗"的根因）
# ================================================================
def case_class_mapping():
    print("\n--- 12. 类别映射自动适配 ---")

    # COCO 80 类：cat=15, dog=16
    cat, dog = K._class_ids_from_labels(K.LABELS)
    check(cat == 15 and dog == 16, "COCO 80 类 -> cat=15 dog=16（实际 %d/%d）"
          % (cat, dog))
    check(15 in K.KEEP_CLASSES and 16 in K.KEEP_CLASSES, "KEEP_CLASSES 含 15 和 16")

    # 自己训的两类模型：LABELS = ["cat","dog"] -> 0/1
    cat, dog = K._class_ids_from_labels(["cat", "dog"])
    check(cat == 0 and dog == 1, "两类模型 -> cat=0 dog=1（实际 %d/%d）" % (cat, dog))

    # 顺序反过来也得认
    cat, dog = K._class_ids_from_labels(["dog", "cat"])
    check(cat == 1 and dog == 0, "顺序反过来也能认（cat=1 dog=0）")

    # 大小写/空格脏数据
    cat, dog = K._class_ids_from_labels([" Cat ", "DOG"])
    check(cat == 0 and dog == 1, "名字带空格/大小写也能认")

    # 完全没有 cat/dog 的列表 -> 退回默认 15/16，而不是 -1
    cat, dog = K._class_ids_from_labels(["a", "b", "c"])
    check(cat == 15 and dog == 16, "认不出时退回默认 15/16（不是 -1，否则全被过滤）")

    # 关键回归：如果还是硬编码 15/16，两类模型的检测结果会被全部丢掉
    check(K.CLASS_CAT == 15 and K.CLASS_DOG == 16,
          "当前配置下 CLASS_CAT/DOG 与 LABELS 一致")


# ================================================================
# 13. run() 返回值判别 + postprocess（"框永远是 0"的根因）
# ================================================================
class FakeTensor(object):
    """模拟 libs.YOLO 的原始输出张量：形状 (1, C, H, W)，不是框。"""

    def __init__(self, shape, dtype="float32"):
        self.shape = shape
        self.dtype = dtype

    def __len__(self):
        return self.shape[0]

    def __getitem__(self, i):
        # 关键：张量切片拿到的是另一个张量，不是数字。
        # 老代码 int(r[4]) / float(r[5]) 在这里必然炸，然后被 except 吞掉。
        return FakeTensor(self.shape[1:]) if len(self.shape) > 1 else 0.0


class FakeKModel(object):
    """模拟一个真的会出框的模型：run 给张量，postprocess 给框。"""

    def __init__(self, dets, pp_raises=None):
        self.dets = dets
        self.pp_raises = pp_raises
        self.pp_calls = 0

    def run(self, img):
        # 真实形状：YOLOv8n 320 的典型多输出
        return [FakeTensor((1, 144, 40, 40)),
                FakeTensor((1, 144, 20, 20)),
                FakeTensor((1, 144, 10, 10))]

    def postprocess(self, results, *a):
        self.pp_calls += 1
        if self.pp_raises:
            raise self.pp_raises
        return self.dets


def _detector_with(model):
    """绕开 __init__ 里加载 kmodel 的部分，只装一个假模型。"""
    d = K.YoloDetector.__new__(K.YoloDetector)
    d.model = model
    d.seen = {}
    d.seen_total = {}
    d.filtered_log_ms = 0
    d.results = None
    d._warned_pp = False
    d._step_left = 0
    d._verbose = False
    d._last_hit_log = 0
    d._warned_scale = False
    return d


def case_detect_pipeline():
    print("\n--- 13. run() 张量 -> postprocess -> 框 ---")

    # ---- 13a. 形状判别：张量 vs 框 ----
    check(not K._looks_like_dets([FakeTensor((1, 144, 40, 40))]),
          "原始张量不会被当成框")
    check(K._looks_like_dets([[10, 20, 30, 40, 15, 0.9]]),
          "6 个数的框会被认成框")
    check(K._looks_like_dets([]), "空列表当'没有框'")
    # 长度 6 但里面不是数字（例如每项还是张量）不能误判
    check(not K._looks_like_dets([FakeTensor((1, 6))]),
          "长度 6 但是张量 -> 不算框")

    # ---- 13b. 官方两步：run -> postprocess，必须调用 postprocess ----
    model = FakeKModel([[10, 20, 30, 40, 15, 0.91],
                        [50, 60, 20, 20, 16, 0.55]])
    d = _detector_with(model)
    out = d.detect(object())
    check(model.pp_calls == 1, "每帧都调 postprocess（张量不自己解）")
    check(len(out) == 2, "postprocess 给的 2 个框全拿到（实际 %d）" % len(out))
    check(out[0][4] == 15 and abs(out[0][5] - 0.91) < 1e-6,
          "第一个框是 cat conf=0.91")
    check(out[1][4] == 16, "第二个框是 dog")

    # ---- 13c. 非猫狗的框必须被过滤，但仍记进 seen ----
    model = FakeKModel([[1, 2, 3, 4, 0, 0.88],       # person
                        [5, 6, 7, 8, 15, 0.70]])     # cat
    d = _detector_with(model)
    out = d.detect(object())
    check(len(out) == 1 and out[0][4] == 15, "person 被过滤，只留 cat")
    check(d.seen.get(0, 0) > 0.8, "被过滤的 person 仍记进 seen（诊断要看）")
    check(d.seen_total.get(0, 0) > 0.8, "seen_total 也记了（心跳读的是这份）")

    # ---- 13d. run() 返回垃圾（张量被误读）也没关系：postprocess 才是权威 ----
    # 现场日志：conf=2.80e+38、类别 ID 是 30 位天文数字。
    # 那是把没写过的内存（0x7F7F7F7F）当 float32 读出来的结果。
    # 现在根本不看 run() 的返回值，所以垃圾也污染不到结果。
    garbage = [[2.6e38, 2.8e38, 2.8e38, 2.8e38, 2.8e38, 2.80e38]] * 3

    class GarbageModel(object):
        def __init__(self):
            self.pp_calls = 0

        def run(self, img):
            return garbage

        def postprocess(self, results, *a):
            self.pp_calls += 1
            return [[100, 80, 90, 110, 15, 0.86]]

    model = GarbageModel()
    d = _detector_with(model)
    out = d.detect(object())
    check(model.pp_calls == 1, "run() 给垃圾也照样走 postprocess（调用 1 次）")
    check(len(out) == 1 and out[0][4] == 15,
          "最终拿到真框 cat（实际 %d 个）" % len(out))
    check(d.seen == {15: 0.86},
          "seen 里只有 cat，可能 ID 没有污染心跳日志（实际 %s）" % (d.seen,))

    # ---- 13e. postprocess 抛异常 -> 空列表，不崩 ----
    model = FakeKModel([], pp_raises=ValueError("boom"))
    d = _detector_with(model)
    out = d.detect(object())
    check(out == [], "postprocess 异常时返回空列表而不是崩溃")

    # ---- 13f. run() 返回空 -> 空列表 ----
    class EmptyModel(object):
        def run(self, img):
            return None

        def postprocess(self, r, *a):
            raise AssertionError("run 返回 None 时不该继续")

    d = _detector_with(EmptyModel())
    check(d.detect(object()) == [], "run() 返回 None 时安全返回空")

    # ---- 13g. 置信度为 0 / 超范围的框要丢掉 ----
    model = FakeKModel([[0, 0, 1, 1, 15, 0.0],
                        [5, 5, 9, 9, 15, 0.5],
                        [1, 1, 2, 2, 15, 2.8e38]])
    d = _detector_with(model)
    out = d.detect(object())
    check(len(out) == 1, "conf=0 和 conf=2.8e38 的框都被丢掉（留 %d 个）"
          % len(out))

    # ---- 13h. 库有 draw_result 时优先用它（坐标换算交给库）----
    class DrawModel(FakeKModel):
        def __init__(self, dets):
            FakeKModel.__init__(self, dets)
            self.drawn = None

        def draw_result(self, dets, canvas):
            self.drawn = (len(dets), canvas)

    model = DrawModel([[1, 2, 3, 4, 15, 0.9]])
    d = _detector_with(model)
    out = d.detect(object())
    check(len(out) == 1, "出框正常")
    check(hasattr(model, "draw_result"), "库里有 draw_result 就用它画")

    # ---- 13i. 绝不调无参 postprocess（会静默卡死）----
    # 现场代价：日志停在半句话，必须重插 USB 才能恢复。
    # 宁可报错也不要一个会卡死的兜底。
    class NoArgTrap(object):
        noarg_called = False

        def run(self, img):
            return ["whatever"]

        def postprocess(self, *a):
            if len(a) == 0:
                NoArgTrap.noarg_called = True    # 这里本该"卡死"，测试里只记录
                return [[1, 2, 3, 4, 15, 0.9]]
            raise TypeError("wrong number of arguments")

    NoArgTrap.noarg_called = False
    d = _detector_with(NoArgTrap())
    out = d.detect(object())
    check(not NoArgTrap.noarg_called, "签名不符时**不会**去调 postprocess()")
    check(out == [], "拿不到框就返回空，不硬猜")

    # ---- 13j. 第二候选签名（results, nms）能用时要用上 ----
    class TwoArgModel(object):
        def run(self, img):
            return ["t"]

        def postprocess(self, results, nms):
            return [[10, 10, 20, 20, 16, 0.7]]

    d = _detector_with(TwoArgModel())
    out = d.detect(object())
    check(len(out) == 1 and out[0][4] == 16, "两参数签名也能调通")

    # ---- 13i. 合理值必须放过，不能误杀 ----
    check(K._dets_are_sane([[10, 20, 30, 40, 15, 0.91]]), "正常框通过验值")
    check(K._dets_are_sane([[10, 20, 30, 40, 15, 1.0]]), "conf=1.0 也算合法")
    check(not K._dets_are_sane([[10, 20, 30, 40, 15, 1.5]]),
          "conf=1.5 超出概率范围 -> 判定垃圾")
    check(not K._dets_are_sane([[10, 20, 0, 40, 15, 0.9]]),
          "宽为 0 的框不合法")
    check(not K._dets_are_sane([[-9e9, 20, 30, 40, 15, 0.9]]),
          "坐标为天文数字不合法")
    check(K._dets_are_sane([]), "空结果算合理（就是没检测到）")


# ================================================================
# 14. JPEG 编码兜底（current format not support save function!）
# ================================================================
class FakeImage(object):
    """模拟 image.Image：不同格式对 compress/save 的支持不一样。

    planar=True 就是这个 bug 的现场：compress 能用，save 直接报
    "current format not support save function!"。
    """

    def __init__(self, planar=False, compress_ok=True):
        self.planar = planar
        self.compress_ok = compress_ok
        self.saved = []

    def compress(self, quality=85):
        if not self.compress_ok:
            raise OSError("current format not support compress function!")
        return b"\xff\xd8\xff\xd9FAKEJPEG"

    def save(self, path, **kw):
        if self.planar:
            raise OSError("current format not support save function!")
        self.saved.append(path)


def case_jpeg_fallback():
    print("\n--- 14. 存图/上传的 JPEG 兜底 ---")
    import tempfile

    # 13a. 平面格式：save() 不行，但 compress() 能救回来
    im = FakeImage(planar=True)
    p = os.path.join(tempfile.gettempdir(), "_catdog_test_planar.jpg")
    ok, how = K._save_jpeg(im, p)
    check(ok, "平面格式也存下来了（走 %s）" % how)
    if ok:
        check(os.path.getsize(p) == len(b"\xff\xd8\xff\xd9FAKEJPEG"),
              "写进去的字节数对（%d 字节假 JPEG）"
              % os.path.getsize(p))
        os.remove(p)
    check(not im.saved, "平面格式没去调那个会炸的 save()")

    # 13b. compress 也不行 -> 退回 save()
    im = FakeImage(planar=False, compress_ok=False)
    p2 = os.path.join(tempfile.gettempdir(), "_catdog_test_save.jpg")
    ok, how = K._save_jpeg(im, p2)
    check(ok and im.saved == [p2], "compress 不行时退回 save()（%s）" % how)
    if os.path.exists(p2):
        os.remove(p2)

    # 13c. 两条都不行 -> 明确返回 False，不抛异常
    class Hopeless(object):
        def compress(self, quality=85):
            raise OSError("nope")

        def save(self, path, **kw):
            raise OSError("current format not support save function!")

    ok, how = K._save_jpeg(Hopeless(), "/tmp/x.jpg")
    check(not ok and "不支持" in how, "两条路都断时返回 False 并说明原因")

    # 13d. _jpeg_bytes 就是上传用的那条路
    check(K._jpeg_bytes(FakeImage(planar=True)) == b"\xff\xd8\xff\xd9FAKEJPEG",
          "上传取字节走 compress，平面格式也能拿到")


# ================================================================
# 15. PipeLine 初始化：逐行日志 + 显示模式自动降级
# ================================================================
def case_pipeline_fallback():
    print("\n--- 15. PipeLine 初始化降级 ---")

    # libs.PipeLine 在 PC 上不存在，塞几个假模块进去
    made = []
    created = []
    destroyed = []

    class FakePipeLine(object):
        fail_modes = ()          # 这些 mode 在 create() 时抛异常

        def __init__(self, rgb888p_size=None, display_size=None,
                     display_mode=None):
            self.mode = display_mode
            made.append(display_mode)

        def create(self):
            if self.mode in FakePipeLine.fail_modes:
                raise OSError("mock: display init failed")
            created.append(self.mode)

        def destroy(self):
            destroyed.append(self.mode)

        def get_display_size(self):
            return list(K.DISPLAY_SIZE)

    libs = types.ModuleType("libs")
    plmod = types.ModuleType("libs.PipeLine")
    plmod.PipeLine = FakePipeLine
    had_libs = "libs" in sys.modules
    had_pl = "libs.PipeLine" in sys.modules
    old_libs = sys.modules.get("libs")
    old_pl = sys.modules.get("libs.PipeLine")
    sys.modules["libs"] = libs
    sys.modules["libs.PipeLine"] = plmod

    old_enable = K.ENABLE_DISPLAY
    old_mode = K.DISPLAY_MODE
    try:
        # ---- 15a. lcd 能用就直接用，不做多余重试 ----
        K.ENABLE_DISPLAY = True
        K.DISPLAY_MODE = "lcd"
        FakePipeLine.fail_modes = ()
        del made[:]
        pl, mode = K._open_pipeline()
        check(mode == "lcd", "首选 mode=lcd 成功（实际 %s）" % mode)
        check(made == ["lcd"], "成功时只尝试了一次（实际 %s）" % (made,))

        # ---- 15b. lcd 失败 -> 自动退到 virt ----
        FakePipeLine.fail_modes = ("lcd",)
        del made[:]
        del destroyed[:]
        pl, mode = K._open_pipeline()
        check(mode == "virt", "lcd 失败后自动退到 virt（实际 %s）" % mode)
        check(made[:2] == ["lcd", "virt"], "按 lcd -> virt 顺序试（实际 %s）" % made)
        check("lcd" in destroyed, "失败的 lcd 实例被 destroy 掉了（防残留）")

        # ---- 15c. 全都不行 -> 抛异常且带错误信息 ----
        FakePipeLine.fail_modes = ("lcd", "virt", "auto")
        raised = None
        try:
            K._open_pipeline()
        except Exception as e:
            raised = e
        check(raised is not None, "所有模式都失败时抛异常，而不是返回个坏对象")
        check(raised is not None and "mock" in str(raised),
              "异常里带上了底层原因（%s）" % (raised,))

        # ---- 15d. ENABLE_DISPLAY=False 必须直接用 virt ----
        K.ENABLE_DISPLAY = False
        FakePipeLine.fail_modes = ()
        del made[:]
        pl, mode = K._open_pipeline()
        check(mode == "virt" and made == ["virt"],
              "关屏时直接用 virt，不去点屏（实际 %s）" % (made,))
    finally:
        K.ENABLE_DISPLAY = old_enable
        K.DISPLAY_MODE = old_mode
        if had_libs:
            sys.modules["libs"] = old_libs
        else:
            sys.modules.pop("libs", None)
        if had_pl:
            sys.modules["libs.PipeLine"] = old_pl
        else:
            sys.modules.pop("libs.PipeLine", None)


def case_full_frame_and_override():
    print("\n--- 16. 全帧上传 + 序号可写死 ---")

    # 全帧上传：照片不依赖框坐标准不准，web 端直接显示整张。
    check(K.UPLOAD_FULL_FRAME is True,
          "默认传整张画面（UPLOAD_FULL_FRAME=True）")

    # 序号默认按名字查，不给死值 —— 换模型/换顺序都不用手改
    check(K.CLASS_IDS_OVERRIDE is None, "默认不写死序号，按 LABELS 名字查")
    check(K.CLASS_CAT == 15 and K.CLASS_DOG == 16,
          "默认落到 COCO 的 15/16")

    # 想写死必须真的生效（用户说的 14/15 就是走这条路）
    src = open(K.__file__.replace(".pyc", ".py"), "r", encoding="utf-8")
    text = src.read()
    src.close()
    check("CLASS_IDS_OVERRIDE = None" in text,
          "配置项可被用户改成 (14, 15) 直接覆盖")

    # KEEP_CLASSES 必须跟最终序号一致，否则框全被过滤
    check(K.KEEP_CLASSES == (K.CLASS_CAT, K.CLASS_DOG),
          "KEEP_CLASSES 与 CLASS_CAT/DOG 保持一致")

    # _dump_lib_source 在 PC 上没有 libs.YOLO，必须安静跳过而不是抛异常
    try:
        K._dump_lib_source()
        ok = True
    except Exception:
        ok = False
    check(ok, "找不到 libs/YOLO 时 _dump_lib_source 只跳过、不抛异常")


# ================================================================
# 18. 真实返回结构：postprocess 给的是 (框, 序号, 分数) 三元组
#
# 依据是 v1.4.3 板载 libs/YOLO.py 第 74 行起的 postprocess()，
# 以及同文件 draw_result() 自己怎么用它：
#     res[0][i] -> x, y, w, h
#     res[1][i] -> 类别序号
#     res[2][i] -> 置信度
# 这就是"模型在跑但永远 0 个框"的真正原因。
# ================================================================
def case_real_postprocess_shape():
    print("\n--- 18. 真实 postprocess 返回结构 ---")

    # ---- 18a. 官方三元组：box / id / conf 三条平行列表 ----
    boxes = [(212.0, 140.0, 168.0, 190.0),    # cat
             (400.0, 300.0, 120.0, 140.0)]    # dog
    ids = [15, 16]
    confs = [0.87, 0.74]
    real = (boxes, ids, confs)

    rows = K._flatten_detections(real)
    check(rows is not None, "认得官方三元组结构")
    check(rows is not None and len(rows) == 2,
          "两个框都被摊平出来（实际 %s）" % (len(rows) if rows else None))
    if rows:
        check(rows[0] == [212.0, 140.0, 168.0, 190.0, 15, 0.87],
              "第 1 行 = 框 + 序号 15 + conf 0.87（实际 %s）" % (rows[0],))
        check(rows[1][4] == 16 and abs(rows[1][5] - 0.74) < 1e-6,
              "第 2 行序号 16 / conf 0.74")

        # 关键回归：老代码读 r[4]/r[5] 会去框列表里取第 5 个框 —— 必然失败
        check(len(rows[0]) == 6, "摊平后是 6 元组，老写法取 [4]/[5] 才成立")

    # ---- 18b. 空三元组（画面里没目标）不能崩 ----
    rows = K._flatten_detections(([], [], []))
    check(rows == [], "空图像返回 []，不抛异常")

    # ---- 18c. 一框一行的老结构仍然要认（自己训的模型可能这样） ----
    rows = K._flatten_detections([[10.0, 20.0, 30.0, 40.0, 15, 0.9]])
    check(rows == [[10.0, 20.0, 30.0, 40.0, 15, 0.9]],
          "一框一行 [x,y,w,h,cid,conf] 继续可用")

    # ---- 18d. 三元组里 id/conf 数量和框对不上 -> 不是这个结构 ----
    rows = K._flatten_detections((boxes, [15], [0.87, 0.74]))
    check(rows is None, "数量对不上时拒绝当三元组（宁可报结构，也不瞎猜）")

    # ---- 18e. 完全认不出来的东西 -> None，让调用方打印结构 ----
    rows = K._flatten_detections([[1, 2], [3, 4]])
    check(rows is None, "认不出来时返回 None，交给日志打印结构")

    # ---- 18f. _describe_result 不能崩 ----
    try:
        d = K._describe_result(real)
        ok = isinstance(d, str) and len(d) > 0
    except Exception:
        ok = False
    check(ok, "_describe_result 能安全描述三元组")

    # ---- 18g. 端到端：真实结构喂进 detect()，猫狗必须命中 ----
    class RealShapeModel(object):
        """完全模拟 v1.4.3：run 给张量，postprocess 给三元组。"""

        def __init__(self):
            self.results = None

        def run(self, img):
            self.results = ("fake_tensor",)
            return self.results

        def postprocess(self, results):
            return ([(212.0, 140.0, 168.0, 190.0)],
                    [15],
                    [0.87])

    d = _detector_with(RealShapeModel())
    d.results = None
    dets = d.detect("fake_frame")
    check(len(dets) == 1, "真实结构下能解出 1 个框（实际 %d）" % len(dets))
    if dets:
        check(dets[0][4] == K.CLASS_CAT,
              "解出来的序号是猫 %d（实际 %d）" % (K.CLASS_CAT, dets[0][4]))
        check(abs(dets[0][5] - 0.87) < 1e-6, "置信度 0.87 正确")
        check(dets[0][2] == 168.0 and dets[0][3] == 190.0,
              "宽高正确（不是取到了别的框）")

    # ---- 18h. display_size 和 AI 帧不一致时坐标要缩放 ----
    # 库按 display_size 输出坐标，触发区按 RGB888P_SIZE 算，不缩放就会错位。
    class ScaledModel(RealShapeModel):
        display_size = [1280, 960]      # 是 RGB888P 的 2 倍

        def postprocess(self, results):
            return ([(424.0, 280.0, 336.0, 380.0)], [15], [0.9])

    d = _detector_with(ScaledModel())
    d.results = None
    dets = d.detect("fake_frame")
    check(len(dets) == 1, "缩放场景下也能解出框")
    if dets:
        check(dets[0][0] == 212.0 and dets[0][1] == 140.0,
              "坐标按 0.5 缩放（实际 %.1f,%.1f）" % (dets[0][0], dets[0][1]))
        check(dets[0][2] == 168.0, "宽也一起缩放（实际 %.1f）" % dets[0][2])
# ================================================================
# 19. 上传被拒时必须能读到真实状态码（不是只看到 ECONNRESET）
#
# 现场现象：每张照片都 "第 1/2 次失败([Errno 104] ECONNRESET)"。
# 原因：服务端校验 token 失败时**不读 body 就关连接**，客户端还在写
#       360KB，内核回 RST，于是那个精心写的 403 提示永远看不到。
# 两端都修了：服务端先 drain body；客户端 send 失败后仍读一次响应。
# ================================================================
def case_reject_readable():
    print("\n--- 19. 被拒时也要读到真实状态码 ---")

    old_url = K.SERVER_URL
    old_tok = K.API_TOKEN

    def _make_sock(status_line, raise_on_send=False):
        calls = {"connect": 0, "send": 0, "recv": 0}

        class FakeSock(object):
            def settimeout(self, t):
                pass

            def connect(self, addr):
                calls["connect"] += 1

            def send(self, data):
                calls["send"] += 1
                if raise_on_send:
                    raise OSError(104, "ECONNRESET")
                return len(data)

            def recv(self, n):
                calls["recv"] += 1
                if calls["recv"] > 1:
                    return b""
                return status_line

            def close(self):
                pass

        class Mod(object):
            def socket(self):
                return FakeSock()

        return Mod(), calls

    def _run(mod, tok=""):
        saved = sys.modules.get("socket")
        sys.modules["socket"] = mod
        old_token = K.API_TOKEN
        K.API_TOKEN = tok
        try:
            up = K.Uploader()
            det = [10.0, 10.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
            return up._post(b"\xff\xd8jpeg", "cat", det)
        finally:
            K.API_TOKEN = old_token
            if saved is not None:
                sys.modules["socket"] = saved
            else:
                sys.modules.pop("socket", None)

    try:
        K.SERVER_URL = "http://127.0.0.1:8000"

        # ---- 19a. send 报 ECONNRESET，但服务端其实回了 403 ----
        # 这是现场的真实情况：必须先读响应，才能知道是 token 问题。
        mod, calls = _make_sock(b"HTTP/1.0 403 Forbidden\r\n\r\n",
                                raise_on_send=True)
        ok = _run(mod, tok="wrong")
        check(not ok, "403 判为失败")
        check(calls["connect"] == 1,
              "403 不重试（实际连了 %d 次）" % calls["connect"])
        check(calls["recv"] >= 1,
              "send 失败后仍然去读了响应（实际 %d 次）" % calls["recv"])

        # ---- 19b. 413 也不该重试（同一张图，重试还是一样） ----
        mod, calls = _make_sock(b"HTTP/1.0 413 Payload Too Large\r\n\r\n")
        ok = _run(mod)
        check(not ok, "413 判为失败")
        check(calls["connect"] == 1,
              "413 不重试（实际连了 %d 次）" % calls["connect"])

        # ---- 19c. 服务端静默关闭（0 字节响应）-> 该重试 ----
        mod, calls = _make_sock(b"")
        ok = _run(mod)
        check(not ok, "无响应判为失败")
        check(calls["connect"] > 1,
              "无响应时会重试（实际连了 %d 次）" % calls["connect"])

        # ---- 19d. 正常 200 一次就成功 ----
        mod, calls = _make_sock(b"HTTP/1.0 200 OK\r\n\r\n")
        ok = _run(mod)
        check(ok, "200 判为成功")
        check(calls["connect"] == 1, "200 不需要重试")
    finally:
        K.SERVER_URL = old_url
        K.API_TOKEN = old_tok


# ================================================================
# 20. 照片太大时自动降质量（360KB 在 SDIO WiFi 上太慢）
# ================================================================
def case_jpeg_size_cap():
    print("\n--- 20. 照片过大自动降质量 ---")

    old_max = K.UPLOAD_MAX_BYTES
    old_q = K.JPEG_QUALITY
    old_fb = K.JPEG_QUALITY_FALLBACKS

    def fake_bytes(size_by_q):
        """模拟 compress：不同 quality 给出不同长度。"""
        def fn(im, quality=None):
            q = quality if quality is not None else old_q
            n = size_by_q.get(q)
            if n is None:
                return None
            return b"\xff\xd8" + b"x" * (n - 2)
        return fn

    try:
        K.UPLOAD_MAX_BYTES = 100 * 1024
        K.JPEG_QUALITY = 82
        K.JPEG_QUALITY_FALLBACKS = (65, 50, 40)
        saved = K._jpeg_bytes

        # ---- 20a. q82 是 360KB -> 必须降到够小 ----
        K._jpeg_bytes = fake_bytes({82: 360 * 1024, 65: 150 * 1024,
                                    50: 90 * 1024, 40: 60 * 1024})
        up = K.Uploader()
        b = up._jpeg(object())
        check(b is not None and len(b) <= 100 * 1024,
              "降质量后 <= 上限（实际 %d KB）" % (len(b) // 1024 if b else -1))
        check(len(b) == 90 * 1024,
              "用 q50 就够小了，不再多压一次（实际 %d KB）" % (len(b) // 1024))

        # ---- 20b. 本来就不大 -> 一次都不该重压 ----
        K._jpeg_bytes = fake_bytes({82: 40 * 1024, 65: 20 * 1024,
                                    50: 15 * 1024, 40: 10 * 1024})
        up = K.Uploader()
        b = up._jpeg(object())
        check(len(b) == 40 * 1024,
              "够小就不动它（实际 %d KB）" % (len(b) // 1024))

        # ---- 20c. 降到底还是大 -> 用最小的那个，别放弃 ----
        K._jpeg_bytes = fake_bytes({82: 900 * 1024, 65: 800 * 1024,
                                    50: 700 * 1024, 40: 650 * 1024})
        up = K.Uploader()
        b = up._jpeg(object())
        check(len(b) == 650 * 1024,
              "降到底仍然超限时用最小的（实际 %d KB）" % (len(b) // 1024))

        # ---- 20d. 降质量失败(None) -> 保留原来的，不能变空 ----
        K._jpeg_bytes = fake_bytes({82: 360 * 1024})
        up = K.Uploader()
        b = up._jpeg(object())
        check(b is not None and len(b) == 360 * 1024,
              "降质量拿不到字节时保留原图，不能返回空")

        K._jpeg_bytes = saved
    finally:
        K.UPLOAD_MAX_BYTES = old_max
        K.JPEG_QUALITY = old_q
        K.JPEG_QUALITY_FALLBACKS = old_fb


# ================================================================
# 21. 上传不能阻塞主循环（fps 从 15 掉到 0 的根因）
#
# 现场现象：一次失败要等 3×8s=24s，期间不检测不喂饭，日志里
#           "1.0 fps" 然后 "0.0 fps"。动物在盆边，却没有饭。
# ================================================================
def case_upload_nonblocking():
    print("\n--- 21. 上传不能阻塞主循环 ---")

    old_bg = K.UPLOAD_IN_BACKGROUND
    old_timeout = K.UPLOAD_TIMEOUT_S

    try:
        # ---- 21a. 打开后台后，send() 必须立刻返回，不等网络 ----
        K.UPLOAD_IN_BACKGROUND = True
        up = K.Uploader.__new__(K.Uploader)
        up.ok = up.fail = up.saved = 0
        up.ready = True
        up.status = "wifi"
        up.ip = "192.168.43.170"
        up._q = []
        up._worker = None
        up._uid_seq = 0

        # 假装已经在跑了，免得真起线程
        up._worker = True

        # 模拟压图：给个固定字节
        saved_jpeg = K.Uploader._jpeg
        K.Uploader._jpeg = lambda self, im: b"\xff\xd8" + b"x" * 40000
        saved_frame = K._frame_to_image
        K._frame_to_image = lambda img, key=None: object()
        try:
            det = [10.0, 10.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
            t0 = _clock[0]
            ok = up.send(object(), det)
            elapsed = _clock[0] - t0
            check(ok, "send() 返回 True（已交给后台）")
            check(elapsed < 50,
                  "send() 立刻返回，不阻塞（模拟耗时 %d ms）" % elapsed)
            check(len(up._q) == 1, "任务进了队列（实际 %d）" % len(up._q))
            check(up.status == "queued", "状态是 queued（实际 %s）" % up.status)

            # ---- 21b. 内容指纹是确定性的（只用于文件名/排查）----
            # 注意：去重用的 uid 不是它，而是 _event_uid（见 case 24）。
            # 内容指纹当 uid 会出事 —— 静止画面两次到访的 JPEG 字节完全相同。
            photo_a = b"\xff\xd8" + bytes(range(60)) * 2000
            photo_b = b"\xff\xd8" + bytes(range(60)) * 2000 + b"\x01"
            check(up._jpeg_uid(photo_a) == up._jpeg_uid(photo_a),
                  "同一份字节 -> 同一个内容指纹")
            check(up._jpeg_uid(photo_a) != up._jpeg_uid(photo_b),
                  "字节不同 -> 指纹不同")
            check(len(up._jpeg_uid(photo_a).split("-")) >= 3,
                  "指纹里带设备号和长度，方便排查")
            # 采样算法不能太慢：100KB 在 K230 上必须是毫秒级
            big = b"\xff\xd8" + bytes(range(256)) * 400
            t1 = _clock[0]
            up._jpeg_uid(big)
            check(_clock[0] - t1 < 50,
                  "算 uid 很快（%d ms）" % (_clock[0] - t1))

            # ---- 21c. 队列有上限，满了丢最旧的而不是无限堆内存 ----
            K.UPLOAD_QUEUE_MAX = 2
            up._q = []
            for _ in range(6):
                up.send(object(), det)
            check(len(up._q) <= 2,
                  "队列不超过上限（实际 %d）" % len(up._q))
            K.UPLOAD_QUEUE_MAX = 2
        finally:
            K.Uploader._jpeg = saved_jpeg
            K._frame_to_image = saved_frame

        # ---- 21d. 关掉后台时必须还走同步（老固件兜底） ----
        K.UPLOAD_IN_BACKGROUND = False
        up2 = K.Uploader.__new__(K.Uploader)
        up2.ok = up2.fail = up2.saved = 0
        up2.ready = False            # 没网 -> 直接落盘
        up2.status = "off"
        up2._q = []
        up2._worker = None
        up2._uid_seq = 0
        K.Uploader._jpeg = lambda self, im: b"\xff\xd8" + b"x" * 100
        K._frame_to_image = lambda img, key=None: object()
        saved_save = K.Uploader._save
        saved_list = []
        K.Uploader._save = lambda self, j, s, d=None, uid=None: saved_list.append(s)
        try:
            ok = up2.send(object(), [10.0, 10.0, 100.0, 120.0,
                                     K.CLASS_CAT, 0.9])
            check(ok is False, "无后台时同步返回 False（没网）")
            check(len(saved_list) == 1, "同步模式下失败照片会落盘")
        finally:
            K.Uploader._jpeg = saved_jpeg
            K._frame_to_image = saved_frame
            K.Uploader._save = saved_save
    finally:
        K.UPLOAD_IN_BACKGROUND = old_bg
        K.UPLOAD_TIMEOUT_S = old_timeout


def case_no_response_real_reason():
    print("\n--- 22. 「服务端没有任何响应」的真实原因 ---")

    # ---- 22a. recv 返回 None（MicroPython 的超时写法）不能被当成对端关闭 ----
    class NoneFirstSock(object):
        """先返回几次 None（超时），再给出真响应。

        现场就是这样：服务端已经存好了图，响应还在路上，
        板子的 recv 先超时返回 None —— 老代码 `if not chunk: break`
        直接当成"没响应"，于是重传，一张照片被存 3 份。
        """
        def __init__(self, none_times):
            self.none_times = none_times
            self.calls = 0

        def recv(self, n):
            self.calls += 1
            if self.calls <= self.none_times:
                return None
            if self.calls == self.none_times + 1:
                return b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n"
            return b""

    s = NoneFirstSock(5)
    code = K._read_status(s, deadline_ms=5000)
    check(code == 200,
          "recv 先返回 None 也要继续等，最终读到 200（实际 %s）" % code)

    # ---- 22b. 真的关闭（b""）立刻返回 0，不空转 ----
    class ClosedSock(object):
        def recv(self, n):
            return b""

    check(K._read_status(ClosedSock(), deadline_ms=5000) == 0,
          "对端真的关了，立刻判定拿不到响应")

    # ---- 22b2. 超时假装成空包（真正的现场原因）----
    #
    # 日志里每次上传的第 1 次都失败：
    #   [up] 第 1 次失败(服务端没有任何响应, 等了 1173 ms)，重试
    #   [up] 上传成功 261 ms
    # 1173ms ≈ 1 秒 recv 超时。这个固件在超时时返回 b""，老代码当成
    # "对端关了"，于是服务端还在算特征、板子已经放弃并重传。
    #
    # 假 socket：每次 recv 推进 1 秒假时钟再返回 b""（= 超时），第 3 次
    # 才给出真响应 —— 必须能等到它。
    class TimeoutAsEmptySock(object):
        def __init__(self):
            self.calls = 0

        def settimeout(self, t):
            pass

        def recv(self, n):
            self.calls += 1
            _clock[0] += 1000            # 空等了 1 秒
            if self.calls <= 2:
                return b""               # 超时，但长得像 EOF
            return b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n"

    bs = TimeoutAsEmptySock()
    check(K._read_status(bs, deadline_ms=20000) == 200,
          "recv 超时返回 b'' 也要继续等，最终读到 200（实际试了 %d 次）"
          % bs.calls)
    check(bs.calls >= 3, "确实等了 3 次以上，没有在第 1 个空包就放弃")

    # 但总预算用完还是得认输，不能永远转
    class AlwaysTimeoutSock(object):
        def settimeout(self, t):
            pass

        def recv(self, n):
            _clock[0] += 1000
            return b""

    _clock[0] = 0
    check(K._read_status(AlwaysTimeoutSock(), deadline_ms=5000) == 0,
          "一直等不到就在 deadline 处放弃（不会死循环）")
    check(_clock[0] < 12000,
          "放弃时间受 deadline 约束（实际等了 %d ms）" % _clock[0])

    # ---- 22c. 一半的响应（分两包）也能拼出状态码 ----
    class SplitSock(object):
        def __init__(self):
            self.parts = [b"HTTP/1.0 ", b"200 OK\r\n", b"\r\n"]

        def recv(self, n):
            return self.parts.pop(0) if self.parts else b""

    check(K._read_status(SplitSock(), deadline_ms=2000) == 200,
          "响应分几包发也能正确解析")

    # ---- 22d. uid 必须进请求行，否则服务端没法去重 ----
    sent = {}
    import sys, types

    class FakeSock(object):
        def __init__(self, *a, **kw):
            self.buf = b""

        def settimeout(self, t):
            sent.setdefault("timeouts", []).append(t)

        def connect(self, addr):
            sent["addr"] = addr

        def send(self, data):
            self.buf += bytes(data)
            sent["all"] = self.buf
            return len(data)

        def recv(self, n):
            return b"HTTP/1.0 200 OK\r\n\r\n"

        def close(self):
            pass

    fake_mod = types.ModuleType("socket")
    fake_mod.socket = lambda *a, **kw: FakeSock()
    old_mod = sys.modules.get("socket")
    sys.modules["socket"] = fake_mod
    try:
        up = K.Uploader.__new__(K.Uploader)
        up.ok = up.fail = up.saved = 0
        up.ready = True
        up.status = "wifi"
        ok = up._post(b"\xff\xd8" + b"z" * 200, "cat",
                      [0.0, 0.0, 50.0, 50.0, K.CLASS_CAT, 0.9],
                      ts=1700000000, uid="k230-01-123-1")
        check(ok, "_post 成功（假服务器回 200）")
        head = sent["all"].split(b"\r\n\r\n")[0]
        check(b"uid=k230-01-123-1" in head, "请求里带了 uid")
        check(b"ts=1700000000" in head, "请求里带了拍照时间")
        check(b"Content-Length: 202" in head,
              "Content-Length 和实际字节数一致")
        check(K.UPLOAD_TIMEOUT_S in sent.get("timeouts", []),
              "连接/发送用的是 UPLOAD_TIMEOUT_S 这个预算（实际 %s）"
              % sent.get("timeouts"))
        check(1.0 in sent.get("timeouts", []),
              "recv 改成 1 秒一段，超时后还能继续等（不被一次卡死）")
    finally:
        if old_mod is not None:
            sys.modules["socket"] = old_mod


    # ---- 22e. 发给服务端的时间必须是 epoch，不能是 ticks ----
    # 踩过：job 里放了 time.ticks_ms()，服务端存进 device_ts 变成 "396239"，
    # 网页上时间列一片乱。
    saved_time = K.time.time
    try:
        K.time.time = lambda: 396.239          # 板子没 RTC 时的典型返回值
        check(K._epoch_now() is None,
              "time() 不可信（几百秒）时不发时间戳，交给服务端用收到时刻")
        K.time.time = lambda: 1700000000.5
        check(K._epoch_now() == 1700000000,
              "可信的 time() 才转成 epoch 整数（实际 %s）" % K._epoch_now())
    finally:
        K.time.time = saved_time


def case_visit_shot_alignment():
    print("\n--- 23. 到访次数 = 拍照张数（三个数对不上的根因）---")

    # 现场日志：串口打出"第 5 次到访"，网页上只有 1 条照片。
    # 原因：gate.visits 数到访边沿，拍照却是按墙上时钟 60 秒节流。
    # 现在 VisitShooter 让拍照跟着到访走。
    s = K.VisitShooter(per_visit=True)
    det = [400.0, 300.0, 300.0, 350.0, K.CLASS_CAT, 0.8]

    # 复刻日志里的节奏：来 -> 拍 -> 走 -> 又来（没拍照的也要算）
    shots = []
    for visit in range(1, 6):
        s.on_arrive()
        # 到访后第一帧可能还没框（消抖），第二帧才有
        if not s.tick(None, True):
            pass
        if s.tick(det, True):
            shots.append(visit)
        # 动物离开
        s.on_leave()

    check(len(shots) == 5,
          "5 次到访拍了 5 张（实际 %d 张）" % len(shots))
    check(shots == [1, 2, 3, 4, 5], "每次到访各拍一张，没有漏也没有多")
    check(s.missed == 0, "没有漏拍（实际漏 %d）" % s.missed)

    # 同一次到访里，蹲着不走只拍一张（不是每帧一张）
    s2 = K.VisitShooter(per_visit=True)
    s2.on_arrive()
    n = 0
    for _ in range(300):            # 3 秒 15fps
        if s2.tick(det, True):
            n += 1
    check(n == 1, "同一只动物蹲着不走只拍 1 张（实际 %d）" % n)

    # 消抖期间一直没框 -> 算漏拍，而且绝不能算到下一次到访头上
    s3 = K.VisitShooter(per_visit=True)
    s3.on_arrive()
    s3.tick(None, True)
    s3.on_leave()                    # 这次到访没抓到框
    check(s3.missed == 1, "没抓到框的到访记为漏拍")
    s3.on_arrive()                   # 下一次到访
    check(s3.tick(det, True) is True, "下一次到访照常拍一张（没被上次拖累）")
    check(s3.shots == 1, "只算 1 张（上次的漏拍没有变成多拍）")

    # 关掉 per_visit 就退回旧的"有框就交给时钟节流"
    s4 = K.VisitShooter(per_visit=False)
    got = sum(1 for _ in range(10) if s4.tick(det, True))
    check(got == 10, "旧模式下每帧都交给 maybe_upload 去节流（实际 %d）" % got)


def case_uid_per_event():
    print("\n--- 24. uid 必须按「到访事件」算，不能按照片内容 ---")

    # 现场证据：库里 #10 和 #69（相隔 23 分钟的两条记录）sha1 完全相同。
    # 相机对着静止画面（比如显示猫狗照片的屏幕）会产出**字节一样**的 JPEG。
    # 如果 uid 按内容算，第二次到访会被服务端认成重传、去重掉 ——
    # 串口"第 2 次到访"在网页上就消失了，又变成"数据对不上"。
    up = K.Uploader.__new__(K.Uploader)
    up.ok = up.fail = up.saved = 0
    up.ready = True
    up.status = "wifi"

    same_jpeg = b"\xff\xd8" + bytes(range(64)) * 300
    u1 = up._event_uid(1)
    u2 = up._event_uid(2)
    check(u1 != u2,
          "第 1 次和第 2 次到访的 uid 不同，即便照片字节完全一样")
    check(u1.startswith(K.DEVICE_ID), "uid 带设备号，方便排查")

    # 内容指纹只用来排查，不参与去重
    check(up._jpeg_uid(same_jpeg) == up._jpeg_uid(same_jpeg),
          "内容指纹是确定性的（同字节 -> 同指纹）")
    check(up._jpeg_uid(same_jpeg) != up._jpeg_uid(same_jpeg + b"\x00"),
          "内容不同则指纹不同")

    # ---- 24b. uid 写进文件名，补传时能原样读回来 ----
    import tempfile
    tmp = tempfile.mkdtemp()
    old_dir = K.SAVE_LOCAL_DIR
    K.SAVE_LOCAL_DIR = tmp
    try:
        uid = "k230-01-89213-3-4512"
        up._save(b"\xff\xd8zz", "cat", [0, 0, 0, 0, K.CLASS_CAT, 0.9], uid=uid)
        names = os.listdir(tmp)
        check(len(names) == 1, "存下 1 个文件")
        check(uid in names[0], "uid 写进了文件名（不是只存在内存里）")
        check(K._uid_from_saved(names[0]) == uid,
              "补传时能从文件名读回同一个 uid")
        # 老格式（三段）要能安全退回
        check(K._uid_from_saved("cat_913_12345.jpg") is None,
              "老格式文件名读不到 uid，返回 None（补传会退回内容指纹）")

        # 排序键取第 3 段 ticks —— 新格式最后一段是 uid，不能拿它排序
        a = K._saved_key("cat_900_100_k230-01-1-1.jpg")
        b = K._saved_key("cat_900_200_k230-01-1-2.jpg")
        check(a == 100 and b == 200, "新格式按 ticks 排序（%s < %s）" % (a, b))
        check(K._saved_key("cat_900_150.jpg") == 150, "老格式也能排序")
    finally:
        K.SAVE_LOCAL_DIR = old_dir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def case_boot_selfheal():
    print("\n--- 17. create() 卡死自救 ---")

    import tempfile
    tmp = tempfile.mkdtemp()
    state_file = os.path.join(tmp, ".k230_boot")
    old_file = K.BOOT_STATE_FILE
    old_enable = K.ENABLE_DISPLAY
    old_mode = K.DISPLAY_MODE
    old_wd = K.ENABLE_BOOT_WATCHDOG

    made = []
    created = []

    class FakePipeLine(object):
        fail_modes = ()

        def __init__(self, rgb888p_size=None, display_size=None,
                     display_mode=None):
            self.mode = display_mode
            made.append(display_mode)

        def create(self):
            if self.mode in FakePipeLine.fail_modes:
                raise OSError("mock fail")
            created.append(self.mode)

        def destroy(self):
            pass

        def get_display_size(self):
            return list(K.DISPLAY_SIZE)

    plmod = types.ModuleType("libs.PipeLine")
    plmod.PipeLine = FakePipeLine
    libs = types.ModuleType("libs")
    had_libs = "libs" in sys.modules
    had_pl = "libs.PipeLine" in sys.modules
    old_libs = sys.modules.get("libs")
    old_pl = sys.modules.get("libs.PipeLine")
    sys.modules["libs"] = libs
    sys.modules["libs.PipeLine"] = plmod

    K.BOOT_STATE_FILE = state_file
    K.ENABLE_DISPLAY = True
    K.DISPLAY_MODE = "lcd"
    # 测试里绝对不能真去 machine.reset()，显式关掉看门狗线程
    K.ENABLE_BOOT_WATCHDOG = False

    try:
        # ---- 17a. 没有记录文件时按正常顺序 ----
        check(K._read_boot_state() == (0, None), "没有记录文件时读回 (0, None)")

        # ---- 17b. 上次卡在 lcd -> 这次必须先试 virt ----
        f = open(state_file, "w")
        f.write("1,lcd")
        f.close()
        check(K._read_boot_state() == (1, "lcd"), "能读回上次卡住的模式")

        FakePipeLine.fail_modes = ()
        del made[:]
        pl, mode = K._open_pipeline()
        check(mode == "virt", "上次卡在 lcd -> 这次先用 virt（实际 %s）" % mode)
        check(made[0] == "virt",
              "第一枪不再撞 lcd（实际顺序 %s）" % (made,))

        # ---- 17c. 启动成功后要把记录删掉 ----
        check(not os.path.exists(state_file),
              "启动成功就删掉记录文件，下次恢复首选模式")

        # ---- 17d. 只卡一次也要能恢复：清了记录后又从 lcd 开始 ----
        del made[:]
        pl, mode = K._open_pipeline()
        check(made[0] == "lcd", "记录清掉后回到首选 lcd（实际 %s）" % (made,))

        # ---- 17e. 记录里是 virt -> 从 virt 之后开始 ----
        f = open(state_file, "w")
        f.write("2,virt")
        f.close()
        FakePipeLine.fail_modes = ()
        del made[:]
        pl, mode = K._open_pipeline()
        check(made[0] != "virt",
              "上次卡在 virt 就不会再用 virt 开头（实际 %s）" % (made,))

        # ---- 17f. 记录文件损坏 / 是垃圾 -> 不能崩 ----
        f = open(state_file, "w")
        f.write("garbage")
        f.close()
        n, m2 = K._read_boot_state()
        check(n == 0 and m2 is None, "记录文件是垃圾时安全退回 (0, None)")

        # ---- 17g. 看门狗：该重启才重启，正常跑绝不动 ----
        K.ENABLE_BOOT_WATCHDOG = True
        fired = []
        had_reset = hasattr(_machine, "reset")
        _machine.reset = lambda: fired.append(1)
        try:
            # 关掉开关时必须是纯 no-op（生产环境想手动控制就用这个）
            K.ENABLE_BOOT_WATCHDOG = False
            check(K._arm_boot_watchdog(1000, "lcd", {"ok": False}) is False,
                  "ENABLE_BOOT_WATCHDOG=False 时不起线程")
            K.ENABLE_BOOT_WATCHDOG = True

            def _real_spin(seconds):
                """真等一会儿，让工作线程有机会跑。

                不能用 time.sleep —— 本测试把 time.sleep/sleep_ms 都换成了
                "只推假时钟、不让出 CPU"的假函数，线程根本轮不到执行。
                time.time() 没被替换，用它自旋是真的在消耗墙钟时间。
                """
                import time as _rt
                end = _rt.time() + seconds
                while _rt.time() < end:
                    pass

            def _wait_for(fn, rounds=150, spin=0.02):
                for _ in range(rounds):
                    if fn():
                        return True
                    _real_spin(spin)
                return fn()

            # 正常：create() 已经返回 -> 看门狗必须安静退出，绝不重启
            flag = {"ok": True}
            armed = K._arm_boot_watchdog(1000, "lcd", flag)
            check(armed is True, "能起看门狗线程")
            _wait_for(lambda: False, rounds=60)
            check(not fired, "create() 成功时看门狗绝不重启板子")

            # 卡死：flag 永远 False -> 超时后必须调 reset
            flag2 = {"ok": False}
            K._arm_boot_watchdog(1000, "lcd", flag2)
            check(_wait_for(lambda: bool(fired)),
                  "create() 卡住超过超时时间 -> 自动重启板子")
        finally:
            if had_reset:
                _machine.reset = _machine.__dict__["reset"]
            else:
                try:
                    del _machine.reset
                except Exception:
                    pass
    finally:
        K.BOOT_STATE_FILE = old_file
        K.ENABLE_DISPLAY = old_enable
        K.DISPLAY_MODE = old_mode
        K.ENABLE_BOOT_WATCHDOG = old_wd
        try:
            if os.path.exists(state_file):
                os.remove(state_file)
            os.rmdir(tmp)
        except Exception:
            pass
        if had_libs:
            sys.modules["libs"] = old_libs
        else:
            sys.modules.pop("libs", None)
        if had_pl:
            sys.modules["libs.PipeLine"] = old_pl
        else:
            sys.modules.pop("libs.PipeLine", None)


# ================================================================
# 25. 网页上是灰图 —— 帧转 image 必须校验像素
# ================================================================
def _fake_frame_module(h, w):
    import types as _t

    class Plane(object):
        def __init__(self, c):
            self.c = c

        def tobytes(self):
            b = bytearray(h * w)
            i = 0
            for y in range(h):
                for x in range(w):
                    b[i] = (x * 7 + y * 3 + self.c * 40) % 256
                    i += 1
            return bytes(b)

    class HWC(object):
        """transpose 之后的视图。"""

        def tobytes(self):
            b = bytearray(h * w * 3)
            i = 0
            for y in range(h):
                for x in range(w):
                    for c in range(3):
                        b[i] = (x * 7 + y * 3 + c * 40) % 256
                        i += 1
            return bytes(b)

        def copy(self):
            return self

    class Frame(object):
        def __init__(self):
            self.shape = (3, h, w)

        def __getitem__(self, idx):
            if isinstance(idx, int):
                return Plane(idx)
            c, y, x = idx
            return (x * 7 + y * 3 + c * 40) % 256

        def transpose(self, a, b, c):
            return HWC()

        def tobytes(self):
            b = bytearray(h * w * 3)
            i = 0
            for c in range(3):
                for y in range(h):
                    for x in range(w):
                        b[i] = (x * 7 + y * 3 + c * 40) % 256
                        i += 1
            return bytes(b)

        def min(self):
            return 0

        def max(self):
            return 255

        def mean(self):
            return 127

    return Frame()


def _fake_image_module(honour):
    """honour = 哪几种 data 会被真的读进去，其余一律返回"灰"。"""
    import types as _t

    mod = _t.ModuleType("image")
    mod.RGB888 = 1
    mod.RGBP888 = 2
    mod.ALLOC_REF = 3

    class Img(object):
        def __init__(self, w, h, fmt, alloc=None, data=None):
            self.w, self.h, self.fmt = w, h, fmt
            self._px = None
            kind = type(data).__name__
            if kind in honour:
                raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
                if fmt == mod.RGBP888 and len(raw) == w * h * 3:
                    # 平面 -> 交织，还原成能取像素的样子
                    out = bytearray(w * h * 3)
                    n = w * h
                    for i in range(n):
                        out[i * 3] = raw[i]
                        out[i * 3 + 1] = raw[n + i]
                        out[i * 3 + 2] = raw[2 * n + i]
                    raw = bytes(out)
                if len(raw) == w * h * 3:
                    self._px = raw

        def get_pixel(self, x, y):
            if self._px is None:
                # 这就是现场那坨灰：读的是没初始化的内存，不会报错
                return (136, 120, 140)
            i = (y * self.w + x) * 3
            return (self._px[i], self._px[i + 1], self._px[i + 2])

    mod.Image = Img
    return mod


def _with_fake_frame_modules(honour, fn):
    import types as _t
    saved = {}
    for name in ("image", "ulab", "ulab.numpy"):
        saved[name] = sys.modules.get(name)

    imod = _fake_image_module(honour)
    umod = _t.ModuleType("ulab")
    nmod = _t.ModuleType("ulab.numpy")

    def reshape(a, shape):
        return a

    def array(a, dtype=None):
        return a

    nmod.reshape = reshape
    nmod.array = array
    nmod.np = nmod
    umod.numpy = nmod
    sys.modules["image"] = imod
    sys.modules["ulab"] = umod
    sys.modules["ulab.numpy"] = nmod
    try:
        return fn()
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def case_grey_photo():
    print("\n--- 25. 网页上是灰图（帧转 image 校验像素）---")
    H, W = 24, 32
    frame = _fake_frame_module(H, W)

    # 25a. 固件只认 bytearray：老写法（塞 ndarray）就是灰的，必须被识破
    def run_ok():
        K._CONV_LOGGED.clear()
        K._CONV_FAILED.clear()
        return K._frame_to_image(frame, key="t1")

    out = _with_fake_frame_modules({"bytearray"}, run_ok)
    check(out is not None, "能拿到一张可用的图（走的是被校验通过的那条路）")
    if out is not None:
        x, y = W // 2, H // 2
        want = (x * 7 + y * 3 + 0 * 40) % 256
        got = out.get_pixel(x, y)
        check(abs(got[0] - want) <= 2,
              "图上的像素和源帧一致（不是灰板）：%s vs R=%d" % (got, want))
        check(got != (136, 120, 140), "没有退化成现场那块灰")

    # 25b. 所有策略都读错 -> 必须明确返回 None，并且把原因记下来
    def run_bad():
        K._CONV_LOGGED.clear()
        K._CONV_FAILED.clear()
        return K._frame_to_image(frame, key="t2")

    out2 = _with_fake_frame_modules(set(), run_bad)
    check(out2 is None, "全都对不上时返回 None（不静默给一张灰图）")
    check(K._CONV_FAILED.get("t2") is True, "失败原因只记一次，不会每帧刷屏")

    # 25c. 手动交织出来的字节是对的（最后兜底不能也错）
    ba = K._interleave(frame, H, W)
    check(ba is not None and len(ba) == W * H * 3,
          "手动交织长度对（%s）" % (len(ba) if ba else None))
    if ba:
        i = ((H // 2) * W + (W // 2)) * 3
        x, y = W // 2, H // 2
        check(ba[i] == (x * 7 + y * 3) % 256 and
              ba[i + 1] == (x * 7 + y * 3 + 40) % 256,
              "手动交织的像素排布是对的（RGB 依次交错）")

    # 25d. 专用拍照通道：配得上就用，配不上就永久退回，不拖累主流程
    class SnapImg(object):
        def __init__(self, flat=False):
            self.flat = flat
            self.w, self.h = W, H

        def width(self):
            return self.w

        def height(self):
            return self.h

        def get_pixel(self, x, y):
            if self.flat:
                return (128, 128, 128)      # 纯色 = 这路通道没数据
            return ((x * 8) % 256, (y * 8) % 256, 128)

        def compress(self, quality=85):
            return b"\xff\xd8GOOD\xff\xd9"

    class FakeSensor(object):
        RGB888 = 7

        def __init__(self, snap=None, raise_on=None):
            self.snap = snap
            self.raise_on = raise_on or set()
            self.calls = []

        def set_framesize(self, width=None, height=None, chn=None):
            if "framesize" in self.raise_on:
                raise OSError("no mem")
            self.calls.append(("framesize", width, height, chn))

        def set_pixformat(self, fmt, chn=None):
            if "pixformat" in self.raise_on:
                raise OSError("bad fmt")
            self.calls.append(("pixformat", fmt, chn))

        def snapshot(self, chn=None):
            if "snapshot" in self.raise_on:
                raise OSError("chn failed")
            return self.snap

    class FakePL(object):
        def __init__(self, sensor):
            self.sensor = sensor

    # 正常：配置成功，拍出来的图有内容 -> 用它
    s1 = K.SnapChannel()
    sn = FakeSensor(snap=SnapImg())
    check(s1.setup(FakePL(sn)) is True, "拍照通道能配上")
    check(any(c[0] == "pixformat" for c in sn.calls),
          "确实设了打包 RGB888 格式")
    got = s1.grab()
    check(got is not None, "拍照通道取到了一张图")
    check(s1.ok is True, "通道状态仍然是可用")

    # 纯色 -> 立刻停用，以后都走帧转换
    s2 = K.SnapChannel()
    s2.setup(FakePL(FakeSensor(snap=SnapImg(flat=True))))
    check(s2.grab() is None, "纯色帧不会被当成照片")
    check(s2.ok is False and "纯色" in s2.why,
          "纯色时永久停用这路通道（原因：%s）" % s2.why)
    check(s2.grab() is None, "停用后不再反复去拍")

    # 配不上（内存不够）-> 安静退回，不抛异常
    s3 = K.SnapChannel()
    check(s3.setup(FakePL(FakeSensor(raise_on={"framesize"}))) is False,
          "配不上时返回 False")
    check(s3.grab() is None, "配不上就不产出图片，交给帧转换")

    # snapshot 报错 -> 停用
    s4 = K.SnapChannel()
    s4.setup(FakePL(FakeSensor(snap=SnapImg(), raise_on={"snapshot"})))
    check(s4.grab() is None and s4.ok is False,
          "snapshot 报错时停用通道")

    # 25e. send() 优先用专用通道，而不是去转换平面帧
    up = K.Uploader()
    up._worker = True                      # 假装后台线程已经在跑，不真起线程
    saved_jpeg = K.Uploader._jpeg
    saved_conv = K._frame_to_image
    saved_snap = K._SNAP
    saved_bg = K.UPLOAD_IN_BACKGROUND
    K.Uploader._jpeg = lambda self, im: b"\xff\xd8USED\xff\xd9"
    K.UPLOAD_IN_BACKGROUND = True
    conv_called = {"n": 0}

    def _conv(*a, **kw):
        conv_called["n"] += 1
        return None

    K._frame_to_image = _conv
    K._SNAP = s1
    try:
        det = [10.0, 10.0, 100.0, 120.0, K.CLASS_CAT, 0.9]
        ok = up.send(object(), det)
        check(ok and up.status == "queued",
              "send() 用专用通道拍到了照片（状态 %s）" % up.status)
        check(len(up._q) == 1, "一张照片进了待传队列")
        check(conv_called["n"] == 0, "有专用通道时不去转换平面帧")
    finally:
        K.Uploader._jpeg = saved_jpeg
        K._frame_to_image = saved_conv
        K._SNAP = saved_snap
        K.UPLOAD_IN_BACKGROUND = saved_bg


if __name__ == "__main__":
    for fn in (case_trigger_level, case_one_feed_per_visit, case_debounce,
               case_leave_and_return, case_pin_follows_presence, case_zone,
               case_parse_url, case_token_in_request, case_403_no_retry,
               case_backfill, case_partial_send, case_class_mapping,
               case_detect_pipeline, case_jpeg_fallback,
               case_full_frame_and_override, case_real_postprocess_shape,
               case_reject_readable, case_jpeg_size_cap,
               case_upload_nonblocking, case_no_response_real_reason,
               case_visit_shot_alignment, case_uid_per_event,
               case_boot_selfheal, case_grey_photo):
        fn()
    print("\n=== %s (%d/%d) ===" % ("ALL PASS" if _ok == _total else "PROBLEMS FOUND",
                                    _ok, _total))
    sys.exit(0 if _ok == _total else 1)
