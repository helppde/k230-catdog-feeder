/*
 * 执行板（GPIO 触发）Arduino 固件自检 —— 在 PC 上跑，不需要 Nano
 *
 * 用 tests/arduino 里的 Arduino.h / Servo.h 桩，把真的 simple_trigger.ino
 * include 进来，然后：
 *   - 把触发脚拉高，看它有没有跑完整一轮
 *   - 看舵机角度、步进步数、是否到位断电
 *   - 看忙的时候再来一个高电平会不会被忽略
 *   - 看"有粮 / 没粮"两条分支的时长差
 *
 * 编译运行（项目根目录）：
 *   g++ -std=c++11 -Wno-trigraphs -I tests/arduino \
 *       -o tests/arduino/simple_trigger_test.exe tests/arduino/simple_trigger_test.cpp
 *   tests/arduino/simple_trigger_test.exe
 *
 * 注意：桩里的 delay() 只是把时钟往前推，所以 63 秒的一轮在 PC 上是瞬间跑完的。
 *      因此这里绝不能把 g_millis 归零（会把"距上次多久"算成无符号溢出），
 *      要用 advance() 往前推。
 */

#include <cstdio>
#include <string>

#include "Arduino.h"

/* ---- 桩全局量 ---- */
std::string g_serialIn;
std::string g_serialOut;
unsigned long g_millis = 0;
unsigned long g_micros = 0;
int g_ena = -1;
int g_enb = -1;
int g_pinLevel[24];
int g_analog[24];
int g_echoUs[24];
unsigned long (*g_pulseInHook)(uint8_t, uint8_t, unsigned long) = 0;
void (*g_tickHook)(void) = 0;
SerialStub Serial;

/* ---- 被测固件 ---- */
#include "../../arduino/simple_trigger/simple_trigger.ino"

/* ==================== 辅助 ==================== */
static int g_fail = 0;

static void check(bool cond, const char *what) {
  if (cond) {
    printf("  [ ok ] %s\n", what);
  } else {
    printf("  [FAIL] %s\n", what);
    g_fail++;
  }
}

static void checkf(bool cond, const char *fmt, long v) {
  char buf[240];
  snprintf(buf, sizeof(buf), fmt, v);
  check(cond, buf);
}
static bool has(const std::string &hay, const std::string &needle) {
  return hay.find(needle) != std::string::npos;
}

/* 数一段串口输出里出现了几次某个标记 —— 用来数"跑了几轮" */
static int countOf(const std::string &hay, const char *needle) {
  const size_t len = strlen(needle);
  int n = 0;
  for (size_t p = hay.find(needle); p != std::string::npos;
       p = hay.find(needle, p + len)) {
    n++;
  }
  return n;
}

static void resetInputs() {
  for (int i = 0; i < 24; i++) g_pinLevel[i] = HIGH;
  for (int i = 0; i < 24; i++) g_analog[i] = 0;
  for (int i = 0; i < 24; i++) g_echoUs[i] = 0;   /* 默认没回波 */
  g_pulseInHook = 0;
  g_tickHook = 0;
}

static void advance(unsigned long ms) { g_millis += ms; }

/* 线圈是否真的没电流 —— "断电"的唯一可靠判据。
 *
 *   · 接了 ENA/ENB（>= 0）：桥要关断，ENA/ENB 必须被拉低
 *   · 不接 ENA/ENB（-1，靠 L298N 上的跳线帽硬使能）：
 *     ENA/ENB 永远是 5V，桥一直开着，唯一能断电的办法就是 IN1~IN4 全低
 *     —— 四路全低时线圈两端同电位，没有电流、没有保持力矩。
 *     固件里 stepperRelease() 就是干这个的。
 *
 * 所以不能只断言 g_ena（那在 ENA 不接时永远是 -1，抓不到"线圈还通着"的 bug）。 */
static bool coilsDeenergized() {
  if (PIN_ENA >= 0 && g_ena != LOW) return false;
  if (PIN_ENB >= 0 && g_enb != LOW) return false;
  return g_pinLevel[PIN_IN1] == LOW && g_pinLevel[PIN_IN2] == LOW &&
         g_pinLevel[PIN_IN3] == LOW && g_pinLevel[PIN_IN4] == LOW;
}

