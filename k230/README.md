# K230 端代码：识别猫/狗 → GPIO42 高电平 → 拍照上传

这个目录是 **K230 侧**的独立程序，配合本项目的 PC Web 端（`pc/server.py`）使用。

```
k230/main.py     ← 单文件，CanMV IDE 打开直接运行（推荐先用这个）
k230/README.md   ← 本说明
```

> 项目根目录还有一份 `k230_trigger.py`（两千多行），是之前做的**完整版**：
> 多了显示看门狗、BUSY 反馈线、触发区裁剪、开机卡死自动换模式等。
> 两份代码**接的是同一个 Web 接口、同一根 GPIO42**，用哪份都行：
> 先跑 `main.py` 把链路跑通，需要那些额外功能再换 `k230_trigger.py`。

---

## 1. 它到底做了什么

```
摄像头每帧
   ↓
YOLOv8（K230 KPU，出厂自带的 80 类 COCO 模型）
   ↓
只保留 cat / dog 两类，置信度 ≥ 0.45
   ↓
连续 3 帧都看到  ──→ 认为"来了"：GPIO42 输出高电平
连续 2 秒没看到 ──→ 认为"走了"：GPIO42 拉低
   ↓
"来了"的那一帧拍照，POST 到 Web 端；之后只要还在，每 60 秒再补一张
```

- 高电平是 **3.3V**，持续保持（不是脉冲）。
- 拍的是**整帧照片**（不是只裁框）：即使框坐标有偏差，照片也一定是完整画面，
  网页上能自己看清猫/狗在哪。文件大约几十 KB。
- 上传失败不会丢照片：先存到 `/sdcard/captures/`，空闲时补传（每张最多 2 次）。
- 上传**不会拖慢识别**：拍照压缩在主循环里（几十毫秒），收发在后台线程。
  每张照片带一个 `uid`，服务端靠它去重 —— 超时重传不会在统计里变成好几条。

---

## 2. 接线（只接两根线）

```
K230 12pin 排针 第 1 脚 (GPIO42) ──────→  执行板/继电器/舵机的信号输入
K230 12pin 排针 第 4 脚 (GND)    ──────→  对方 GND（必须共地）
```

| K230 排针脚 | 引脚 | 说明 |
|---|---|---|
| 1 | GPIO42 | 默认普通 GPIO，本程序用它输出高/低 |
| 4 | GND | 信号必须共地 |
| 9 / 11 | 5V | **不要接**，接错会烧板子 |

⚠️ 注意事项：

1. GPIO42 是 3.3V 电平。接 Arduino/继电器模块一般都能直接识别高电平；
   如果对方是 5V 输入且要求 5V 高电平，需要加电平转换/三极管，别直接把 5V 灌进 GPIO42。
2. **不要用 GPIO26**：它是 SDIO 的 MMC1_CLK，占了 WiFi 就废了（上传照片全靠 WiFi）。
3. 接执行板时信号线建议串 100~330Ω 电阻，防止对方上电瞬间倒灌。

---

## 3. Web 端接口（`pc/server.py` 里真实存在的）

代码里已经按这个接口写好了，一般不用改：

```
POST {SERVER_URL}/api/v1/visit?device=k230-01&species=cat&conf=0.912&ws=640&hs=480&ts=123456[&token=xxx]
Content-Type: image/jpeg
Connection: close
body = JPEG 原始字节（不是 multipart）
```

| query 字段 | 说明 |
|---|---|
| device | 设备名，网页上区分是哪台 K230 |
| species | 只能是 `cat` / `dog`（其它值服务端当 `unknown`） |
| conf | 置信度，浮点数 |
| ws / hs | 图片宽高（服务端目前忽略，发了无害） |
| ts | K230 自己报的时间戳，可空 |
| token | 服务端设了 `CATDOG_TOKEN` 时才需要，值必须一致 |

成功返回 `200` + JSON：

