// Minimal orbit-camera math for the 3D knowledge graph. No dependencies.
//
// World axes: X right and Z depth span the force-simulation plane; Y is up —
// the abstraction axis the strata stack along (entities above, sources below).

export const FOV = (50 * Math.PI) / 180;

const PITCH_LIMIT = (88.5 * Math.PI) / 180; // shy of vertical so the basis never degenerates

export const sub = (a, b) => ({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z });
export const add = (a, b) => ({ x: a.x + b.x, y: a.y + b.y, z: a.z + b.z });
export const mul = (a, s) => ({ x: a.x * s, y: a.y * s, z: a.z * s });
export const dot = (a, b) => a.x * b.x + a.y * b.y + a.z * b.z;
export const cross = (a, b) => ({
  x: a.y * b.z - a.z * b.y,
  y: a.z * b.x - a.x * b.z,
  z: a.x * b.y - a.y * b.x,
});
export const norm = (a) => {
  const l = Math.hypot(a.x, a.y, a.z) || 1;
  return { x: a.x / l, y: a.y / l, z: a.z / l };
};

export function clampPitch(p) {
  return Math.min(PITCH_LIMIT, Math.max(-PITCH_LIMIT, p));
}

/** Camera position + orthonormal view basis (right, up, forward). */
export function basis(cam) {
  const cp = Math.cos(cam.pitch);
  const sp = Math.sin(cam.pitch);
  const cy = Math.cos(cam.yaw);
  const sy = Math.sin(cam.yaw);
  const pos = {
    x: cam.target.x + cam.dist * cp * sy,
    y: cam.target.y - cam.dist * sp, // pitch < 0 → camera above the target
    z: cam.target.z + cam.dist * cp * cy,
  };
  const f = norm(sub(cam.target, pos));
  const r = norm(cross(f, { x: 0, y: 1, z: 0 }));
  const u = cross(r, f);
  return { pos, r, u, f };
}

/** World point → screen. Null when behind the camera. */
export function project(p, b, vp) {
  const v = sub(p, b.pos);
  const z = dot(v, b.f);
  if (z < 1) return null;
  const F = vp.h / 2 / Math.tan(FOV / 2);
  return {
    sx: vp.w / 2 + (F * dot(v, b.r)) / z,
    sy: vp.h / 2 - (F * dot(v, b.u)) / z,
    depth: z,
    scale: F / z, // px per world unit at this depth
  };
}

/** The point on the horizontal plane y=planeY under screen pixel (sx, sy). */
export function unprojectToPlane(sx, sy, planeY, b, vp) {
  const F = vp.h / 2 / Math.tan(FOV / 2);
  const dir = norm(
    add(add(mul(b.r, (sx - vp.w / 2) / F), mul(b.u, -(sy - vp.h / 2) / F)), b.f)
  );
  if (Math.abs(dir.y) < 1e-6) return null;
  const t = (planeY - b.pos.y) / dir.y;
  if (t <= 0) return null;
  return add(b.pos, mul(dir, t));
}

/** Axis-aligned view orientations for the gizmo. ±Y keeps the current yaw. */
export function axisView(id, currentYaw) {
  switch (id) {
    case "+x": return { yaw: Math.PI / 2, pitch: 0 };
    case "-x": return { yaw: -Math.PI / 2, pitch: 0 };
    case "+z": return { yaw: 0, pitch: 0 };
    case "-z": return { yaw: Math.PI, pitch: 0 };
    case "+y": return { yaw: currentYaw, pitch: -PITCH_LIMIT }; // top — the flat Obsidian look
    case "-y": return { yaw: currentYaw, pitch: PITCH_LIMIT };
  }
}

/** Shortest signed angular difference. */
export function angleDelta(from, to) {
  return Math.atan2(Math.sin(to - from), Math.cos(to - from));
}
