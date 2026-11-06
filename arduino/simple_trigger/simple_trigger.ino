/*
 * ============================================================
 *  流浪猫狗自动放饭 —— 执行板固件（单根 GPIO 触发）
 * ============================================================
 *
 * 为什么是单根 GPIO：
 *   执行板本来就自己测盆里有没有粮、自己跑完整一轮，K230 只需要告诉它"有动物"。
 *   一根线的电平就够，不用串口收发/交叉/分压，也不会因为掉一条命令而停在半开状态。
 *   代价：本板不回话。想更准就接可选的 PIN_BUSY 反馈线。
 *
 * 信号语义（K230 那边是持续电平，不是脉冲）：
 *   高电平 = 有动物；低电平 = 没有。
 *
 *   两条放饭路径，都只在本板里决定：
 *     ① 上升沿（低->高）        -> 立刻跑一轮（动物刚来）
 *     ② 一直高电平              -> 每 2 分钟再看一眼电平，
 *                                  还是高就再跑一轮（动物还在吃，补粮）
 *   电平变低 -> 立刻停止补放，等下一次上升沿。
 *
 *   所以"一只猫蹲半小时"会得到：一次到访 + 每 2 分钟一次的补粮，
 *   而不是只喂一次，也不是疯狂连喂。
 *
 * 一轮动作：
 *   超声波测距判断盆里有没有粮（装在盆口上方朝下照，d 小 = 有粮）
 *     有粮：舵机1 90->0 开门 -> 步进前进 -> 保持 60s -> 步进退回 -> 舵机1 0->90 关门
 *     没粮：舵机2 90->0 出粮 10s -> 0->90 关 -> 然后同上
 *     测距失败（没回波/超量程）：**整轮放弃**，不开门不出粮，等下次触发
 *
 * 只有一根信号线的代价（知道就好，不影响用）：
 *   - K230 不知道这一轮跑完没有，也不知道本板自己补了几轮 ——
 *     它只负责"有动物/没动物"。补粮节奏完全由本板决定。
 *   - 有个前提要保证：**K230 拉低要是真的"动物走了"**。
 *     K230 那边用「连续 2 秒没检测到」来确认离开，不会因为漏一两帧就掉电平，
 *     否则本板会把补放周期清零、重新当成新到访。这两边的参数是配套的。
 *   想补回"板子在忙"这个信息：把 PIN_BUSY 接一根线回 K230（可选，见下）。
 *
 * ======================= 接线 =======================
 *
 *  K230 GPIO（3.3V）───────> D2     触发信号；共地！
 *
 *  HC-SR04 超声波（盆口上方朝下照盆里）
 *     VCC  -> 5V   （建议外部 5V，别从 Nano 的 5V 取太多电流）
 *     TRIG -> D11  （Arduino 输出 10µs 高脉冲）
 *     ECHO -> D12  （Arduino 输入，5V 电平，可以直接读）
 *     GND  -> GND  （和 Arduino 共地）
 *
 *  舵机1（门）   信号 -> D3     电源红 -> 外部 5V/3A    地 -> 公共地
 *  舵机2（出粮） 信号 -> D5
 *
 *  L298N（42 步进，四线接 OUT1~OUT4）
 *     A+ -> OUT1   A- -> OUT2   B+ -> OUT3   B- -> OUT4
 *     IN1 -> D7    IN2 -> D8    IN3 -> D9    IN4 -> D10
 *     ENA / ENB **不接**（这两个是 PWM 调速用的，我们只用开关量驱动）
 *       → 所以 L298N 上的两个跳线帽必须留着，别拔！
 *         跳线帽在 = 桥永远使能，IN1~IN4 直接控制线圈；
 *         拔了又没接 ENA/ENB = 桥一直关断 = 电机完全不动（且不报错）。
 *     步进电源单独一路，GND 必须和 Nano 共地
 *
 *  ⚠️ 两件事别忽略（详见 README 2.3）：
 *     1) L298N 没有限流。42 步进相电阻通常 1.5~3Ω，12V 直供要 4A+，
 *        L298N 撑不住。把步进电源降到 6~9V，或者换 A4988/TMC2208（推荐）。
 *     2) 60 秒保持期间别让线圈一直通电（很烫）。丝杠自锁，
 *        所以默认 HOLD_DEENERGIZE = true：到位断电，靠丝杠顶住。
 *        如果发现滑台会被顶回去，改成 false，但那就必须先降压。
 *
 *  ⚠️ 3.3V 直接进 Arduino 的数字输入：ATmega328P 在 5V 供电时 VIH = 0.6*5 = 3.0V，
 *     3.3V 在门限之上，实际能读到 HIGH，但这种"擦边"用法在干扰大时会误读。
 *     如果现场干扰大，加一个电平转换（或一个 1k 串阻 + 10k 下拉）更稳。
 *
 * ==================== 目标板 ====================
 *  Arduino Nano（ATmega328P, 16MHz, 5V）。
 *  按真板子编译验证过：flash 10032B / 30.6%，静态 RAM 737B / 36.0%。
 *  2KB SRAM 里给栈剩约 1.3KB —— 够用，但别再加全局 String / 大数组。
 *
 *  Nano 上要注意的（详见 README 2.2.1）：
 *    - D11/D12 是 ISP 的 MOSI/MISO，本项目不用 SPI 所以可以占用；
 *      以后加 SD 卡 / SPI 屏就冲突了。
 *    - Servo 库独占 Timer1 -> D9/D10 没有 PWM。步进只用 digitalWrite，没事。
 *    - 老版 Nano（旧 bootloader）上传要选 "ATmega328P (Old Bootloader)"。
 *    - 别用 VIN 喂 12V（板载线性稳压会过热）。
 * ============================================================
 */

