import assert from "node:assert/strict";
import { test } from "node:test";
import { readRoute, routeHash } from "../src/navigation.ts";

test("saved page URLs survive reload and browser back navigation", () => {
  for (const section of ["materials", "discovery", "dictionary", "chat", "progress", "diagnostics", "settings"]) {
    assert.deepEqual(readRoute(routeHash({section})), {section});
  }
  assert.deepEqual(readRoute("#material/abc123-45"), {section: "materials", materialId: "abc123-45"});
});
test("invalid routes safely return to the library", () => {
  for (const hash of ["", "#no-page", "#material/", "#material/../../data", "#material/id?key=x", "#material/%zz"]) {
    assert.deepEqual(readRoute(hash), {section: "materials"});
  }
});
test("text citations reopen the saved paragraph", () => {
  const route = {section: "materials", materialId: "text-17", blockId: "paragraph-3"};
  assert.equal(routeHash(route), "#material/text-17?block=paragraph-3");
  assert.deepEqual(readRoute("#material/text-17?block=paragraph-3"), route);
  assert.deepEqual(readRoute("#material/text-17?block=../../private"), {section: "materials"});
});
