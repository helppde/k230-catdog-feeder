# -*- coding: utf-8 -*-
"""
亚博 K230：识别猫/狗 -> GPIO42 高电平 -> 拍照上传 Web 端

=========================================================================
这份代码只做三件事：
    1. 用 K230 的 KPU 跑 YOLO（出厂固件自带 yolov8n，80 类 COCO）
    2. 画面里出现猫(cat)或狗(dog) -> GPIO42 输出高电平；连续 2 秒没有 -> 拉低
    3. 检测到猫/狗时拍一张 JPEG，POST 到 PC 的 Web 端（每 60 秒最多再补传一张）

硬件：亚博智能 K230（CanMV 固件，GC2093 摄像头 + 640x480 MIPI 屏）
      GPIO42 = 散热片旁 12pin 排针第 1 脚（3.3V 电平，默认功能就是普通 GPIO）
      GND    = 排针第 4 脚（信号必须共地）
      ⚠️ 排针第 9/11 脚是 5V，别接到 GPIO42 上，会烧板子
      ⚠️ 不要用 GPIO26（它是 SDIO 的 MMC1_CLK，占了 WiFi 就废了）

Web 端接口（本项目的 pc/server.py）：
      POST {SERVER_URL}/api/v1/visit?device=&species=&conf=&ws=&hs=&ts=[&token=]
      请求头：Content-Type: image/jpeg
      请求体：JPEG 原始字节（不是 multipart）
      成功：HTTP 200 + JSON {"ok": true, ...}
      失败：400（不是图片）/ 403（token 不对）/ 413（太大）

运行步骤：
      1. 改下面【配置】里的 WIFI_SSID / WIFI_PASS / SERVER_URL
      2. PC 上先跑 python pc/run.py，浏览器打开 http://<PC的IP>:8000
      3. CanMV IDE 打开本文件 -> 点运行
      4. 网页上出现照片、串口打印 [up] 上传成功 = 通了

常见问题：
      - "Kmodel file not exist"：出厂卡的模型在 /sdcard/kmodel/yolov8n_320.kmodel
        （目录名是 kmodel 不是 models）。本代码会自动在几个常见路径里找。
      - 串口刷 "snapshot chn(2) failed"：通常是 IDE 里反复运行没清媒体状态，
        拔 USB 重新上电；仍不行把 DISPLAY_MODE 改成 "virt" 再试。
      - 网页能开但没照片：先看串口 [up] 那几行，再看 Windows 防火墙有没有放行 8000。
=========================================================================
"""

import gc
import os
import time

# ============================================================
# 一、配置（一般只需要改这一段）
# ============================================================

# ---- WiFi ----
WIFI_SSID = "666666"
WIFI_PASS = "12345678"
WIFI_TIMEOUT_MS = 20000            # 连 WiFi 最多等 20 秒

# ---- Web 端地址 ----
# 局域网：填 pc/run.py 启动时打印的"局域网"那一行，带 http:// 和端口
# 云端  ：填 https://你的域名（需要固件带 ssl 模块；局域网建议直接用 http）
SERVER_URL = "http://192.168.1.100:8000"
API_TOKEN = ""                     # 服务端设了 CATDOG_TOKEN 就填一样的值
DEVICE_ID = "k230-01"              # 网页上区分是哪台 K230

# ---- 触发引脚 ----
# 42 = 亚博 12pin 排针第 1 脚，默认普通 GPIO，最省心。
TRIG_PIN = 42
TRIG_ACTIVE_HIGH = True            # True: 检测到=高电平；接反相器才改 False

# ---- 模型 ----
# 出厂固件自带 80 类 COCO 的 yolov8n（320x320），cat=15、dog=16。
KMODEL_PATH = "/sdcard/kmodel/yolov8n_320.kmodel"
KMODEL_FALLBACKS = (
    "/sdcard/kmodel/yolov8n_320.kmodel",
    "/sdcard/examples/kmodel/yolov8n_320.kmodel",
    "/sdcard/models/yolov8n_320.kmodel",
)
MODEL_INPUT_SIZE = (320, 320)      # 必须和 kmodel 的真实输入一致