#include <Servo.h>

/* ==================== 1. 引脚 ==================== */
const int8_t PIN_TRIG = 2;      /* K230 信号：高电平 = 有动物在盆边，低电平 = 没有 */
const int8_t PIN_BUSY = -1;     /* 可选：本板跑一轮期间拉高，给 K230 看。
                                 * 不用就保持 -1；要用就改成某个脚（如 6）并接线回 K230。 */

const uint8_t PIN_DOOR   = 3;   /* 舵机1：门   90°=关, 0°=开 */
const uint8_t PIN_HOPPER = 5;   /* 舵机2：粮仓 90°=关, 0°=开 */

/* HC-SR04 超声波测粮（盆口上方朝下照盆里）
 * 原 D4 的触碰传感器已取消；D11/D12 相邻，正好接模块的 TRIG/ECHO 两个脚，
 * 模块的 VCC/GND 从扩展排针的 5V/GND 取（4 个针脚不必连着插）。
 * 这两脚原本是 ENA/ENB 的位置，现在空着，正好用上。 */
const int8_t PIN_US_TRIG = 11;  /* 输出：发 10µs 高脉冲 */
const int8_t PIN_US_ECHO = 12;  /* 输入：回波宽度 = 距离 */

/* 42 步进经 L298N */
const uint8_t PIN_IN1 = 7;
const uint8_t PIN_IN2 = 8;
const uint8_t PIN_IN3 = 9;
const uint8_t PIN_IN4 = 10;

/* ENA / ENB 不接（填 -1）。
 *
 * ⚠️ 不接 ENA/ENB 的前提是：**L298N 上那两个跳线帽必须留着（装回去）**。
 *    跳线帽在的时候 ENA/ENB 被硬拉到 5V，两个 H 桥永远是"使能"状态，
 *    IN1~IN4 的电平直接决定线圈通不通电。
 *    如果跳线帽拔了又没接 ENA/ENB，桥一直关断，电机会一点反应都没有
 *    （而且不会报错，很容易误判成"代码没跑"）。
 *
 * 断电靠 applyCoil(0,0,0,0)：四个 IN 全低，线圈两端同电位 = 没有电流 = 没有保持力矩。
 * 这一点和接了 ENA/ENB 的效果一样，所以下面 stepperRelease() 依然有效。 */
const int8_t PIN_ENA = -1;
const int8_t PIN_ENB = -1;

