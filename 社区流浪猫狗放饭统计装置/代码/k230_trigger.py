# -*- coding: utf-8 -*-
"""
K230 流浪猫狗识别 —— 持续电平版

K230 干三件事：
    1. 识别猫/狗，判断"现在有没有动物在盆边"
    2. 把一根 GPIO 电平实时反映这个状态：有动物就一直高，动物走了才拉低
    3. 有动物期间，每 60 秒拍一张照上传到 PC

机械动作（舵机/步进/超声测粮）全在 Arduino 那边。

为什么用一根线而不是串口：
    串口要收发、要交叉、要分压、要担心掉包，而且掉一条命令机构就停在半开状态。
    这里执行板本来就自己读盆内传感器、自己跑完整一轮，K230 只需要告诉它"有动物"。
    少一根线就少一处故障。

信号语义（重要）：
    高电平 = 有动物在这里，低电平 = 没有。
    K230 只负责如实反映这个状态，**不决定放几次饭**：

        低 -> 高（上升沿）  执行板跑一轮
        一直高               执行板每 2 分钟自己补放一轮（RE_FEED_WHILE_HIGH_MS）
        高 -> 低             执行板停止补放，等待下次上升沿

    所以"一只猫蹲半小时"会得到一次到访 + 每 2 分钟一次的补粮。
    补粮节奏完全在执行板上，K230 死机/WiFi 断了也不影响补粮。

代价（已知取舍）：Arduino 不会回话，K230 不知道这一轮跑完没有。
            如果想精确知道，可以接可选的 BUSY 反馈线（见 BUSY_PIN）。

本文件是单文件、无外部依赖，拷到 /sdcard 直接跑：
    CanMV IDE 打开本文件 -> 运行

适用硬件：亚博智能 K230（GC2093 摄像头 1920x1080 + 2.4 寸 MIPI 屏 640x480，
          出厂固件 CanMV_K230_YAHBOOM_micropython_V1.3.x）。
          换别的 K230 板子只要改 RGB888P_SIZE / DISPLAY_SIZE / DISPLAY_MODE。

⚠️ 触发引脚一定要先确认，见 tools/gpio_probe.py 和 README 第 12 节。
⚠️ 相机出不了帧见 README「snapshot chn(2) failed」，或用 tools/camera_probe.py。
⚠️ 识别不到猫狗见 README「识别不到猫狗」，或用 tools/det_probe.py 单步排查。
"""

import gc
import os
import time

# ============================================================
# 配置（改这里就够了）
# ============================================================

# ---------------- 触发引脚 ----------------
# Pin(N) 里的 N 是**芯片 GPIO 编号**，不是排针序号。
#
# 亚博（YAHBOOM）K230 模块只引出 7 个 GPIO，全部在散热片旁边那排 12pin 排针上
# （来源：亚博官方文档「2.基础例程 / 1.FPIOA引脚分配」）：
#
#   排针脚 | GPIO   | 可复用功能（任一时刻只能选一个）
#   -------+--------+------------------------------------------------------
#     1    | GPIO42 | GPIO42 / UART1_RTS / PWM0 / QSPI1_D2
#     2    | GPIO43 | GPIO43 / UART1_CTS / PWM1 / QSPI1_D3
#     3    | GPIO33 | GPIO33 / IIC0_SDA / IIS_WS   / UART3_RXD
#     4    | GND    |
#     5    | GPIO32 | GPIO32 / IIC0_SCL / IIS_CLK  / UART3_TXD
#     6    | GPIO26 | GPIO26 / MMC1_CLK / RESV     / PDM_CLK   ← 见下面警告
#     7    | GND    |
#     8    | GPIO34 | GPIO34 / IIC1_SCL / IIS_D_IN0/ PDM_IN3 / UART3_RTS
#     9    | 5V 输出|
#    10    | GPIO35 | GPIO35 / IIC1_SDA / IIS_D_OUT0/PDM_IN1 / UART3_CTS
#    11    | 5V 输出|
#    12    | 3.3V   |
#
# 默认填 42：排针脚 1，默认功能就是普通 GPIO，没有总线占用，最省心。
#
# ⚠️ 三个必须知道的坑：
#   ① GPIO26 = MMC1_CLK，就是 SDIO 的时钟线。板载 SDIO WiFi（RTL8189/AIC8800 之类）
#      用的就是 MMC1，占掉 GPIO26 的话 WiFi 直接废掉 —— **要用 WiFi 上传就别用 26**。
#   ② GPIO32/33/34/35 是 I2S 音频的时钟/数据线，GPIO26/34/35 还能当 PDM 麦克风。
#      默认镜像只开内置 codec（不占引脚），所以平时能当普通 GPIO 用；
#      但一旦你用了「音频录制 / 播放 / 语音播报」那几个例程，这几个脚就被抢走了。
#   ③ GPIO42/43 同时也是 PWM0/PWM1。如果板子上有东西靠 PWM 调光/调速（屏幕背光、
#      风扇），先确认不是这两个脚。
#
# 想实测确认：跑 tools/gpio_probe.py，或者在这里设 PRINT_PIN_INFO = True，
# 启动时会打印 fpioa.help() 里这个脚的真实状态。
TRIG_PIN = 42

# GPIO -> 亚博 12pin 排针的脚号（只为打印提示，接线时对着板子找）
HEADER_PIN = {42: 1, 43: 2, 33: 3, 32: 5, 26: 6, 34: 8, 35: 10}

PRINT_PIN_INFO = True        # 启动时打印该引脚当前的复用功能，方便核对

# ============================================================
# 触发信号：现在是"有动物就一直高电平"，不再是 800ms 脉冲
# ============================================================
#
# 以前是：检测到 -> 拉高 800ms -> 拉低。执行板靠这个脉冲开一次饭。
# 现在改成：检测到就一直高，动物走了才拉低。等于把"有没有动物"实时告诉执行板。
#
# 为什么执行板不用改：simple_trigger.ino 里本来就是**边沿 + armed 锁**
#   armed 只有在线变成 LOW 时才恢复 true；
#   触发一次后 armed = false，所以 K230 一直高着也不会重复开饭。
#   一只猫蹲着吃 10 分钟 -> 只开 1 次；走了再来 -> 才开第 2 次。
#   这个语义跟原来的脉冲完全一致，只是信号变成了"电平常高"。
#
# 但有个新问题：单帧检测会闪。猫动一下模型就可能漏一帧，电平跟着抖，
# 执行板就会看到一堆假边沿。所以下面加了消抖参数（ON 要连续 N 帧，OFF 要等 T 毫秒）。
TRIG_ACTIVE_HIGH = True     # True: 有动物=高电平。接反相缓冲才改成 False

# 判"有动物"的门槛。三个参数一起决定电平稳不稳：
PRESENCE_ON_FRAMES = 3      # 连续 3 帧都检测到才认为"来了"（单帧极易误报）
PRESENCE_ON_CONF = 0.45     # 确认用的置信度，比画框的门槛高一点，宁可不喂也别乱喂
PRESENCE_OFF_MS = 2000      # 连续 2 秒都没检测到才算"走了"（猫低头吃东西会被漏检）

# 同一次到访里，信号掉下来又升上去的最小间隔。
# 作用：动物在盆边蹭来蹭去导致电平抖一下，不会马上又开一次饭。
VISIT_MIN_GAP_MS = 30000

# 可选：接一根 Arduino -> K230 的"忙"信号回来（必须也是上面那 7 个脚之一）。
#   -1  = 没接（默认）
#   其它 = 引脚号，执行板跑一轮期间会拉高
#   ⚠️ Arduino 输出是 5V，K230 输入只吃 3.3V —— 中间必须分压（1k/2k）。
BUSY_PIN = -1
BUSY_WAIT_TIMEOUT_MS = 100000     # 等 BUSY 变低的最长时间

# 拍照上传的节奏。
#
# 以前是"每 60 秒最多一张"（墙上时钟）。问题是它和 `gate.visits` 是两套
# 完全不相干的计数：串口打出"第 5 次到访"，网页上只有 1 条 ——
# 用户看到的就是"串口、后台、网页三个数对不上"。
#
# 现在改成**跟着到访走**：每来一次动物就拍一张，串口"第 N 次到访"
# 和网页第 N 条记录一一对应。性能上也不再担心 —— 上传已经挪到后台线程，
# 主循环不会被拖住（见 UPLOAD_IN_BACKGROUND）。
UPLOAD_PER_VISIT = True
UPLOAD_ONLY_WHEN_PRESENT = True

# 只有 UPLOAD_PER_VISIT=False 时才用这个（旧的"每 N 秒一张"行为）。
UPLOAD_INTERVAL_MS = 60000

# ---------------- 识别 ----------------
# PipeLine 会开两个相机通道：
#   chn0 -> 屏幕（DISPLAY_SIZE）
#   chn2 -> 给模型用的 RGB888P 帧（RGB888P_SIZE）
# 串口刷 "sensor(0) snapshot chn(2) failed(3)" 就是 chn2 出不了帧。
# 这不是 Python 代码的问题，也不是模型的问题，是相机通道/内存的问题。
# 排查顺序见 README「snapshot chn(2) failed」。
SIMULATE_DETECTION = False       # True: 不加载模型，用假猫框把流程跑通

# ---- 模型路径 ----
# 亚博/CanMV 出厂固件**自带的** 80 类 COCO yolov8n 就放在这里，320x320 输入。
# 实测（CanMV v1.4.3 k230_canmv_yahboom）：
#   加载 OK、输入 1 个、输出 4 个，和 libs.YOLO.YOLOv8 完全对得上。
# 注意目录名是 kmodel 不是 models —— 早期版本写的是 /sdcard/models/，
# 那个目录在出厂卡上根本不存在，于是报 "Kmodel file not exist."。
KMODEL_PATH = "/sdcard/kmodel/yolov8n_320.kmodel"

# 找不到上面那个时，按顺序试这几个。都是厂商固件/例程里用过的位置。
KMODEL_FALLBACKS = (
    "/sdcard/kmodel/yolov8n_320.kmodel",
    "/sdcard/examples/kmodel/yolov8n_320.kmodel",
    "/sdcard/models/yolov8n_320.kmodel",
)

# ---- 分辨率（已按亚博 K230 实机参数核对）----
# 亚博 K230 的标配：GC2093 摄像头（1920x1080）+ 2.4 寸 MIPI 屏 **640x480**。
# 亚博官方例程里用的也是 640x480。
#
# 两个尺寸的约束不一样：
#   DISPLAY_SIZE  必须等于屏幕真实分辨率，填大了右键/花屏，填小了两边有黑边
#   RGB888P_SIZE  是 AI 取图分辨率，必须 <= 摄像头输出，宽度 16 对齐，
#                 而且要和屏幕同比例（4:3）。屏幕是 4:3 却给 16:9 的帧，
#                 画面会被压扁，检测框和触发区都会算偏。
#
# 640x480 同时满足：16 对齐（640/16=40）、和屏幕同比例、VB 内存最省。
# 模型输入只有 320x320，降到 640x480 不会掉检测精度。
# 原来写的 1280x720 + 800x480 是错的：800x480 这块屏根本没有，
# 而 1280x720 的 RGBP888 一路要 2.7MB，正是 chn2 "failed(3)" 的常见原因。
RGB888P_SIZE = (640, 480)
DISPLAY_SIZE = (640, 480)
DISPLAY_MODE = "lcd"             # 亚博的屏是 ST7701，用 "lcd"；没有屏就用 "virt"
ENABLE_DISPLAY = True            # False -> 自动改用 "virt"（不点屏，但仍有画面数据）

# ---- 卡死自救 ----
# pl.create() 卡住时是 C 层死等，Python 的 try/except 抓不住，日志就停在
# "vb common pool count 6" 后面一片空白。所以光有"失败就换模式"的循环没用 ——
# 循环根本没机会执行。这里做两件事：
#   1) 开跑前先把「这次用的是哪个模式」写进文件。卡死重启后读回来，
#      自动跳过那个模式（手动重插 USB 也能自动恢复，不用改代码）。
#   2) 另起一个线程盯着 create()，超时就直接重启板子 —— 不用你去拔线。
PIPELINE_TIMEOUT_MS = 45000      # create() 超过这么久没返回就判定卡死
BOOT_STATE_FILE = "/sdcard/.k230_boot"   # 记录卡住次数和模式，成功启动就删掉
ENABLE_BOOT_WATCHDOG = True      # 卡死自动重启；想手动控制就设 False

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

def _class_ids_from_labels(labels, default_cat=15, default_dog=16):
    """从 LABELS 里自动找出 cat / dog 的下标。

    为什么必须自动找，而不是硬写 15/16：
        COCO 80 类模型     -> LABELS 是那 80 个名字，cat=15, dog=16
        自己训的两类模型   -> LABELS = ["cat", "dog"]，cat=0, dog=1

    如果换了 2 类模型却还按 15/16 过滤（KEEP_CLASSES），**所有检测结果都会被扔掉**，
    表现就是"模型明明加载了、屏幕上却一个框都没有"。
    这是最容易踩、也最难查的坑，所以这里改成按名字反查。
    """
    cat = dog = -1
    for i, name in enumerate(labels):
        n = str(name).strip().lower()
        if n == "cat" and cat < 0:
            cat = i
        elif n == "dog" and dog < 0:
            dog = i
    return (cat if cat >= 0 else default_cat,
            dog if dog >= 0 else default_dog)


# COCO 模型：cat=15, dog=16；换自定义两类模型会自动变成 0/1，不用手改
#
# 如果你想直接写死序号（比如你确认了就是 14/15），把下一行改成 (14, 15)。
# None = 按 LABELS 里的名字自动查（推荐：换模型、换类别顺序都不会错）。
CLASS_IDS_OVERRIDE = None
if CLASS_IDS_OVERRIDE:
    CLASS_CAT, CLASS_DOG = int(CLASS_IDS_OVERRIDE[0]), int(CLASS_IDS_OVERRIDE[1])
else:
    CLASS_CAT, CLASS_DOG = _class_ids_from_labels(LABELS)
KEEP_CLASSES = tuple(c for c in (CLASS_CAT, CLASS_DOG) if 0 <= c < len(LABELS))

CONF_THRESH = 0.40

# ---- 两个门槛，别混在一起 ----
# MODEL_CONF_THRESH: 传给 libs.YOLO，决定**模型后处理输出多少候选框**。
#   故意设得比开饭门槛低，这样即使分数不够，也能在心跳日志的"看到:"里
#   看见模型到底看到了什么。如果这个也设成 0.40，低分候选会被后处理直接扔掉，
#   日志里永远一片空白 —— 到底是"没目标"还是"分数差一点"就分不清了。
MODEL_CONF_THRESH = 0.20
NMS_THRESH = 0.45

# 模型输入尺寸 —— 必须和 kmodel 训练时的一致。
# 文件名里的 320 就是这个意思：yolov8n_320.kmodel -> (320, 320)
# 填错了要么模型直接报错，要么框整体错位。
MODEL_INPUT_SIZE = (320, 320)

# 预处理方式。AI 帧是 4:3、模型输入是 1:1，这一步决定会不会把猫压扁：
#   "padding" = 加黑边保持比例（推荐，识别率最高）
#   "fit"     = 直接拉伸（老固件默认值，会让远处的猫掉到阈值以下）
#   "crop"    = 裁掉两边（目标贴边时会被切掉）
RESIZE_MODE = "padding"

# 触发区：检测框**中心**落在这里才开饭。默认全画面。
# 强烈建议缩到饭盆/门口那一块，不然远处路过的猫也开饭。
# ⚠️ 单位是 RGB888P_SIZE 的**像素**，改了分辨率这里要跟着改。
TRIGGER_ZONE = (0, 0, 640, 480)

# ---------------- 上传（可以整段关掉） ----------------
SERVE_UPLOAD = True
UPLOAD_MODE = "wifi"             # "wifi" / "off"
WIFI_SSID = "666666"
WIFI_PASS = "12345678"
WIFI_TIMEOUT_MS = 15000

# 服务端地址。写成完整 URL，局域网和云端用同一套代码：
#   局域网 : SERVER_URL = "http://192.168.1.100:8000"
#   云端   : SERVER_URL = "https://catdog.example.com"
#   只写 IP:端口也认（默认按 http 处理）："192.168.1.100:8000"
#
# ⚠️ 这里填的是 K230 **主动连出去**的地址。K230 在你家 NAT 后面没关系，
#    不需要在路由器上做端口映射 —— 回家的方向是 K230 发起的。
#
# 手机热点（SSID=666666）下的写法：电脑在 192.168.43.154 上跑 pc/run.py，
# K230 自己拿到 192.168.43.170。两边前三段一样才能通。
# ⚠️ 这两个地址都是 DHCP 分发的，重连热点后可能变。两个办法：
#    ① 手机热点设置里给设备固定 IP / 路由器做 DHCP 静态绑定（一次搞定）
#    ② 变了就改这一行 —— 看 pc/run.py 启动时打印的「局域网」那行抄过来
#    想确认当前该填什么：在 Windows 上跑 ipconfig，看 WLAN 的 IPv4 地址
SERVER_URL = "http://192.168.43.154:8000"

# 云端必须开鉴权。服务端 pc/config.py 里的 CATDOG_TOKEN 要和这里一致，
# 否则上传会被 403 拒掉（照片只能存 SD 卡，网页上看不到）。
# 只允许字母数字和 - _ . ，因为 K230 上没有 urlencode 可用。
API_TOKEN = ""                   # 空 = 不鉴权（只在纯局域网内可以这样）

# HTTPS 要不要校验服务器证书。
# CanMV 固件通常没有 CA 证书包，设 True 会连不上 —— 所以默认 False。
# 代价：不校验的话没法防中间人。真要安全就把证书放到 /sdcard 再改这里。
SSL_VERIFY = False