/* 超声波：用"距离"造回波宽度。25°C 声速 -> 1cm 来回约 58µs */
static int cmToUs(float cm) { return (int)(cm * 58.0f + 0.5f); }

/* 盆里的粮况：present=true 装作满粮，false 装作空盆。
 * 故意不用 FOOD_PRESENT_CM 本身来算，而是用"离阈值明显有距离"的两个值，
 * 这样阈值改了也不会让用例变得没有意义。 */
static void setFood(bool present) {
  g_echoUs[PIN_US_ECHO] = present ? cmToUs(FOOD_PRESENT_CM * 0.5f)
                                  : cmToUs(FOOD_PRESENT_CM * 1.6f);
}

/* 没有回波（线松了 / 探头脏了 / 超出量程） */
static void setEchoNone() {
  g_echoUs[PIN_US_ECHO] = 0;
}

static void loopWithTrigger(int level, int n = 3) {
  g_pinLevel[PIN_TRIG] = level ? HIGH : LOW;
  for (int i = 0; i < n; i++) loop();
}

/* 让 MIN_GAP 满足：先拉低信号复位 armed，再把时钟推到间隔之外 */
static void readyNextCycle() {
  loopWithTrigger(0, 1);
  advance(MIN_GAP_MS + 1000);
  loopWithTrigger(0, 1);
}