/* ============ 粮食检测参数（超声波，最常调的就是第一个） ============ */

/* 距离小于这个值就算"盆里有粮"。单位 cm。
 *
 * 怎么定：先烧进去，开串口看日志里的实测距离，然后：
 *   空盆时读到 18cm、装满读到 6cm  ->  填 12（取中间偏空那侧，宁可多出一次粮）
 *   空盆时读到 10cm、装满读到 7cm  ->  填 8.5（差值太小，见文件尾的警告）
 *
 * 距离是从探头下端面量到粮面的直线距离（超声波走来回，固件已÷2）。 */
const float FOOD_PRESENT_CM = 4.0;

/* 桶底/盆底到探头太近会测不出（HC-SR04 盲区约 2cm），
 * 距离超过量程也没意义。这两个范围外的读数一律当"测量失败"。 */
const float US_VALID_MIN_CM = 2.0;
const float US_VALID_MAX_CM = 60.0;

/* 每次判断取几次、取中位数。粮堆成斜坡时读数会跳，中位数天然抗离群值。 */
const uint8_t US_SAMPLES          = 5;
const uint8_t US_MIN_VALID        = 3;      /* 有效样本少于这个数 = 测量失败 */
const unsigned long US_SAMPLE_GAP_MS = 60;  /* 两次之间的间隔，避开余波 */

/* 等回波的最长时间。400cm 满量程 ≈ 23200µs，留点余量。 */
const unsigned long US_ECHO_TIMEOUT_US = 30000;

/* 测量失败时怎么办：整轮放弃（默认）。
 *
 * 这是安全的选择（不会在没粮时瞎开门、也不会溢出），但要清楚代价：
 * 探头脏了/线松了，K230 每 90 秒触发一次、每次都放弃，动物就一直吃不上。
 * 从外面看是"系统什么都没做"，很容易误判成死机 —— 所以串口一定会打印原因。
 *
 * 想要兜底就把下面改成 3：连续失败 3 次后当作"没粮"放行一次并出粮。
 *   0 = 永不兜底（默认）
 */
const uint8_t US_FAIL_FALLBACK_AFTER = 0;

/* 行程限位（可选，装了最安全） */
const int8_t PIN_LIMIT_OUT = -1;
const int8_t PIN_LIMIT_IN  = -1;
const bool   LIMIT_ACTIVE_LOW = true;

/* ==================== 2. 机械参数（按你的机构改这里） ==================== */
const int DOOR_CLOSED   = 90;
const int DOOR_OPEN     = 0;
const int HOPPER_CLOSED = 90;
const int HOPPER_OPEN   = 0;

const int SERVO_MOVE_MS = 600;      /* 舵机转 90° 用多久 */

/* 42 步进：1.8°/步 -> 200 步/圈。丝杠导程 2mm 的话，200 步 = 2mm。
 * 量一下滑台要推出去多少毫米： 步数 = 毫米 / 导程 * 200 */
const long STEPS_FORWARD = 40000;
const unsigned int STEP_US = 500;  /* 每步微秒，2000 = 500 步/秒 */
const bool STEP_HALF = true;       /* true: 半步，更平滑，力矩略小 */
const bool HOLD_DEENERGIZE = true;  /* 60s 保持期间断电（见文件头警告） */
const unsigned long STEP_TIMEOUT_MS = 20000;

const unsigned long FEED_HOLD_MS   = 60000;  /* 保持 60 秒 */
const unsigned long FEED_HOPPER_MS = 10000;  /* 没粮时出粮口开 10 秒 */

/* 触发防抖：信号至少要稳定这么久才算一次有效的"来客" */
const unsigned long TRIG_STABLE_MS = 150;
/* 两次"上升沿触发"之间的最短间隔（第二道保险） */
const unsigned long MIN_GAP_MS = 30000;