# 等回应的秒数。2s 只够局域网；走公网一次往返加传图至少要 5~8s，
# 尤其家里上行带宽小的时候。宁可等久一点，也别把照片白丢到 SD 卡。
#
# 为什么从 8s 提到 20s：服务端收到图之后还要做**特征提取 + 个体匹配**，
# 这段时间客户端是在干等的。实测本地响应能超过 8 秒，于是板子以为失败、
# 立刻重传 —— 同一张照片在库里被存 3 份，主循环也被堵住（fps 掉到 0）。
UPLOAD_TIMEOUT_S = 20

# 失败重试次数（公网上偶发失败很常见，重试一次往往就过了）
UPLOAD_RETRIES = 2

# 网络 IO 放到后台线程。
# 为什么必须这样：以前 send() 在主循环里同步收发包，一次失败要等 3×20s=60s，
# 期间完全不检测、不喂饭 —— 日志里 fps 从 15 掉到 1.0 再到 0.0 就是这个原因。
# 检测和放饭是主线，上传是支线，支线不许拖住主线。
UPLOAD_IN_BACKGROUND = True
UPLOAD_QUEUE_MAX = 4             # 待传队列上限，防止内存被照片堆满
                                 # 4 张 ≈ 400KB（q50 整帧约 60~100KB），
                                 # 够吸收"动物来回走了几次"的突发。

JPEG_QUALITY = 82

# 单张照片的大小上限。超了就自动降质量重压一次。
# 为什么要有：640x480 的实拍图在 q82 下可能到 300KB+，SDIO WiFi 传一张要好几秒，
# 期间主循环的 fps 会明显掉（日志里能看到 15fps 掉到 11fps），也更容易被服务端
# 或中间设备掐断。降到 ~100KB 视觉上几乎看不出差别，稳定得多。
UPLOAD_MAX_BYTES = 110 * 1024
# 降级顺序：从高到低试，够小就停。保留尽可能多的细节。
JPEG_QUALITY_FALLBACKS = (65, 50, 40)

# 上传时拍什么：
#   True  = 传**整张**画面（推荐）。不依赖框坐标准不准，照片一定是完整的，
#           一眼就能看出猫在哪。代价只是文件大一点，局域网根本无所谓。
#   False = 只裁出检测框那一块（老行为）。框一旦偏了，照片就偏了。
UPLOAD_FULL_FRAME = True

CROP_PAD = 0.15                  # 裁剪时四周各外扩 15%，让整只动物进画面
MIN_BOX = 80                     # 太小的框不传（只在 UPLOAD_FULL_FRAME=False 时生效）
DEVICE_ID = "k230-01"
SAVE_LOCAL_DIR = "/sdcard/captures"   # 上传失败时存这里，别丢照片
SAVE_LOCAL_MAX = 200

# 开机标记：让重启后的 uid 不会和上一轮撞车。
# 用启动后的 ticks 值（每轮都不同），再混一点 time.time() 的秒数。
# 为什么需要：uid 里有"第几次到访"，重启后又从 1 开始数，
# 不加这个的话两轮的 "第 1 次到访" 会得到同一个 uid，被服务端当成重传。
try:
    _BOOT_TAG = (time.ticks_ms() % 100000) * 7 + int(
        (time.time() if time.time() else 0)) % 1000
except Exception:
    _BOOT_TAG = 0

# 补传：断网期间存下的照片，联网后自动补上去。
# 走公网时断网很常见，没有这个那些照片永远进不了统计。
BACKFILL_PERIOD_MS = 20000       # 每 20 秒看一次
BACKFILL_BATCH = 2               # 一次最多补传几张（别抢放饭的时间）

# 心跳日志间隔：每这么多毫秒打一行"fps / 框数 / 是否确认"。
# 没猫的时候也要有输出，否则出问题时分不清是"卡死"还是"真的没东西"。
HEARTBEAT_MS = 5000

# 启动后前多少帧打印分段耗时（run 用多久、postprocess 用多久）。
# 出现"卡住"时，这几行能直接说明是 KPU 慢、后处理慢、还是后处理死循环。
# 一切正常后设 0 关掉。
DET_STEP_LOG = 3

# 启动时把板子上 libs/YOLO.py 里 run/postprocess/draw_result 的实现打印出来。
# 只读文件、不执行库代码，不会拖慢也不会卡。排查"识别不到"时这段最有用，
# 跑通以后可以设 False 让日志干净点。
#
# 现在识别已经通了，所以关掉：这段会刷几百行日志，串口打印本身就是耗时的，
# 开机也白等好几秒。以后换模型/换固件再排查时改回 True。
DUMP_LIB_SOURCE = False

# 现场取证：每隔这么久把**模型看到的那一帧**存成 jpg 到 SD 卡。
# 排查"永远 0 个框"时这是最快的办法 —— 照片是黑的、对着墙、或者根本没有
# 猫在画面里，一眼就知道问题不在模型。
# 设 0 关掉（正常使用建议关掉，省 SD 卡寿命和内存）。
#
# 现在识别已经通了，所以默认关掉。它写一次是 260KB + 一次整帧压缩，
# 现场日志里 "已存 debug_view.jpg" 紧跟着 "0.0 fps" 就是这么来的。
# 以后要再排查，把它改回 15000 即可。
DEBUG_DUMP_MS = 0
DEBUG_DUMP_PATH = "/sdcard/debug_view.jpg"

# ---------------- 拍照方式 ----------------
# True：额外开一路 chn1（打包 RGB888）专门拍照。
#       为什么：AI 用的 chn2 是**平面格式**（官方文档：get_frame 返回
#       Sensor.RGBP888），把它当打包 RGB888 去压缩，存出来就是**一片灰**。
#       多开一路打包格式的通道，拍照和推理互不干扰，这是社区验证过的解法。
#
#       代价是多占一份缓冲区（640x480 RGB888 ≈ 900KB）。如果板子在这个
#       配置下起不来、或者串口出现内存不足，就把它改成 False —— 会自动
#       退回"把平面帧转成图片"那条带像素校验的路，功能不受影响。
SNAP_CHANNEL = True

VERBOSE = True


def log(msg):
    if VERBOSE:
        print(msg)


# ============================================================
# 触发引脚
# ============================================================
class Trigger(object):
    """把一根 GPIO 当"开饭"信号线用。

    不同 CanMV 固件对 GPIO 的处理不一样：
      新固件：Pin(32, Pin.OUT) 直接能用
      老固件：要先 fpioa.set_function(42, FPIOA.GPIO42) 做 IOMUX 复用
    所以两条路都试一遍，哪条成都行。
    """

    def __init__(self, num, busy_num=-1):
        from machine import Pin
        self.Pin = Pin
        self.num = num
        self.busy_num = busy_num
        self._print_pin_info(num)
        self.pin = self._make_out(num)
        self.busy_obj = None          # 注意：别叫 self.busy，会盖掉 is_busy() 方法
        if busy_num is not None and busy_num >= 0:
            self.busy_obj = self._make_in(busy_num)
        self.low()
        self._level = 0 if TRIG_ACTIVE_HIGH else 1   # 上电是"没动物"

    @property
    def has_busy(self):
        return self.busy_obj is not None

    def _print_pin_info(self, num):
        """打印这个脚现在的真实复用状态，用来核对"它到底是不是空的"。

        fpioa.help(N) 会输出 cur func（当前功能）和 can be func（可复用清单）。
        如果 cur func 不是 GPIO，说明它正被别的外设占着（I2S/WiFi/PDM...），
        这时候硬改成 GPIO 要么失败，要么把别的功能弄坏。
        """
        if not PRINT_PIN_INFO:
            return
        try:
            from machine import FPIOA
            if FPIOA is None:            # 模拟器里没有 FPIOA
                return
            fpioa = FPIOA()
            try:
                cur = fpioa.get_pin_func(num)
                log("[trig] GPIO%d 当前功能 = %s" % (num, cur))
            except Exception:
                pass
            try:
                fpioa.help(num)          # 直接打到串口终端
            except Exception:
                pass
        except Exception as e:
            log("[trig] 读引脚状态失败（不影响运行）: %s" % e)

    def _make_out(self, num):
        err = None
        try:
            from machine import FPIOA
            fpioa = FPIOA()
            fn = getattr(FPIOA, "GPIO%d" % num, None)
            if fn is not None:
                fpioa.set_function(num, fn)
                log("[trig] FPIOA 复用 GPIO%d 成功" % num)
        except Exception as e:
            err = e
        try:
            p = self.Pin(num, self.Pin.OUT)
            log("[trig] GPIO%d 配置为输出" % num)
            return p
        except Exception as e:
            log("[trig] !! GPIO%d 配置失败: %s" % (num, e if err is None else err))
            raise

    def _make_in(self, num):
        try:
            from machine import FPIOA
            fpioa = FPIOA()
            fn = getattr(FPIOA, "GPIO%d" % num, None)
            if fn is not None:
                fpioa.set_function(num, fn)
        except Exception:
            pass
        try:
            return self.Pin(num, self.Pin.IN, pull=self.Pin.PULL_DOWN)
        except Exception:
            return self.Pin(num, self.Pin.IN)

    def high(self):
        try:
            self.pin.value(1)
        except Exception:
            try:
                self.pin.high()
            except Exception:
                self.pin.on()

    def low(self):
        try:
            self.pin.value(0)
        except Exception:
            try:
                self.pin.low()
            except Exception:
                self.pin.off()

    def is_busy(self):
        """True = Arduino 正在跑一轮"""
        if self.busy_obj is None:
            return False
        try:
            return bool(self.busy_obj.value())
        except Exception:
            return False

    def set_level(self, on):
        """输出"有动物/没动物"的电平。只在状态真的变了才写引脚。

        为什么要判断是否变化：主循环每帧都会调，一直重复写同一个值虽然不致命，
        但会在串口刷满日志，也会掩盖真正的那次跳变。
        """
        want = 1 if on else 0
        if not TRIG_ACTIVE_HIGH:
            want = 1 - want
        if getattr(self, "_level", None) == want:
            return
        self._level = want
        if want:
            self.high()
            log("[trig] GPIO%d -> 高（检测到动物）" % self.num)
        else:
            self.low()
            log("[trig] GPIO%d -> 低（动物离开）" % self.num)

    def force_low(self):
        """不管当前状态，强制拉低。退出时用，别把信号线留在高电平。"""
        self._level = 1 if not TRIG_ACTIVE_HIGH else 0
        self.low()

    def wait_idle(self, timeout_ms=None):
        """等 Arduino 跑完（只在接了 BUSY 线时有用）"""
        if self.busy_obj is None:
            return True
        if timeout_ms is None:
            timeout_ms = BUSY_WAIT_TIMEOUT_MS
        t0 = time.ticks_ms()
        while self.is_busy():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                log("[trig] !! 等 BUSY 超时，继续往下走")
                return False
            time.sleep_ms(100)
        return True


# ============================================================
# 检测
# ============================================================
class SimDetector(object):
    """**只在 SIMULATE_DETECTION = True 时**才会被用到。

    它返回的框是编出来的，不是真的识别结果 —— 千万别拿它当"识别"用，
    它只是用来把机械/上传/状态机跑通。
    """

    def __init__(self):
        self.t0 = time.ticks_ms()

    def detect(self, img):
        el = time.ticks_diff(time.ticks_ms(), self.t0) / 1000.0
        if el % 40.0 > 28.0:
            return []                     # 后 12s 离开，用来测"走了再来"
        W, H = RGB888P_SIZE
        return [[int(W * 0.32), int(H * 0.40), int(W * 0.22), int(H * 0.42),
                 CLASS_CAT, 0.88]]


class NullDetector(object):
    """模型没加载成功时的替代品：**什么都不返回**。

    以前这里是静默退回 SimDetector —— 那等于"假装识别到猫"，会按编造的框
    反复开饭、还把假照片传上网。断粮风险比"不工作"大得多，所以改成彻底不出结果。
    """

    def __init__(self, reason=""):
        self.reason = reason

    def detect(self, img):
        return []

    def deinit(self):
        pass


def _load_yolo_cls(kmodel_path, names=("YOLO11", "YOLOv8", "YOLOv5")):
    """按模型文件名挑 libs.YOLO 里对应的类。

    三个类的前处理一模一样，**后处理不同**（各自调不同的 aidemo 函数）。
    所以用错类不会报错，只会"永远检不到东西"，必须按模型名对号入座：
        yolov8n_320.kmodel  -> YOLOv8
        yolo11n_320.kmodel  -> YOLO11
        yolov5n.kmodel      -> YOLOv5
    """
    hint = str(kmodel_path or "").lower()
    order = list(names)
    if "yolov8" in hint:
        order = ["YOLOv8"] + [n for n in order if n != "YOLOv8"]
    elif "yolo11" in hint or "yolov11" in hint:
        order = ["YOLO11"] + [n for n in order if n != "YOLO11"]
    elif "yolov5" in hint:
        order = ["YOLOv5"] + [n for n in order if n != "YOLOv5"]

    for name in order:
        try:
            mod = __import__("libs.YOLO", None, None, [name])
            cls = getattr(mod, name, None)
            if cls is not None:
                log("[det] 使用 libs.YOLO.%s（按模型名 %s 匹配）"
                    % (name, kmodel_path.rsplit("/", 1)[-1]))
                return cls
        except Exception:
            continue
    return None


def _describe_result(obj, depth=0):
    """人话描述 postprocess 的返回值，用于诊断日志。"""
    try:
        n = len(obj)
    except Exception:
        return "%s(%r)" % (type(obj).__name__, obj)
    if depth >= 1:
        return "list(%d)" % n
    if n and n <= 8:
        parts = []
        for i in range(n):
            try:
                parts.append(_describe_result(obj[i], depth + 1))
            except Exception:
                parts.append("?")
        return "[%s]" % ", ".join(parts)
    return "%s(%d)" % (type(obj).__name__, n)


def _flatten_detections(dets):
    """把 postprocess 的返回值摊平成 [[x, y, w, h, cid, conf], ...]。

    为什么要单独一个函数：**这个库的返回结构不是"每框一个 6 元组"**。
    v1.4.3 的 libs/YOLO.py 第 74 行起是这样的：

        det_res = aidemo.yolov5_det_postprocess(results[0][0], ...)
        return det_res

    而同一个文件第 97 行的 draw_result 自己这么用它：

        for i in range(len(res[0])):
            x, y, w, h = res[0][i]          # res[0] = 框
            ... self.labels[res[1][i]] ...   # res[1] = 类别序号
            ... "".format(res[2][i]) ...     # res[2] = 置信度

    也就是说返回的是 **(框列表, 序号列表, 分数列表)** 这个三元组，
    不是一框一行。原来按 r[4] / r[5] 去读，等于在框列表里取第 5 个框，
    再 float() 一个元组 —— 必然抛异常，然后被 except 吞掉，
    表现就是"模型在跑、fps 正常、框永远是 0"。

    同时兼容"确实是一框一行"的固件（自己训的模型/新版本可能改成这样），
    两种结构都认，不用改代码。

    认不出来时返回 None（调用方会打印结构，方便下次对齐）。
    """
    if not dets:
        return []

    # ---- 情况 A：三元组 (boxes, ids, confs) ----
    # 判据：长度是 3，且前两个都能当列表用。
    try:
        if len(dets) == 3:
            boxes, ids, confs = dets[0], dets[1], dets[2]
            nb = len(boxes)
            # 三条平行列表全空 = 这一帧没有目标，正常情况
            if nb == 0 and len(ids) == 0 and len(confs) == 0:
                return []
            # ids / confs 的数量必须和框一致，否则不是这个结构
            if nb and len(ids) == nb and len(confs) == nb:
                # 再确认框本身是 4 个数（x, y, w, h）
                probe = boxes[0]
                if len(probe) == 4:
                    rows = []
                    for i in range(nb):
                        b = boxes[i]
                        rows.append([float(b[0]), float(b[1]),
                                     float(b[2]), float(b[3]),
                                     int(ids[i]), float(confs[i])])
                    return rows
    except Exception:
        pass

    # ---- 情况 B：一框一行 [x, y, w, h, cid, conf] ----
    try:
        rows = []
        for r in dets:
            if len(r) < 6:
                rows = None
                break
            rows.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]),
                         int(r[4]), float(r[5])])
        if rows is not None:
            return rows
    except Exception:
        pass

    return None


def _rows_look_sane(rows):
    """至少有一行像**真的**检测结果，才敢认为 run() 直接给的是框。

    为什么光看长度不够：
        KPU 输出没写过的内存按 float32 读就是 2.8e38，长度也可能正好是 6，
        只验形状会被骗过去 —— 之前 conf=2.80e+38 就是这么来的。
        所以再验一遍数值：置信度必须在 (0,1]、序号在合理范围、宽高为正。
    """
    for r in rows:
        try:
            x, y, w, h, cid, conf = r
        except Exception:
            continue
        if 0 < conf <= 1 and 0 <= cid <= 1000 and w > 0 and h > 0 \
                and abs(x) < 100000 and abs(y) < 100000:
            return True
    return False