/* ==================== 用例 ==================== */
int main() {
  printf("=== 执行板固件自检（GPIO 触发）===\n");
  resetInputs();

  setup();
  check(has(g_serialOut, "READY"), "上电打印 READY");
  check(coilsDeenergized(), "上电时步进线圈是断电的（IN1~IN4 全低）");

  /* ---- 1. 没有触发信号时不能动 ---- */
  printf("\n--- 1. 没有触发信号时不能动 ---\n");
  g_serialOut.clear();
  loopWithTrigger(0, 5);
  check(!has(g_serialOut, "cycle"), "低电平不触发任何动作");
  check(angDoor == DOOR_CLOSED && angHopper == HOPPER_CLOSED, "舵机保持关闭");
  check(stepPos == 0, "步进没动");

  /* ---- 2. 上电保护：前 5 秒不接受触发 ---- */
  printf("\n--- 2. 上电后 5 秒内不响应（信号线还没稳定）---\n");
  resetInputs();
  setFood(true);
  g_serialOut.clear();
  advance(BOOT_GUARD_MS - 1000);        /* 才过 4 秒 */
  loopWithTrigger(1, 1);
  check(!has(g_serialOut, "cycle start"), "4 秒时仍被上电保护挡住");
  loopWithTrigger(0, 1);

  /* ---- 3. 盆里有粮：直接开门+推出，跳过 10s 出粮 ---- */
  printf("\n--- 3. 盆里有粮：直接开门+推出 ---\n");
  resetInputs();
  setFood(true);
  advance(2000);                        /* 跨过 5 秒保护 */
  loopWithTrigger(0, 1);
  g_serialOut.clear();
  unsigned long t0 = g_millis;
  loopWithTrigger(1, 1);
  unsigned long dtFood = g_millis - t0;

  check(has(g_serialOut, "#food=1"), "读到'有粮'，跳过出粮");
  /* 一轮 = 开门0.6 + 前进0.8 + 保持60 + 退回0.8 + 关门0.6 ≈ 63s。
   * 如果误走了出粮，会多出 0.6+10+0.6 ≈ 11s，所以用 60s+5s 当上限卡它。 */
  checkf(dtFood < FEED_HOLD_MS + 5000,
         "没多走出粮那 10s（实耗 %ldms，基准约 63s）", (long)dtFood);
  checkf(dtFood >= FEED_HOLD_MS, "确实保持了 60s（实耗 %ldms）", (long)dtFood);
  check(angDoor == DOOR_CLOSED, "一轮结束后门是关的");
  check(angHopper == HOPPER_CLOSED, "粮仓口是关的");
  check(stepPos == 0, "滑台回到原点");
  check(coilsDeenergized(), "到位立刻断电（IN1~IN4 全低，不堵转发热）");
  check(has(g_serialOut, "#=== feed cycle done ==="), "有一轮完成的标记");

  /* ---- 4. 上升沿触发一次后，电平一直高着也不会立刻重复 ---- */
  printf("\n--- 4. 刚放完不会立刻重复（要等补放周期）---\n");
  g_serialOut.clear();
  advance(10000);                       /* 才过 10 秒，远没到补放周期 */
  loopWithTrigger(1, 5);
  check(!has(g_serialOut, "cycle start"), "10 秒内不再触发（远没到补放周期）");
  check(busy == false, "没有卡在 busy 状态");

  /* ---- 5. 盆里没粮：要先出粮 10s ---- */
  printf("\n--- 5. 盆里没粮：先出粮 10s 再开门 ---\n");
  resetInputs();
  setFood(false);
  readyNextCycle();
  g_serialOut.clear();
  t0 = g_millis;
  loopWithTrigger(1, 1);
  unsigned long dtNoFood = g_millis - t0;

  check(has(g_serialOut, "#food=0"), "读到'没粮'");
  checkf(dtNoFood >= FEED_HOPPER_MS, "先出了 10s 粮（实耗 %ldms）", (long)dtNoFood);
  checkf(dtNoFood > dtFood, "没粮那一轮比有粮那一轮耗时长（差 %ldms）",
         (long)(dtNoFood - dtFood));

  /* ---- 6. 短毛刺（<150ms）不能触发整轮 ---- */
  printf("\n--- 6. 短毛刺（<150ms）不能触发整轮 ---\n");
  resetInputs();
  setFood(true);
  readyNextCycle();
  g_serialOut.clear();
  static int glitchLeft = 0;
  glitchLeft = 2;
  g_tickHook = []() {
    /* 在"稳定 150ms"的判定循环里被调 2 次就把信号打下去 */
    if (glitchLeft > 0) {
      glitchLeft--;
      if (glitchLeft == 0) g_pinLevel[PIN_TRIG] = LOW;
    }
  };
  g_pinLevel[PIN_TRIG] = HIGH;
  loop();
  g_tickHook = 0;
  check(!has(g_serialOut, "cycle start"), "毛刺被丢弃，没开饭");
  check(angDoor == DOOR_CLOSED, "门没被打开");

  /* ---- 7. 两轮之间至少要间隔 MIN_GAP ---- */
  printf("\n--- 7. 两轮之间至少要间隔 MIN_GAP ---\n");
  resetInputs();
  setFood(true);
  readyNextCycle();
  g_serialOut.clear();
  loopWithTrigger(1, 1);
  check(has(g_serialOut, "cycle start"), "间隔够了就正常开饭");

  g_serialOut.clear();
  loopWithTrigger(0, 1);
  advance(1000);                        /* 才过 1 秒 */
  loopWithTrigger(1, 1);
  check(!has(g_serialOut, "cycle start"), "1 秒后紧接着再来，被 MIN_GAP 挡住");

  /* ---- 8. 限位 ---- */
  printf("\n--- 8. 限位开关（可选脚）---\n");
  check(PIN_LIMIT_OUT < 0 && PIN_LIMIT_IN < 0,
        "默认没装限位（PIN_LIMIT_OUT/IN = -1），靠步数定位");

  /* ---- 9. 超声波：回波宽度 -> 距离的换算要准 ---- */
  printf("\n--- 9. 超声波距离换算 ---\n");
  resetInputs();
  /* 10cm 来回 = 20cm 声程 = 20/343*1e6 ≈ 583µs。这里写死 583 当期望值，
   * 而不是用固件里同一个 58 去算 —— 否则算错了测试也照样通过。 */
  g_echoUs[PIN_US_ECHO] = 583;
  float d10 = readFoodDistance();
  checkf(d10 > 9.9f && d10 < 10.2f, "583µs 读成约 10cm（实际 %ld 分厘米）",
         (long)(d10 * 100));

  /* 2m 声程 = 200cm/343m/s ≈ 1166µs -> 20cm */
  g_echoUs[PIN_US_ECHO] = 1166;
  float d20 = readFoodDistance();
  checkf(d20 > 19.9f && d20 < 20.2f, "1166µs 读成约 20cm",
         (long)(d20 * 100));

  /* ---- 10. 距离判断：近=有粮，远=没粮 ---- */
  printf("\n--- 10. 距离判断（近=有粮）---\n");
  resetInputs();
  g_echoUs[PIN_US_ECHO] = cmToUs(FOOD_PRESENT_CM * 0.5f);
  check(readFoodDistance() < FOOD_PRESENT_CM, "近处（半阈值）判为有粮");
  g_echoUs[PIN_US_ECHO] = cmToUs(FOOD_PRESENT_CM * 1.6f);
  check(readFoodDistance() > FOOD_PRESENT_CM, "远处（1.6 倍阈值）判为空盆");

  /* ---- 11. 没回波：整轮放弃，什么都不许动 ---- */
  printf("\n--- 11. 测距失败：整轮放弃 ---\n");
  resetInputs();
  setEchoNone();
  readyNextCycle();
  g_serialOut.clear();
  unsigned long tSkip0 = g_millis;
  loopWithTrigger(1, 1);
  unsigned long dtSkip = g_millis - tSkip0;

  check(has(g_serialOut, "cycle SKIPPED"), "打印了跳过原因");
  check(!has(g_serialOut, "cycle start"), "没宣布开始（不会出现 start 没 done）");
  check(!has(g_serialOut, "hopper"), "没有出粮");
  check(angHopper == HOPPER_CLOSED, "粮仓舵机没动");
  check(angDoor == DOOR_CLOSED, "门没被打开");
  check(stepPos == 0, "步进没动");
  /* 5 次采样 × 30ms 超时 + 4 次 60ms 间隔 ≈ 390ms，绝不会是 10s/60s 级别 */
  checkf(dtSkip < 3000, "没有卡住（耗时 %ldms，正常应 <1s）", (long)dtSkip);

  /* ---- 12. 噪声里取中位数，不被离群值带跑 ---- */
  printf("\n--- 12. 噪声取中位数 ---\n");
  resetInputs();
  /* 5 次采样：4 个近距离（有粮）+ 1 个远距离离群值（空盆）。
   * 全部按 FOOD_PRESENT_CM 的倍数生成，这样改阈值不会让用例失效。
   *
   *   0.50, 0.52, 0.55, 0.60  -> 都在"有粮"那一侧
   *   8.00                    -> 明显"空盆"
   *
   *   排序后中位数 = 0.55 倍阈值 -> 判为有粮   （正确）
   *   平均值 = (0.50+8.00+0.55+0.60+0.52)/5 = 2.03 倍阈值 -> 判为空盆（被带跑）
   * 这就是为什么固件用中位数而不是平均值。 */
  static const float noiseFactor[5] = {0.50f, 8.00f, 0.55f, 0.60f, 0.52f};
  static int noiseIdx = 0;
  noiseIdx = 0;
  g_pulseInHook = [](uint8_t, uint8_t, unsigned long) -> unsigned long {
    return (unsigned long)cmToUs(FOOD_PRESENT_CM * noiseFactor[noiseIdx++ % 5]);
  };
  float dMed = readFoodDistance();
  g_pulseInHook = 0;

  float noiseMean = 0.0f;
  for (int i = 0; i < 5; i++) noiseMean += FOOD_PRESENT_CM * noiseFactor[i];
  noiseMean /= 5.0f;

  checkf(dMed > FOOD_PRESENT_CM * 0.5f && dMed < FOOD_PRESENT_CM * 0.6f,
         "中位数落在 0.5~0.6 倍阈值之间（实际 %ld 分厘米）", (long)(dMed * 100));
  check(dMed < FOOD_PRESENT_CM, "中位数判为有粮（没被离群值带跑）");
  check(noiseMean > FOOD_PRESENT_CM,
        "同样这组数据取平均值就会被误判成空盆（所以不能用均值）");

  /* ---- 13. 有效样本不足 -> 按测量失败处理 ---- */
  printf("\n--- 13. 有效样本不足 ---\n");
  resetInputs();
  static int fewIdx = 0;
  static const int few[5] = {583, 0, 0, 0, 0};   /* 只有 1 个有效，小于 US_MIN_VALID */
  fewIdx = 0;
  g_pulseInHook = [](uint8_t, uint8_t, unsigned long) -> unsigned long {
    return (unsigned long)few[fewIdx++ % 5];
  };
  float dFew = readFoodDistance();
  g_pulseInHook = 0;
  check(dFew < 0, "只成功 1 次（<3）就判为测量失败");

  /* ---- 14. 连续失败计数（兜底开关依赖它）---- */
  printf("\n--- 14. 连续失败计数 ---\n");
  check(US_FAIL_FALLBACK_AFTER == 0, "默认 US_FAIL_FALLBACK_AFTER = 0（失败即放弃）");

  /* 上一节已经失败过一次 */
  check(usFailStreak == 1, "失败 1 次后计数 = 1");

  resetInputs();
  setEchoNone();
  readyNextCycle();
  g_serialOut.clear();
  loopWithTrigger(1, 1);
  check(usFailStreak == 2, "再失败一次，计数累加到 2（兜底开关就是靠它）");

  /* 成功测到一次就要清零，否则偶发失败会一直累加到兜底 */
  resetInputs();
  setFood(true);
  readyNextCycle();
  g_serialOut.clear();
  loopWithTrigger(1, 1);
  check(has(g_serialOut, "cycle start"), "这回测到了，正常开工");
  check(usFailStreak == 0, "测成功一次就把失败计数清零");

  /* ---- 15. 持续高电平：每 2 分钟补放一次 ----
   *
   * 这是执行板最核心的定时契约：
   *   高电平 = 有动物。动物不走就一直高，本板每 RE_FEED_WHILE_HIGH_MS
   *   再看一眼电平，还是高就再放一轮。变低了就什么都不做。
   *
   * 如果这条错了，实际表现会是"猫吃半小时只给一次"或"疯狂连放"。 */
  printf("\n--- 15. 持续高电平：每 2 分钟补放一次 ---\n");
  resetInputs();
  setFood(true);
  readyNextCycle();
  g_serialOut.clear();
  loopWithTrigger(1, 1);
  check(countOf(g_serialOut, "cycle start") == 1, "第 1 个上升沿 -> 放第 1 轮");
  check(angDoor == DOOR_CLOSED, "第 1 轮跑完门是关的、机构已归位");

  /* --- 差 1 秒到周期：不能放 --- */
  g_serialOut.clear();
  if (g_millis < cycleStartMs + RE_FEED_WHILE_HIGH_MS - 1000) {
    advance(cycleStartMs + RE_FEED_WHILE_HIGH_MS - 1000 - g_millis);
  }
  loopWithTrigger(1, 1);
  check(countOf(g_serialOut, "cycle start") == 0,
        "差 1 秒到 2 分钟时还不放（不能提前）");

  /* --- 满周期：还是高 -> 补放第 2 轮 --- */
  g_serialOut.clear();
  advance(2000);
  loopWithTrigger(1, 1);
  check(countOf(g_serialOut, "cycle start") == 1,
        "满 2 分钟电平仍高 -> 补放第 2 轮");
  check(has(g_serialOut, "still HIGH"), "日志说明这是周期补放，不是新到访");

  /* --- 再一个周期：继续补放第 3 轮（能一直持续下去） --- */
  g_serialOut.clear();
  advance(RE_FEED_WHILE_HIGH_MS);
  loopWithTrigger(1, 1);
  check(countOf(g_serialOut, "cycle start") == 1, "再过 2 分钟 -> 补放第 3 轮");

  /* --- 动物走了：电平变低，立刻停止补放 --- */
  loopWithTrigger(0, 3);
  check(armed, "电平回落 -> 重新武装");
  g_serialOut.clear();
  advance(RE_FEED_WHILE_HIGH_MS * 3);   /* 走开很久，期间反复 loop */
  loopWithTrigger(0, 20);
  check(countOf(g_serialOut, "cycle start") == 0,
        "动物走了之后不再补放（低电平不喂）");

  /* --- 又回来：新的上升沿正常开一轮 --- */
  g_serialOut.clear();
  loopWithTrigger(1, 1);
  check(countOf(g_serialOut, "cycle start") == 1, "走了再来 -> 正常放第 4 轮");

  printf("\n=== %s (%d failure(s)) ===\n",
         g_fail ? "PROBLEMS FOUND" : "ALL PASS", g_fail);
  return g_fail ? 1 : 0;
}