# ---- 摄像头 / 屏幕 ----
RGB888P_SIZE = (640, 480)          # 给 AI 的帧，宽度要 16 对齐，和屏幕同比例
DISPLAY_SIZE = (640, 480)          # 亚博 2.4 寸屏是 640x480
DISPLAY_MODE = "lcd"               # 亚博屏用 "lcd"；没屏/IDE 里看画面用 "virt"

# ---- 检测参数 ----
MODEL_CONF_THRESH = 0.25           # 模型内部候选框门槛
NMS_THRESH = 0.45
CONF_THRESH = 0.45                 # 低于这个分数不认（宁可少认，不要瞎触发）
PRESENCE_ON_FRAMES = 3             # 连续 3 帧都看到才算"来了"（单帧容易误报）
PRESENCE_OFF_MS = 2000             # 连续 2 秒没看到才算"走了"（猫低头吃东西会漏检）

# ---- 拍照上传 ----
UPLOAD_ENABLE = True
UPLOAD_INTERVAL_MS = 60000         # 已经在吃的时候，每 60 秒最多再传一张
UPLOAD_TIMEOUT_S = 8               # 单次上传超时（秒）
UPLOAD_RETRIES = 1                 # 失败重试次数
JPEG_QUALITY = 80
SAVE_LOCAL_DIR = "/sdcard/captures"   # 上传失败先存这里，联网后补传
SAVE_LOCAL_MAX = 100
BACKFILL_PERIOD_MS = 20000         # 每 20 秒看一次有没有要补传的
BACKFILL_BATCH = 2                 # 一次最多补 2 张，别抢检测的时间

# ---- COCO 80 类（yolov8n 的类别顺序，cat=15 dog=16）----
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


def _find_label(name, default):
    """按名字反查类别序号，这样换成自己训的 2 类模型也不用改代码。"""
    i = 0
    for s in LABELS:
        if s == name:
            return i
        i += 1
    return default


CLASS_CAT = _find_label("cat", 15)
CLASS_DOG = _find_label("dog", 16)


def _log(msg):
    try:
        print(msg)
    except Exception:
        pass


# ============================================================
# 二、找模型 / 加载 YOLO
# ============================================================

def _stat_ok(path):
    try:
        os.stat(path)
        return True
    except Exception:
        return False


def _find_kmodel():
    """按已知位置找 kmodel，返回真实存在的那个。"""
    for p in (KMODEL_PATH,) + tuple(KMODEL_FALLBACKS):
        if _stat_ok(p):
            if p != KMODEL_PATH:
                _log("[det] 配置的 %s 不存在，改用 %s" % (KMODEL_PATH, p))
            return p
    # 常见位置都没有，扫一下 /sdcard/kmodel
    try:
        for name in os.listdir("/sdcard/kmodel"):
            low = name.lower()
            if low.endswith(".kmodel") and "yolo" in low:
                p = "/sdcard/kmodel/" + name
                _log("[det] 自动找到 %s" % p)
                return p
    except Exception:
        pass
    raise OSError("找不到 .kmodel，请把 KMODEL_PATH 改成卡上真实存在的路径")


def _load_yolo_cls(path):
    """按模型文件名选 libs.YOLO 里对应的类。

    YOLOv8 和 YOLO11 的前处理一样、**后处理不同**。用错类不会报错，
    只会"模型在跑但永远 0 个框"，所以必须按名字对号入座。
    """
    hint = str(path or "").lower()
    order = ["YOLOv8", "YOLO11", "YOLOv5"]
    if "yolov5" in hint:
        order = ["YOLOv5", "YOLOv8", "YOLO11"]
    elif "yolo11" in hint or "yolov11" in hint:
        order = ["YOLO11", "YOLOv8", "YOLOv5"]
    for name in order:
        try:
            mod = __import__("libs.YOLO", None, None, [name])
            cls = getattr(mod, name, None)
            if cls is not None:
                _log("[det] 使用 libs.YOLO.%s" % name)
                return cls
        except Exception:
            continue
    return None


# ============================================================
# 三、把模型输出统一成 [[x, y, w, h, cid, conf], ...]
# ============================================================

def _has_fraction(vals):
    """这列数是不是"带小数的 0~1"——用来判断哪一列是置信度。"""
    for v in vals:
        try:
            f = float(v)
        except Exception:
            return False
        if f < -0.0001 or f > 1.0001:
            return False
        if abs(f - int(f)) > 0.0001:
            return True
    return False


