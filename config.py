# -*- coding: utf-8 -*-
"""
K230 流浪猫狗识别 · 自动放饭  —— 视觉配置（K230 侧）

分工（**不用串口**）：
    K230（摄像头 + 识别 + 拍照上传）-- GPIO 拉高 800ms --> 执行板（Nano）
    执行板：2 个舵机 + 42 步进电机(L298N) + 盆口 HC-SR04 超声测距，
            收到一个高电平脉冲就跑完整一轮。

所以这一份文件里**只有**视觉/网络参数；
    触发引脚和时序在 k230_trigger.py 顶部，
    舵机角度、步进行程、60s 保持时间这些机械参数在
arduino/simple_trigger/simple_trigger.ino 里。

一轮完整动作（执行板本地执行，K230 只给一个脉冲）：
    检测到猫/狗 -> 拍照上传到 PC -> GPIO42 拉高 800ms
      -> 执行板用超声波测距看盆里有没有粮
           有粮：舵机1 90->0 开门 -> 步进前进 -> 停 60s -> 步进退回 -> 舵机1 0->90 关门
           无粮：舵机2 90->0 出粮 10s -> 0->90 关 -> 舵机1 开门 -> 步进前进
                 -> 停 60s -> 步进退回 -> 舵机1 关门
"""

# ============================================================
# 0. 运行模式
# ============================================================
SIMULATE_DETECTION = False   # True: 不加载 kmodel，用假检测框调状态机
ENABLE_DISPLAY = True        # False: 不初始化屏幕（省内存、跑得更快）
VERBOSE = True               # 串口打印每一步

# 没接执行板时也能跑（只发命令不等待完成），调试视觉用
REQUIRE_MCU = True           # True: 执行板不回话就算错误，中止本轮
# 兼容旧名字
REQUIRE_NANO = REQUIRE_MCU

DEVICE_ID = "k230-01"        # 多台设备时区分，PC 端会记下来

# ============================================================
# 1. 模型与相机
# ============================================================
# ---- 情况 A：先用 COCO 预训练模型（当前配置）----
# COCO 里 cat=15, dog=16。**亚博/CanMV 出厂固件自带这个模型**，不用自己训练，
# 也不用往卡上拷任何东西 —— 它就在 /sdcard/kmodel/ 里。
# 实测 CanMV v1.4.3 k230_canmv_yahboom：加载正常，配合 libs.YOLO.YOLOv8 可用。
#
# ⚠️ 目录是 kmodel，不是 models。以前默认写的 /sdcard/models/ 在出厂卡上
#    不存在，于是启动就报 "Kmodel file not exist."。
KMODEL_PATH = "/sdcard/kmodel/yolov8n_320.kmodel"

# 万一上面这个不在，按顺序再试（自定模型放哪都行，加进这个元组即可）
KMODEL_FALLBACKS = (
    "/sdcard/kmodel/yolov8n_320.kmodel",
    "/sdcard/examples/kmodel/yolov8n_320.kmodel",
    "/sdcard/models/yolov8n_320.kmodel",
)

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
CLASS_CAT = 15
CLASS_DOG = 16

# ---- 情况 B：以后换成自己训的两类模型 ----
#   LABELS = ["cat", "dog"]
#   CLASS_CAT = 0
#   CLASS_DOG = 1

# 只保留这两类，其余 78 类(人/车/椅子...)直接丢弃，省得计数器乱记
KEEP_CLASSES = (CLASS_CAT, CLASS_DOG)

CONF_THRESH = 0.40
NMS_THRESH = 0.45

# 管道取帧分辨率（检测框坐标 / ROI 坐标都用这个坐标系）
#
# 亚博 K230 实机参数：GC2093 摄像头 + 2.4 寸 MIPI 屏 640x480。
#   - DISPLAY_SIZE 必须等于屏幕真实分辨率
#   - RGB888P_SIZE 要 <= 摄像头输出、宽度 16 对齐、且与屏幕同比例(4:3)
# 原来写的 800x480 这块屏没有；1280x720 是 16:9，和 4:3 的屏不同比例，
# 画面会被压扁、检测框也会算偏，而且 RGBP888 一路 2.7MB 容易把 VB 内存撑爆。
RGB888P_SIZE = (640, 480)
DISPLAY_SIZE = (640, 480)
DISPLAY_MODE = "lcd"         # "lcd"(ST7701) / "hdmi"(LT9611) / "virt" / "auto"

# ============================================================
# 2. 触发（K230 -> 执行板，单根 GPIO）
# ============================================================
# 本方案**不用串口**：K230 检测到猫/狗的瞬间把一根 GPIO 拉高，执行板收到
# 就自己跑完一整轮（读盆内传感器 -> 出粮/开门 -> 推盆 -> 保持 60s -> 退回 -> 关门）。
#
# 触发引脚和拉高时长在 k230_trigger.py 顶部（TRIG_PIN / TRIG_PULSE_MS），
# 因为本文件只服务于视觉部分。引脚核对表见 README 2.1 和 k230_trigger.py 注释：
#   亚博 K230 只引出 7 个 GPIO：42/43/33/32/26/34/35（散热片旁 12pin 排针）
#   默认用 GPIO42（排针第 1 脚），默认就是普通 GPIO，不带任何总线
#   **GPIO26 不要用**：它是 MMC1_CLK，SDIO WiFi 的时钟线，占掉就没法传图了

