# 单根 GPIO 触发 · 执行板固件

这是"K230 拉高一根线，Arduino 自己跑完一轮"的版本 —— 也是本项目**唯一**的方案。
不用串口：K230 只在检测到猫/狗时拉高 800ms，执行板收到就开始跑，整轮本地完成。

**目标板：Arduino Nano（ATmega328P / 16MHz / 5V）。** 已按真板子编译验证：

```
Program:   10036 bytes (30.6% Full)     flash 30.7KB 可用
Data:        737 bytes (36.0% Full)     静态 RAM；2KB 里剩约 1.3KB 给栈
```

Nano 的几处坑（上传要选 Old Bootloader、D9/D10 没 PWM、D11/D12 是 ISP 脚、
别用 VIN 喂 12V 等）写在主 `README.md` 的 **2.2.1** 里，烧录前先看一眼。

## 为什么不用串口

| 串口能做的 | 本方案怎么处理 |
|---|---|
| 下发"跑一轮" | 一根线拉高（有动物就一直高） |
| 回执 `10A` / `10D` | 不回执。执行板自己跑完一整轮，K230 不需要知道进度 |
| 远程急停 `8` | 没有。靠本板看门狗 + 上电归位 |
| 当场标定行程 `11,100` | 改代码重烧（`STEPS_FORWARD` / `STEP_US`） |
| 报告"盆里原来有没有粮" | 不需要——判断就在本板本地做（超声波测距） |

少一根线就少一处故障：没有波特率、没有 TX/RX 交叉、没有 5V→3.3V 分压、
也不会因为掉一条命令让机构停在半开状态。

唯一想要补回的是"板子在忙"这个信息 —— 接可选的 `PIN_BUSY` 反馈线即可（见下文）。

## 接线

```
K230 排针第 1 脚 (GPIO42, 3.3V) ──> D2       触发信号；必须共地
K230 排针第 4 脚 (GND)          ──> GND

HC-SR04 超声波（装在盆口上方，探头朝下照盆里）
    VCC  -> 5V     建议外部 5V，别从 Nano 的 5V 抢太多
    TRIG -> D11
    ECHO -> D12    （5V 电平，Arduino 直接读，不用分压）
    GND  -> GND

舵机1（门）   信号 -> D3     电源 5V/3A 独立，地共地
舵机2（出粮） 信号 -> D5

L298N：A+→OUT1  A-→OUT2  B+→OUT3  B-→OUT4
       IN1→D7  IN2→D8  IN3→D9  IN4→D10
       ENA/ENB 不接 —— 但 L298N 上那两个跳线帽必须留着别拔！
```

> D0/D1 空着（不用串口），所以可以插着 USB 边跑边看 `Serial` 调试输出。
> D4 现在空出来了（原触碰传感器）。
>
> ⚠️ 超声波探头必须**垂直朝下**、别照到盆沿或支架 —— 锥角约 15°，
> 照到盆沿时空盆也会读成"有粮"。

## 粮食检测：超声波测距

```
         ┌── 探头（朝下）
         │
   ──────┼──────  盆沿
         │  d
      ███████     粮面
   ───────────    盆底
```

`d` 小 = 有粮，`d` 大 = 空盆。**只有一个阈值** `FOOD_PRESENT_CM`（默认 12.0）。

### 阈值怎么定（不用猜）

烧进去、开串口，上电时会自动打一次距离：

```
#boot distance=18.4 cm
#-> set FOOD_PRESENT_CM between (full) and (empty)
READY
```

先空盆看一次，再装满粮看一次，取中间值填进 `FOOD_PRESENT_CM`。
每轮开饭前也会再打印一次 `#dist=xx.x cm`，方便观察。

### 判定细节

| 参数 | 默认 | 作用 |
|---|---|---|
| `FOOD_PRESENT_CM` | 12.0 | 小于它 = 有粮 |
| `US_SAMPLES` | 5 | 每次取 5 个样本 |
| `US_MIN_VALID` | 3 | 有效样本少于 3 个 = 测量失败 |
| `US_SAMPLE_GAP_MS` | 60 | 样本间隔，避开余波 |
| `US_VALID_MIN_CM` | 2.0 | 盲区，低于它算失败 |
| `US_VALID_MAX_CM` | 60.0 | 超量程算失败 |
| `US_ECHO_TIMEOUT_US` | 30000 | 等回波上限（400cm ≈ 23ms） |

**取的是中位数，不是平均值。** 粮堆成斜坡时读数会跳，中位数天然抗离群值
—— 一个离谱的读数不会把"有粮"判成"空盆"。

### 测距失败就放弃整轮

没回波（线松了 / 探头脏了 / 超出量程）时，**不开门、不出粮、步进不动**：

```
#dist=-- cm (no echo)
#=== cycle SKIPPED: ultrasonic no echo / out of range ===
#    check: probe wiring, probe facing the bowl, bowl deeper than 2cm
#    (nothing was moved: door closed, no feed, stepper still)
```

