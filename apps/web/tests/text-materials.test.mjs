import assert from "node:assert/strict";
import { test } from "node:test";
import { readTextFile } from "../src/textMaterials.ts";

test("text file import rejects invalid UTF-8 before adding a material", async () => {
  const file = new File([new Uint8Array([0xff, 0xfe, 0xff])], "story.txt");
  await assert.rejects(readTextFile(file), /UTF-8/);
});
