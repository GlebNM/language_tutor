const sections = new Set([
  "materials",
  "discovery",
  "dictionary",
  "chat",
  "progress",
  "diagnostics",
  "settings",
]);
export type Route = { section: string; materialId?: string; blockId?: string };
export function readRoute(hash: string): Route {
  const path = hash.replace(/^#/, "");
  const material = /^material\/([a-zA-Z0-9-]+)(?:\?block=([a-zA-Z0-9-]+))?$/.exec(path);
  if (material)
    return { section: "materials", materialId: material[1], ...(material[2] ? { blockId: material[2] } : {}) };
  return { section: sections.has(path) ? path : "materials" };
}
export function routeHash(route: Route): string {
  return route.materialId
    ? `#material/${route.materialId}${route.blockId ? `?block=${route.blockId}` : ""}`
    : `#${route.section}`;
}