```json
{"ok": true, "visit_id": 12, "species": "cat", "name": "猫-03", "visits": 5, ...}
```

常见失败：

| 状态码 | 原因 |
|---|---|
| 400 | body 不是 JPEG/PNG（照片没编码成功） |
| 403 | token 不对（K230 的 `API_TOKEN` 和服务端 `CATDOG_TOKEN` 不一致） |
| 413 | 图片超过 4MB |

> 服务端也兼容 `multipart/form-data`（方便 curl 手测），但 K230 程序走的是
> 更省内存的"原始 JPEG body"这条。

---

## 4. 配置（`k230/main.py` 开头，只改这一段）

| 变量 | 默认值 | 改成什么 |
|---|---|---|
| `WIFI_SSID` / `WIFI_PASS` | 666666 / 12345678 | 你的 WiFi（2.4G，K230 不支持 5G） |
| `SERVER_URL` | `http://192.168.1.100:8000` | **PC 端启动时打印的"局域网"地址**，例如 `http://192.168.1.23:8000` |
| `API_TOKEN` | 空 | 服务端设了 `CATDOG_TOKEN` 就填一样的值 |
| `DEVICE_ID` | `k230-01` | 网页上显示的名字 |
| `TRIG_PIN` | 42 | 一般不用改 |
| `KMODEL_PATH` | `/sdcard/kmodel/yolov8n_320.kmodel` | 出厂卡自带，一般不用改 |
| `DISPLAY_MODE` | `lcd` | 没接屏 / 想在 CanMV IDE 里看画面：改成 `"virt"` |
| `CONF_THRESH` | 0.45 | 老误触发就调高（0.5~0.6）；老漏检就调低（0.35） |
| `UPLOAD_INTERVAL_MS` | 60000 | 持续有动物时，隔多久再传一张 |

模型输入尺寸 `MODEL_INPUT_SIZE = (320, 320)` 必须和 kmodel 真实输入一致：
出厂 `yolov8n_320.kmodel` 就是 320；如果换成 640 的模型要改成 `(640, 640)`。

---

## 5. 上板步骤

### 第 1 步：PC 端先跑起来

```bash
cd D:\a本人项目文件\流浪猫狗识别放饭
python pc/run.py
```

记下打印的 `局域网: http://192.168.x.x:8000`，浏览器打开确认网页正常。
第一次运行 Windows 会弹防火墙，选"允许"；没弹窗就手动放行 8000 端口。

### 第 2 步：改 K230 配置

把 `main.py` 里的 `WIFI_SSID` / `WIFI_PASS` / `SERVER_URL` 改成实际值。

### 第 3 步：CanMV IDE 打开 `k230/main.py` 运行

正常启动的串口日志大概是这样：

```
[main] 亚博 K230 猫狗识别 -> GPIO42 高电平 + 拍照上传
[main] 初始化相机/屏幕 mode=lcd ...
[main] 相机就绪
[trig] GPIO42 -> 低（没有目标）
[trig] GPIO42 已配置为输出（3.3V 电平）
[det] 使用 libs.YOLO.YOLOv8
[det] 模型加载完成: /sdcard/kmodel/yolov8n_320.kmodel
[det] 猫/狗类别序号: cat=15 dog=16
[up] 目标 http://192.168.1.23:8000/
[up] WiFi 已连接: 192.168.1.50
```

拿一张猫/狗照片对着镜头（或者手机放猫叫视频），应该看到：

```
[det] 命中 猫 conf=0.87 框=212,140,168,190
[trig] GPIO42 -> 高（检测到猫/狗）
[up] 拍照: cat conf=0.87 81234 字节
[up] 上传成功（累计 1 张）
```

然后刷新网页，应该能看到这张照片和"猫"的记录。
移开照片约 2 秒后，串口打印 `GPIO42 -> 低（没有目标）`。

### 第 4 步：如果没有猫狗照片

拿手机搜一张猫的图片全屏对着摄像头，或者直接打印一张猫的照片。
实在测不了，可以临时把 `CONF_THRESH` 调到 0.30 试。