> ⚠️ **代价**：探头一旦坏了，K230 每 90 秒触发一次、每次都跳过，动物就一直吃不上。
> 从外面看是"系统什么都没做"，很容易误判成死机 —— 所以串口一定会打印原因。
> 不接 USB 的话，可以看 K230 那边有没有在正常触发来区分。

想要兜底，把文件顶部那个开关改成非 0：

```cpp
const uint8_t US_FAIL_FALLBACK_AFTER = 3;   /* 连续失败 3 次后当作没粮，放行一次 */
```

## 引脚核对（已联网核对）

`k230_trigger.py` 里的 `TRIG_PIN` 是 **GPIO 编号**，不是排针序号。

亚博 YAHBOOM K230 只引出 7 个 GPIO，全在散热片旁 12pin 排针上：

| 排针脚 | GPIO | 可复用功能 | 评价 |
|---|---|---|---|
| 1 | GPIO42 | GPIO42 / UART1_RTS / PWM0 / QSPI1_D2 | ✅ 默认普通 GPIO，**首选** |
| 2 | GPIO43 | GPIO43 / UART1_CTS / PWM1 / QSPI1_D3 | ✅ 次选 |
| 8 | GPIO34 | IIC1_SCL / IIS_D_IN0 / PDM_IN3 / UART3_RTS | ⚠️ 用音频就没了 |
| 10 | GPIO35 | IIC1_SDA / IIS_D_OUT0 / PDM_IN1 / UART3_CTS | ⚠️ 同上 |
| 3 | GPIO33 | IIC0_SDA / IIS_WS / UART3_RXD | ⚠️ 同上 |
| 5 | GPIO32 | IIC0_SCL / IIS_CLK / UART3_TXD | ⚠️ 同上 |
| 6 | GPIO26 | **MMC1_CLK** / RESV / PDM_CLK | ❌ SDIO 时钟，用 WiFi 就别碰 |

所以默认 `TRIG_PIN = 42`（排针第 1 脚）。想自己再确认一遍：

```python
# tools/gpio_probe.py 里改 CANDIDATES，然后 CanMV IDE 运行
CANDIDATES = [42, 43, 34, 35, 33, 32, 26]
```

它会让每个脚以 2Hz 闪烁 6 秒，串口打印"现在轮到 GPIO42（排针第 1 脚）"。
拿 LED+1k 电阻（或万用表）量排针，**哪根在闪就填哪根**。

- 如果轮到某个脚时板子重启/卡住/屏幕黑掉 → 那是启动脚或被外设占用，**记下来别再碰**。
- 探测顺序是"最不容易冲突 → 最容易冲突"，所以 `26` 排在最后。
- 你原来候选里的 `3` 在亚博板上没有引出（GPIO3 只在芯片封装上），
  你的 7 个脚实际是 `32 / 33 / 42 / 43 / 26 / 34 / 35`。

## 参数

`config.py`（`k230_trigger.py` 顶部同值）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `TRIG_PIN` | 42 | 排针第 1 脚；要换就换成 43/34/35/33/32，别用 26 |
| `PRESENCE_ON_FRAMES` | 3 | 连续几帧检测到才算"来了"（决定上升沿何时出现） |
| `PRESENCE_OFF_MS` | 2000 | 连续多久没检测到才算"走了"（决定何时拉低） |
| `VISIT_MIN_GAP_MS` | 30000 | 掉下去又升上来，小于它算同一次抖动 |
| `BUSY_PIN` | -1 | 接一根板→K230 的忙信号（可选，见下） |
| `TRIGGER_ZONE` | 全画面 | 强烈建议缩到饭盆/门口 |
| `SIMULATE_DETECTION` | False | True = 不加载模型，用假猫框跑流程 |

> 注意 K230 这边**没有**"两次开饭最短间隔"这种参数了 ——
> 补粮节奏由执行板的 `RE_FEED_WHILE_HIGH_MS` 决定，K230 只出电平。

`simple_trigger.ino` 顶部：

| 参数 | 默认 | 说明 |
|---|---|---|
| `PIN_TRIG` | 2 | 触发输入 |
| `PIN_US_TRIG` / `PIN_US_ECHO` | 11 / 12 | HC-SR04 的 TRIG / ECHO |
| `FOOD_PRESENT_CM` | 12.0 | **最常调的一个**：小于它算有粮 |
| `US_SAMPLES` / `US_MIN_VALID` | 5 / 3 | 取样数 / 有效样本下限 |
| `US_VALID_MIN_CM` / `US_VALID_MAX_CM` | 2.0 / 60.0 | 盲区 / 量程，超出的算失败 |
| `US_FAIL_FALLBACK_AFTER` | 0 | 0 = 测距失败就放弃本轮；N = 连续失败 N 次后兜底出粮 |
| `STEPS_FORWARD` | 400 | 推出行程（步数 = 毫米 / 导程 × 200） |
| `STEP_US` | 2000 | 每步微秒，越小越快，太小会丢步 |
| `FEED_HOLD_MS` | 60000 | 保持 60 秒 |
| `FEED_HOPPER_MS` | 10000 | 出粮 10 秒 |
| `HOLD_DEENERGIZE` | true | 保持期间断电（丝杠自锁），见坑 2 |
| `MIN_GAP_MS` | 30000 | 两次**上升沿**触发的最短间隔（第二道保险） |
| `RE_FEED_WHILE_HIGH_MS` | 120000 | **最常调的第二个**：电平一直高时，每 2 分钟补放一轮 |
| `TRIG_STABLE_MS` | 150 | 信号要稳定多久才算有效，滤毛刺 |
| `BOOT_GUARD_MS` | 5000 | 上电后多久开始接受触发 |

