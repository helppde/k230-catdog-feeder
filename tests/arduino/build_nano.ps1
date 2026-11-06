# ============================================================
#  按 Arduino Nano（ATmega328P / 16MHz / 5V）真编译一遍固件
# ============================================================
#
#  为什么要有这个脚本：
#    tests/arduino/simple_trigger_test.cpp 是在 PC 上用 g++ 跑的，
#    int 是 32 位、指针 8 字节、内存无限 —— 它能过，不代表 Nano 能过。
#    Nano 上：int 只有 16 位、SRAM 只有 2KB、flash 只有 30.7KB 可用，
#    引脚号也受板子限制（A6/A7 没数字功能、D9/D10 没 PWM 等）。
#    所以改完 .ino 之后跑一下这个，能提前抓到：
#      - 编译错误（PC 测试用的是桩，可能放过真编译器不接受的写法）
#      - RAM 溢出（超过 2KB 的报错很隐晦，常见表现是"跑一会儿就重启"）
#      - flash 超了（32KB - 0.5KB bootloader）
#
#  用法（在仓库根目录）：
#      powershell -ExecutionPolicy Bypass -File tests/arduino/build_nano.ps1
#
#  需要：装了 Arduino IDE 的 AVR 板级支持（Arduino15\packages\arduino\hardware\avr）
#        以及 Servo 库（Arduino15\libraries\Servo 或 文档\Arduino\libraries\Servo）
#
#  退出码 0 = 编译通过；非 0 = 失败（CI 里可以直接用）
# ============================================================

$ErrorActionPreference = "Stop"

$PROJ = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$INO  = Join-Path $PROJ "arduino\simple_trigger\simple_trigger.ino"

# ---------- 找 AVR 核心 ----------
$ARDROOT = "$env:LOCALAPPDATA\Arduino15\packages\arduino\hardware\avr"
if (-not (Test-Path $ARDROOT)) {
  Write-Host "找不到 AVR 核心：$ARDROOT" -ForegroundColor Red
  Write-Host "先装 Arduino IDE + 板级支持包（Arduino AVR Boards）。" -ForegroundColor Red
  exit 2
}
# 取最新版本目录（1.8.7 之类）
$ARD = (Get-ChildItem $ARDROOT -Directory | Sort-Object Name -Descending | Select-Object -First 1).FullName

# ---------- 找 avr-gcc ----------
$GCCROOT = "$env:LOCALAPPDATA\Arduino15\packages\arduino\tools\avr-gcc"
if (-not (Test-Path $GCCROOT)) {
  Write-Host "找不到 avr-gcc：$GCCROOT" -ForegroundColor Red
  exit 2
}
$BIN = Join-Path (Get-ChildItem $GCCROOT -Directory | Sort-Object Name -Descending | Select-Object -First 1).FullName "bin"

$GPP = Join-Path $BIN "avr-g++.exe"
$GCC = Join-Path $BIN "avr-gcc.exe"
$SZ  = Join-Path $BIN "avr-size.exe"

# ---------- 找 Servo 库 ----------
$SRV = $null
foreach ($c in @(
  "$env:LOCALAPPDATA\Arduino15\libraries\Servo\src",
  "$env:USERPROFILE\Documents\Arduino\libraries\Servo\src"
)) {
  if (Test-Path (Join-Path $c "Servo.h")) { $SRV = $c; break }
}
if (-not $SRV) {
  Write-Host "找不到 Servo 库（Servo.h）" -ForegroundColor Red
  Write-Host "Arduino IDE 里 库管理器 搜 Servo 装一下即可（官方库，很小）。" -ForegroundColor Red
  exit 2
}

# ---------- 准备输出目录 ----------
$OUT = Join-Path $env:TEMP "nano_build"
if (Test-Path $OUT) { Remove-Item $OUT -Recurse -Force }
New-Item -ItemType Directory -Path $OUT | Out-Null

# Arduino IDE 预处理：.ino 拼成 .cpp，前面补 Arduino.h
# 用无 BOM 的 UTF8，否则中文注释会让 avr-g++ 报奇怪的错
$src = "#include <Arduino.h>`n#line 1 `"simple_trigger.ino`"`n" + (Get-Content $INO -Raw)
[System.IO.File]::WriteAllText(
  (Join-Path $OUT "sketch.cpp"), $src, (New-Object System.Text.UTF8Encoding($false)))