class YoloDetector(object):

    def __init__(self):
        # 先定模型，再按模型名字挑对应的类 —— 顺序不能反。
        # 为什么：libs.YOLO 里 YOLO11 / YOLOv8 / YOLOv5 三个类的前处理一样，
        # 但**后处理走的 aidemo 函数不同**（yolov11_det / yolov8_det / yolov5_det）。
        # 拿 YOLO11 的类去解 yolov8n 的输出，可能一个框都出不来还完全不报错，
        # 表现就是"模型加载成功但永远识别不到" —— 极难查。
        self.kmodel_path = _resolve_kmodel_path()
        cls = _load_yolo_cls(self.kmodel_path)
        if cls is None:
            raise OSError("libs.YOLO 不可用")

        self.cls = cls
        self.model = self._build(cls)
        self._configure_preprocess()
        self.seen = {}                 # 诊断用：近 5 秒看到的类别 -> 最高置信度
        self.seen_total = {}           # 累积版本，给心跳日志用（不清空）
        self.filtered_log_ms = 0
        self.results = None            # 最近一次 run() 的返回值（交给 postprocess）
        self._warned_pp = False        # 推理/后处理失败只报一次，别刷屏
        self._step_left = DET_STEP_LOG # 前几帧打印分段耗时，定位卡点
        self._verbose = False          # 当前帧是否打分段日志
        self._last_hit_log = 0         # 命中日志限频用
        self._warned_scale = False     # 坐标缩放提示只打一次

        # ---- 把序号映射喊出来，这是最该确认的一件事 ----
        # 序号来自 LABELS 里按名字查，所以换模型（哪怕类别顺序不同）也不用改。
        log("[det] 模型加载完成: %s" % self.kmodel_path)
        log("[det] 猫狗序号: cat=%d dog=%d  (LABELS 共 %d 类，按名字查出)"
            % (CLASS_CAT, CLASS_DOG, len(LABELS)))
        if (CLASS_CAT, CLASS_DOG) == (14, 15):
            log("[det]   注意：是 14/15 而不是常见的 15/16 —— LABELS 顺序"
                "和标准 COCO 差 1，已按实际值用，不用手改")

        # 把库的 run/postprocess/draw_result 源码打出来（只读文件，不会卡）。
        # 一旦出现"永远 0 个框"或"调 postprocess 就卡死"，答案就在这段里。
        if DUMP_LIB_SOURCE:
            _dump_lib_source()

    def _build(self, cls):
        """构造模型。不同固件版本的参数名不一样，逐级降级试。"""
        base = dict(kmodel_path=self.kmodel_path, labels=LABELS,
                    rgb888p_size=list(RGB888P_SIZE),
                    display_size=list(DISPLAY_SIZE),
                    conf_thresh=MODEL_CONF_THRESH, nms_thresh=NMS_THRESH,
                    debug_mode=0)
        # 模型输入尺寸：写成显式参数，别让库去猜。
        # kmodel 的训练输入和这里不一致会直接报错或框错位。
        attempts = [
            dict(model_input_size=list(MODEL_INPUT_SIZE), **base),
            dict(base),
        ]
        last = None
        for kw in attempts:
            try:
                return cls(task_type="detect", mode="video", **kw)
            except TypeError as e:
                last = e
            except Exception as e:
                # 不是参数问题（比如 kmodel 打不开），直接抛出去，别掩盖
                raise
        # 老固件连 task_type 都不认
        return cls(mode="video", **base)

    def _configure_preprocess(self):
        """配置预处理。

        为什么宽高比会直接影响"识别准不准"：
            AI 帧是 640x480（4:3），模型输入是 320x320（1:1）。
            如果直接拉伸，猫会被横向压扁 1.33 倍 —— 模型没在压扁的数据上训练过，
            置信度整体下降，远一点的猫直接掉到阈值以下。

        实测（CanMV v1.4.3 k230_canmv_yahboom 的 libs/YOLO.py）：
            YOLOv8.config_preprocess 里 detect 分支本身就调
            letterbox_pad_param + ai2d.pad，**默认就是加灰边保比例**。
            它只接受 input_image_size 一个参数，**没有** resize_mode。
        所以下面这个 resize_mode 尝试纯属兼容更老的固件，不生效也没关系 ——
        默认路径已经是正确的 letterbox，不会把猫压扁。
        """
        for kw in ({"resize_mode": RESIZE_MODE}, {}):
            try:
                self.model.config_preprocess(**kw)
                if kw:
                    log("[det] 预处理 resize_mode=%s（保持 4:3 不变形）"
                        % RESIZE_MODE)
                return
            except TypeError:
                continue
            except Exception as e:
                log("[det] config_preprocess 异常（继续跑）: %s" % e)
                return
        log("[det] 这个固件的 config_preprocess 不认 resize_mode —— 不用管，"
            "本固件的 detect 分支自带 letterbox（加灰边保比例），本来就不会拉变形")

    def detect(self, img):
        """返回 [[x, y, w, h, cid, conf], ...]，坐标是输入帧的像素。

        只用官方那两步，绝不碰张量：
            run(img)        -> 模型内部处理
            postprocess(res)-> 库自己把结果变成框
        张量是库的内部实现，这里一概不看、不猜、不自己解码。

        这就是亚博/CanMV 例程的写法，也是唯一可靠的做法：
            res  = model.run(img)
            dets = model.postprocess(res)
        """
        out = []
        t0 = time.ticks_ms()
        # 前 DET_STEP_LOG 帧打开分段日志。用实例变量传给 _postprocess，
        # 这样它内部重试时也能按同一节奏打印，不会前后不一致。
        self._verbose = self._step_left > 0
        if self._verbose:
            self._step_left -= 1

        try:
            self.results = self.model.run(img)
        except Exception as e:
            if not self._warned_pp:
                self._warned_pp = True
                log("[det] !! run() 失败: %s" % e)
            return out
        if self.results is None:
            return out

        # ---- 分段计时/进度：卡住的时候靠这两行定位 ----
        # 为什么必须分段：
        #   run() 是 KPU 上的固定开销（几十毫秒），postprocess() 是 CPU 上的
        #   Python+C 混合代码。只有分开报，才知道是谁把主循环堵住了。
        #   之前"卡住"的日志停在帧类型那一行，是因为整段 detect 没有任何中间输出。
        if self._verbose:
            log("[det] run() 完成 %d ms"
                % time.ticks_diff(time.ticks_ms(), t0))

        # ---- run() 给的到底是框，还是原始张量？ ----
        # 两种官方写法都存在，必须都支持：
        #   ① run() 内部已经后处理过，直接返回 (框, 序号, 分数)
        #        例程写法： res = model.run(img); model.draw_result(res, osd)
        #   ② run() 只给 KPU 原始输出，必须自己再调 postprocess
        #        例程写法： res = model.run(img); out = model.postprocess(res)
        # 判据不看类型、看**能不能真的解出框**：解得出就是 ①，解不出就走 ②。
        # 张量解出来的东西会在这里被判掉（框不是 4 个数、或者长度不够 6），
        # 所以不会把张量误当成框 —— 这正是之前 conf=1e38 的坑。
        rows = _flatten_detections(self.results)
        # 空的三元组（画面里没目标）是合法的；有内容就必须经得起数值校验，
        # 否则当它是张量/垃圾，退回 postprocess。
        if rows is not None and (not rows or _rows_look_sane(rows)):
            if not getattr(self, "_noted_direct", False):
                self._noted_direct = True
                log("[det] run() 已直接返回检测结果，跳过 postprocess")
        else:
            if self._verbose:
                log("[det] postprocess() 开始（日志若停在这里，就是它卡住）")
            try:
                dets = self._postprocess(self.results)
            except Exception as e:
                if not self._warned_pp:
                    self._warned_pp = True
                    log("[det] !! postprocess 失败: %s" % e)
                return out

            if self._verbose:
                log("[det] postprocess() 完成 %d ms，得到 "
                    "%s" % (time.ticks_diff(time.ticks_ms(), t0),
                            _describe_result(dets)))

            if not dets:
                return out

            # ---- 把 postprocess 的返回值摊平成 [x, y, w, h, cid, conf] ----
            rows = _flatten_detections(dets)
            if rows is None:
                if not self._warned_pp:
                    self._warned_pp = True
                    log("[det] !! 认不出 postprocess 的返回结构，"
                        "共有 %d 个顶层元素" % len(dets))
                    for i in range(min(3, len(dets))):
                        log("[det]    res[%d] = %s"
                            % (i, _describe_result(dets[i])))
                    log("      把这个发出来就能对上。")
                return out

        if not rows:
            return out

        # ---- 坐标空间对齐 ----
        # 库的 postprocess 在 mode="video" 时是按 display_size 输出的
        # （源码里传的就是 [display_size[1], display_size[0]]），
        # 而我们后面的触发区和上传都按 RGB888P_SIZE 算。
        # 两者不一致时框会整体错位，所以这里统一缩放到 RGB888P 空间。
        sx = sy = 1.0
        try:
            dw, dh = self.model.display_size[0], self.model.display_size[1]
            if dw and dh and (dw, dh) != RGB888P_SIZE:
                sx = float(RGB888P_SIZE[0]) / float(dw)
                sy = float(RGB888P_SIZE[1]) / float(dh)
                if not getattr(self, "_warned_scale", False):
                    self._warned_scale = True
                    log("[det] 坐标从 display %dx%d 缩放到 AI帧 %dx%d"
                        % (dw, dh, RGB888P_SIZE[0], RGB888P_SIZE[1]))
        except Exception:
            pass

        for r in rows:
            try:
                x, y, w, h, cid, conf = r
                if sx != 1.0 or sy != 1.0:
                    x *= sx
                    y *= sy
                    w *= sx
                    h *= sy
                # 只要百分数在 0~1 之间才认。个别固件会把坏值吐出来，
                # 这个过滤很便宜，能挡住把乱码框画到屏幕上。
                if conf <= 0 or conf > 1:
                    continue
                if cid < 0 or cid > 1000:
                    continue
                # 记录看到的类别（诊断"明明有目标却识别不到猫狗"用）
                if len(self.seen) < 32:
                    if conf > self.seen.get(cid, 0.0):
                        self.seen[cid] = conf
                    if conf > self.seen_total.get(cid, 0.0):
                        self.seen_total[cid] = conf
                if cid not in KEEP_CLASSES:
                    continue
                out.append([x, y, w, h, cid, conf])
                # 每次真的认出猫狗就把序号喊出来。
                # 这就是你要的那一步：看到序号就能验证对不对。
                # 限频是为了不刷屏（15fps 下同一只猫会连续命中很多帧）。
                if self._last_hit_log == 0 or \
                        time.ticks_diff(time.ticks_ms(),
                                        self._last_hit_log) >= 4000:
                    self._last_hit_log = time.ticks_ms()
                    log("[det] 命中 序号=%d(%s) conf=%.2f 框=%d,%d %dx%d"
                        % (cid,
                           LABELS[cid] if 0 <= cid < len(LABELS) else "?",
                           conf, int(x), int(y), int(w), int(h)))
            except Exception:
                continue
        self._maybe_explain_filtered()
        return out

    def _postprocess(self, results):
        """调库自己的 postprocess。不同固件签名略有差异，逐个试。

        ⚠️ 这里刻意**不做** `fn()` 这种无参调用。
            有些版本的无参签名会去读 self.results 重新跑一遍后处理；一旦参数
            对不上，它可能在里面死循环 —— 表现就是整个程序再无输出，必须重插 USB。
            宁可报"签名不匹配 + 怎么改"，也不要一个会静默卡死的兜底。
        """
        fn = getattr(self.model, "postprocess", None)
        if fn is None:
            if not self._warned_pp:
                self._warned_pp = True
                log("[det] !! 模型没有 postprocess()，这个固件的 libs.YOLO 不匹配")
                log("     需要亚博出厂固件，或按 train/README.md 换匹配的 kmodel")
            return None

        attempts = ((results,), (results, NMS_THRESH))
        last = None
        for i, args in enumerate(attempts):
            if self._verbose and i > 0:
                log("[det] postprocess 换参数重试: %d 个参数" % len(args))
            try:
                return fn(*args)
            except TypeError as e:
                last = e                      # 参数个数不对，换下一种
                continue
            except Exception as e:
                last = e
                break

        if not self._warned_pp:
            self._warned_pp = True
            log("[det] !! postprocess 调用失败: %s" % last)
            log("     这是【识别不到任何东西】的唯一原因。")
            log("     启动日志里的 [lib] 段已打印了这个库的真实源码，")
            log("     把那段发出来，就能确定正确的调用方式。")
            log("     也可以单独跑 tools/yolo_inspect.py，它会逐个试签名。")
        return None

    def _maybe_explain_filtered(self):
        """看到了东西，但全被类别过滤掉了 —— 把看到的类别打出来。

        "能识别别的、就是不识别猫狗"是换模型后最常见的现象，原因通常是
        LABELS 和 kmodel 的类别顺序对不上。没有这行日志只能靠猜。
        """
        if not self.seen:
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self.filtered_log_ms) < 10000:
            return
        self.filtered_log_ms = now
        items = sorted(self.seen.items(), key=lambda kv: -kv[1])[:4]
        self.seen = {}
        parts = []
        for cid, conf in items:
            name = LABELS[cid] if 0 <= cid < len(LABELS) else "?%d" % cid
            parts.append("%s(%.2f)" % (name, conf))
        log("[det] 看到: %s   猫狗算数门槛 conf>=%.2f"
            % (", ".join(parts), PRESENCE_ON_CONF))

    def deinit(self):
        try:
            self.model.deinit()
        except Exception:
            pass


def _dump_one_source(tag, path_candidates, import_name, targets):
    """打印某个库文件里指定函数的完整实现。

    只读文件、不执行库代码，所以绝对不会卡。
    """
    path = None
    try:
        mod = __import__(import_name, None, None, ["__file__"]) \
            if "." not in import_name else None
        if mod is not None:
            path = getattr(mod, "__file__", None)
    except Exception:
        pass
    if not path:
        # 走 importlib 式的逐级导入（"libs.YOLO" 这种带点的）
        try:
            parts = import_name.split(".")
            mod = __import__(parts[0])
            for p in parts[1:]:
                mod = getattr(mod, p)
            path = getattr(mod, "__file__", None)
        except Exception:
            pass
    if not path:
        for p in path_candidates:
            try:
                os.stat(p)
                path = p
                break
            except Exception:
                continue
    if not path:
        log("[%s] 找不到源码（%s），跳过" % (tag, path_candidates[0]))
        return
    if not str(path).endswith(".py"):
        log("[%s] %s 是 .pyc（没有源码），跳过" % (tag, path))
        return

    try:
        f = open(path, "r")
        src = f.read()
        f.close()
    except Exception as e:
        log("[%s] 读不了 %s: %s" % (tag, path, e))
        return

    lines = src.split("\n")
    log("[%s] %s  共 %d 行" % (tag, path, len(lines)))

    for t in targets:
        for i, ln in enumerate(lines):
            if not ln.strip().startswith(t):
                continue
            base = len(ln) - len(ln.lstrip())
            log("[%s] ---- %s 开始 (L%d) ----" % (tag, ln.strip()[:60], i + 1))
            for j in range(i, min(i + 30, len(lines))):
                cur = lines[j]
                s = cur.strip()
                # 遇到同缩进的下一个 def/class 就停
                if j > i and s and (len(cur) - len(cur.lstrip())) <= base \
                        and (s.startswith("def ") or s.startswith("class ")):
                    break
                log("[%s]   %s" % (tag, cur.rstrip()))
            log("[%s] ---- %s 结束 ----" % (tag, t.replace("def ", "")))
            break


def _dump_lib_source():
    """把板子上两个关键库的实现打出来。

    为什么值得在启动时做：
        "模型加载成功、fps 正常、但一个框都没有"，"调一下 postprocess 就卡死"，
        "create() 成功但 get_frame() 永远 None" —— 这三种光看日志猜不出来。
        板子上的 libs/*.py 是明文 Python，直接打印出来，答案就在里面。

    YOLO.py   ：run / postprocess / draw_result —— 返回值到底是什么结构
    PipeLine.py：create / get_frame      —— chn2 怎么配的、为什么不出帧
    """
    _dump_one_source("lib", ("/sdcard/libs/YOLO.py",), "libs.YOLO",
                     ("def run", "def postprocess", "def draw_result"))
    _dump_one_source("lib", ("/sdcard/libs/PipeLine.py",), "libs.PipeLine",
                     ("def create", "def get_frame"))


def _resolve_kmodel_path():
    """挑一个**真的存在**的 kmodel 路径，并说明最后用了哪个。

    为什么要有这一步：
        路径写错（比如 /sdcard/models/ 在出厂卡上根本不存在）会直接让整个
        识别器起不来，而报错只给一句 "Kmodel file not exist."。
        厂商固件其实自带 yolov8n_320.kmodel，就躺在 /sdcard/kmodel/ 里 ——
        与其让用户自己去找，不如这里按已知位置挨个试一遍。
    """
    tries = [KMODEL_PATH]
    for p in KMODEL_FALLBACKS:
        if p not in tries:
            tries.append(p)

    for p in tries:
        try:
            if os.stat(p):
                if p != KMODEL_PATH:
                    log("[det] 配置的 %s 不存在，改用卡上现成的 %s"
                        % (KMODEL_PATH, p))
                    log("[det] （建议把 KMODEL_PATH 直接改成这个，省一次 stat）")
                return p
        except Exception:
            continue

    # 已知位置都没有，就扫一遍卡，挑名字最像的
    cands = [p for p, _sz in _find_kmodel_files() if "yolov8" in p.lower()]
    for p in cands:
        if "320" in p:
            log("[det] 自动搜到 %s，先用这个" % p)
            return p
    if cands:
        log("[det] 没找到 320 的，退而用 %s（输入尺寸可能不匹配！）" % cands[0])
        return cands[0]

    return KMODEL_PATH          # 啥都没有，原样返回，让后面报错带上原路径


def _find_kmodel_files(root="/sdcard", max_depth=3, max_files=20):
    """扫出卡上真正存在的 .kmodel，返回 [(path, size_bytes)]。

    为什么需要它：模型加载失败最常见的原因就是"文件不在那个路径上"，
    而固件只回一句 "Kmodel file not exist."，完全看不出卡上有什么。
    把头文件贴出来，改 KMODEL_PATH 就是抄一行的事。

    MicroPython 没有 os.walk，用 os.ilistdir 自己递归。
    ilistdir 每项是 (name, type[, inode, size])，type 里 0x4000 位是目录。
    任何一步失败都静默跳过 —— 这只是诊断，不能反过来把程序搞崩。
    """
    out = []

    def walk(d, depth):
        if depth > max_depth or len(out) >= max_files:
            return
        try:
            # MicroPython 的 ilistdir 是生成器：目录不存在/没权限时
            # OSError 在**迭代**时抛出，不在调用那一行。
            # 所以 for 必须一起放在 try 里，否则诊断代码自己会崩。
            for ent in os.ilistdir(d):
                try:
                    name = ent[0]
                    typ = int(ent[1])
                except Exception:
                    continue
                if name in (".", ".."):
                    continue
                p = d.rstrip("/") + "/" + name
                if typ & 0x4000:                      # 目录
                    walk(p, depth + 1)
                elif str(name).lower().endswith(".kmodel"):
                    size = 0
                    try:
                        size = int(os.stat(p)[6])
                    except Exception:
                        pass
                    out.append((p, size))
                    if len(out) >= max_files:
                        return
        except Exception:
            return

    walk(root, 0)
    out.sort()
    return out


