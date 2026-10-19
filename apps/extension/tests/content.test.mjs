import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source=readFileSync(new URL("../content.js",import.meta.url),"utf8");

const defaultBackend="http://127.0.0.1:8765";
const material={id:"material-1",youtube_video_id:"abcdefghijk",cues:[]};

function deferred() {
  let resolve;
  const promise=new Promise(done=>{resolve=done;});
  return {promise,resolve};
}

function harness({backend=defaultBackend,getBackend,load}={}) {
  let listener,intervalCallback;
  const messages=[];
  const storageListeners=[];
  const video={currentTime:12.5,paused:false,pause(){this.paused=true;},play(){return Promise.resolve();}};
  const chrome={
    runtime:{id:"extension-id",onMessage:{addListener(callback){listener=callback;}},sendMessage(message){
      messages.push(message);
      return message.type==="lt:load"?load?.(message)??Promise.resolve({ok:false,backend_origin:backend}):Promise.resolve();
    }},
    storage:{
      local:{get(defaults){return getBackend?.(defaults,backend)??Promise.resolve({...defaults,backend});}},
      onChanged:{addListener(callback){storageListeners.push(callback);}},
    },
  };
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
  const dispatchAsync=(message,sender={id:"extension-id"})=>new Promise(resolve=>{
    let replied=false;
    const respond=value=>{if(!replied){replied=true;resolve(value);}};
    const keepChannelOpen=listener(message,sender,respond);
    if(!keepChannelOpen&&!replied)resolve(undefined);
  });
  const setBackend=next=>{
    const oldValue=backend;backend=next;
    for(const callback of storageListeners)callback({backend:{oldValue,newValue:next}},"local");
  };
  return {dispatch,dispatchAsync,getState:()=>dispatch({type:"lt:get"}),messages,video,setBackend};
}

test("occurrence selection validates context, seeks and publishes the selected token",async()=>{
  const {dispatch,dispatchAsync,messages,video}=harness();
  const token={id:"token-1",lexeme_id:"lexeme-1",lemma:"learn",part_of_speech:"VERB",surface:"learned"};
  const cue={id:"cue-1",start_ms:42000,end_ms:45000,text:"I learned a word.",tokens:[token]};
  const material={id:"material-1",youtube_video_id:"abcdefghijk",cues:[cue]};
  assert.equal((await dispatchAsync({type:"lt:material",material,backend_origin:defaultBackend})).ok,true);
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

test("manual material import binds a fresh idle tab to its configured backend",async()=>{
  const {dispatchAsync,getState}=harness();
  const result=await dispatchAsync({type:"lt:material",material,backend_origin:defaultBackend});
  const state=getState();
  assert.equal(result.ok,true);
  assert.equal(state.status,"ready");
  assert.equal(state.enabled,true);
  assert.equal(state.backend_origin,defaultBackend);
  assert.equal(state.material.id,material.id);
});

test("manual material import rejects a backend origin that is not configured",async()=>{
  const {dispatchAsync,getState}=harness();
  const result=await dispatchAsync({type:"lt:material",material,backend_origin:"http://localhost:8765"});
  const state=getState();
  assert.equal(result.ok,false);
  assert.equal(state.status,"idle");
  assert.equal(state.material,undefined);
});

test("delayed manual import is discarded after the profile changes",async()=>{
  const lookup=deferred();
  const {dispatch,dispatchAsync,getState}=harness({getBackend:()=>lookup.promise});
  const applying=dispatchAsync({type:"lt:material",material,backend_origin:defaultBackend});
  assert.equal(dispatch({type:"lt:profile-changed"}).ok,true);
  lookup.resolve({backend:defaultBackend});
  const result=await applying;
  const state=getState();
  assert.equal(result.ok,false);
  assert.equal(state.status,"idle");
  assert.equal(state.enabled,false);
  assert.equal(state.material,undefined);
});

test("backend storage changes invalidate tab material without an open panel",async()=>{
  const {dispatchAsync,getState,setBackend}=harness();
  await dispatchAsync({type:"lt:material",material,backend_origin:defaultBackend});
  setBackend("http://localhost:8765");
  const state=getState();
  assert.equal(state.status,"idle");
  assert.equal(state.enabled,false);
  assert.equal(state.backend_origin,null);
  assert.equal(state.material,undefined);
});

test("an in-flight material load cannot commit after profile invalidation",async()=>{
  const loading=deferred();
  const {dispatch,getState}=harness({load:()=>loading.promise});
  assert.equal(dispatch({type:"lt:start",backend_origin:defaultBackend}).ok,true);
  assert.equal(getState().status,"loading");
  assert.equal(dispatch({type:"lt:profile-changed"}).ok,true);
  loading.resolve({ok:true,material,backend_origin:defaultBackend});
  await new Promise(resolve=>setImmediate(resolve));
  const state=getState();
  assert.equal(state.status,"idle");
  assert.equal(state.enabled,false);
  assert.equal(state.material,undefined);
  assert.equal(state.backend_origin,null);
});