# Nano 的编译参数（和 boards.txt 里 arduino:avr:nano 一致）
$cxx = @(
  "-mmcu=atmega328p", "-DF_CPU=16000000L", "-DARDUINO=10819",
  "-DARDUINO_AVR_NANO", "-DARDUINO_ARCH_AVR",
  "-Os", "-w", "-std=gnu++11", "-fno-exceptions",
  "-ffunction-sections", "-fdata-sections",
  "-I$ARD\cores\arduino", "-I$ARD\variants\eightanaloginputs", "-I$SRV"
)
$cc = $cxx + @("-std=gnu11")

# 核心库本身警告很多，照旧 -w 掉；
# 但固件本体要开 -Wall —— AVR 上 int 只有 16 位，"悄悄溢出"是最典型的坑，
# 编译器会警告 narrowing / overflow，这些在 PC 自检里根本看不到。
$cxxSketch = @($cxx | Where-Object { $_ -ne "-w" }) + @("-Wall", "-Wextra", "-Wno-unused-parameter")

$objs = @()
$build = {
  param($file, $obj, $isC, $isAsm)
  if ($isAsm) { & $GCC @cxx -c $file -o $obj }
  elseif ($isC) { & $GCC @cc  -c $file -o $obj }
  else          { & $GPP @cxx -c $file -o $obj }
  if ($LASTEXITCODE -ne 0) { throw "编译失败: $file" }
  return $obj
}

# ---------- 核心库 ----------
foreach ($f in Get-ChildItem "$ARD\cores\arduino" -File) {
  if ($f.Extension -notin @(".c", ".cpp", ".S")) { continue }
  # 用带扩展名的 obj 名，否则 wiring_pulse.c 和 wiring_pulse.S 会撞车
  $o = Join-Path $OUT ($f.Name.Replace(".", "_") + ".o")
  $isAsm = ($f.Extension -eq ".S")
  $isC   = ($f.Extension -eq ".c")
  $objs += (& $build $f.FullName $o $isC $isAsm)
}

# ---------- Servo ----------
foreach ($f in Get-ChildItem $SRV -File -Filter *.cpp) {
  $o = Join-Path $OUT ("servo_" + $f.BaseName + ".o")
  $objs += (& $build $f.FullName $o $false $false)
}
$avrDir = Join-Path $SRV "avr"
if (Test-Path $avrDir) {
  foreach ($f in Get-ChildItem $avrDir -File -Filter *.cpp) {
    $o = Join-Path $OUT ("servo_avr_" + $f.BaseName + ".o")
    $objs += (& $build $f.FullName $o $false $false)
  }
}

# ---------- 固件本体 ----------
$so = Join-Path $OUT "sketch.o"
& $GPP @cxxSketch -c (Join-Path $OUT "sketch.cpp") -o $so
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "!!! 固件本体编译失败（上面是 avr-g++ 的报错）" -ForegroundColor Red
  exit 1
}
$objs += $so

# ---------- 链接 ----------
$elf = Join-Path $OUT "simple_trigger.elf"
& $GPP "-mmcu=atmega328p" "-Os" "-Wl,--gc-sections" "-o" $elf @objs "-lm"
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "!!! 链接失败（常见原因：RAM 超了，或者两个库抢同一个符号）" -ForegroundColor Red
  exit 1
}

Write-Host ""
Write-Host "=== BUILD OK : Arduino Nano / ATmega328P ===" -ForegroundColor Green
& $SZ "--mcu=atmega328p" "--format=avr" $elf

# ---------- 资源粗检 ----------
# 静态 RAM 只是一部分，栈还要占。经验值：静态别超过 1200B（约 58%），
# 否则运行时容易栈溢出 —— 表现是随机重启 / 变量莫名变值，极难查。
$info = & $SZ "--mcu=atmega328p" "--format=avr" $elf | Out-String
$m = [regex]::Match($info, 'Data:\s+(\d+)\s+bytes\s+\((\d+\.\d)%')
if ($m.Success) {
  $bytes = [int]$m.Groups[1].Value
  if ($bytes -gt 1200) {
    Write-Host ""
    Write-Host ("WARNING: 静态 RAM {0}B 偏多（2KB 的 {1:P0}），给栈剩的不多了。" -f $bytes, ($bytes/2048)) -ForegroundColor Yellow
    Write-Host "         检查是不是新加了全局数组 / String / 大缓冲。" -ForegroundColor Yellow
  }
}

exit 0