def _flatten(dets):
    """兼容两种返回结构：

        A) (框列表, 类别列表, 分数列表)   <- 亚博 v1.4.3 的 libs.YOLO 就是这样
        B) [[x, y, w, h, cid, conf], ...] <- 部分固件/自训模型是一框一行

    返回 None 表示认不出来（调用方会再试原始返回值），返回 [] 表示这一帧没目标。
    """
    if dets is None:
        return None

    # ---- A：三元组 ----
    try:
        if len(dets) == 3:
            boxes, a, b = dets[0], dets[1], dets[2]
            n = len(boxes)
            if n == 0:
                return []
            if len(a) == n and len(b) == n:
                # 有的固件顺序是 (框, 分数, 类别)，按"哪列有小数"判一下
                if _has_fraction(a) and not _has_fraction(b):
                    ids, confs = b, a
                else:
                    ids, confs = a, b
                rows = []
                for i in range(n):
                    bx = boxes[i]
                    if len(bx) < 4:
                        continue
                    rows.append([float(bx[0]), float(bx[1]),
                                 float(bx[2]), float(bx[3]),
                                 int(ids[i]), float(confs[i])])
                return rows
    except Exception:
        pass

    # ---- B：一框一行 ----
    try:
        rows = []
        for r in dets:
            if len(r) < 6:
                return None
            rows.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]),
                         int(r[4]), float(r[5])])
        return rows
    except Exception:
        return None


def _rows_sane(rows):
    """空列表算合理；有内容就必须经得起数值检查（防止把原始张量当框）。"""
    if rows is None:
        return False
    if len(rows) == 0:
        return True
    for r in rows[:5]:
        try:
            x, y, w, h, cid, conf = r
        except Exception:
            return False
        if conf < 0.0 or conf > 1.0:
            return False
        if cid < 0 or cid > 1000:
            return False
        if w <= 0 or h <= 0:
            return False
        if abs(x) > 100000 or abs(y) > 100000:
            return False
    return True


# ============================================================
# 四、检测器
# ============================================================

class Detector(object):
    def __init__(self):
        self.path = _find_kmodel()
        cls = _load_yolo_cls(self.path)
        if cls is None:
            raise OSError("没有 libs.YOLO（固件不是 CanMV 出厂固件？）")

        kw = dict(kmodel_path=self.path, labels=LABELS,
                  rgb888p_size=list(RGB888P_SIZE),
                  display_size=list(DISPLAY_SIZE),
                  model_input_size=list(MODEL_INPUT_SIZE),
                  conf_thresh=MODEL_CONF_THRESH,
                  nms_thresh=NMS_THRESH, debug_mode=0)

        # 不同固件参数名略有差异，逐级降级
        self.model = None
        try:
            self.model = cls(task_type="detect", mode="video", **kw)
        except TypeError:
            kw2 = dict(kw)
            kw2.pop("model_input_size", None)
            try:
                self.model = cls(task_type="detect", mode="video", **kw2)
            except TypeError:
                self.model = cls(mode="video", **kw2)
        if self.model is None:
            raise OSError("YOLO 构造失败（kmodel 和固件不匹配）")

        # 预处理（老固件不认参数就无参调用；detect 分支本身带 letterbox）
        try:
            self.model.config_preprocess()
        except Exception:
            try:
                self.model.config_preprocess(model_input_size=list(MODEL_INPUT_SIZE))
            except Exception:
                pass

        self.display_size = DISPLAY_SIZE
        try:
            d = self.model.display_size
            self.display_size = (int(d[0]), int(d[1]))
        except Exception:
            pass

        self._warned_run = False
        self._warned_filter = False
        self._last_hit_log = 0

        _log("[det] 模型加载完成: %s" % self.path)
        _log("[det] 猫/狗类别序号: cat=%d dog=%d（从 LABELS 按名字查到）"
             % (CLASS_CAT, CLASS_DOG))

    def detect(self, img):
        """返回 [[x, y, w, h, cid, conf], ...]，坐标已换算到 RGB888P_SIZE 空间。"""
        try:
            res = self.model.run(img)
        except Exception as e:
            if not self._warned_run:
                self._warned_run = True
                _log("[det] !! run() 失败: %s" % e)
            return []

        # 官方两种写法都存在：run() 直接给框，或者只给张量、要再调 postprocess。
        rows = None
        pp = getattr(self.model, "postprocess", None)
        if pp is not None:
            for args in ((res, NMS_THRESH), (res,)):
                try:
                    rows = _flatten(pp(*args))
                    break
                except TypeError:
                    continue
                except Exception:
                    rows = None
                    break
        if not _rows_sane(rows):
            alt = _flatten(res)
            if _rows_sane(alt):
                rows = alt
        if not rows:
            return []

        # ---- 坐标空间对齐（video 模式下库按 display_size 输出）----
        W, H = RGB888P_SIZE
        dw, dh = self.display_size
        sx = sy = 1.0
        if dw and dh and (dw, dh) != (W, H):
            sx = float(W) / float(dw)
            sy = float(H) / float(dh)

        out = []
        now = time.ticks_ms()
        for r in rows:
            x, y, w, h, cid, conf = r
            if conf < CONF_THRESH or conf > 1.0:
                continue
            if cid != CLASS_CAT and cid != CLASS_DOG:
                continue
            if w <= 0 or h <= 0:
                continue
            out.append([x * sx, y * sy, w * sx, h * sy, cid, conf])

        if out and time.ticks_diff(now, self._last_hit_log) >= 3000:
            self._last_hit_log = now
            d = out[0]
            _log("[det] 命中 %s conf=%.2f 框=%d,%d %dx%d"
                 % ("猫" if d[4] == CLASS_CAT else "狗", d[5],
                    int(d[0]), int(d[1]), int(d[2]), int(d[3])))

        if not out and rows and not self._warned_filter:
            self._warned_filter = True
            _log("[det] 看到 %d 个目标，但类别都不是猫/狗（检查 LABELS 是否和模型一致）"
                 % len(rows))
            for r in rows[:3]:
                _log("[det]   类别序号=%d 分数=%.2f" % (r[4], r[5]))
        return out

    def deinit(self):
        try:
            self.model.deinit()
        except Exception:
            pass


