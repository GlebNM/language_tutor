const sections = new Set([
  "materials",
  "discovery",
  "dictionary",
  "chat",
  "progress",
  "diagnostics",
  "settings",
]);
export type Route = { section: string; materialId?: string };
export function readRoute(hash: string): Route {
  const path = hash.replace(/^#/, "");
  if (/^material\/[a-zA-Z0-9-]+$/.test(path))
    return { section: "materials", materialId: path.slice(9) };
  return { section: sections.has(path) ? path : "materials" };
}
export function routeHash(route: Route): string {
  return route.materialId
    ? `#material/${route.materialId}`
    : `#${route.section}`;
}
