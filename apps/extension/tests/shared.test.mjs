import {test} from "node:test";
import assert from "node:assert/strict";
import {backendUrl, importPayload, orderVideoVocabulary, permissionOrigin, timestamp, videoVocabulary} from "../shared.js";

test("settings accept a local HTTP server and reject remote or credential-bearing URLs", () => {
  assert.equal(backendUrl("http://localhost:8765/"), "http://localhost:8765");
  assert.equal(permissionOrigin("http://127.0.0.1:9000"), "http://127.0.0.1/*");
  for (const value of ["https://example.com", "http://127.0.0.1.evil.test", "http://user:pass@localhost", "http://localhost/api", "http://localhost?secret=x", "file:///tmp/data", "https://localhost"])
    assert.throws(() => backendUrl(value));
});

test("automatic captions enter the shared import contract with original source and full timing", () => {
  const payload = importPayload({video_id:"abcdefghijk",language:"en",source:"youtube-automatic",is_complete:true,
    cues:[{id:"one",start_ms:1250,end_ms:2500,text:"Hello, world."},{id:"two",start_ms:65000,end_ms:67200,text:"Hello again."}]}, "Sample");
  assert.deepEqual(payload, {youtube_url:"https://www.youtube.com/watch?v=abcdefghijk",title:"Sample",format:"srt",language:"en",source:"youtube-automatic",is_complete:true,
    content:"1\n00:00:01,250 --> 00:00:02,500\nHello, world.\n\n2\n00:01:05,000 --> 00:01:07,200\nHello again.\n"});
  assert.equal(timestamp(67200), "1:07");
});

test("video vocabulary groups forms by lexeme ID and keeps parts of speech separate", () => {
  const entries=videoVocabulary([
    {id:"c1",start_ms:1000,text:"run runs running",tokens:[
      {id:"t1",lexeme_id:"verb",lemma:"run",part_of_speech:"VERB",surface:"run",knowledge:null},
      {id:"t2",lexeme_id:"verb",lemma:"run",part_of_speech:"VERB",surface:"runs",knowledge:null},
      {id:"t3",lexeme_id:"noun",lemma:"run",part_of_speech:"NOUN",surface:"running",knowledge:{score:2}},
    ]},
    {id:"c2",start_ms:2500,text:"ran",tokens:[
      {id:"t4",lexeme_id:"verb",lemma:"run",part_of_speech:"VERB",surface:"ran",knowledge:null},
    ]},
  ]);
  assert.equal(entries.length,2);
  const verb=entries.find(entry=>entry.id==="verb");
  assert.deepEqual(verb.forms,["run","runs","ran"]);
  assert.equal(verb.count,3);
  assert.deepEqual(verb.occurrences.map(item=>[item.cue_id,item.token_id]),[["c1","t1"],["c1","t2"],["c2","t4"]]);
  assert.equal(entries.find(entry=>entry.id==="noun").part_of_speech,"NOUN");
});

test("video vocabulary order starts with unassessed words then score and frequency", () => {
  const entries=[
    {lemma:"frequent",part_of_speech:"NOUN",count:8,knowledge:{score:1}},
    {lemma:"known",part_of_speech:"VERB",count:3,knowledge:{score:2}},
    {lemma:"rare",part_of_speech:"NOUN",count:1,knowledge:{score:null}},
    {lemma:"busy",part_of_speech:"VERB",count:4,knowledge:{score:0}},
    {lemma:"often",part_of_speech:"ADV",count:10,knowledge:{score:null}},
  ];
  assert.deepEqual(orderVideoVocabulary(entries).map(entry=>entry.lemma),["often","rare","busy","frequent","known"]);
});