# ============================================================
# 五、GPIO 触发（GPIO42 输出）
# ============================================================

class Trigger(object):
    def __init__(self, num):
        from machine import Pin
        self.num = num
        self.level = None
        # 老固件要先做 FPIOA 复用；新固件 Pin() 直接可用。两条路都走一遍。
        try:
            from machine import FPIOA
            fpioa = FPIOA()
            fn = getattr(FPIOA, "GPIO%d" % num, None)
            if fn is not None:
                fpioa.set_function(num, fn)
        except Exception:
            pass
        self.pin = Pin(num, Pin.OUT)
        self.set(False)
        _log("[trig] GPIO%d 已配置为输出（3.3V 电平）" % num)

    def set(self, on):
        """只在状态真的变化时才写引脚，避免每帧重复写。"""
        want = 1 if on else 0
        if not TRIG_ACTIVE_HIGH:
            want = 1 - want
        if self.level == want:
            return
        self.level = want
        try:
            self.pin.value(want)
        except Exception:
            try:
                if want:
                    self.pin.high()
                else:
                    self.pin.low()
            except Exception:
                pass
        _log("[trig] GPIO%d -> %s"
             % (self.num, "高（检测到猫/狗）" if want else "低（没有目标）"))


# ============================================================
# 六、存在消抖：连续 N 帧才算来，连续 T 毫秒没看到才算走
# ============================================================

class PresenceGate(object):
    def __init__(self, on_frames, off_ms):
        self.on_frames = on_frames
        self.off_ms = off_ms
        self.present = False
        self.hits = 0
        self.last_seen = 0
        self.visits = 0

    def update(self, now, det):
        """返回 True 表示"这次是新的一次到访"（GPIO 刚由低变高）。"""
        if det is not None:
            self.hits += 1
            self.last_seen = now
        else:
            self.hits = 0

        if not self.present:
            if self.hits >= self.on_frames:
                self.present = True
                self.visits += 1
                return True
        else:
            if time.ticks_diff(now, self.last_seen) >= self.off_ms:
                self.present = False
        return False


# ============================================================
# 七、取帧 -> image.Image -> JPEG 字节
# ============================================================
# image.Image(alloc=ALLOC_REF, data=xxx) 不拥有那块内存，必须把底层数组挂住，
# 否则下一帧 GC 回收后 compress() 会读到已释放内存，板子直接硬崩。
_FRAME_HOLD = {}