---

## 6. 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 串口报 `Kmodel file not exist.` | 模型路径不对。出厂卡在 `/sdcard/kmodel/`（不是 `models/`）；用 `tools/list_models.py` 看卡上实际路径 |
| 串口刷 `snapshot chn(2) failed(3)`，画面出不来 | 多半是 IDE 里反复运行没清媒体状态。拔 USB **重新上电**；还不行把 `DISPLAY_MODE` 改成 `"virt"` |
| 模型加载了但永远 0 个框 | 换过模型的话检查 `LABELS` / `MODEL_INPUT_SIZE`；A 板固件用 `libs.YOLO.YOLOv8`，B 板自训 2 类模型序号是 0/1，程序会按 `LABELS` 名字自动找 |
| 网页能打开，但一直没有新照片 | 看串口 `[up]` 那几行：WiFi 通没通？403（token）？连接超时（SERVER_URL 写错/防火墙）？ |
| WiFi 连不上 | 只支持 2.4G；热点名/密码别带特殊字符；路由器开了 AP 隔离也会连不上 |
| GPIO42 一直不变化 | 先确认串口有没有 `[trig] GPIO42 -> 高`；有的话量排针第 1 脚对第 4 脚电压（高=3.3V）。注意别用万用表电流档 |
| 板子屏幕是黑的，但串口正常 | `mode="lcd"` 只推板载屏，CanMV IDE 右侧预览本来就是黑的；想 IDE 里看画面把 `DISPLAY_MODE` 改成 `"virt"` |
| 运行十几秒后报 `ndarray malloc fail` | 主循环每帧已经 `gc.collect()`；如果还报，把 `DISPLAY_MODE` 改 `virt` 或降低 `RGB888P_SIZE` |

---

## 7. 想改行为

| 想改 | 改哪里 |
|---|---|
| 只要猫 / 只要狗 | `Detector.detect()` 里 `if cid != CLASS_CAT and cid != CLASS_DOG` 判断处，只留一个 |
| 检测到只拍一张，不重复传 | `UPLOAD_INTERVAL_MS` 调大（比如 3600000） |
| 不想要屏幕 | `DISPLAY_MODE = "virt"` |
| 不上传、只存 SD 卡 | `UPLOAD_ENABLE = False`（照片会存到 `/sdcard/captures/`） |
| 换自己训练的模型 | 改 `KMODEL_PATH` + `LABELS`；cat/dog 序号会按名字自动查 |
| 想只拍动物那一块（裁剪） | 完整版 `k230_trigger.py` 里有 `UPLOAD_FULL_FRAME`，把裁剪逻辑搬过来即可 |

---

## 8. 这份代码在 PC 上验证过什么

用假的 CanMV 模块（`machine` / `network` / `image` / `ulab.numpy` /
`libs.PipeLine` / `libs.YOLO`）+ **真的 PC Web 服务** 跑了一遍完整流程：

- 猫 → 连续 3 帧确认 → GPIO42 拉高 → 照片过真实 HTTP 接口 → 服务端落盘并入库
- 狗 → 同样触发一次；只有人（person）→ 不触发、不上传
- 2 秒没目标 → GPIO42 拉低；退出时强制拉低
- 持续 75 秒 → 到访 1 张 + 60 秒后再传 1 张
- 服务不可达 → 照片落 `/sdcard/captures`；恢复后补传成功并清缓存
- 400 / 403 / 413 不重试（403 时照片存本地等 token 修正后再补传）
- 上传的 JPEG 带完整 `FF D8 ... FF D9`，服务端 Pillow 能解码

接口契约另外用 43 个用例独立验证过（原始 JPEG、multipart、token 放 query 才认、
Content-Length 必须、413 两档、/img 回读、路径穿越等全部符合预期）。

（仿真脚本在 `D:\harness\k230_sim\sim.py`，它不属于交付代码，只在 PC 上跑。）