/* ============ 持续高电平时的补放周期（最常调的第二个参数） ============ */
/*
 * K230 那边"有动物就一直高电平"，所以高电平不等于"刚来"，也可能是
 * "一只猫在盆边吃了半小时"。如果只在上升沿放一次，它吃完盆里的就没了。
 *
 * 本板自己每 RE_FEED_WHILE_HIGH_MS 再检查一次电平：
 *   还是高 -> 动物还在 -> 再跑一轮（补粮）
 *   变低了 -> 动物走了 -> 什么都不做，等下一次上升沿
 *
 * 计时起点是**上一轮的开始时刻**，所以"每 2 分钟判断一次"是字面意义上的
 * 2 分钟一个周期（一轮本身约 63~74s，剩下的是空档）。
 * 调小 = 补得勤、费粮；调大 = 省粮、动物可能等。
 *
 * ⚠️ 这个值不要小于一轮的时长（约 74s），否则会把机器压得没有喘息时间。
 */
const unsigned long RE_FEED_WHILE_HIGH_MS = 120000;   /* 2 分钟 */

/* 上电后多久才开始接受触发。
 * 为什么不是直接用 MIN_GAP_MS：K230 可能比 Arduino 早就绪，如果上电后
 * 30 秒内不接受触发，第一只来的猫就被吞掉了。这里只等 5 秒，
 * 让信号线稳定下来（K230 初始化后会把线拉低），之后立刻可以用。 */
const unsigned long BOOT_GUARD_MS = 5000;

const bool DEBUG_SERIAL = true;     /* 想安静就改 false */

/* ==================== 3. 状态 ==================== */
Servo door, hopper;
int angDoor   = DOOR_CLOSED;
int angHopper = HOPPER_CLOSED;
long stepPos  = 0;
int  seqIdx   = 0;
bool busy     = false;
bool armed    = true;
/* 连续测距失败次数（给 US_FAIL_FALLBACK_AFTER 用）。
 * 用有符号：和常量 0 比较时编译器不会报"恒真"警告，
 * 而且能一眼看出"这个计数不会为负、只在成功时清零"。
 * 到 127 就饱和，不绕回负数。 */
int8_t usFailStreak = 0;
unsigned long lastCycleMs = 0;      /* 上一轮**结束**的时刻（给 MIN_GAP 用） */
unsigned long cycleStartMs = 0;     /* 上一轮**开始**的时刻（给补放周期用） */

/* ==================== 4. 步进驱动 ==================== */
const int8_t SEQ_FULL[4][4] = {
  {1, 0, 1, 0},
  {0, 1, 1, 0},
  {0, 1, 0, 1},
  {1, 0, 0, 1},
};
const int8_t SEQ_HALF[8][4] = {
  {1, 0, 0, 0}, {1, 0, 1, 0}, {0, 0, 1, 0}, {0, 1, 1, 0},
  {0, 1, 0, 0}, {0, 1, 0, 1}, {0, 0, 0, 1}, {1, 0, 0, 1},
};

void applyCoil(int a, int b, int c, int d) {
  digitalWrite(PIN_IN1, a ? HIGH : LOW);
  digitalWrite(PIN_IN2, b ? HIGH : LOW);
  digitalWrite(PIN_IN3, c ? HIGH : LOW);
  digitalWrite(PIN_IN4, d ? HIGH : LOW);
}

void coilsPower(bool on) {
  if (PIN_ENA >= 0) digitalWrite(PIN_ENA, on ? HIGH : LOW);
  if (PIN_ENB >= 0) digitalWrite(PIN_ENB, on ? HIGH : LOW);
}

void stepperRelease() {
  applyCoil(0, 0, 0, 0);
  coilsPower(false);
}

void stepperStep(int dir) {
  const int n = STEP_HALF ? 8 : 4;
  seqIdx = (seqIdx + (dir > 0 ? 1 : -1) + n) % n;
  if (STEP_HALF) {
    applyCoil(SEQ_HALF[seqIdx][0], SEQ_HALF[seqIdx][1],
              SEQ_HALF[seqIdx][2], SEQ_HALF[seqIdx][3]);
  } else {
    applyCoil(SEQ_FULL[seqIdx][0], SEQ_FULL[seqIdx][1],
              SEQ_FULL[seqIdx][2], SEQ_FULL[seqIdx][3]);
  }
}