def _explain_missing_model(err):
    """模型加载失败时，把卡上实际有的 kmodel 列出来。"""
    found = _find_kmodel_files()
    if found:
        log("!! 卡上实际存在的 .kmodel（%d 个）:" % len(found))
        for p, sz in found:
            log("!!   %s   (%.1f MB)" % (p, sz / 1048576.0))
        log("!! -> 把 KMODEL_PATH 改成上面真正存在的那个路径")
        # 文件名里带 640 / 320，而 MODEL_INPUT_SIZE 对不上，是最隐蔽的错配
        size_hint = "%dx%d" % MODEL_INPUT_SIZE
        for p, _sz in found:
            if ("320" in p) != ("320" in size_hint):
                log("!! ⚠️  %s 的尺寸可能和 MODEL_INPUT_SIZE=%s 不一致，"
                    "对不上会框错位/检不到" % (p, size_hint))
                break
    else:
        log("!! 卡上 /sdcard 里**一个 .kmodel 都没有** —— 必须先放一个上去")
        log("!!   自己训练转: train/README.md")
        log("!!   板子自带例程: 去 /sdcard/examples/ 里找 yolov8n.kmodel 之类")
        log("!!   弄好后用 tools/list_models.py 可以确认放对了没有")


def build_detector():
    if SIMULATE_DETECTION:
        log("[det] !! 模拟检测模式：返回的是编出来的框，不是真识别")
        return SimDetector()
    try:
        return YoloDetector()
    except Exception as e:
        log("")
        log("!" * 56)
        log("!! 模型加载失败，现在**不会识别任何东西**，也不会开饭。")
        log("!! 原因: %s" % e)
        log("!! 检查:")
        log("!!   1) KMODEL_PATH 指向的文件在不在（%s）" % KMODEL_PATH)
        log("!!   2) kmodel 和固件版本是否匹配（训练/转换见 train/README.md）")
        log("!!   3) 只想调机械流程再把 SIMULATE_DETECTION 改 True")
        if not SIMULATE_DETECTION:
            _explain_missing_model(e)
        log("!" * 56)
        log("")
        return NullDetector(str(e))


# ============================================================
# 服务端地址解析 / TLS / 读响应
# ============================================================
#
# 为什么要自己解析 URL：
#   K230 上没有 urllib.parse，而且我们需要知道 scheme 才能决定要不要套 TLS。
#   只写 "192.168.1.100:8000" 也认，默认按 http 处理。
#
# 关键点：**K230 永远是主动发起连接的一方**。
#   所以后台放在云端、K230 在家里的 NAT 后面，完全不需要在路由器上做端口映射 ——
#   出去的连接本来就能出去。这正是这套架构天然支持云端的原因。

def _is_private_host(host):
    """判断主机名是不是"局域网地址"。

    只看字符串形态，不做 DNS —— 自检要在开机瞬间完成，不能卡在网络请求上。
    认不出来的（域名、公网 IP）一律当公网，这样 API_TOKEN 的警告会偏严格，
    而严格那一侧是安全的方向。
    """
    h = str(host or "")
    if not h:
        return True                 # 空的当本地，别报警
    if h == "localhost":
        return True
    parts = h.split(".")
    if len(parts) != 4:
        return False                # 域名 -> 公网
    try:
        n = [int(p) for p in parts]
    except Exception:
        return False
    if n[0] == 10:
        return True
    if n[0] == 192 and n[1] == 168:
        return True
    if n[0] == 172 and 16 <= n[1] <= 31:
        return True
    if n[0] == 127:
        return True
    return False


def parse_server_url():
    """把 SERVER_URL 拆成 (scheme, host, port, base_path)。"""
    s = (SERVER_URL or "").strip()
    if "://" not in s:
        s = "http://" + s                       # 只写了 IP:端口

    scheme = s[:s.find("://")].lower()
    rest = s[s.find("://") + 3:]

    # 去掉路径部分（允许后台挂在 https://host/catdog 这样的子路径下）
    path = ""
    j = rest.find("/")
    if j >= 0:
        rest, path = rest[:j], rest[j:]

    if scheme == "https":
        default_port = 443
    else:
        default_port = 80
    port = default_port
    if ":" in rest:
        h, p = rest.rsplit(":", 1)
        try:
            port = int(p)
            rest = h
        except Exception:
            pass                                # 冒号后面不是数字，当整个是 host

    if path.endswith("/"):
        path = path[:-1]                        # 统一不带结尾斜杠
    return scheme, rest, port, path


