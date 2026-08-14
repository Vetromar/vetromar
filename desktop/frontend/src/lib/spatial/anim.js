// Shared motion vocabulary from the greybox handoff: button presses depress
// ~220ms with a flash, slide-ins ease out cubically, syncing objects pulse
// sinusoidally at ~6 rad/s.

export const PRESS_MS = 220;

export const cubicOut = (p) => 1 - Math.pow(1 - p, 3);

/** 0..1 sinusoid at the handoff's sync-pulse rate; t in seconds. */
export const pulse = (t) => 0.5 + 0.5 * Math.sin(t * 6);

/** Momentary press latch, polled from the frame loop. */
export function makePress() {
  let until = 0;
  return {
    fire() {
      until = performance.now() + PRESS_MS;
    },
    get active() {
      return performance.now() < until;
    },
  };
}