# ============================================================
# 3. 时序（K230 侧判断用）
# ============================================================
T_HOLD_MS     = 60000        # 饭盆推出去后保持多久（= 给动物吃的时间，需求固定 60s）
T_HOPPER_MS   = 10000        # 盆里没粮时，舵机2 出粮口开多久（需求固定 10s）
T_REARM_MS    = 3000         # 动物必须离开触发区 3s 以上，才允许再次触发（防止一只动物算多次）
T_COOLDOWN_MS = 15000        # 一次放饭结束后冷却，防止紧接着又开一轮

# ============================================================
# 4. 视觉区域（rgb888p 坐标系，单位像素）
# ============================================================
# 触发区：检测框中心落在这里才开饭。默认全画面，建议缩小到"饭盆门口"那块
TRIGGER_ZONE = (0, 0, 1280, 720)

# 拍照时的裁剪框外扩比例（0.15 = 四周各扩 15%，让整只动物进画面，便于 PC 认个体）
CROP_PAD = 0.15
# 太小的框不传（远处一个像素大的猫认不出个体，也浪费流量）
MIN_BOX_W = 80
MIN_BOX_H = 80

# 饭盆 ROI：只在显示上画个参考框，判断"有没有粮"已经改用超声波测距了
# ⚠️ 单位是 RGB888P_SIZE 的像素（现在 640x480）。下面是按老分辨率大致折算的，
#    要真用就先跑 tools/vision_probe.py 重新标一次。
BOWL_ROI = (235, 267, 170, 160)

# ============================================================
# 5. 计数去重（K230 本地计数，主要用于屏幕显示）
# ============================================================
# 真正的"每只猫每只狗来了几次、什么时间段"由 PC 端按个体统计，见 pc/
CONFIRM_HITS = 3             # 连续命中多少帧才算"来了一个新目标"
MAX_MISSING  = 15            # 连续多少帧跟丢就删除轨迹
IOU_THRESH   = 0.30          # 帧间匹配 IoU 阈值
CENTER_MATCH_RATIO = 0.60    # IoU 匹配失败时，中心距离小于 "0.6 x 目标尺寸" 也算同一只
REJOIN_MS    = 4000          # 跟丢后多久内又原地出现，视为同一只，不重复计数

# ============================================================
# 6. 拍照上传到 PC   ⚠️ 这一段已经不被 k230_trigger.py 使用了
# ============================================================
# 真正上线跑的是 k230_trigger.py，它的配置**全部写在自己文件开头**，
# 不读这里的任何值。改这里的 SERVER_URL 不会有任何效果 —— 别踩这个坑。
# 这里保留下来只是为了下面的 food_vision.py / tools/vision_probe.py 还能单独跑。
#
# 要改上传地址 / token / 超时，请改 k230_trigger.py 开头的：
#     SERVER_URL / API_TOKEN / UPLOAD_TIMEOUT_S / UPLOAD_RETRIES
UPLOAD_MODE = "wifi"         # "wifi" = 直接 POST 到 PC；"sd" = 只存 SD 卡(离线攒着)；"off" = 不上传

WIFI_ENABLE = True           # K230 板载 WiFi（K230 是 WiFi6，CanMV 有 network 模块）
WIFI_SSID   = "YOUR_WIFI_SSID"
WIFI_PASS   = "YOUR_WIFI_PASSWORD"
WIFI_TIMEOUT_MS = 15000      # 连不上就别一直卡着，先放饭

# PC 服务端地址：填你 PC 的局域网 IP（Windows 上 ipconfig 看 IPv4）
SERVER_HOST = "192.168.1.100"
SERVER_PORT = 8000
SERVER_URL  = "http://%s:%d/api/v1/visit" % (SERVER_HOST, SERVER_PORT)

UPLOAD_TIMEOUT_S = 2         # 单次 HTTP 超时，失败就进本地队列，不阻塞放饭
JPEG_QUALITY     = 82
UPLOAD_RETRY_MAX = 1         # 单次上传重试次数（最坏 2×(2s+0.2s) ≈ 4.4s，别拖住主循环）

QUEUE_DIR     = "/sdcard/upload_queue"   # 上传失败的图攒在这里，联网后补传
QUEUE_MAX     = 200          # 队列上限，超了丢最旧的（防撑爆 SD）

# 上传时间戳：优先用 RTC；RTC 没对上时（年份 < 2024）用开机毫秒兜底，
# PC 端会用"接收时间"作为准，所以不影响统计。
RTC_MIN_YEAR = 2024

# ============================================================
# 7. 旧接口兼容（food_vision.py / tools/vision_probe.py 仍可单独跑）
# ============================================================
# 这一版的"有没有粮"已经改用盆口超声波测距，视觉残食率不再参与决策。
# 下面的参数只为让 food_vision.py 保持可运行（想对比两种判据时用）。
USE_FOOD_VISION = False

ROI_OCCLUDE_OVERLAP = 0.12
FOOD_EMPTY_RATIO    = 0.22
T_EMPTY_CONFIRM_MS  = 3000
FOOD_OCCLUDE_RATIO  = 2.00
BASELINE_SAMPLES    = 12
T_BASELINE_TIMEOUT_MS = 6000