## 三种失败模式（单根线必然要面对的）

1. **K230 发触发时 Arduino 正在忙** → 这一下会被忽略。
   但**不会漏喂**：一轮跑完后电平还是高的话，`RE_FEED_WHILE_HIGH_MS`
   到点会补一轮，所以这只动物最多晚 2 分钟吃到，不会饿着走。真怕晚就接 BUSY 线。
2. **信号线毛刺误触发** → `TRIG_STABLE_MS` 要求稳定 150ms，加上
   `MIN_GAP_MS` 和上电 `BOOT_GUARD_MS` 三重保护。
3. **K230 死机 / 线被拔** → Arduino 不会知道，但它的动作是自洽的：
   做完一轮就把盆收回、门关上，不会停在半路。本固件没有"等心跳超时"这种
   依赖外部信号的看门狗，所以 K230 掉线只影响两件事：
   ① 下一次上升沿什么时候来（到访没被识别）；② 线被拔时的电平状态。
   **只要线还高着，补粮照旧** —— 补粮节奏完全在本板。

## 可选：加一根 BUSY 线（推荐，一条线换掉失败模式 1）

```
Arduino D6 ────────> K230 GPIOxx     跑一轮期间拉高，跑完拉低
```

```cpp
// simple_trigger.ino
const int8_t PIN_BUSY = 6;
```

```python
# k230_trigger.py
BUSY_PIN = 33          # 你探测出来的另一个能用的脚
```

接上之后 K230 会**等 Arduino 真的跑完才继续**，于是：

- 日志能准确写出"这一轮结束了"，排故障更好看；
- 补粮周期不会被"跑一轮跑到一半又来一次电平面变化"干扰。

代价：Arduino D6 是 5V 输出，进 K230 的 3.3V 输入**要分压**（和串口那根一样，1k/2k）。

## 上板顺序

1. **先只接 Arduino**（不接 K230），烧 `simple_trigger.ino`，串口助手 115200 看日志。
   上电先看超声波有没有读数：
   ```
   #boot distance=18.4 cm
   #-> set FOOD_PRESENT_CM between (full) and (empty)
   READY
   ```
   然后空盆测一次、装满粮测一次，两个读数取中间值填进 `FOOD_PRESENT_CM` 重烧。
   再用一根杜邦线把 **D2 短接到 3.3V/5V** 再拔掉，应该看到：
   ```
   #dist=18.4 cm
   #=== feed cycle start ===
   #food=0 (hopper first)     ← 或者是 food=1
   #hopper open
   ...
   #door open
   #bowl out
   ...（60 秒后）
   #bowl in
   #door close
   #=== feed cycle done ===
   ```
   > 注意：上电后要等 5 秒（`BOOT_GUARD_MS`）才会响应第一次。

2. **调角度/行程**：直接改 `.ino` 顶部的常数重烧（`STEPS_FORWARD`、`STEP_US`、
   `DOOR_OPEN`/`DOOR_CLOSED` 等）。没有串口下达命令的路子，所以是"改一次烧一次"。

3. **测超声波**：手动把粮盆装满 / 倒空，各触发一次，看 `#dist=` 有没有明显变化。
   - 空盆和满粮的读数差 < 3cm → 探头装太高或盆太浅，换个位置或换盆；
   - 读到 `-- cm (no echo)` → 接线问题，先查 VCC/GND，再查 TRIG/ECHO 是不是接反；
   - 读数忽大忽小 → 探头照到盆沿/支架了，挪一下角度（锥角约 15°）。

4. **接 K230**：先跑 `tools/gpio_probe.py` 定 `TRIG_PIN`，
   再设 `SIMULATE_DETECTION = True` 跑 `k230_trigger.py`，确认每次日志
   `[trig] GPIOxx 拉高 800ms -> 开饭` 之后 Arduino 串口都跟着动。

5. **关掉模拟**，上真模型，设 `TRIGGER_ZONE`。

## 自检（不用硬件）

```bash
python tests/k230_trigger_sim.py        # 75 项：电平消抖、到访计数、类别映射、URL/token、补传

g++ -std=c++11 -Wno-trigraphs -I tests/arduino \
    -o tests/arduino/simple_trigger_test.exe tests/arduino/simple_trigger_test.cpp
tests/arduino/simple_trigger_test.exe   # 15 组：有粮/没粮分支、毛刺、MIN_GAP、
                                        #        上电保护、2 分钟补粮周期
```

真编译一遍（需要 avr-gcc，防止"测试过了但烧不进去"）：

```powershell
powershell -ExecutionPolicy Bypass -File tests/arduino/build_nano.ps1
```