/* ==================== 5. 小工具 ==================== */
void dbg(const char *s) {
  if (!DEBUG_SERIAL) return;
  Serial.println(s);
}

void busyPin(bool on) {
  if (PIN_BUSY >= 0) digitalWrite(PIN_BUSY, on ? HIGH : LOW);
}

bool limitHit(int8_t pin) {
  if (pin < 0) return false;
  int v = digitalRead(pin);
  return LIMIT_ACTIVE_LOW ? (v == LOW) : (v == HIGH);
}

/* ==================== 6. 粮食检测（HC-SR04 超声波） ==================== */
/*
 * 装法：探头朝下、垂直照盆里。
 *
 *          ┌── 探头
 *          │
 *    ──────┼──────  盆沿
 *          │  d
 *       ███████     粮面
 *    ───────────    盆底
 *
 * d 小 = 有粮，d 大 = 空盆。所以只有一个阈值 FOOD_PRESENT_CM。
 *
 * 传感器脏了、线松了、超出量程、盆太浅导致回波糊在一起 —— 都会让
 * pulseIn 返回 0 或读出离谱的值，这些统一返回 -1 表示"没测出来"。
 */

/* 测一次，返回 cm；测不出来返回 -1。 */
float measureCmOnce() {
  if (PIN_US_TRIG < 0 || PIN_US_ECHO < 0) return -1.0f;

  /* HC-SR04 时序：先确保 TRIG 是低，再给一个 ≥10µs 的高脉冲 */
  digitalWrite(PIN_US_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_US_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_US_TRIG, LOW);

  /* ECHO 变高的宽度 = 声波来回的时间。没回波就等满超时、返回 0。 */
  unsigned long us = pulseIn(PIN_US_ECHO, HIGH, US_ECHO_TIMEOUT_US);
  if (us == 0) return -1.0f;                 /* 没回波 */

  /* 25°C 声速 343m/s -> 来回 1cm 约 58.3µs */
  float cm = us / 58.0f;
  if (cm < US_VALID_MIN_CM || cm > US_VALID_MAX_CM) return -1.0f;
  return cm;
}

/* 取 US_SAMPLES 次的中位数，返回 cm；有效样本不足就返回 -1。 */
float readFoodDistance() {
  if (PIN_US_TRIG < 0 || PIN_US_ECHO < 0) return -1.0f;

  float s[US_SAMPLES];
  uint8_t n = 0;

  for (uint8_t i = 0; i < US_SAMPLES; i++) {
    float d = measureCmOnce();
    if (d > 0) s[n++] = d;
    /* 最后一轮不用等，省 60ms */
    if (i + 1 < US_SAMPLES) delay(US_SAMPLE_GAP_MS);
  }

  if (n < US_MIN_VALID) return -1.0f;

  /* 插入排序（avr 上用不着 qsort 那么重） */
  for (uint8_t i = 1; i < n; i++) {
    float k = s[i];
    int8_t j = (int8_t)i - 1;
    while (j >= 0 && s[j] > k) {
      s[j + 1] = s[j];
      j--;
    }
    s[j + 1] = k;
  }

  if (n & 1) return s[n / 2];
  return (s[n / 2 - 1] + s[n / 2]) * 0.5f;
}

/* 打印距离。不用 Serial.print(float)：avr 的 printf 默认不带 %f，
 * 直接 print(float) 只会打出个问号，所以自己拆成整数和小数。 */
void dbgDist(const char *prefix, float cm) {
  if (!DEBUG_SERIAL) return;
  char buf[64];

  if (cm < 0) {
    snprintf(buf, sizeof(buf), "%s-- cm (no echo)", prefix);
  } else {
    int whole = (int)cm;
    int frac  = (int)((cm - (float)whole) * 10.0f + 0.5f);
    if (frac >= 10) { whole++; frac = 0; }
    snprintf(buf, sizeof(buf), "%s%d.%d cm", prefix, whole, frac);
  }
  Serial.println(buf);
}

