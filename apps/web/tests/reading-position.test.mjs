import assert from "node:assert/strict";
import { test } from "node:test";
import { captureReadingPosition, restoreReadingPosition } from "../src/readingPosition.ts";

function longParagraph() {
  // Offsets are Unicode code points from the material API: the emoji counts once.
  const text = "😀 First words. Later words. Final words.";
  const tokens = [
    { surface: "First", offset: 2, top: 90 },
    { surface: "words", offset: 8, top: 90 },
    { surface: "Later", offset: 15, top: 190 },
    { surface: "words", offset: 21, top: 190 },
    { surface: "Final", offset: 28, top: 290 },
  ];
  let contentScroll = 0;
  const words = tokens.map(token => ({
    dataset: { readingOffset: String(token.offset) },
    getBoundingClientRect: () => ({ top: token.top - contentScroll,
      bottom: token.top - contentScroll + 20 }),
  }));
  const paragraph = {
    dataset: { paragraph: "long-paragraph" },
    getBoundingClientRect: () => ({ top: 90 - contentScroll, bottom: 700 - contentScroll }),
    querySelectorAll: () => words,
  };
  const article = {
    get scrollTop() { return contentScroll; },
    set scrollTop(value) { contentScroll = value; },
    getBoundingClientRect: () => ({ top: 100, bottom: 500 }),
    querySelectorAll: () => [paragraph],
  };
  return { text, article, words };
}

test("reading position follows the first visible word inside a long paragraph", () => {
  const { article, text } = longParagraph();
  article.scrollTop = 60;
  assert.equal(Array.from(text).slice(15, 20).join(""), "Later");
  assert.deepEqual(captureReadingPosition(article), { block_id: "long-paragraph", offset: 15 });
});

test("reopening restores the saved word to the reading line instead of the paragraph start", () => {
  const { article, words } = longParagraph();
  restoreReadingPosition(article, { block_id: "long-paragraph", offset: 28 });
  assert.equal(words[4].getBoundingClientRect().top, 130);
  assert.equal(article.scrollTop, 160);
  assert.deepEqual(captureReadingPosition(article), { block_id: "long-paragraph", offset: 28 });
});

test("a paragraph citation starts at its first word and missing anchors leave scroll intact", () => {
  const { article } = longParagraph();
  article.scrollTop = 160;
  restoreReadingPosition(article, { block_id: "long-paragraph", offset: 0 });
  assert.deepEqual(captureReadingPosition(article), { block_id: "long-paragraph", offset: 2 });
  const previous = article.scrollTop;
  restoreReadingPosition(article, { block_id: "deleted-paragraph", offset: 28 });
  assert.equal(article.scrollTop, previous);
});