def _frame_to_image(img):
    """把 PipeLine.get_frame() 的 CHW ndarray 转成 image.Image。"""
    if img is None:
        return None
    # 已经是 image 对象就直接用。
    # 注意：不能只判断 hasattr(img, "compress") —— ulab/numpy 的 ndarray 也有
    # 一个同名的 compress()（签名完全不同），那样会把原始帧当图片传下去。
    if hasattr(img, "compress") and not hasattr(img, "shape"):
        return img
    try:
        import image
        import ulab.numpy as np
    except Exception:
        return None
    try:
        shape = img.shape
    except Exception:
        return None
    if len(shape) != 3:
        return None
    h = int(shape[1])
    w = int(shape[2])
    fmt = getattr(image, "RGB888", None)
    if fmt is None:
        return None
    try:
        hwc = np.reshape(img, (3, h, w)).transpose(1, 2, 0)
    except Exception:
        return None
    try:
        hwc = hwc.copy()
    except Exception:
        try:
            hwc = np.array(hwc)
        except Exception:
            return None
    try:
        out = image.Image(w, h, fmt, alloc=image.ALLOC_REF, data=hwc)
    except Exception:
        return None
    _FRAME_HOLD["up"] = (out, hwc)
    return out


def _jpeg_bytes(im):
    """压成 JPEG 字节。compress() 对格式最宽容，优先用它。"""
    for name in ("compress", "to_jpeg"):
        fn = getattr(im, name, None)
        if fn is None:
            continue
        try:
            b = fn(quality=JPEG_QUALITY)
            if b:
                return b
        except Exception:
            pass
        try:
            b = fn(JPEG_QUALITY)
            if b:
                return b
        except Exception:
            pass
        try:
            b = fn()
            if b:
                return b
        except Exception:
            pass
    return None


# ============================================================
# 八、HTTP 上传（裸 socket，不依赖 urequests）
# ============================================================