/* ==================== 7. 舵机 ==================== */
bool servoSlow(Servo &s, int &cur, int target, int ms) {
  if (target == cur) { s.write(target); return true; }
  int steps = ms / 15;
  if (steps < 2) steps = 2;
  int stepMs = ms / steps;
  int from = cur;
  for (int i = 1; i <= steps; i++) {
    s.write(from + (long)(target - from) * i / steps);
    delay(stepMs);
  }
  s.write(target);
  cur = target;
  return true;
}

/* ==================== 8. 步进动作 ==================== */
bool moveSteps(long n) {
  if (n == 0) return true;
  int dir = n > 0 ? 1 : -1;
  long cnt = n > 0 ? n : -n;

  coilsPower(true);
  unsigned long t0 = millis();
  int8_t lim = dir > 0 ? PIN_LIMIT_OUT : PIN_LIMIT_IN;
  bool ok = true;

  for (long i = 0; i < cnt; i++) {
    if (lim >= 0 && limitHit(lim)) break;         /* 到位就停 */
    stepperStep(dir);
    stepPos += dir;
    delayMicroseconds(STEP_US);
    if (millis() - t0 > STEP_TIMEOUT_MS) {
      dbg("#step timeout");
      ok = false;
      break;
    }
  }

  if (HOLD_DEENERGIZE) stepperRelease();
  else coilsPower(true);
  return ok;
}

/* ==================== 9. 完整一轮 ==================== */
void runCycle() {
  /* ---- 先测粮。测不出来就整轮放弃（不开门、不出粮、步进不动）----
   * 注意顺序：这里是**测完才**打印 cycle start，因为"放弃"也算一次触发，
   * 但没真的开工。否则日志里会看到 start 却没有 done，容易误判成中途卡死。 */
  float dist = readFoodDistance();
  dbgDist("#dist=", dist);

  bool hasFood;
  if (dist < 0) {
    if (usFailStreak < 127) usFailStreak++;   /* 饱和，别绕回负数 */
    if (US_FAIL_FALLBACK_AFTER > 0 && usFailStreak >= US_FAIL_FALLBACK_AFTER) {
      dbg("#us fail: fallback -> treat as EMPTY, hopper will run");
      usFailStreak = 0;
      hasFood = false;
    } else {
      dbg("#=== cycle SKIPPED: ultrasonic no echo / out of range ===");
      dbg("#    check: probe wiring, probe facing the bowl, bowl deeper than 2cm");
      dbg("#    (nothing was moved: door closed, no feed, stepper still)");
      return;
    }
  } else {
    usFailStreak = 0;
    hasFood = (dist < FOOD_PRESENT_CM);
  }

  dbg("#=== feed cycle start ===");
  dbg(hasFood ? "#food=1 (skip hopper)" : "#food=0 (hopper first)");

  /* ① 没粮才出粮 */
  if (!hasFood) {
    servoSlow(hopper, angHopper, HOPPER_OPEN, SERVO_MOVE_MS);
    delay(FEED_HOPPER_MS);                 /* 让粮真的掉进盆里 */
    servoSlow(hopper, angHopper, HOPPER_CLOSED, SERVO_MOVE_MS);
  }

  /* ② 开门 */
  dbg("#door open");
  servoSlow(door, angDoor, DOOR_OPEN, SERVO_MOVE_MS);

  /* ③ 步进前进，把盆推出去 */
  dbg("#bowl out");
  moveSteps(STEPS_FORWARD);

  /* ④ 保持 60 秒 */
  dbg("#hold 60s");
  delay(FEED_HOLD_MS);

  /* ⑤ 退回 */
  dbg("#bowl in");
  moveSteps(-STEPS_FORWARD);

  /* ⑥ 关门 */
  dbg("#door close");
  servoSlow(door, angDoor, DOOR_CLOSED, SERVO_MOVE_MS);

  dbg("#=== feed cycle done ===");
}

/* ==================== 10. setup / loop ==================== */

/* 跑一轮并把状态记账记好。
 * 抽出来是因为有两个入口会调它（上升沿触发 / 持续高电平补放），
 * 记账写两遍迟早会漏一处。 */
void runOneCycle() {
  busy = true;
  busyPin(true);
  cycleStartMs = millis();
  runCycle();
  busy = false;
  busyPin(false);
  lastCycleMs = millis();
}

