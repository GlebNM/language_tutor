import test from "node:test";
import assert from "node:assert/strict";
import {expressionHighlights, phrasalForToken, orderedTokens, sourceKey} from "../src/expressionSelection.ts";

const phrase = {id:"meaning", occurrence_id:"match", canonical:"turn off", kind:"phrasal_verb", token_ids:["turned","off"], knowledge:{score:3,ignored:true}};
const idiom = {id:"idiom", occurrence_id:"ice", canonical:"break the ice", kind:"idiom", token_ids:["break","the","ice"], knowledge:{score:0,ignored:false}};

test("clicking either separated phrasal component highlights both at every familiarity", () => {
  for (const score of [null,0,1,2,3]) for (const ignored of [false,true]) {
    const entry = {...phrase,knowledge:{score,ignored}};
    for (const token of ["turned","off"]) {
      assert.deepEqual([...expressionHighlights([entry],token,null)],["turned","off"]);
    }
  }
});
test("replaceable object and unrelated tokens are not phrasal components", () => {
  assert.equal(phrasalForToken([phrase],"it").length,0);
  assert.equal(expressionHighlights([phrase],null,null).size,0);
});
test("idioms require explicit selection and do not take over an ordinary word click", () => {
  assert.equal(expressionHighlights([idiom],"break",null).size,0);
  assert.deepEqual([...expressionHighlights([idiom],"break","ice")],["break","the","ice"]);
});
test("manual words follow source order even when clicked in reverse", () => {
  assert.deepEqual(orderedTokens(["off","turned"],["He","turned","it","off"]),["turned","off"]);
});
test("source keys separate material versions and message ownership", () => {
  assert.notEqual(sourceKey({material_id:"v1"}),sourceKey({material_id:"v2"}));
  assert.notEqual(sourceKey({conversation_id:"one",message_id:"reply"}),sourceKey({conversation_id:"two",message_id:"reply"}));
});