def _tls_wrap(sock, host):
    try:
        import ssl
    except Exception:
        return None
    try:
        if hasattr(ssl, "SSLContext"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            except Exception:
                pass
            return ctx.wrap_socket(sock, server_hostname=host)
    except Exception:
        pass
    try:
        return ssl.wrap_socket(sock, server_hostname=host)
    except Exception:
        return None


def _send_all(sock, data):
    """send() 不保证一次发完，几十 KB 的图片必须循环发。"""
    n = len(data)
    if n == 0:
        return
    try:
        mv = memoryview(data)
    except Exception:
        mv = data
    total = 0
    while total < n:
        sent = sock.send(mv[total:] if total else mv)
        if sent is None:            # 个别固件的 send() 返回 None
            sent = n - total
        if not sent:
            raise OSError("send 返回 0，连接已断开")
        total += sent


def _read_status(sock, limit=512):
    """读响应头，返回 HTTP 状态码。"""
    buf = b""
    while len(buf) < limit:
        try:
            chunk = sock.recv(256)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf:
            break
        if b"\r\n" in buf and b" " in buf.split(b"\r\n", 1)[0]:
            # 已经有状态行了，再等一小会儿就够
            if len(buf) > 32:
                break
    if not buf:
        return 0
    line = buf.split(b"\r\n", 1)[0]
    parts = line.split(b" ")
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except Exception:
        return 0


def _parse_saved_name(name):
    """本地缓存文件名：cat_913_12345.jpg -> ("cat", 0.913)"""
    parts = name[:-4].split("_")
    species = parts[0]
    if species not in ("cat", "dog"):
        species = "unknown"
    conf = 0.5
    if len(parts) >= 2:
        try:
            conf = int(parts[1]) / 1000.0
        except Exception:
            pass
    return species, conf


class Uploader(object):
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.ready = False
        self.scheme = "http"
        self.host = ""
        self.port = 80
        self.base = ""
        self._parse_url()
        if not UPLOAD_ENABLE:
            _log("[up] 上传已关闭（UPLOAD_ENABLE=False）")
            return
        self.ready = self._wifi()

    def _parse_url(self):
        s = (SERVER_URL or "").strip()
        if "://" not in s:
            s = "http://" + s
        i = s.find("://")
        self.scheme = s[:i].lower()
        rest = s[i + 3:]
        j = rest.find("/")
        if j >= 0:
            hostport = rest[:j]
            self.base = rest[j:].rstrip("/")
        else:
            hostport = rest
            self.base = ""
        if ":" in hostport:
            hp = hostport.rsplit(":", 1)
            self.host = hp[0]
            try:
                self.port = int(hp[1])
            except Exception:
                self.port = 443 if self.scheme == "https" else 80
        else:
            self.host = hostport
            self.port = 443 if self.scheme == "https" else 80
        _log("[up] 目标 %s://%s:%d%s"
             % (self.scheme, self.host, self.port, self.base or "/"))

    def _wifi(self):
        try:
            import network
        except Exception:
            _log("[up] 固件没有 network 模块")
            return False
        try:
            wlan = network.WLAN(network.STA_IF)
            wlan.active(True)
            if not wlan.isconnected():
                _log("[up] 正在连 WiFi: %s" % WIFI_SSID)
                wlan.connect(WIFI_SSID, WIFI_PASS)
                t0 = time.ticks_ms()
                while not wlan.isconnected():
                    if time.ticks_diff(time.ticks_ms(), t0) > WIFI_TIMEOUT_MS:
                        _log("[up] !! WiFi 连接超时（检查 SSID/密码/信号）")
                        return False
                    time.sleep_ms(200)
            _log("[up] WiFi 已连接: %s" % (wlan.ifconfig()[0],))
            return True
        except Exception as e:
            _log("[up] !! WiFi 失败: %s" % e)
            return False

    # ---------- 对外：拍一张、传一张 ----------
    def send(self, img, det):
        if not UPLOAD_ENABLE or det is None:
            return False
        src = _frame_to_image(img)
        if src is None:
            _log("[up] !! 这一帧转不成 image.Image，拍不了照")
            self.fail += 1
            return False
        jpeg = _jpeg_bytes(src)
        if not jpeg:
            _log("[up] !! JPEG 编码失败")
            self.fail += 1
            return False
        species = "cat" if int(det[4]) == CLASS_CAT else "dog"
        conf = float(det[5])
        _log("[up] 拍照: %s conf=%.2f %d 字节" % (species, conf, len(jpeg)))
        if self.ready and self._post(jpeg, species, conf):
            self.ok += 1
            _log("[up] 上传成功（累计 %d 张）" % self.ok)
            return True
        self._save_local(jpeg, species, conf)
        self.fail += 1
        return False

    # ---------- HTTP POST ----------
    def _post(self, jpeg, species, conf):
        qs = ("device=%s&species=%s&conf=%.3f&ws=%d&hs=%d&ts=%d"
              % (DEVICE_ID, species, conf,
                 RGB888P_SIZE[0], RGB888P_SIZE[1], int(time.ticks_ms())))
        if API_TOKEN:
            qs += "&token=" + API_TOKEN
        path = (self.base or "") + "/api/v1/visit?" + qs
        host = self.host
        if self.port not in (80, 443):
            host = "%s:%d" % (self.host, self.port)
        head = ("POST %s HTTP/1.0\r\n"
                "Host: %s\r\n"
                "Content-Type: image/jpeg\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n\r\n"
                % (path, host, len(jpeg)))
        try:
            import socket
        except Exception:
            import usocket as socket

        err = None
        for attempt in range(1 + UPLOAD_RETRIES):
            s = None
            try:
                s = socket.socket()
                s.settimeout(UPLOAD_TIMEOUT_S)
                s.connect((self.host, self.port))
                if self.scheme == "https":
                    s = _tls_wrap(s, self.host)
                    if s is None:
                        _log("[up] 这个固件没有可用的 ssl，https 传不了；改用 http")
                        return False
                # 服务端可能"先把 body 读掉再回错误"（比如 403/413），
                # 这种时候 send 有可能先抛异常，但响应其实已经发出来了。
                # 所以 send 失败也要继续读一次状态行，别把可读的错误丢掉。
                send_err = None
                try:
                    _send_all(s, head.encode())
                    _send_all(s, jpeg)
                except Exception as e:
                    send_err = e
                code = _read_status(s)
                if code == 200:
                    return True
                if code == 403:
                    _log("[up] 403：token 不对（K230 的 API_TOKEN 要和服务端 CATDOG_TOKEN 一致）")
                    return False
                if code in (400, 413):
                    _log("[up] !! 服务端拒绝（HTTP %d）：检查图片/请求，不重试" % code)
                    return False
                err = "HTTP %d" % code if code else (send_err or "无响应")
            except Exception as e:
                err = e
            finally:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            if attempt < UPLOAD_RETRIES:
                _log("[up] 第 %d 次失败（%s），重试..." % (attempt + 1, err))
                time.sleep_ms(500)
        _log("[up] !! 上传失败: %s" % (err,))
        return False

    # ---------- 本地兜底 + 补传 ----------
    def _save_local(self, jpeg, species, conf):
        try:
            try:
                os.mkdir(SAVE_LOCAL_DIR)
            except Exception:
                pass
            c3 = int(conf * 1000)
            if c3 < 0:
                c3 = 0
            if c3 > 999:
                c3 = 999
            path = "%s/%s_%03d_%d.jpg" % (SAVE_LOCAL_DIR, species, c3,
                                          int(time.ticks_ms()) % 10000000)
            f = open(path, "wb")
            f.write(jpeg)
            f.close()
            _log("[up] 已存本地（联网后自动补传）: %s" % path)
            self._trim()
        except Exception as e:
            _log("[up] !! 存本地也失败: %s" % e)

    def _trim(self):
        try:
            names = [n for n in os.listdir(SAVE_LOCAL_DIR) if n.endswith(".jpg")]
            names.sort()
            for n in names[:max(0, len(names) - SAVE_LOCAL_MAX)]:
                try:
                    os.remove(SAVE_LOCAL_DIR + "/" + n)
                except Exception:
                    pass
        except Exception:
            pass

    def backfill(self, limit=BACKFILL_BATCH):
        """把之前上传失败、存在 SD 卡上的照片补传上去。"""
        if not UPLOAD_ENABLE or not self.ready:
            return 0
        try:
            names = sorted([n for n in os.listdir(SAVE_LOCAL_DIR)
                            if n.endswith(".jpg")])
        except Exception:
            return 0
        done = 0
        for name in names[:limit]:
            path = SAVE_LOCAL_DIR + "/" + name
            try:
                f = open(path, "rb")
                jpeg = f.read()
                f.close()
            except Exception:
                continue
            if not jpeg:
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            species, conf = _parse_saved_name(name)
            if self._post(jpeg, species, conf):
                try:
                    os.remove(path)
                except Exception:
                    pass
                done += 1
                _log("[up] 补传成功: %s" % name)
            else:
                break       # 还是不通，别每张都等一遍超时
        return done


# ============================================================
# 九、相机 / 显示
# ============================================================

def _rearm_chn2(pl):
    """create() 之后重新配一次 AI 通道 chn2。

    某些固件初始化屏幕时会把 chn2 的格式/缓冲冲掉，表现是 create() 成功、
    屏也亮，但 get_frame() 永远 None，串口刷 snapshot chn(2) failed(3)。
    """
    try:
        import sensor as _sensor
    except Exception:
        return
    target = getattr(pl, "sensor", None) or _sensor
    chn = getattr(_sensor, "CAM_CHN_ID_2", None)
    if chn is None:
        chn = 2
    try:
        target.set_framesize(width=RGB888P_SIZE[0], height=RGB888P_SIZE[1], chn=chn)
    except Exception:
        pass
    try:
        fmt = getattr(target, "RGB888_PLANAR", None) or getattr(_sensor, "RGB888_PLANAR", None)
        if fmt is not None:
            target.set_pixformat(fmt, chn=chn)
    except Exception:
        pass


def open_pipeline():
    # 清掉上一次运行残留的相机/显示状态（IDE 里反复运行很容易卡在缓冲池）
    try:
        import sensor
        sensor.stop()
    except Exception:
        pass
    try:
        import display
        display.deinit()
    except Exception:
        pass
    gc.collect()

    from libs.PipeLine import PipeLine
    _log("[main] 初始化相机/屏幕 mode=%s ..." % DISPLAY_MODE)
    pl = PipeLine(rgb888p_size=list(RGB888P_SIZE),
                  display_size=list(DISPLAY_SIZE),
                  display_mode=DISPLAY_MODE)
    pl.create()
    _rearm_chn2(pl)
    _log("[main] 相机就绪")
    return pl


def _draw(pl, dets, present, best, visits, sx, sy):
    canvas = getattr(pl, "osd_img", None)
    if canvas is None:
        return
    try:
        canvas.clear()
        for d in dets:
            color = (255, 255, 0) if d is best else (0, 255, 0)
            canvas.draw_rectangle(int(d[0] * sx), int(d[1] * sy),
                                  int(d[2] * sx), int(d[3] * sy),
                                  color=color, thickness=2)
        canvas.draw_string_advanced(8, 8, 24,
                                    "GPIO%d=%s  visits=%d"
                                    % (TRIG_PIN, "H" if present else "L", visits),
                                    color=(255, 255, 0))
        pl.show_image()
    except Exception:
        pass


# ============================================================
# 十、主循环
# ============================================================

def main():
    _log("=" * 54)
    _log(" 亚博 K230 猫狗识别 -> GPIO%d 高电平 + 拍照上传" % TRIG_PIN)
    _log(" 有猫/狗: GPIO 高；连续 %.1f 秒没有: 拉低" % (PRESENCE_OFF_MS / 1000.0))
    _log(" 上传: 检测到先传 1 张，之后每 %d 秒最多 1 张" % (UPLOAD_INTERVAL_MS // 1000))
    _log("=" * 54)

    pl = None
    trig = None
    det_model = None
    up = None
    try:
        pl = open_pipeline()
        trig = Trigger(TRIG_PIN)
        det_model = Detector()
        up = Uploader()
        gate = PresenceGate(PRESENCE_ON_FRAMES, PRESENCE_OFF_MS)

        # 画面坐标换算（屏幕实际分辨率可能和配置不同）
        DW, DH = DISPLAY_SIZE
        try:
            real = pl.get_display_size()
            if real and len(real) == 2:
                DW, DH = int(real[0]), int(real[1])
        except Exception:
            pass
        sx = float(DW) / float(RGB888P_SIZE[0])
        sy = float(DH) / float(RGB888P_SIZE[1])

        last_upload = None
        last_backfill = time.ticks_ms()
        bad_credit = 0
        while True:
            img = pl.get_frame()
            if img is None:
                bad_credit += 1
                if bad_credit == 60:
                    _log("[main] !! 连续 60 帧取不到画面：检查摄像头排线，")
                    _log("       或把 DISPLAY_MODE 改成 \"virt\"，再断电重上电试")
                time.sleep_ms(5)
                continue
            bad_credit = 0

            now = time.ticks_ms()
            try:
                dets = det_model.detect(img)
            except Exception as e:
                dets = []
                _log("[main] !! 检测异常（跳过这一帧，程序继续）: %s" % e)

            best = None
            for d in dets:
                if best is None or d[5] > best[5]:
                    best = d

            became = gate.update(now, best)
            trig.set(gate.present)

            if became:
                _log("")
                _log("[main] 第 %d 次到访: %s conf=%.2f -> GPIO%d 拉高"
                     % (gate.visits, "猫" if best[4] == CLASS_CAT else "狗",
                        best[5], TRIG_PIN))
                if up is not None:
                    up.send(img, best)
                last_upload = now
            elif gate.present and best is not None and up is not None:
                if last_upload is None or \
                        time.ticks_diff(now, last_upload) >= UPLOAD_INTERVAL_MS:
                    up.send(img, best)
                    last_upload = now

            if time.ticks_diff(now, last_backfill) >= BACKFILL_PERIOD_MS:
                last_backfill = now
                if up is not None:
                    try:
                        up.backfill(BACKFILL_BATCH)
                    except Exception as e:
                        _log("[main] 补传异常（忽略）: %s" % e)

            _draw(pl, dets, gate.present, best, gate.visits, sx, sy)
            gc.collect()

    except KeyboardInterrupt:
        _log("[main] 手动停止")
    except Exception as e:
        _log("[main] !! 程序退出: %s" % e)
    finally:
        try:
            if trig is not None:
                trig.set(False)         # 退出时别把信号线留在高电平
        except Exception:
            pass
        try:
            if det_model is not None:
                det_model.deinit()
        except Exception:
            pass
        try:
            if pl is not None:
                pl.destroy()
        except Exception:
            pass
        _log("[main] 已退出")


if __name__ == "__main__":
    main()