void setup() {
  if (DEBUG_SERIAL) Serial.begin(115200);

  pinMode(PIN_TRIG, INPUT);                 /* K230 推挽输出，不用上拉 */
  if (PIN_BUSY >= 0) {
    pinMode(PIN_BUSY, OUTPUT);
    digitalWrite(PIN_BUSY, LOW);
  }
  if (PIN_US_TRIG >= 0) {
    pinMode(PIN_US_TRIG, OUTPUT);
    digitalWrite(PIN_US_TRIG, LOW);
  }
  if (PIN_US_ECHO >= 0) pinMode(PIN_US_ECHO, INPUT);
  if (PIN_LIMIT_OUT >= 0) pinMode(PIN_LIMIT_OUT, INPUT_PULLUP);
  if (PIN_LIMIT_IN  >= 0) pinMode(PIN_LIMIT_IN,  INPUT_PULLUP);

  pinMode(PIN_IN1, OUTPUT);
  pinMode(PIN_IN2, OUTPUT);
  pinMode(PIN_IN3, OUTPUT);
  pinMode(PIN_IN4, OUTPUT);
  if (PIN_ENA >= 0) pinMode(PIN_ENA, OUTPUT);
  if (PIN_ENB >= 0) pinMode(PIN_ENB, OUTPUT);
  stepperRelease();

  door.attach(PIN_DOOR);
  hopper.attach(PIN_HOPPER);
  door.write(angDoor);
  hopper.write(angHopper);
  delay(300);

  lastCycleMs = millis();
  /* 让"上电后第一次触发"只需要等 BOOT_GUARD_MS 而不是 MIN_GAP_MS：
   * 把 lastCycleMs 往回挪，(MIN_GAP_MS - BOOT_GUARD_MS) 之后就跑满了间隔。 */
  if (MIN_GAP_MS > BOOT_GUARD_MS) {
    lastCycleMs -= (MIN_GAP_MS - BOOT_GUARD_MS);
  }

  /* 上电测一次，把当前距离打出来 —— 这样你不用改代码就能知道阈值该填多少。
   * 先空盆看一次、装满粮再看一次，取中间值填进 FOOD_PRESENT_CM。 */
  if (PIN_US_TRIG >= 0 && PIN_US_ECHO >= 0) {
    dbgDist("#boot distance=", readFoodDistance());
    dbg("#-> set FOOD_PRESENT_CM between (full) and (empty)");
  }

  dbg("READY");
}

void loop() {
  int lvl = digitalRead(PIN_TRIG);

  /* 信号回落到低电平，才允许下一次"上升沿触发" */
  if (lvl == LOW) armed = true;

  if (lvl != HIGH) return;

  /* ---- 路径 1：新的上升沿（低 -> 高）---- */
  if (armed) {
    /* 再确认一次：信号要稳定 TRIG_STABLE_MS 以上才算一次真触发，
     * 免得电源干扰/上电瞬间的毛刺误触发一次完整放饭。 */
    unsigned long t0 = millis();
    bool stable = true;
    while (millis() - t0 < TRIG_STABLE_MS) {
      if (digitalRead(PIN_TRIG) == LOW) { stable = false; break; }
      delay(5);
    }

    if (stable && (millis() - lastCycleMs >= MIN_GAP_MS)) {
      armed = false;
      runOneCycle();
    }
    return;
  }

  /* ---- 路径 2：电平一直高着（动物还在），按周期补放 ----
   *
   * 这就是"每 2 分钟判断一次高低电平，还是高就继续放饭"。
   * armed 已经是 false，所以这里不会被当成新的到访；
   * 它是一条独立的定时补放路径，两条路径不会同时进。 */
  unsigned long need = RE_FEED_WHILE_HIGH_MS;
  if (need < MIN_GAP_MS) need = MIN_GAP_MS;   /* 别和 MIN_GAP 打架 */
  if (millis() - cycleStartMs >= need) {
    dbg("#still HIGH -> feed again (periodic top-up)");
    runOneCycle();
  }
}
