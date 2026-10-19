import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source=readFileSync(new URL("../content.js",import.meta.url),"utf8");

function harness() {
  let listener,intervalCallback;
  const messages=[];
  const video={currentTime:12.5,paused:false,pause(){this.paused=true;},play(){return Promise.resolve();}};
  const chrome={runtime:{id:"extension-id",onMessage:{addListener(callback){listener=callback;}},sendMessage(message){messages.push(message);return Promise.resolve();}}};
  const document={title:"Example - YouTube",fullscreenElement:null,querySelector(selector){
    if(selector==="#movie_player video")return video;
    return null;
  }};
  vm.runInNewContext(source,{chrome,document,location:{pathname:"/watch",href:"https://www.youtube.com/watch?v=abcdefghijk"},URL,
    setInterval(callback){intervalCallback=callback;return 1;}});
  assert.equal(typeof listener,"function");
  assert.equal(typeof intervalCallback,"function");
  const dispatch=(message,sender={id:"extension-id"})=>{
    let response;
    listener(message,sender,value=>{response=value;});
    return response;
  };
  return {dispatch,messages,video};
}

test("occurrence selection validates context, seeks and publishes the selected token",()=>{
  const {dispatch,messages,video}=harness();
  const token={id:"token-1",lexeme_id:"lexeme-1",lemma:"learn",part_of_speech:"VERB",surface:"learned"};
  const cue={id:"cue-1",start_ms:42000,end_ms:45000,text:"I learned a word.",tokens:[token]};
  const material={id:"material-1",youtube_video_id:"abcdefghijk",cues:[cue]};
  assert.equal(dispatch({type:"lt:material",material}).ok,true);
  const selected=dispatch({type:"lt:select-occurrence",video_id:"abcdefghijk",material_id:"material-1",cue_id:"cue-1",token_id:"token-1"});
  assert.equal(selected.ok,true);
  assert.equal(video.currentTime,42);
  assert.equal(video.paused,true);
  const published=messages.at(-1);
  assert.equal(published.type,"lt:selection");
  assert.equal(published.state.selection.material_id,"material-1");
  assert.equal(published.state.selection.cue.id,"cue-1");
  assert.equal(published.state.selection.token.id,"token-1");

  for(const invalid of [
    {video_id:"wrong-video",material_id:"material-1",cue_id:"cue-1",token_id:"token-1"},
    {video_id:"abcdefghijk",material_id:"wrong-material",cue_id:"cue-1",token_id:"token-1"},
    {video_id:"abcdefghijk",material_id:"material-1",cue_id:"wrong-cue",token_id:"token-1"},
    {video_id:"abcdefghijk",material_id:"material-1",cue_id:"cue-1",token_id:"wrong-token"},
  ]){
    video.currentTime=99;video.paused=false;
    const messageCount=messages.length;
    assert.equal(dispatch({type:"lt:select-occurrence",...invalid}).ok,false);
    assert.equal(video.currentTime,99);
    assert.equal(video.paused,false);
    assert.equal(messages.length,messageCount);
  }
  video.currentTime=77;video.paused=false;
  const messageCount=messages.length;
  assert.equal(dispatch({type:"lt:select-occurrence",video_id:"abcdefghijk",material_id:"material-1",cue_id:"cue-1",token_id:"token-1"},{id:"other-extension"}),undefined);
  assert.equal(video.currentTime,77);
  assert.equal(video.paused,false);
  assert.equal(messages.length,messageCount);
});
