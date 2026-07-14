import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

/**
 * WebUI 首页猫猫精灵图动画组件。
 *
 * 使用 pet 模块（claw/pet/assets/yuexinmiao/spritesheet.webp）中已定义的
 * 精灵图资源，实现与桌宠一致的帧动画。
 *
 * 状态机：
 * - walk: 左右来回走动（running-right / running-left），参考拖动桌宠的移动
 * - idle: 偶尔停止（idle 动画），参考无点击操作时桌宠的静止状态
 * - action: 偶尔跳舞/挥手（jumping / waving），参考点击桌宠时的动画效果
 *
 * 所有动画定义与 claw/pet/app.py 的 ANIMATIONS 字典保持一致。
 */

// ---- spritesheet 布局（与 claw/pet/app.py 一致）----
const CELL_WIDTH = 192;
const CELL_HEIGHT = 208;
const SHEET_COLS = 8;
const SHEET_ROWS = 9;

// ---- 显示尺寸（缩放到适合 WebUI 首页的大小）----
const DISPLAY_WIDTH = 85;
const DISPLAY_HEIGHT = 92;

// ---- 动画定义：[行号, [每帧持续时间ms]] ----
const ANIMATIONS = {
  idle: { row: 0, frames: [280, 110, 110, 140, 140, 320] },
  "running-right": { row: 1, frames: [120, 120, 120, 120, 120, 120, 120, 220] },
  "running-left": { row: 2, frames: [120, 120, 120, 120, 120, 120, 120, 220] },
  waving: { row: 3, frames: [140, 140, 140, 280] },
  jumping: { row: 4, frames: [140, 140, 140, 140, 280] },
} as const;

type AnimName = keyof typeof ANIMATIONS;
type PetMode = "walk" | "idle" | "action";

// ---- 移动参数 ----
const WALK_SPEED = 28; // px/s，参考桌宠拖动时的移动速度
const MAX_OFFSET = 55; // 左右移动范围（px，相对于中心）

interface PetSpriteProps {
  className?: string;
}

export function PetSprite({ className }: PetSpriteProps) {
  const spriteRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = spriteRef.current;
    if (!el) return;

    // 立即设置初始帧，防止首帧空白
    const initAnim = ANIMATIONS["running-right"];
    el.style.backgroundPosition = `0px ${-(initAnim.row * DISPLAY_HEIGHT)}px`;
    el.style.transform = "translateX(0px)";

    let raf = 0;
    let lastTime = performance.now();

    // 状态机
    const s = {
      mode: "walk" as PetMode,
      direction: 1 as 1 | -1,
      offsetX: 0,
      frame: 0,
      frameTime: 0,
      // 下次状态切换时间（walk 模式下计时）
      nextSwitchAt: Date.now() + 5000 + Math.random() * 5000,
      // idle/action 状态结束时间
      stateEndsAt: 0,
      // 当前动作类型
      actionType: "jumping" as AnimName,
    };

    const getAnim = (): AnimName => {
      if (s.mode === "idle") return "idle";
      if (s.mode === "action") return s.actionType;
      return s.direction > 0 ? "running-right" : "running-left";
    };

    const tick = (now: number) => {
      const dt = Math.min(now - lastTime, 100); // 限制单帧最大间隔，防止切后台后跳变
      lastTime = now;

      const animName = getAnim();
      const animDef = ANIMATIONS[animName];

      // ---- 帧切换 ----
      s.frameTime += dt;
      if (s.frameTime >= animDef.frames[s.frame]) {
        s.frameTime = 0;
        s.frame++;
        if (s.frame >= animDef.frames.length) {
          s.frame = 0;
          // action 模式：动作播放完毕一个循环 → 恢复行走
          if (s.mode === "action") {
            s.mode = "walk";
            s.frame = 0;
            s.frameTime = 0;
            s.nextSwitchAt = Date.now() + 5000 + Math.random() * 5000;
          }
        }
      }

      // ---- 状态机逻辑 ----
      if (s.mode === "walk") {
        // 左右移动
        s.offsetX += s.direction * WALK_SPEED * (dt / 1000);
        if (s.offsetX >= MAX_OFFSET) {
          s.offsetX = MAX_OFFSET;
          s.direction = -1;
        } else if (s.offsetX <= -MAX_OFFSET) {
          s.offsetX = -MAX_OFFSET;
          s.direction = 1;
        }

        // 随机切换到 idle 或 action
        if (Date.now() >= s.nextSwitchAt) {
          const r = Math.random();
          if (r < 0.4) {
            // 停下来休息
            s.mode = "idle";
            s.frame = 0;
            s.frameTime = 0;
            s.stateEndsAt = Date.now() + 2500 + Math.random() * 2500;
          } else {
            // 跳舞或挥手
            s.mode = "action";
            s.frame = 0;
            s.frameTime = 0;
            s.actionType = Math.random() < 0.5 ? "jumping" : "waving";
          }
        }
      } else if (s.mode === "idle") {
        // idle 结束 → 恢复行走
        if (Date.now() >= s.stateEndsAt) {
          s.mode = "walk";
          s.frame = 0;
          s.frameTime = 0;
          s.nextSwitchAt = Date.now() + 5000 + Math.random() * 5000;
        }
      }
      // action 模式的结束由帧循环完成时处理（见上方帧切换逻辑）

      // ---- 更新 DOM ----
      const bgX = -(s.frame * DISPLAY_WIDTH);
      const bgY = -(animDef.row * DISPLAY_HEIGHT);
      el.style.backgroundPosition = `${bgX}px ${bgY}px`;
      el.style.transform = `translateX(${s.offsetX}px)`;

      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  const sheetW = SHEET_COLS * DISPLAY_WIDTH;
  const sheetH = SHEET_ROWS * DISPLAY_HEIGHT;
  // 容器宽度需容纳精灵图 + 左右移动范围，确保移动时不被裁切
  const containerW = DISPLAY_WIDTH + MAX_OFFSET * 2;

  return (
    <div
      className={cn("flex justify-center mx-auto", className)}
      style={{ width: containerW, height: DISPLAY_HEIGHT }}
    >
      <div
        ref={spriteRef}
        style={{
          width: DISPLAY_WIDTH,
          height: DISPLAY_HEIGHT,
          backgroundImage: "url(/pet-spritesheet.webp)",
          backgroundSize: `${sheetW}px ${sheetH}px`,
          backgroundRepeat: "no-repeat",
          flexShrink: 0,
        }}
      />
    </div>
  );
}