def _tls_wrap(sock, host):
    """给 socket 套 TLS。旧固件用 ssl.wrap_socket，新固件用 SSLContext。

    返回 None 表示这个固件根本没有 ssl 模块 —— 那时只能用 http。
    """
    try:
        import ssl
    except Exception:
        return None

    try:
        if hasattr(ssl, "SSLContext"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if not SSL_VERIFY:
                try:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                except Exception:
                    pass
            return ctx.wrap_socket(sock, server_hostname=host)
    except Exception as e:
        log("[up] TLS(SSLContext) 失败: %s" % e)

    try:
        # 老接口：MicroPython 的 ssl.wrap_socket
        kw = {"server_hostname": host}
        if not SSL_VERIFY:
            kw["cert_reqs"] = 0
        return ssl.wrap_socket(sock, **kw)
    except Exception as e:
        log("[up] TLS 失败: %s" % e)
        return None


def _send_all(sock, data):
    """把 data 完整发出去。

    为什么不能直接 sock.send(data)：MicroPython 的 send() 返回"实际发出去的字节数"，
    不保证等于 len(data)。局域网小包通常一次就发完了，但走公网 / 图片几十 KB 时
    很可能只发了一部分就返回 —— 那服务端收到的 JPEG 就是残的，PIL 解不开，
    报一个看起来莫名其妙的 400。原代码就是这么写的，只是局域网没暴露出来。
    """
    n = len(data)
    if n == 0:
        return
    try:
        mv = memoryview(data)          # 避免反复切片拷贝（MCU 上很贵）
    except Exception:
        mv = data
    total = 0
    while total < n:
        sent = sock.send(mv[total:] if total else mv)
        if not sent:
            raise OSError("send 返回 0，连接已断开")
        total += sent


def _read_status(sock, limit=512, deadline_ms=None):
    """读响应头，返回状态码；读不到返回 0。

    之前只 recv(64) 一次就判断，碰到服务器分两段发（或者先发 100-continue）
    就会误判成失败。这里读到空行或者攒够上限为止。

    为什么写得这么啰嗦 —— 现场踩过的三个坑：

    1) MicroPython 的 socket 超时**不一定抛异常**，有的端口直接返回 None。
       老代码 `if not chunk: break` 把 None 当成"对端关了"，于是服务端刚
       存完图、响应还在路上，板子就判定失败并重传 —— 一张照片存 3 份。

    2) 有的端口超时抛 OSError(ETIMEDOUT)。那也是"还没到"，不是"断了"。

    3) **有的端口超时直接返回 b""** —— 和"对端真的关了"长得一模一样。
       这就是现场日志里"每次上传的第 1 次都失败，重试就成功"的原因：

           [up] 第 1 次失败(服务端没有任何响应, 等了 1173 ms)，重试
           [up] 上传成功 261 ms

       1173 ms ≈ 我们的 1 秒 recv 超时。服务端其实正在做特征提取，
       板子 1 秒没等到就把 b"" 当成"关了"，白白重传一次。

    怎么区分：**看这次 recv 花了多久**。真的 EOF 是立刻返回的（微秒级）；
    超时假装的 b"" 一定耗满 SHORT_S。于是：

        返回 b"" 且耗时 >= SHORT_S * 0.5  ->  当成超时，继续等
        返回 b"" 且很快就回来             ->  真的是 EOF，收工

    总时长由 deadline_ms 兜住，不会无限等。
    """
    SHORT_S = 1.0
    t0 = time.ticks_ms()
    buf = b""
    while len(buf) < limit:
        try:
            sock.settimeout(SHORT_S)
        except Exception:
            pass
        t_recv = time.ticks_ms()
        try:
            chunk = sock.recv(256)
        except Exception as e:
            # 超时类错误 = 数据还没到，接着等；其它（ECONNRESET 等）真断了
            code = getattr(e, "args", (None,))[0]
            if code in (110, 11, 119) or "timeout" in str(e).lower():
                chunk = None
            elif deadline_ms is None:
                break
            else:
                # 分不清就当还没到，交给 deadline 收场
                chunk = None
        spent = time.ticks_diff(time.ticks_ms(), t_recv)
        if chunk is None:
            if deadline_ms is not None and \
                    time.ticks_diff(time.ticks_ms(), t0) >= deadline_ms:
                break
            continue
        if not chunk:
            # 区分"超时假装成空包"和"真的 EOF"：看耗时
            if spent >= int(SHORT_S * 1000 * 0.5) and not buf:
                if deadline_ms is not None and \
                        time.ticks_diff(time.ticks_ms(), t0) >= deadline_ms:
                    break
                continue
            break                    # 立刻返回的空包 = 真的对端关闭
        buf += chunk
        if b"\r\n\r\n" in buf:
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


# ============================================================
# 上传（失败就存本地，不阻塞放饭）
# ============================================================
# 保存转换结果的引用。
# ⚠️ 这一行不是多余的：image.Image(alloc=ALLOC_REF, data=xxx) 创建的对象**不拥有**
# 那块内存，只是拿着指针。如果 xxx 是临时对象，函数返回后它会被 GC 回收，
# 而 image 还指着已释放的内存 —— 之后 compress()/VENC 一读就硬崩（K230 直接
# 掉 USB、屏幕定格，日志停在半句话）。所以必须把底层数组挂在这里保命。
_FRAME_HOLD = {}


def _to_bytes(obj):
    """把 ulab ndarray / memoryview 变成真正的 bytes。拿不到返回 None。"""
    if obj is None:
        return None
    for name in ("tobytes", "to_bytes"):
        fn = getattr(obj, name, None)
        if fn is not None:
            try:
                b = fn()
                if b:
                    return bytes(b)
            except Exception:
                pass
    try:
        return bytes(obj)
    except Exception:
        return None


def _frame_stats(img):
    """源帧自身的 min/max/mean。

    用途是在"网页上是灰图"的时候分清责任：
        源帧就是一片纯色  -> 相机/驱动的问题，跟这里的转换无关；
        源帧有内容但存出来是灰 -> 转换把数据弄坏了。
    """
    out = {}
    for name in ("min", "max", "mean"):
        fn = getattr(img, name, None)
        if fn is None:
            continue
        try:
            out[name] = int(fn())
        except Exception:
            try:
                out[name] = float(fn())
            except Exception:
                pass
    return out


# 抽样点（八分之几的网格，避开最外圈）
_SAMPLE_GRID = ((1, 1), (4, 1), (7, 1), (1, 4), (4, 4), (7, 4), (1, 7), (4, 7), (7, 7))


def _sample_xy(w, h):
    pts = []
    for (cx, cy) in _SAMPLE_GRID:
        x = min(w - 1, max(0, w * cx // 8))
        y = min(h - 1, max(0, h * cy // 8))
        pts.append((x, y))
    return pts


def _sample_wanted(img, w, h):
    """**立刻**把源帧的采样点读出来存成普通数值。

    为什么不能等到比较时再读 img：get_frame() 给的是相机 / VB 缓冲区的
    引用，下一帧可能就把它覆盖了。转换要走几十毫秒到几百毫秒，期间再回头
    读 img 拿到的可能已经不是同一帧 —— 那样"校验不过"就变成了假警报。

    返回 [(x, y, (r,g,b)), ...]，读不出来就返回 None（放弃校验）。
    """
    want = []
    for (x, y) in _sample_xy(w, h):
        try:
            want.append((x, y, (int(img[0, y, x]),
                                int(img[1, y, x]),
                                int(img[2, y, x]))))
        except Exception:
            return None
    return want


def _check_pixels(out, want, tol=40):
    """抽样把 image.Image 的像素和源帧比一遍。

    这是"灰图"bug 的照妖镜：
        转换一旦读错内存/读错布局，采样点会**全线不符**；
        布局正确时，源帧是什么，取出来就该是什么。

    返回 (通过?, 命中数, 总数, 第一个不符的样例)。
    """
    if not want:
        return True, 0, 0, None
    gp = getattr(out, "get_pixel", None)
    if gp is None:
        return True, 0, 0, None        # 固件没有 get_pixel，只能信
    ok = 0
    total = 0
    first = None
    for (x, y, exp) in want:
        try:
            got = gp(x, y)
            got = (int(got[0]), int(got[1]), int(got[2]))
        except Exception:
            return True, 0, 0, None    # 读不出来就不瞎判，放过
        total += 1
        d = max(abs(got[0] - exp[0]), abs(got[1] - exp[1]), abs(got[2] - exp[2]))
        if d <= tol:
            ok += 1
        elif first is None:
            first = (x, y, got, exp)
    return ok >= total - 1, ok, total, first


def _hwc_view(img, np, h, w):
    """CHW -> HWC 的视图（不拷贝）。"""
    try:
        return np.reshape(img, (3, h, w)).transpose(1, 2, 0)
    except Exception:
        pass
    try:
        tr = getattr(img, "transpose", None)
        if tr is not None:
            return tr(1, 2, 0)
    except Exception:
        pass
    return None


def _interleave(img, h, w):
    """最后手段：拿三个平面自己拼成 RGB 交织的 bytearray。

    慢（几十万次循环），但完全不依赖固件的 transpose / 视图语义，
    所以只要三个平面本身的字节是对的，出来的图就一定是对的。
    """
    planes = []
    for c in range(3):
        try:
            b = _to_bytes(img[c])
        except Exception:
            b = None
        if not b or len(b) != w * h:
            return None
        planes.append(b)
    ba = bytearray(w * h * 3)
    # 先试扩展切片赋值（C 实现，瞬间完成）
    try:
        ba[0::3] = planes[0]
        ba[1::3] = planes[1]
        ba[2::3] = planes[2]
        return ba
    except Exception:
        pass
    # 不行就老老实实逐像素
    p0, p1, p2 = planes[0], planes[1], planes[2]
    j = 0
    for i in range(w * h):
        ba[j] = p0[i]
        ba[j + 1] = p1[i]
        ba[j + 2] = p2[i]
        j += 3
    return ba


# 每条转换策略只打印一次结果 / 只报一次错，免得刷屏
_CONV_LOGGED = {}
_CONV_FAILED = {}


def _frame_to_image(img, key="up"):
    """把 get_frame() 的帧转成 image.Image（拍照/压缩必须用它）。

    为什么必须转：
        当前 CanMV（含亚博 v1.4.3）的 PipeLine.get_frame() 返回的是
        **ulab.numpy.ndarray**，形状 [3,H,W]（CHW，RGB888 平面格式）。
        这是官方设计，不是固件版本问题。而 compress() / draw_rectangle()
        都是 image.Image 的方法，ndarray 上一个都没有。

    为什么要"逐策略校验"（这段是踩坑后加的，别改回去）：
        现场出过一次：识别完全正常（串口一直刷 dog/cat），网页上却每张
        照片都是**一片灰**。把服务端存的 jpg 拆开看像素才发现，图里只有
        细密噪声、没有一点画面结构 —— 也就是说 ndarray 本身是对的（模型
        就是在它上面认出的猫狗），是"把它交给 image.Image"这一步读错了
        内存。

        不同固件对
            Image(w, h, RGB888, alloc=ALLOC_REF, data=?)
        里的 data 要求不一样：有的要真正的 bytes/bytearray，喂 ulab 视图
        过去**不报错**，但图像指向的是没初始化的内存 —— 于是存出来就是灰
        的/雪花。这种失败不会抛异常，只能靠**验证像素**发现。

        所以这里按"最可能对"的顺序挨个试，每条都抽样跟源帧比一遍像素，
        第一个对上的就用。全都不对就把每种策略的偏差打出来。

    返回 image.Image 或 None（None 时调用方必须明确报"拍不了照"，不许静默）。
    """
    if img is None:
        return None
    if hasattr(img, "compress"):        # 已经是 image 对象，直接用
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
    h, w = int(shape[1]), int(shape[2])

    tried = []
    stats = _frame_stats(img)
    want = _sample_wanted(img, w, h)

    def accept(out, hold, name):
        """校验像素，对得上就收下；对不上就丢掉并记一笔。"""
        ok, hits, total, first = _check_pixels(out, want)
        if not ok:
            tried.append("%s(像素不符 %d/%d 例:%s)" % (name, hits, total, first))
            return None
        _FRAME_HOLD[key] = hold        # 见文件顶部的注释，必须留住内存
        if key not in _CONV_LOGGED:
            _CONV_LOGGED[key] = True
            if total:
                log("[up] 拍照转换 OK：%s（抽样 %d/%d 与源帧一致，源帧 %s）"
                    % (name, hits, total, stats))
            else:
                log("[up] 拍照转换 OK：%s（没有 get_pixel，没法抽样校验）"
                    % name)
        return out

    fmt = getattr(image, "RGB888", None)
    if fmt is not None:
        hwc = _hwc_view(img, np, h, w)

        # --- 策略 1：HWC 视图 -> 真正的 bytes -> Image ---
        # 这条路最稳：bytearray 一定支持缓冲协议，Image 拿到的是确定的内存。
        if hwc is not None:
            raw = _to_bytes(hwc)
            if raw:
                try:
                    ba = bytearray(raw)
                    out = image.Image(w, h, fmt, alloc=image.ALLOC_REF, data=ba)
                    got = accept(out, (out, ba), "HWC字节拷贝")
                    if got is not None:
                        return got
                except Exception as e:
                    tried.append("HWC字节拷贝(建图失败:%s)" % e)

        # --- 策略 2：HWC 视图原样塞给 Image（老写法，部分固件只认这个）---
        if hwc is not None:
            cp = hwc
            try:
                cp = hwc.copy()           # 连续内存，防止视图被当成平面格式
            except Exception:
                try:
                    cp = np.array(hwc)    # 有的 ulab 版本没有 .copy()
                except Exception:
                    cp = hwc
            try:
                out = image.Image(w, h, fmt, alloc=image.ALLOC_REF, data=cp)
                got = accept(out, (out, cp), "HWC转置")
                if got is not None:
                    return got
            except Exception as e:
                tried.append("HWC转置(建图失败:%s)" % e)

    # --- 策略 3/4：平面格式（RGBP888），连转置都不用 ---
    fmtp = getattr(image, "RGBP888", None)
    if fmtp is not None:
        try:
            out = image.Image(w, h, fmtp, alloc=image.ALLOC_REF, data=img)
            got = accept(out, (out, img), "RGBP888平面(源帧)")
            if got is not None:
                return got
        except Exception as e:
            tried.append("RGBP888平面源帧(建图失败:%s)" % e)

        raw2 = _to_bytes(img)
        if raw2:
            try:
                ba2 = bytearray(raw2)
                out = image.Image(w, h, fmtp, alloc=image.ALLOC_REF, data=ba2)
                got = accept(out, (out, ba2), "RGBP888平面(字节拷贝)")
                if got is not None:
                    return got
            except Exception as e:
                tried.append("RGBP888平面字节拷贝(建图失败:%s)" % e)

    # --- 策略 5：三个平面手动交织（最慢，但最不依赖固件语义）---
    if fmt is not None:
        ba3 = _interleave(img, h, w)
        if ba3 is not None:
            try:
                out = image.Image(w, h, fmt, alloc=image.ALLOC_REF, data=ba3)
                got = accept(out, (out, ba3), "手动交织")
                if got is not None:
                    return got
            except Exception as e:
                tried.append("手动交织(建图失败:%s)" % e)
        else:
            tried.append("手动交织(平面拆不出来)")

    # --- 全都不行：把能定位问题的信息一次打全 ---
    if key not in _CONV_FAILED:
        _CONV_FAILED[key] = True
        log("[up] !! 每一路帧转换都和源帧对不上，网页上不会有照片。")
        log("     源帧统计: %s" % (stats,))
        for t in tried:
            log("       - %s" % t)
        lohi = None
        try:
            if "max" in stats and "min" in stats:
                lohi = stats["max"] - stats["min"]
        except Exception:
            lohi = None
        if lohi is not None and lohi < 12:
            log("     => 源帧本身几乎是一片纯色，问题在**相机**，不在这段转换。")
            log("        先断电重上电；还这样就试 RGB888P_SIZE=(640,360)。")
        else:
            log("     => 源帧有内容但转换对不上，是格式/布局问题。")
            log("        把上面几行发出来，按里面的偏差再定方案。")

    return None


def _jpeg_bytes(im, quality=None):
    """把 image.Image 压成 JPEG 字节。拿不到就返回 None。

    为什么要绕过 save()：
        save() 只认**打包**格式（RGB565 / RGB888 / 灰度）。
        平面格式（RGBP888）调 save() 会报
            current format not support save function!
        而 compress() 对格式宽容得多。所以先拿字节再自己写文件，
        能救回一批"能编码但 save() 不支持"的情况。
    """
    if quality is None:
        quality = JPEG_QUALITY
    for name in ("compress", "to_jpeg"):
        fn = getattr(im, name, None)
        if fn is None:
            continue
        for args in ((), (quality,)):
            try:
                b = fn(quality=quality) if not args else fn(*args)
            except TypeError:
                continue
            except Exception:
                break
            if b:
                return b
    return None


def _save_jpeg(im, path, quality=None):
    """存一张 JPEG。返回 (成功?, 走过的路径说明)。

    顺序：compress 取字节自己写 -> save()。
    两条都试，是因为不同固件上这两条的支持范围正好互补。
    """
    b = _jpeg_bytes(im, quality)
    if b:
        f = None
        try:
            f = open(path, "wb")
            f.write(b)
            return True, "compress(%d 字节)" % len(b)
        except Exception as e:
            return False, "写文件失败: %s" % e
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass

    try:
        im.save(path)
        return True, "save()"
    except TypeError:
        pass
    except Exception as e:
        return False, "两边都不支持这个格式：compress 不行，save() 报 %s" % e
    return False, "两边都不支持这个格式"


def _dump_frame(img, pl, now):
    """把"模型看到的那一帧"存成 jpg，供事后肉眼确认镜头拍到的是什么。

    两级兜底，因为每一级在不同固件上都可能不支持：
      ① 帧 -> image.Image -> compress 取字节自己写文件（和上传走同一条路）
      ② 重新取一帧再转一次（偶发的转换失败，重来一遍往往就好了）
    全程只碰 pl.get_frame()，**不碰 sensor.snapshot()** —— 见下面的注释。
    """
    try:
        snap = _frame_to_image(img, key="dump")
        if snap is not None:
            ok, how = _save_jpeg(snap, DEBUG_DUMP_PATH, 70)
            if ok:
                log("[beat] 已存 %s（模型看到的画面）via %s"
                    % (DEBUG_DUMP_PATH, how))
                return
            log("[beat] 存图降级：%s" % how)
            del snap
        else:
            log("[beat] 存图降级：帧转不成 image")
    except Exception as e:
        log("[beat] 存图降级：%s" % e)

    # ---- 最后兜底：重新取一帧再转一次 ----
    # ⚠️ 这里**故意不用** pl.sensor.snapshot()。
    #    PipeLine 已经把 sensor 的通道绑好了，再让 sensor 去 snapshot()
    #    等于从管道手里抢通道 —— 轻则 chn2 报 failed(3)，重则整个管道卡死。
    #    之前在排查里踩过一次，代价是整块板子重新上电，不值。
    try:
        img2 = pl.get_frame()
        if img2 is None:
            log("[beat] !! 取证失败：这一帧也取不到（相机通道有问题）")
            return
        snap = _frame_to_image(img2, key="dump2")
        if snap is None:
            log("[beat] !! 取证失败：frame 转 image 一直不成功")
            log("     识别和放饭不受影响；想看画面可以跑 tools/camera_probe.py")
            return
        ok, how = _save_jpeg(snap, DEBUG_DUMP_PATH, 70)
        if ok:
            log("[beat] 已存 %s via %s" % (DEBUG_DUMP_PATH, how))
        else:
            log("[beat] !! 取证失败：%s" % how)
        del snap
    except Exception as e:
        log("[beat] !! 取证异常（不影响主流程）: %s" % e)
    finally:
        gc.collect()


class VisitShooter(object):
    """把"到访边沿"翻译成"这一帧要不要拍一张"。

    为什么单独抽成一个类：main() 里那个 while 循环在 PC 上跑不起来，
    但"每次到访恰好一张照片"这条规则必须能被验证 —— 否则串口的
    "第 N 次到访" 和网页的第 N 条记录就又会各走各的。

    规则：
      - 到访边沿（False->True）：记一笔"这次到访还没拍"；
      - 每帧检查：还没拍 且 这一帧真的有框  -> 拍，清掉标记；
      - 动物走了还挂着标记 -> 算漏拍（那一次确实没抓到框），
        并**不能**留到下一次到访，否则下次会多拍一张，数字又乱了。
    """

    def __init__(self, per_visit=None):
        self.per_visit = UPLOAD_PER_VISIT if per_visit is None else per_visit
        self.pending = False
        self.shots = 0
        self.missed = 0

    def on_arrive(self):
        if self.per_visit:
            self.pending = True

    def on_leave(self):
        if self.pending:
            self.pending = False
            self.missed += 1

    def tick(self, det, present):
        """每帧调用一次。返回 True 表示这一帧该拍照上传。"""
        if not self.per_visit:
            # 旧行为：有框就交给 maybe_upload 的时钟节流去判断
            return det is not None and (present or not UPLOAD_ONLY_WHEN_PRESENT)
        if self.pending and det is not None:
            self.pending = False
            self.shots += 1
            return True
        return False


class Uploader(object):
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.saved = 0
        self.ready = False
        self.status = "off"
        self.ip = None              # 连上 WiFi 后填，自检和日志都用
        self._q = []                # 待传任务（后台线程消费）
        self._worker = None         # 后台线程是否已启动
        self._uid_seq = 0
        if UPLOAD_MODE != "wifi" or not SERVE_UPLOAD:
            self.status = "disabled"
            return
        self.ready = self._wifi()
        if self.ready:
            try:
                self._preflight()
            except Exception as e:
                log("[up] 自检异常（不影响主流程）: %s" % e)

    # ---------------- 后台线程 ----------------
    def _jpeg_uid(self, jpeg):
        """按**照片内容**算一个短指纹（只用于文件名/排查，不再当 uid 用）。

        为什么不整张做 sha256：100KB 在 K230 上逐字节算要几百毫秒，
        这是主循环里不能接受的。这里按步长采样约 1KB 做 FNV-1a。
        """
        n = len(jpeg)
        step = 1 if n <= 4096 else (n // 1024)
        h = 0x811C9DC5
        i = 0
        while i < n:
            b = jpeg[i]
            if not isinstance(b, int):
                b = ord(b)
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
            i += step
        return "%s-%d-%08x" % (DEVICE_ID, n, h)

    def _event_uid(self, visit=0):
        """一次"到访"一个 uid：DEVICE_ID-开机标记-到访号-拍照时刻。

        为什么不用内容指纹：相机对静止画面会产出字节相同的 JPEG
        （现场 #10 与 #69 的 sha1 完全相同），那样两次到访会被去重成一条。
        """
        return "%s-%d-%d-%d" % (DEVICE_ID, _BOOT_TAG, int(visit or 0),
                                time.ticks_ms())

    def _ensure_worker(self):
        """第一次需要上传时再起线程。

        为什么不放在 __init__：WiFi 没连上时就不用白起一个线程；
        而且测试环境（PC）里不想真起线程。
        """
        if not UPLOAD_IN_BACKGROUND:
            return False
        if self._worker is not None:
            return True
        try:
            import _thread
        except Exception:
            log("[up] 固件没有 _thread，上传会在主循环里同步进行")
            return False
        try:
            _thread.start_new_thread(self._loop, ())
            self._worker = True
            return True
        except Exception as e:
            log("[up] 后台线程起不来（%s），改成同步上传" % e)
            return False

    def _loop(self):
        """后台线程主体：先传队列里的，空闲时顺手补传 SD 卡上的。

        全程不碰相机（照片已经是字节了），也不碰 GPIO —— 只做网络。
        """
        last_fill = time.ticks_ms()
        while True:
            job = None
            try:
                if self._q:
                    job = self._q.pop(0)
            except Exception:
                job = None
            if job is None:
                # 空闲：补传之前失败的。
                # 为什么要冷却：retry_pending 每次都要 listdir 一遍 SD 卡，
                # 每 0.5 秒扫一次纯属浪费，还抢 SDIO 带宽。
                if time.ticks_diff(time.ticks_ms(), last_fill) >= \
                        BACKFILL_PERIOD_MS:
                    last_fill = time.ticks_ms()
                    try:
                        self.retry_pending(BACKFILL_BATCH)
                    except Exception:
                        pass
                time.sleep_ms(500)
                continue
            try:
                self._deliver(job)
            except Exception as e:
                log("[up] 后台上传异常: %s" % e)

    def _deliver(self, job):
        """真正干活：发一张，失败就落到 SD 卡（uid 一起写进文件名）。"""
        jpeg, species, det, ts, uid = job
        if self.ready and self._post(jpeg, species, det, ts=ts, uid=uid):
            self.ok += 1
            self.status = "sent"
            return True
        self._save(jpeg, species, det, uid=uid)
        self.fail += 1
        self.status = "saved_local"
        return False

    def _wifi(self):
        try:
            import network
        except Exception:
            log("[up] 固件没有 network 模块")
            return False
        try:
            wlan = network.WLAN(network.STA_IF)
            if not wlan.isconnected():
                wlan.active(True)
                wlan.connect(WIFI_SSID, WIFI_PASS)
                t0 = time.ticks_ms()
                while not wlan.isconnected():
                    if time.ticks_diff(time.ticks_ms(), t0) > WIFI_TIMEOUT_MS:
                        log("[up] !! WiFi 连接超时（SSID=%s）" % WIFI_SSID)
                        self.status = "offline"
                        return False
                    time.sleep_ms(200)
            log("[up] WiFi 已连接 %s" % (wlan.ifconfig()[0],))
            self.status = "wifi"
            self.ip = wlan.ifconfig()[0]
            return True
        except Exception as e:
            log("[up] WiFi 失败: %s" % e)
            return False

    # ---------------- 上电自检（连上 WiFi 之后马上做一次）----------------
    def _preflight(self):
        """把"云端配置和当前网络对不对得上"在开机时就喊出来。

        为什么值得单独做：配置错了以后，程序看起来一切正常 —— 检测、开饭、
        画框全都在跑，只有照片悄悄进了 SD 卡。等你发现网页上没有新记录，
        可能已经是几天之后了。所以这些检查必须在**开机那几秒**就打印。

        这里只做静态判断（不占网络、不耽误启动），真正的连通性验证用
        tools/cloud_check.py 单独跑。
        """
        scheme, host, port, base = parse_server_url()
        log("[up] 目标 %s://%s:%d%s" % (scheme, host, port, base or "/"))

        # ---- 1. https 但固件没有 ssl：这个组合 100% 传不上去 ----
        if scheme == "https":
            try:
                import ssl          # noqa: F401
                log("[up] https 可用（ssl 模块在）")
            except Exception:
                log("[up] !! SERVER_URL 是 https，但这个固件没有 ssl 模块")
                log("     照片会一直失败。换 http，或烧带 ssl 的固件。")

        # ---- 2. token ----
        if not API_TOKEN:
            if _is_private_host(host):
                log("[up] API_TOKEN 为空 —— 局域网内可以先这样")
            else:
                log("[up] !! 目标是公网地址，但 API_TOKEN 是空的")
                log("     服务端一旦设了 CATDOG_TOKEN，上传就会 403。")

        # ---- 3. 地址和本机是不是同一个网段 ----
        # 现场最常见的一种错：SERVER_URL 还留着示例的 192.168.1.100，
        # 而板子连的是手机热点 192.168.43.x —— 两个网段根本不通，
        # 表现就是连接超时，但日志里只有一句"上传失败"。
        if _is_private_host(host) and self.ip:
            a = host.rsplit(".", 1)[0]
            b = str(self.ip).rsplit(".", 1)[0]
            if a != b:
                log("[up] !! 地址看着像局域网，但不在同一网段：")
                log("     后台 %s  /  本机 %s" % (host, self.ip))
                log("     多半连不上。要么把 SERVER_URL 改成 %s 开头，"
                    "要么放到云端去。" % b)
        log("[up] 想确认到底通不通：跑 tools/cloud_check.py")

    # ---------------- 取图 ----------------
    def _crop(self, img, rect):
        for name in ("crop", "copy"):
            fn = getattr(img, name, None)
            if fn is None:
                continue
            try:
                return fn(rect[0], rect[1], rect[2], rect[3])
            except TypeError:
                try:
                    return fn(roi=rect)
                except Exception:
                    continue
            except Exception:
                continue
        return img

    def _jpeg(self, img):
        # 走和存图同一条路：compress 取字节优先，save() 不参与（上传要的是字节）
        b = _jpeg_bytes(img, JPEG_QUALITY)
        if not b:
            try:
                return img.to_jpeg(quality=JPEG_QUALITY)
            except Exception:
                return None

        # 太大就降质量重压。只压一次通常不够（q82 -> q65 一般能小 40%），
        # 所以按顺序多试几档，够小就立刻停 —— 别为了省几百字节多压三次。
        if len(b) > UPLOAD_MAX_BYTES:
            for q in JPEG_QUALITY_FALLBACKS:
                nb = _jpeg_bytes(img, q)
                if not nb:
                    continue
                if len(nb) < len(b):
                    b = nb
                if len(b) <= UPLOAD_MAX_BYTES:
                    if not getattr(self, "_noted_shrink", False):
                        self._noted_shrink = True
                        log("[up] 照片偏大，已自动降到 q%d（%d 字节）"
                            % (q, len(b)))
                    break
        return b

    def _rect(self, det):
        W, H = RGB888P_SIZE
        x, y, w, h = det[0], det[1], det[2], det[3]
        x -= w * CROP_PAD
        y -= h * CROP_PAD
        w *= (1 + 2 * CROP_PAD)
        h *= (1 + 2 * CROP_PAD)
        x = int(max(0, min(x, W - 8)))
        y = int(max(0, min(y, H - 8)))
        w = int(min(w, W - x))
        h = int(min(h, H - y))
        return (x, y, max(8, w), max(8, h))

    # ---------------- 主入口 ----------------
    def send(self, img, det, visit=0):
        if UPLOAD_MODE != "wifi" or not SERVE_UPLOAD:
            return False
        if det is None:
            self.status = "no_det"
            return False
        # 全帧模式下不检查框大小 —— 反正传的是整张图
        if not UPLOAD_FULL_FRAME and (det[2] < MIN_BOX or det[3] < MIN_BOX):
            self.status = "too_small"
            return False

        # get_frame() 给的是 CHW ndarray，compress()/crop() 都没有 ——
        # 必须转成 image.Image 才能裁剪和压 JPEG。
        #
        # 优先用专用拍照通道（打包 RGB888）：get_frame() 那路是**平面**格式，
        # 当成打包格式用会得到一片灰图。专用通道拿不到才退回帧转换。
        src = _SNAP.grab()
        if src is None:
            src = _frame_to_image(img)
        if src is None:
            if not getattr(self, "_warned_fmt", False):
                self._warned_fmt = True
                log("[up] !! 这一帧转不成 image.Image，拍照功能不可用。")
                log("      检测/放饭不受影响，只是网页上没有照片。")
                log("      原因通常是固件的 image 模块不支持 ALLOC_REF，")
                log("      把这段日志发出来，改成逐像素/分块方案即可。")
            self.status = "no_image"
            return False

        # ---- 传整张图还是裁出框 ----
        # UPLOAD_FULL_FRAME=True：直接压整帧。好处是不依赖框坐标是否准确，
        #   哪怕库给的框有点偏，拍到的照片也一定是完整的、能看清猫在哪。
        #   代价是文件大一点（几十 KB 变一百多 KB），局域网完全无所谓。
        if UPLOAD_FULL_FRAME:
            rect = (0, 0, RGB888P_SIZE[0], RGB888P_SIZE[1])
            sub = src
        else:
            rect = self._rect(det)
            sub = self._crop(src, rect)
        jpeg = self._jpeg(sub)
        if not jpeg:
            self.fail += 1
            self.status = "encode_fail"
            return False

        species = "cat" if int(det[4]) == CLASS_CAT else (
            "dog" if int(det[4]) == CLASS_DOG else "unknown")
        log("[up] 拍照: %s conf=%.2f %dx%d %dB"
            % (species, det[5], rect[2], rect[3], len(jpeg)))

        # uid 必须"每次事件唯一"，不能按内容算 —— 现场踩过：
        # 相机对着静止画面（比如显示猫狗照片的屏幕）会产出**字节完全相同**的
        # JPEG（#10 和 #69 的 sha1 一样）。按内容算 uid 的话，两次不同的到访
        # 会被服务端当成重传、去重成一条，串口第 2 次到访就"消失"了。
        #
        # 用 到访编号 + 开机标记 + 拍照时刻 组成 uid：同一次到访的重传/补传
        # uid 一致（去重有效），不同到访一定不同（计数准确）。
        job = (jpeg, species, det, _epoch_now(), self._event_uid(visit))

        # ---- 能后台就后台：主循环必须马上回去检测 ----
        if self._ensure_worker():
            # 队列满了就丢最旧的那张，并明确说出来。
            # 为什么不阻塞等待：宁可少一张照片，也不能让检测停 60 秒 ——
            # 停下来的代价是动物在盆边却没饭，照片少一张只是统计少一条。
            while len(self._q) >= UPLOAD_QUEUE_MAX:
                try:
                    self._q.pop(0)
                except Exception:
                    break
                log("[up] !! 待传队列已满，丢掉更旧的一张（优先保证检测）")
            self._q.append(job)
            self.status = "queued"
            return True

        # 没有线程（老固件）就退回同步，行为跟以前一样
        return self._deliver(job)

    # ---------------- 补传 ----------------
    def retry_pending(self, limit=2):
        """把之前失败、存在 SD 卡上的照片补传上去。

        为什么需要：走公网时偶发断网/超时是常态，没有补传的话这些照片
        永远进不了统计（文件还在，但网页上看不到）。
        每次只补 limit 张 —— 别让补传抢了检测和放饭的时间。

        返回这次成功补传的张数。
        """
        if UPLOAD_MODE != "wifi" or not SERVE_UPLOAD:
            return 0
        if not self.ready:
            return 0
        try:
            names = [n for n in os.listdir(SAVE_LOCAL_DIR) if n.endswith(".jpg")]
        except Exception:
            return 0
        if not names:
            return 0

        names.sort(key=_saved_key)
        done = 0
        for name in names[:limit]:
            path = SAVE_LOCAL_DIR + "/" + name
            try:
                species, conf = _parse_saved_name(name)
                with open(path, "rb") as f:
                    jpeg = f.read()
                if not jpeg:
                    os.remove(path)             # 空文件，删掉别占位
                    continue
                det = [0.0, 0.0, 0.0, 0.0,
                       CLASS_CAT if species == "cat" else CLASS_DOG, conf]
                # uid 从文件名读回来 —— 就是当初直传时用的那个。
                # 直传其实成功了、只是没收到回包的情况下，补传必须带同一个
                # uid，服务端才会认成重传，不会多存一条。
                # 老格式（没有第 4 段）读不到 uid，退回内容指纹。
                uid = _uid_from_saved(name) or self._jpeg_uid(jpeg)
                #
                # bf=1 告诉服务端"这是旧照片补传"，ts 用文件的修改时间（可信才带）。
                # 不带的话，一周前拍的照片会被记成"今天刚到访"，网页统计直接错乱。
                if self._post(jpeg, species, det,
                              ts=_epoch_from_mtime(path),
                              uid=uid, backfill=True):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    done += 1
                    self.ok += 1
                    log("[up] 补传成功: %s" % name)
                else:
                    # 还是不通（网又断了 / token 错），这次就停手，
                    # 免得每张都等一遍超时，把主循环拖住。
                    break
            except Exception as e:
                log("[up] 补传出错: %s (%s)" % (name, e))
                break
        if done:
            self.status = "backfill_%d" % done
        return done

    # ---------------- HTTP ----------------
    def _post(self, jpeg, species, det, ts=None, uid=None, backfill=False):
        """往 SERVER_URL 发一张 JPEG。成功返回 True。

        为什么不用 urequests/urllib：CanMV 固件不一定带，而且我们要精确控制
        超时和 Host 头。裸 socket 只有几十行，反而最可控。
        """
        scheme, host, port, base = parse_server_url()
        qs = ("device=%s&species=%s&conf=%.3f&ws=%d&hs=%d"
              % (DEVICE_ID, species, det[5], RGB888P_SIZE[0], RGB888P_SIZE[1]))
        # ts 让服务端知道"照片是什么时候拍的"。直连时几乎用不上，
        # 但从 SD 卡补传时就用得上 —— 否则一排补传的照片时间戳全是补传的时刻。
        if ts:
            qs += "&ts=%d" % int(ts)
        # uid 让服务端能认出"这是同一张照片的重传"，避免存成多条
        if uid:
            qs += "&uid=%s" % uid
        # bf=1 = 这张是旧照片补传。服务端据此把 ts 当作拍照时刻入库，
        # 并在日志里标出来 —— 免得"网页上突然多了几次到访"没法解释。
        if backfill:
            qs += "&bf=1"
        if API_TOKEN:
            qs += "&token=%s" % API_TOKEN

        path = (base + "/api/v1/visit?") if base else "/api/v1/visit?"
        head = ("POST %s%s HTTP/1.0\r\n"
                "Host: %s\r\n"
                "Content-Type: image/jpeg\r\n"
                "Content-Length: %d\r\n"
                "Connection: close\r\n\r\n"
                % (path, qs, host if port in (80, 443) else "%s:%d" % (host, port),
                   len(jpeg)))

        try:
            import socket
        except Exception:
            import usocket as socket

        last_err = None
        for attempt in range(1 + max(0, UPLOAD_RETRIES)):
            s = None
            code = 0
            send_err = None
            t_start = time.ticks_ms()
            try:
                s = socket.socket()
                s.settimeout(UPLOAD_TIMEOUT_S)
                s.connect((host, port))

                if scheme == "https":
                    s = _tls_wrap(s, host)
                    if s is None:
                        log("[up] 这个固件没有 ssl 模块 —— 用不了 https")
                        log("     要么把服务端换成 http，要么烧带 ssl 的固件")
                        return False

                try:
                    _send_all(s, head.encode())
                    _send_all(s, jpeg)
                except Exception as e:
                    # 服务端可能已经拒绝并关了连接（RST）。这时候 send 会报
                    # ECONNRESET，但它其实**已经把原因发出去了**。
                    # 所以别急着当网络错误重试 —— 先读一次响应，拿到真实状态码。
                    send_err = e

                code = _read_status(
                    s, deadline_ms=int(UPLOAD_TIMEOUT_S * 1000))
                if code == 200:
                    log("[up] 上传成功 %d ms" % time.ticks_diff(
                        time.ticks_ms(), t_start))
                    return True
                # 403 是 token 不匹配，重试没意义，直接说清楚
                if code == 403:
                    log("[up] 403 = token 不对。K230 的 API_TOKEN 和")
                    log("     服务端 CATDOG_TOKEN 必须是同一个值。")
                    log("     服务端没设 token 时，K230 这边也必须留空。")
                    return False
                # 400/413 是这张图本身有问题，重试也是同一张，没意义
                if code in (400, 413, 415):
                    log("[up] %d = 服务端拒收这张图（太大或不是 JPEG/PNG）"
                        % code)
                    return False
                if code:
                    last_err = "HTTP %s" % code
                else:
                    last_err = send_err or "服务端没有任何响应"
            except Exception as e:
                last_err = e
            finally:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

            if attempt < UPLOAD_RETRIES:
                log("[up] 第 %d 次失败(%s, 等了 %d ms)，重试"
                    % (attempt + 1, last_err,
                       time.ticks_diff(time.ticks_ms(), t_start)))
                time.sleep_ms(500)

        log("[up] 上传失败: %s" % (last_err,))
        return False

    # ---------------- 本地兜底 ----------------
    def _save(self, jpeg, species, det=None, uid=None):
        """存 SD 卡。文件名里带上 species/conf/uid，补传时不用再记元数据。

        格式：{species}_{conf*1000:03d}_{ticks}_{uid}.jpg
          cat_913_12345_k230-01-89213-1-4512.jpg
          ->  species=cat, conf=0.913, ticks=12345, uid=k230-01-89213-1-4512

        为什么把 uid 塞进文件名：直传时如果超时（服务端其实已经存好了），
        这张图会落盘，之后补传必须带**同一个 uid** 才能被服务端认成重传。
        uid 只存在内存里的话，板子一重启就丢了。

        用文件名而不是旁边再放个 .json：
          - 不会出现"图片删了 json 还在"的不一致
          - 不依赖 json 模块，MicroPython 各版本行为一致
          - 人肉 ls 一眼就能看出这张是什么
        """
        try:
            try:
                os.mkdir(SAVE_LOCAL_DIR)
            except Exception:
                pass                      # 已存在
            conf = 0.5
            if det is not None and len(det) > 5:
                try:
                    conf = float(det[5])
                except Exception:
                    pass
            c3 = int(conf * 1000)
            if c3 < 0:
                c3 = 0
            elif c3 > 999:
                c3 = 999
            # uid 里的 "-" 不会和 "_" 分隔符混淆，解析时安全。
            # 没有 uid 就退回三段式（老格式），别在文件名里留个空的 "-"。
            base = "%s_%03d_%d" % (species, c3, time.ticks_ms() % 10000000)
            if uid:
                base += "_" + str(uid)
            path = "%s/%s.jpg" % (SAVE_LOCAL_DIR, base)
            with open(path, "wb") as f:
                f.write(jpeg)
            self.saved += 1
            log("[up] 已存本地: %s" % path)
            self._trim()
        except Exception as e:
            log("[up] 存本地也失败了: %s" % e)

    def _trim(self):
        """只留最近 SAVE_LOCAL_MAX 张，按文件名里的 ticks 排新旧。"""
        try:
            names = [n for n in os.listdir(SAVE_LOCAL_DIR) if n.endswith(".jpg")]
            names.sort(key=_saved_key)
            for n in names[:max(0, len(names) - SAVE_LOCAL_MAX)]:
                try:
                    os.remove(SAVE_LOCAL_DIR + "/" + n)
                except Exception:
                    pass
        except Exception:
            pass


# ============================================================
# 本地缓存文件名的解析（补传要用）
# ============================================================
#
# 格式：{species}_{conf*1000:03d}_{ticks}.jpg
#   cat_913_12345.jpg -> ("cat", 0.913), key=12345
#
# 兼容早期版本存下的旧名字 cat_12345.jpg（只有两段）：
# 那时没有 conf，后一段是 ticks —— 解析时不能把它当 conf（会得到 12.345）。

def _epoch_now():
    """当前的 epoch 秒；拿不到可信时间就返回 None。

    为什么不能直接用 time.time()：板子没有 RTC/NTP 时它可能是 0、
    或者从 1970 开始算。把 0 当时间发出去，服务端会把它记成 1970 年的到访，
    网页上就是一条永远排在最前面的怪记录。宁可发 None（服务端用收到时刻）。
    """
    try:
        t = time.time()
    except Exception:
        return None
    try:
        t = float(t)
    except Exception:
        return None
    if t < 1600000000 or t > 4102444800:      # 2020 ~ 2100 之外都不可信
        return None
    return int(t)


def _epoch_from_mtime(path):
    """从 SD 卡文件的修改时间推拍照时刻；不可信就返回 None。

    os.stat()[8] 是 st_mtime。FAT 的修改时间取决于板子自己的时钟，
    没设过 RTC 的话同样是垃圾 —— 所以这里只信 2020 年以后的。
    """
    try:
        t = os.stat(path)[8]
    except Exception:
        return None
    try:
        t = float(t)
    except Exception:
        return None
    if t < 1600000000 or t > 4102444800:
        return None
    return int(t)


def _parse_saved_name(name):
    """从文件名里取出 (species, conf)。认不出来就当 unknown/0.5。"""
    try:
        parts = name[:-4].split("_")          # 去掉 .jpg
        species = parts[0]
        if species not in ("cat", "dog"):
            species = "unknown"
        if len(parts) >= 3:
            v = int(parts[1])
            conf = v / 1000.0
        else:
            conf = 0.5                        # 旧格式，没有 conf
        if conf < 0.0 or conf > 1.0:
            conf = 0.5
        return species, conf
    except Exception:
        return "unknown", 0.5


def _uid_from_saved(name):
    """从文件名第 4 段取回 uid；老格式没有就返回 None。

    新格式 cat_913_12345_k230-01-89213-1-4512.jpg
    老格式 cat_913_12345.jpg            -> None（补传时退回内容指纹）
    """
    try:
        parts = name[:-4].split("_")
        if len(parts) >= 4 and parts[3] and parts[3] != "-":
            return parts[3]
    except Exception:
        pass
    return None


def _saved_key(name):
    """排序键：文件名第 3 段是 ticks，越大越新。

    不能直接按字符串排序 —— "dog_..." 会整批排在 "cat_..." 后面，
    那样 trim 删掉的就不是最旧的那张了。

    为什么固定取第 3 段而不是最后一段：新格式在 ticks 后面还有 uid
    （cat_913_12345_k230-01-...jpg），最后一段已经不是数字了。
    老格式 cat_913_12345.jpg 的第 3 段同样是 ticks，两边都适用。
    """
    parts = name[:-4].split("_")
    try:
        if len(parts) >= 3:
            return int(parts[2])
        return int(parts[-1])
    except Exception:
        return 0


# ============================================================
# 触发区判断
# ============================================================
def in_zone(box, zone=None):
    z = zone if zone else TRIGGER_ZONE
    cx = box[0] + box[2] * 0.5
    cy = box[1] + box[3] * 0.5
    return (z[0] <= cx <= z[0] + z[2]) and (z[1] <= cy <= z[1] + z[3])


def best_in_zone(dets):
    best = None
    for d in dets:
        if not in_zone(d):
            continue
        if best is None or d[5] > best[5]:
            best = d
    return best


# ============================================================
# 存在检测闸门：把逐帧检测变成稳定的"有/没有动物"
# ============================================================
def _looks_like_dets(seq):
    """【诊断用】形状上像不像"已经解好的框"。

    主程序**不再用它**（现在无条件走 postprocess，不猜）。
    保留它是给 tools/det_selftest.py 和测试用的：
    自检时要能判断"库吐出来的到底是不是框"，好把问题定位在库那一侧。
    """
    try:
        if len(seq) == 0:
            return True
        first = seq[0]
        if len(first) != 6:
            return False
        for v in first:
            float(v)
        return True
    except Exception:
        return False


def _dets_are_sane(seq, max_check=8):
    """【诊断用】验证一批"框"的数值是否合理。

    主程序只在过滤单帧结果时用到同样的规则（conf 必须在 0~1），
    完整的这套判断留给自检工具 —— 它能识破"形状像框但值是垃圾"。

    为什么需要验值：
        model.run() 返回的原始张量里，很多位置是**没写过数据的内存**。
        把 0x7F7F7F7F 按 float32 读出来就是 3.4e38 —— 正好在 FLT_MAX 附近。
        于是日志里出现 conf=2.80e+38、类别 ID 是 30 位天文数字。
        只看形状完全看不出来，只有验值才拦得住。

    合法框的约束（YOLO 后处理的常规输出格式 [x, y, w, h, cid, conf]）：
        conf  0 ~ 1        （置信度是概率，不可能超过 1）
        w, h  > 0          （宽高必须为正）
        cid   0 ~ 1000     （COCO 80 类，留足余量）
        x, y  在合理范围    （像素坐标，不该是天文数字或极负）
    """
    try:
        n = len(seq)
    except Exception:
        return False
    if n == 0:
        return True                      # 空 = 没检测到，合理
    for i in range(min(n, max_check)):
        try:
            r = seq[i]
            if len(r) != 6:
                return False
            x = float(r[0])
            y = float(r[1])
            w = float(r[2])
            h = float(r[3])
            cid = float(r[4])
            conf = float(r[5])
        except Exception:
            return False
        if not (0.0 <= conf <= 1.0):
            return False                 # 2.8e38 这类垃圾值在这里被拦下
        if w <= 0 or h <= 0:
            return False
        if cid < 0 or cid > 1000:
            return False
        if x < -4096 or y < -4096 or x > 65535 or y > 65535:
            return False
    return True


class PresenceGate(object):
    """逐帧检测 -> 一个稳定的布尔状态 + 开饭时机。

    为什么需要它（这才是"识别不准"的根源）：
        YOLO 是逐帧独立的，同一只猫在这一帧 conf=0.62、下一帧可能 conf=0.31（漏检）。
        如果直接拿单帧结果去驱动 GPIO，电平会疯狂抖动 ——
        执行板看到一堆假边沿，而且照片也全是糊的、背影的。

    两条消抖规则：
      1. **要连续 PRESENCE_ON_FRAMES 帧都检测到**才算"来了"。单帧噪声进不来。
      2. **要连续 PRESENCE_OFF_MS 毫秒都没检测到**才算"走了"。
         猫低头吃粮时头埋在盆里，经常连丢几帧，不能一丢就断电。

    到访规则（一次到访 = 一次上升沿）：
        从"没动物"变成"有动物" = 一次新到访 -> 输出一个上升沿。
        信号掉下去又升上来，如果间隔小于 VISIT_MIN_GAP_MS 就当成同一次抖动，不报新到访。
        之后一直保持"有动物" -> 电平一直高。

        注意：K230 **只负责电平**。动物一直不走时，执行板会自己每 2 分钟
        补放一轮（RE_FEED_WHILE_HIGH_MS，在 .ino 里），那属于硬件行为，
        不会在这里被记成新的到访次数。

    单独抽出来是为了能在 PC 上跑测试（见 tests/k230_trigger_sim.py）。
    """

    def __init__(self, on_frames=None, off_ms=None, min_conf=None, min_gap_ms=None):
        self.on_frames = PRESENCE_ON_FRAMES if on_frames is None else on_frames
        self.off_ms = PRESENCE_OFF_MS if off_ms is None else off_ms
        self.min_conf = PRESENCE_ON_CONF if min_conf is None else min_conf
        self.min_gap_ms = VISIT_MIN_GAP_MS if min_gap_ms is None else min_gap_ms

        self.hits = 0                  # 连续命中帧数
        self.present = False           # 对外输出的稳定状态
        self.last_seen = None          # 最后一次命中的时刻
        self.visits = 0                # 第几次到访
        self.last_feed_at = None

    def update(self, now, det):
        """返回 (present, feed_now, det)。

        det 传触发区里置信度最高的框，没有就传 None。
        present 是要写进 GPIO 的电平；feed_now 表示"这次该开饭了"。
        """
        # ---- 1. 本帧有没有合格目标 ----
        hit = det is not None and det[5] >= self.min_conf
        if hit:
            self.hits += 1
            self.last_seen = now
        else:
            self.hits = 0

        feed_now = False

        # ---- 2. 状态机 ----
        if not self.present:
            if self.hits >= self.on_frames:
                self.present = True
                self.visits += 1
                # 新到访：距上次开饭够久才开（挡住"掉了又升"的抖动）
                if (self.last_feed_at is None or
                        time.ticks_diff(now, self.last_feed_at) >= self.min_gap_ms):
                    self.last_feed_at = now
                    feed_now = True
                else:
                    log("[gate] 第 %d 次到访，离上次开饭不足 %ds，不开饭"
                        % (self.visits, self.min_gap_ms // 1000))
        else:
            # 已经"有动物"了，看是不是真的走了
            if self.last_seen is None or \
                    time.ticks_diff(now, self.last_seen) >= self.off_ms:
                self.present = False
                self.hits = 0
                log("[gate] 动物离开（保持 %.1fs 无检测）"
                    % (self.off_ms / 1000.0))

        return self.present, feed_now, det


# ============================================================
# 主程序
# ============================================================
# 开机自救：把「卡在哪个模式」记在 SD 卡上
#
# 为什么非要有文件：
#     pl.create() 卡死时进程是**活的**，只是卡在 C 层，Python 不会抛异常、
#     不会退出，所以内存里记什么都不可能被下一次运行读到。只有写到 SD 卡
#     这种断电不丢的地方，重启后才知道"上次是撞在 lcd 上"。
#     没有它，每次重启都会一头撞进同一堵墙 —— 就是要你反复拔线的原因。
# ============================================================
def _read_boot_state():
    """返回 (卡住次数, 上次卡住的模式)。没有文件就 (0, None)。"""
    try:
        f = open(BOOT_STATE_FILE, "r")
        s = f.read().strip()
        f.close()
        parts = s.split(",")
        n = int(parts[0]) if parts and parts[0].strip() else 0
        mode = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        return n, mode
    except Exception:
        return 0, None


def _write_boot_state(hangs, mode):
    try:
        f = open(BOOT_STATE_FILE, "w")
        f.write("%d,%s" % (hangs, mode))
        f.close()
    except Exception:
        pass


def _clear_boot_state():
    """启动成功才删。删了就意味着"这次没卡"，下次照常用首选模式。"""
    try:
        os.remove(BOOT_STATE_FILE)
    except Exception:
        pass


def _arm_boot_watchdog(timeout_ms, mode, flag):
    """另起线程盯着 create()，超时就重启板子。

    为什么只能是另一个线程：
        主线程正卡在 create() 里的 C 代码上，它自己没有任何机会检查超时。
        只有别的线程能在这时候做事。

    flag 是 {"ok": False}，create() 成功后主线程把它设成 True，看门狗就退出 ——
    不然它会一直数到超时，把正常运行的板子重启掉。

    返回 True 表示看门狗起来了。起不来也不算错（只是卡住时要手动重插）。
    """
    if not ENABLE_BOOT_WATCHDOG:
        return False
    try:
        import _thread
        import machine
    except Exception:
        return False

    def _wd():
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while not flag.get("ok"):
            if time.ticks_diff(time.ticks_ms(), deadline) > 0:
                log("[main] !! create() 超过 %d 秒没返回，判定卡死"
                    % (timeout_ms // 1000))
                log("[main]    卡住的模式已记在 %s，重启后会跳过它" % BOOT_STATE_FILE)
                log("[main]    现在自动重启板子（不用拔 USB）...")
                try:
                    time.sleep_ms(200)      # 让上面几行日志先发出去
                except Exception:
                    pass
                try:
                    machine.reset()
                except Exception:
                    pass
                return
            time.sleep_ms(200)

    try:
        _thread.start_new_thread(_wd, ())
        return True
    except Exception as e:
        log("[main] 看门狗线程起不来: %s（卡住时只能手动重插）" % e)
        return False


def _rearm_ai_channel(pl):
    """create() 之后重新配一次 chn2（AI 通道）。

    为什么需要这一步：
        PipeLine.create() 里会先给 chn2 设好 RGB888_PLANAR 帧，**再**去初始化
        显示屏（ST7701 / MIPI）。屏驱动初始化会重配 VI/VO，某些固件版本下会
        把 chn2 的格式和缓冲区一起冲掉 —— 于是 create() 顺利返回、屏也是亮的，
        但 get_frame() 永远返回 None，串口一直刷：

            sensor(0) snapshot chn(2) failed(3)

        日志里那行 "buffer_num 4, buffer_size 0" 就是线索：chn2 的缓冲区
        大小是 0，等于没分配。重新设一次格式 + 尺寸，让它重新申请。

    只调 sensor 的三个最标准接口，全部包在 try 里 —— 老固件没有这些参数时
    安静跳过，绝不影响主流程（主程序本来就有 chn2 失败的排查提示）。

    返回 True 表示至少成功调用了 set_framesize 或 set_pixformat。
    """
    try:
        import sensor as _sensor
    except Exception:
        return False

    target = getattr(pl, "sensor", None) or _sensor

    # chn2 的常量在不同固件里位置不一样，挨个找
    chn = None
    for modname in ("media.sensor", "sensor"):
        try:
            mod = __import__(modname, None, None, ["CAM_CHN_ID_2"])
            chn = getattr(mod, "CAM_CHN_ID_2", None)
            if chn is not None:
                break
        except Exception:
            continue
    if chn is None:
        chn = 2                      # 这个固件直接接受整数 2

    done = []
    w, h = RGB888P_SIZE
    # 顺序：先尺寸后格式。反过来的话有些固件会用旧尺寸分配缓冲。
    try:
        target.set_framesize(width=w, height=h, chn=chn)
        done.append("framesize")
    except Exception as e:
        log("[main]   chn2 set_framesize 跳过: %s" % e)
    try:
        fmt = getattr(target, "RGB888_PLANAR", None)
        if fmt is None:
            fmt = getattr(_sensor, "RGB888_PLANAR", None)
        if fmt is not None:
            target.set_pixformat(fmt, chn=chn)
            done.append("pixformat")
    except Exception as e:
        log("[main]   chn2 set_pixformat 跳过: %s" % e)

    if done:
        log("[main]   已重新配置 AI 通道 chn%d: %s" % (chn, "+".join(done)))
        return True
    return False


class SnapChannel(object):
    """专用的"拍照通道"（chn1，打包 RGB888）。

    为什么值得多占一路缓冲：
        PipeLine 给 AI 用的 chn2 是 **RGB888_PLANAR**（平面格式）——
        这是官方文档明写的。而把平面数据当成打包 RGB888 去建 image.Image，
        得到的正是**一片灰**（社区里叫"灰色的九宫格"）。

        网上验证过的解法不是"把平面数据硬转成打包格式"，而是**再加一路
        打包格式的相机通道**专门用来存图：

            sensor.set_framesize(w, h, chn=1)
            sensor.set_pixformat(Sensor.RGB888, chn=1)   # 打包，不是 PLANAR
            img = sensor.snapshot(chn=1)                 # 直接就是 image.Image
            img.compress()                               # 能正常出 JPEG

        这样拍照跟 AI 推理各用各的通道，互不影响。

    保守起见全程"不成功就退":
        - 配不出来（内存不够 / 老固件没这个常量）-> ok=False，永久走老路；
        - 拍出来是纯色（说明这路通道也没数据）-> ok=False，永久走老路。

    老路指的是 _frame_to_image 那条带像素校验的转换，两条互为兜底。
    """

    def __init__(self):
        self.sensor = None
        self.chn = None
        self.ok = None          # None=还没试过, True=可用, False=放弃
        self.why = ""

    # ---------------- 配置 ----------------
    def setup(self, pl):
        if not SNAP_CHANNEL:
            self.ok = False
            self.why = "SNAP_CHANNEL=False（配置里关掉了）"
            return False
        target = getattr(pl, "sensor", None)
        # 已经试过、而且是同一个 sensor -> 直接用上次的结论
        if self.ok is not None and (self.ok is False or self.sensor is target):
            return self.ok
        # sensor 换了（PipeLine 换显示模式重建过）-> 在新对象上重新配
        self.sensor = None
        self.ok = None
        if target is None:
            self.ok = False
            self.why = "PipeLine 没有 sensor 属性"
            return False

        chn = None
        for modname in ("media.sensor", "sensor"):
            try:
                mod = __import__(modname, None, None, ["CAM_CHN_ID_1"])
                chn = getattr(mod, "CAM_CHN_ID_1", None)
                if chn is not None:
                    break
            except Exception:
                continue
        if chn is None:
            chn = 1

        fmt = None
        for holder in (target,):
            fmt = getattr(holder, "RGB888", None)
            if fmt is not None:
                break
        if fmt is None:
            try:
                import sensor as _s
                fmt = getattr(_s, "RGB888", None)
            except Exception:
                fmt = None
        if fmt is None:
            self.ok = False
            self.why = "这个固件没有 RGB888 常量"
            return False

        w, h = RGB888P_SIZE
        done = []
        # 先尺寸后格式，理由同 _rearm_ai_channel
        try:
            target.set_framesize(width=w, height=h, chn=chn)
            done.append("framesize")
        except Exception as e:
            self.ok = False
            self.why = "set_framesize 失败: %s" % e
            log("[up] 拍照通道配不上（%s），走帧转换那条路" % self.why)
            return False
        try:
            target.set_pixformat(fmt, chn=chn)
            done.append("pixformat")
        except Exception as e:
            self.ok = False
            self.why = "set_pixformat 失败: %s" % e
            log("[up] 拍照通道配不上（%s），走帧转换那条路" % self.why)
            return False

        self.sensor = target
        self.chn = chn
        self.ok = True
        log("[up] 拍照走专用通道 chn%s（打包 RGB888 %dx%d）" % (chn, w, h))
        return True

    # ---------------- 取一张 ----------------
    def grab(self):
        if self.ok is not True:
            return None
        snap = None
        try:
            snap = self.sensor.snapshot(chn=self.chn)
        except Exception as e:
            self._give_up("snapshot 报错: %s" % e)
            return None
        if snap is None:
            self._give_up("snapshot 返回 None")
            return None
        if hasattr(snap, "compress") and self._looks_flat(snap):
            self._give_up("拍出来是纯色（这路通道没数据）")
            return None
        return snap

    def _looks_flat(self, im):
        """抽 9 个点看看是不是一片纯色。纯色 = 这路通道其实没出帧。"""
        gp = getattr(im, "get_pixel", None)
        if gp is None:
            return False
        try:
            w, h = int(im.width()), int(im.height())
        except Exception:
            return False
        lo = 255
        hi = 0
        for (cx, cy) in _SAMPLE_GRID:
            x = min(w - 1, max(0, w * cx // 8))
            y = min(h - 1, max(0, h * cy // 8))
            try:
                p = gp(x, y)
                v = (int(p[0]) + int(p[1]) + int(p[2])) // 3
            except Exception:
                return False
            lo = min(lo, v)
            hi = max(hi, v)
        return (hi - lo) < 10

    def _give_up(self, why):
        self.ok = False
        self.why = why
        log("[up] 拍照通道停用（%s），改走帧转换那条路" % why)


_SNAP = SnapChannel()


def _preclean_media():
    """尽量清掉上一次运行残留的相机/显示状态。

    为什么需要：
        在 CanMV IDE 里"停止 -> 再运行"**不会**清 C 层的媒体状态，上一次的
        VB 缓冲池和通道还挂着。这时 create() 就会卡在分配缓冲那一步 ——
        现场表现正是日志停在 "vb common pool count 6" 后一片空白。
        拔电重上电最干净，但总不能每次调试都去拔线。

    只调最安全的两个：sensor.stop() 和 display.deinit()。都没有就算了，
    绝不因为这一步失败而中断启动（PipeLine 自己会重新初始化它们）。
    """
    done = []
    for modname, fnname in (("sensor", "stop"), ("display", "deinit")):
        try:
            mod = __import__(modname)
            fn = getattr(mod, fnname, None)
            if fn is not None:
                fn()
                done.append("%s.%s()" % (modname, fnname))
        except Exception:
            pass
    gc.collect()
    if done:
        log("[main] 已清理上次残留: %s" % ", ".join(done))
    return done


def _open_pipeline():
    """初始化 PipeLine，逐行打日志，失败/卡死自动换显示模式。

    为什么要逐行打日志：
        pl.create() 里做的事是「找 sensor -> 分配 VB 缓冲池 -> 初始化显示屏」，
        全部在 C 层。卡住的时候 Python 那边**不会抛异常**，就那么停住 ——
        现场表现就是日志停在 "vb common pool count 6" 后面一片空白，
        看起来像死机，其实只是不知道卡在哪一步。

    为什么要自动换模式：
        mode="lcd" 要真的驱动 ST7701 那块屏。屏没接好 / 排线松 / 分辨率填错，
        就会卡在屏初始化。这时候 mode="virt"（不点屏，只走 IDE 显示）能救回来，
        至少能确认"相机是好的，问题在屏"。

    为什么还要记文件 + 看门狗：
        "失败就换"只对**抛异常**的失败有用。卡死是另一种失败：循环压根轮不到
        下一轮。所以开跑前先把模式写进文件，卡死重启后跳过它；同时用看门狗
        线程把"永久卡住"变成"45 秒后自动重启并换模式"。

    返回 (pl, 实际用的 mode)。全部失败才抛异常。
    """
    from libs.PipeLine import PipeLine

    # 先把上一次运行残留的相机/屏状态清掉，再开新的。
    # 这一步能救掉"反复在 IDE 里运行导致卡在 vb 缓冲池"的情况。
    _preclean_media()

    first = DISPLAY_MODE if ENABLE_DISPLAY else "virt"
    base = [first]
    for m in ("virt", "lcd", "auto"):
        if m not in base:
            base.append(m)

    # ---- 上次是不是卡死了 ----
    hangs, hung_mode = _read_boot_state()
    if hung_mode and hangs > 0:
        log("[main] 上次启动卡在 mode=%s 的 create() 里（累计 %d 次）"
            % (hung_mode, hangs))
        try:
            idx = base.index(hung_mode)
        except ValueError:
            idx = -1
        if idx >= 0:
            # 从"撞墙的那个"之后开始试，别一上来就再撞一次
            modes = base[idx + 1:] + base[:idx + 1]
        else:
            modes = list(base)
        log("[main] 这次先从 mode=%s 开始" % modes[0])
    else:
        modes = list(base)

    last = None
    for i, m in enumerate(modes):
        log("[main] 初始化 PipeLine   mode=%s  AI帧 %dx%d  屏幕 %dx%d"
            % (m, RGB888P_SIZE[0], RGB888P_SIZE[1],
               DISPLAY_SIZE[0], DISPLAY_SIZE[1]))

        # 先记下来再 create()。顺序不能反 —— 卡死时这行已经执行过，
        # 重启后才有据可查。次数 +1 记的是"这次尝试"。
        _write_boot_state(hangs + 1, m)

        flag = {"ok": False}
        _arm_boot_watchdog(PIPELINE_TIMEOUT_MS, m, flag)

        pl = None
        try:
            pl = PipeLine(rgb888p_size=list(RGB888P_SIZE),
                          display_size=list(DISPLAY_SIZE),
                          display_mode=m)
            log("[main]   对象已创建，开始 create()（卡住就会自动重启）...")
            pl.create()
            flag["ok"] = True                 # 告诉看门狗：别重启
            log("[main]   create() 返回了")
            # 屏初始化可能把 chn2 冲掉 —— 重新配一次，否则 get_frame() 永远 None
            _rearm_ai_channel(pl)
            # 再配一路打包 RGB888 的通道专门拍照 —— AI 那路是平面格式，
            # 直接拿去压缩会得到一片灰图。配不上就自动退回帧转换，不碍事。
            _SNAP.setup(pl)
            _clear_boot_state()               # 这一轮通了，把卡死记录清掉
            return pl, m
        except Exception as e:
            flag["ok"] = True                 # 抛异常了，看门狗别再管
            last = e
            log("[main]   mode=%s 失败: %s" % (m, e))
            # 失败后一定要 destroy，否则残留的缓冲池/通道会让下一轮也失败
            try:
                if pl is not None:
                    pl.destroy()
            except Exception:
                pass
            gc.collect()

    log("[main] !! 所有显示模式都失败了，最后一次错误: %s" % last)
    log("         排查顺序：")
    log("           1) 拔掉 USB 重新上电（上电最干净，能清掉残留的相机/屏状态）")
    log("           2) 屏的排线重新插紧；不接屏就把 DISPLAY_MODE 改成 \"virt\"")
    log("           3) 跑 tools/camera_probe.py 逐组试")
    raise OSError("PipeLine 无法初始化: %s" % last)


def main():
    log("=" * 56)
    log(" K230 流浪猫狗识别 · 持续电平版")
    log(" 信号引脚 GPIO%d（亚博 12pin 排针第 %d 脚）"
        % (TRIG_PIN, HEADER_PIN.get(TRIG_PIN, 0)))
    log("   检测到猫狗 -> 一直高电平；连续 %.1fs 没检测到 -> 拉低"
        % (PRESENCE_OFF_MS / 1000.0))
    log("   消抖: 连续 %d 帧 conf>=%.2f 才算来了"
        % (PRESENCE_ON_FRAMES, PRESENCE_ON_CONF))
    log("   补粮节奏由执行板自己控制（一直高电平就定时补），K230 不管")
    log("   拍照上传: 每 %d 秒最多 1 张" % (UPLOAD_INTERVAL_MS // 1000))
    log("=" * 56)

    # ---- 摄像头 ----
    # PipeLine 不只是"显示"，它同时也是取帧的来源。
    # 所以不能因为不想要屏幕就跳过它 —— 跳过之后 pl 是 None，
    # 主循环里 pl.get_frame() 会直接 AttributeError 崩掉。
    # 不要屏幕的正确做法是 display_mode="virt"。

    # 宽度必须 16 对齐（sensor 会自动对齐，对齐后实际尺寸和你写的不一样）
    if RGB888P_SIZE[0] % 16 != 0:
        log("[main] !! RGB888P_SIZE 宽度 %d 不是 16 的倍数，会被自动对齐，"
            "detector 的框可能和触发区错位" % RGB888P_SIZE[0])

    pl, mode = _open_pipeline()
    try:
        real = pl.get_display_size()
    except Exception:
        real = DISPLAY_SIZE
    log("[main] 摄像头就绪  AI帧 %dx%d  屏幕实测 %sx%s  mode=%s"
        % (RGB888P_SIZE[0], RGB888P_SIZE[1], real[0], real[1], mode))
    if mode == "lcd":
        log("[main] 注意：mode=lcd 时画面只上板载屏，CanMV IDE 右边那块预览是**黑的**")
        log("         这不是故障。想在 IDE 里看画面就把 DISPLAY_MODE 改成 \"virt\"")

    # ---- 触发引脚 ----
    trig = Trigger(TRIG_PIN, BUSY_PIN)

    # ---- 检测 ----
    detector = build_detector()
    uploader = Uploader()

    # ---- 状态 ----
    gate = PresenceGate()

    def on_feed(det):
        """只在"新的一次到访"时调用一次：把信号线拉高，让执行板开饭。"""
        kind = "猫" if int(det[4]) == CLASS_CAT else "狗"
        log("")
        log("========== 第 %d 次到访 -> 拉高电平（%s conf=%.2f）=========="
            % (gate.visits, kind, det[5]))
        log("    执行板会跑一轮；它不退走的话，执行板自己每 2 分钟补放一次")
        # 电平的上升沿本身就是"动物来了"的信号。这里不 sleep ——
        # 主循环要继续跑，动物在就一直保持着高电平。
        trig.set_level(True)

    def on_leave():
        log("[main] 动物离开 -> 信号线拉低，执行板停止补放")

    last_upload = None
    last_busy = False
    shooter = VisitShooter()      # 到访 -> 拍照 的规则（可单测）

    def maybe_upload(now, det, force=False):
        """给这次到访拍一张上传。

        UPLOAD_PER_VISIT=True（默认）：每次"到访"拍一张，和 gate.visits 对齐。
        串口上的"第 N 次到访" 与 网页第 N 条记录一一对应 —— 这就是
        "三边数据对得上"的关键。

        UPLOAD_PER_VISIT=False：退回旧的墙上时钟节流（每 UPLOAD_INTERVAL_MS 一张）。
        那个模式下网上条数一定少于串口次数，是设计使然。
        """
        nonlocal last_upload
        if not SERVE_UPLOAD or UPLOAD_MODE != "wifi":
            return
        if not force and not UPLOAD_PER_VISIT:
            if last_upload is not None and \
                    time.ticks_diff(now, last_upload) < UPLOAD_INTERVAL_MS:
                return
        last_upload = now
        log("[up] 第 %d 次到访 -> 拍照上传（本次开机第 %d 张）"
            % (gate.visits, shooter.shots))
        try:
            uploader.send(img, det, visit=gate.visits)
        except Exception as e:
            log("[main] 上传异常（不影响放饭）: %s" % e)

    W, H = RGB888P_SIZE
    # create() 之后 display_size 会被屏驱动改成实际分辨率（官方文档明确说明），
    # 所以必须取运行时的真实值再算缩放，否则检测框/触发区会整体偏位。
    DW, DH = DISPLAY_SIZE
    try:
        real = pl.get_display_size()
        if real and len(real) == 2:
            DW, DH = int(real[0]), int(real[1])
    except Exception:
        pass
    if (DW, DH) != tuple(DISPLAY_SIZE):
        log("[main] 注意：屏幕实际 %dx%d，配置里写的是 %dx%d —— 已按实际值缩放"
            % (DW, DH, DISPLAY_SIZE[0], DISPLAY_SIZE[1]))
    sx, sy = float(DW) / W, float(DH) / H

    badFrames = 0
    frameChecked = False
    # 心跳用的计数
    lastBeat = time.ticks_ms()
    beatStart = lastBeat
    beatFrames = 0
    lastDump = lastBeat
    # 补传的节奏。放饭时帧率要稳，所以不每帧都去翻 SD 卡 ——
    # 每 BACKFILL_PERIOD_MS 才看一次，而且一次只补 BACKFILL_BATCH 张。
    lastBackfill = time.ticks_ms()

    try:
        while True:
            img = pl.get_frame()
            if img is None:
                # chn2 出不了帧时 get_frame() 会一直返回 None，而串口在刷
                # "sensor(0) snapshot chn(2) failed(3)"。
                # 原样 continue 的话程序会静默空转，看起来像死机 ——
                # 所以攒到一定次数就把排查清单打出来（只打一次）。
                badFrames += 1
                if badFrames == 60:
                    log("")
                    log("!! 连续 %d 次取不到画面：相机 chn2 没出帧。" % badFrames)
                    log("   串口上面那行 snapshot chn(2) failed 说的就是这个。")
                    # 再配一次 chn2。create() 时配过一次，但那时模型还没加载、
                    # WiFi 还没连上；内存布局变了之后 VB 缓冲区可能已经失效。
                    # 重新设一次格式+尺寸会重新申请，成本很低。
                    if _rearm_ai_channel(pl):
                        log("   已在模型/WiFi 就绪后重新配置 chn2，再看下面有没有框")
                    log("   还是不行就按顺序试，每改一项都要**断电重上电**：")
                    log("     1) 断电重上电（IDE 的 interrupt 不释放相机，重跑常会这样）")
                    log("     2) DISPLAY_MODE = \"virt\"     先排除屏幕")
                    log("     3) RGB888P_SIZE = (640, 360)  再排除内存")
                    log("     4) 摄像头排线插到底、卡扣扣好")
                    log("   也可以跑 tools/camera_probe.py 逐个组合试。")
                    log("")
                time.sleep_ms(5)
                continue
            badFrames = 0
            beatFrames += 1
            # 第一次拿到帧时确认一下 API 形态，并把画布准备好。
            # 现状（CanMV 官方文档 + 亚博 v1.4.3 实测）：
            #   get_frame() 返回 ulab.numpy.ndarray，shape=[3,H,W] 的 CHW 数据，
            #   这是**正常设计**，不是固件太老或太新。
            #   画框必须画在 pl.osd_img（ARGB8888 图层）上，拍照则要先转成
            #   image.Image（见 Uploader.send 里的 _frame_to_image）。
            if not frameChecked:
                frameChecked = True
                if hasattr(img, "draw_rectangle"):
                    log("[main] 帧类型：image 对象（老接口）")
                else:
                    shape = getattr(img, "shape", "?")
                    log("[main] 帧类型：ndarray %s（CHW，正常）" % (shape,))
                    log("         画框 -> pl.osd_img 图层；拍照 -> 自动转 image.Image")

            now = time.ticks_ms()

            # 检测必须包起来。
            # 为什么：model.run() 里任何一步抛异常（比如后处理函数在固件里不存在、
            # 或者输入尺寸和 kmodel 对不上）都会**一路抛出去把整个 while 打断** ——
            # 表现就是屏幕定格在最后一帧、串口不再有任何输出，看起来像"卡住"，
            # 其实是程序已经退出了。这里兜住，并且限频打印，程序继续跑。
            try:
                dets = detector.detect(img)
                detOkay = True
            except Exception as e:
                dets = []
                detOkay = False
                if not getattr(detector, "_warned_run", False):
                    detector._warned_run = True
                    log("")
                    log("!! 检测异常，已跳过这一帧（程序继续跑）: %s" % e)
                    log("   连续出现的话，跑 tools/det_selftest.py 定位是")
                    log("   加载/推理/后处理哪一步的问题。")
                    log("")
            det = best_in_zone(dets) if detOkay else None

            # ---- 存在判定 + 开饭 ----
            # 状态机负责消抖：单帧漏检不会让电平抖，连续漏 2 秒才算走。
            was_present = gate.present
            present, feed_now, det = gate.update(now, det)

            # 电平跟着"有动物"走。这就是"检测到就一直高电平"。
            trig.set_level(present)

            if not was_present and present:
                log("[gate] 检测到动物 -> 信号线拉高")
                # 每次"到访"（不管开不开饭）都留一张照，这样串口的次数
                # 和网页的条数才是同一个数。
                shooter.on_arrive()
            elif was_present and not present:
                if shooter.pending:
                    # 这次到访一行框都没抓到（消抖期间全漏检），就明确说出来，
                    # 别让它挂到下一次到访头上 —— 那才是真的"照片对不上"。
                    log("[up] 第 %d 次到访没抓到框，这次不拍照" % gate.visits)
                shooter.on_leave()
                on_leave()

            if feed_now:
                on_feed(det)

            # ---- 心跳日志 ----
            # 为什么要有它：出问题时分不清"画面死了"、"检测停了"、"只是没猫"。
            # 每 HEARTBEAT_MS 打一行，把这三件事一起说清楚：
            #   fps  低  -> 相机/推理卡住
            #   det  永远 0 且有 seen -> 模型在看，但类别/区域不对
            #   seen 空 -> 画面里真的什么都没有（或者输入是全黑）
            if time.ticks_diff(now, lastBeat) >= HEARTBEAT_MS:
                lastBeat = now
                fps = 0.0
                if beatFrames and now != beatStart:
                    fps = beatFrames * 1000.0 / max(1, time.ticks_diff(now, beatStart))
                seen = ""
                try:
                    items = sorted(detector.seen_total.items(),
                                   key=lambda kv: -kv[1])[:3]
                    if items:
                        seen = "  看到:" + ",".join(
                            "%s(%.2f)" % (LABELS[c] if 0 <= c < len(LABELS) else "?%d" % c, v)
                            for c, v in items)
                except Exception:
                    seen = ""
                log("[beat] %.1f fps  框=%d  确认=%d  次=%d  拍=%d%s"
                    % (fps, len(dets), 1 if present else 0, gate.visits,
                       shooter.shots, seen))
                # 心跳报完就清空，这样"看到:"反映的是**最近这 5 秒**，
                # 而不是开机以来看过的一切 —— 否则猫走了半天还在报，反而误导。
                detector.seen_total = {}
                beatStart = now
                beatFrames = 0

                # ---- 取证：把这一帧存成 jpg ----
                # 只为了排查用，所以放在心跳里（本来就每 5 秒才进一次），
                # 且单独 try，存图失败绝不影响主循环。
                if DEBUG_DUMP_MS > 0 and \
                        time.ticks_diff(now, lastDump) >= DEBUG_DUMP_MS:
                    lastDump = now
                    _dump_frame(img, pl, now)

            # ---- 可选的 BUSY 反馈 ----
            # 接了这个脚，就能知道执行板**真的跑完了一轮**。
            # 不接也一切正常（信号语义不依赖它），所以这里只记日志。
            if trig.has_busy:
                busy_now = trig.is_busy()
                if last_busy and not busy_now:
                    log("[main] 执行板跑完一轮（BUSY 回落）")
                elif busy_now and not last_busy:
                    log("[main] 执行板开始跑（BUSY 拉高）")
                last_busy = busy_now

            # ---- 拍照上传 ----
            # 规则在 VisitShooter 里（可单测）：每次到访恰好一张。
            if shooter.tick(det, present):
                maybe_upload(now, det, force=UPLOAD_PER_VISIT)

            # ---- 补传之前失败的图 ----
            # 后台线程在没活干的时候会自己补传，所以这里只在**没有后台线程**
            # （老固件）时才在主循环里做。有线程时绝不在这里发包 ——
            # 一发包主循环就可能卡几十秒，检测和放饭全停。
            if not uploader._worker and \
                    time.ticks_diff(now, lastBackfill) >= BACKFILL_PERIOD_MS:
                lastBackfill = now
                try:
                    uploader.retry_pending(BACKFILL_BATCH)
                except Exception as e:
                    log("[main] 补传异常（忽略）: %s" % e)

            # ---- 画面 ----
            # 优先用库自带的 draw_result()：坐标换算是库内部做的，
            # 我们不用管 letterbox 的偏移和缩放，也就不会画歪。
            # 只有在库没有这个方法时才退回手动画（那块代码保留是为了兼容）。
            try:
                canvas = getattr(pl, "osd_img", None)
                if canvas is None:
                    if not getattr(uploader, "_warned_osd", False):
                        uploader._warned_osd = True
                        log("[main] !! 没有 pl.osd_img 图层，屏幕上看不到框"
                            "（检测和放饭不受影响）")
                else:
                    canvas.clear()
                    drew = False
                    dr = getattr(detector.model, "draw_result", None)
                    if dr is not None and dets:
                        try:
                            dr(dets, canvas)
                            drew = True
                        except Exception:
                            drew = False
                    # 一个框都没解出来，但 run() 有东西：走**官方例程原样**的
                    # 写法，把 run() 的原始返回值直接交给 draw_result。
                    #   屏幕出现框 -> 模型和输入都没问题，卡在 postprocess 解析
                    #   屏幕一片空 -> 问题在模型/输入，跟后处理无关
                    # 这一步只画不判断，不影响 GPIO，纯诊断。
                    if not drew and dr is not None \
                            and getattr(detector, "results", None):
                        try:
                            dr(detector.results, canvas)
                            if not getattr(uploader, "_noted_raw", False):
                                uploader._noted_raw = True
                                log("[main] 用官方写法 draw_result(run 返回值) "
                                    "画了一次；屏幕上有框就说明模型正常，"
                                    "问题只在 postprocess 解析")
                        except Exception:
                            pass
                    if not drew:
                        for d in dets:
                            canvas.draw_rectangle(int(d[0] * sx), int(d[1] * sy),
                                                  int(d[2] * sx), int(d[3] * sy),
                                                  color=(0, 255, 0), thickness=2)
                            if det is not None and d is det:
                                canvas.draw_rectangle(
                                    int(d[0] * sx), int(d[1] * sy),
                                    int(d[2] * sx), int(d[3] * sy),
                                    color=(255, 255, 0), thickness=3)
                    z = TRIGGER_ZONE
                    canvas.draw_rectangle(int(z[0] * sx), int(z[1] * sy),
                                          int(z[2] * sx), int(z[3] * sy),
                                          color=(0, 120, 255), thickness=1)
                    canvas.draw_string_advanced(
                        8, 6, 24,
                        "GPIO%d=%s V%d %s"
                        % (TRIG_PIN, "H" if present else "L", gate.visits,
                           ("%.2f" % det[5]) if det is not None else "-"),
                        color=(255, 255, 0))
                    pl.show_image()
            except Exception as e:
                if not getattr(uploader, "_warned_draw", False):
                    uploader._warned_draw = True
                    log("[main] !! 画框异常（不影响识别和放饭）: %s" % e)

            # ---- 每帧回收一次（这一步不能省）----
            # 现场症状：跑十几秒后报 "ndarray malloc fail"，然后检测就废了。
            # 原因：libs/YOLO 每帧都会把 4 个输出张量 to_numpy() 成 ulab 数组
            # （144x40x40 + 144x20x20 + ... 加起来约 1.2MB），再加上后处理里的
            # transpose() 又要一份。MicroPython 的 gc 是"需要时才跑"，堆被这些
            # 临时大数组切碎后，下一次分配就失败 —— 而失败发生在 C 层，
            # 报的就是 ndarray malloc fail。
            # 官方 PipeLine 示例的循环里就是每帧 gc.collect()，照做。
            gc.collect()

    except KeyboardInterrupt:
        log("[main] 手动停止")
    finally:
        try:
            trig.force_low()    # 退出时别把信号线留在高电平
        except Exception:
            pass
        try:
            detector.deinit()
        except Exception:
            pass
        try:
            if pl is not None:
                pl.destroy()
        except Exception:
            pass
        log("[main] 已退出，共开饭 %d 次，上传成功 %d / 失败 %d"
            % (gate.visits, uploader.ok, uploader.fail))


if __name__ == "__main__":
    main()
