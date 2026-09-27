const vm=require('vm'),assert=require('assert');
const flow=require(require('path').join(__dirname,'..','btc-zec-live.flow.json'));
const acc=flow.find(n=>n.id==='bf10000000000008');
let fakeNow=1_700_000_000_000;
const ctx=vm.createContext({Buffer,console,Date:{now:()=>fakeNow},Math,Number,JSON,Map,Object,Infinity,String});
vm.runInContext(acc.initialize,ctx);
const fn=vm.runInContext('(function(msg,node){'+acc.func+'\n})',ctx);
const debug=[];
function send(msg){const r=fn(msg,{});if(r&&r[2])for(const m of [].concat(r[2]))debug.push(m.payload);return r;}
const ws={_session:{type:'websocket',id:'x'}};
let id=100;const raw={buy:0,sell:0,n:0};
function trade(T,p,q,m,sym='BTCUSDT'){id++;if(sym==='BTCUSDT'){raw.n++;m?raw.sell+=p*q:raw.buy+=p*q;}
 send({...ws,payload:JSON.stringify({e:'trade',s:sym,t:id,p:String(p),q:String(q),T,m})});}
const t0=Math.floor(fakeNow/1000)*1000;
// second 0: 2 buys, 1 sell ; second 1: nothing ; second 2: 1 sell
fakeNow=t0+100; trade(t0+10,100,2,false); trade(t0+20,101,1,false); trade(t0+900,99,3,true);
fakeNow=t0+2100; trade(t0+2050,98,1,true);   // closes s0 and empty s1
assert.equal(debug.length,2);
const [b0,b1]=debug;
assert.equal(b0.buy_usd,301); assert.equal(b0.sell_usd,297); assert.equal(b0.delta_usd,4);
assert.equal(b0.buy_trades,2); assert.equal(b0.sell_trades,1); assert.equal(b0.total_trades,3);
assert.equal(b0.open_price,100); assert.equal(b0.close_price,99); assert.equal(b0.price_change,-1);
assert.equal(b0.buy_qty,3); assert.equal(b0.sell_qty,3); assert.ok(Math.abs(b0.buy_percent-301/598*100)<1e-9);
assert.equal(b1.total_trades,0); assert.equal(b1.open_price,99); assert.equal(b1.price_carried,true); assert.equal(b1.buy_percent,null);
assert.equal(b1.cvd_usd,4);
// tick closes s2 after grace
fakeNow=t0+3000+900; send({topic:'flow:tick'});
assert.equal(debug.length,3); assert.equal(debug[2].sell_usd,98); assert.equal(debug[2].cvd_usd,4-98);
// late trade for s0 arrives: must be added (revised), cvd propagated
trade(t0+500,100,1,false);
const rev=debug[3]; assert.equal(rev.timestamp,t0); assert.equal(rev.revised,true); assert.equal(rev.buy_usd,401);
// pull from a client
const r=send({topic:'flow:pull',_client:{socketId:'s'},payload:{symbol:'BTCUSDT',token:'k'}});
const p=r[0].payload; assert.equal(r[1],null);
assert.equal(p.reset,true); assert.equal(p.rows.length,5); assert.equal(p.buckets.length,3);
const sum=k=>p.buckets.reduce((a,b)=>a+b[k],0);
assert.equal(sum('buy_usd'),raw.buy); assert.equal(sum('sell_usd'),raw.sell); assert.equal(sum('total_trades'),raw.n);
assert.equal(p.buckets[2].cvd_usd,raw.buy-raw.sell);
assert.equal(p.late,1); assert.equal(p.tooLate,0);
// incremental pull returns nothing new
const r2=send({topic:'flow:pull',_client:{socketId:'s'},payload:{symbol:'BTCUSDT',token:'k2',epoch:p.epoch,after:p.after,bver:p.bver}}).at(0).payload;
assert.equal(r2.reset,false); assert.equal(r2.rows.length,0); assert.equal(r2.buckets.length,0);
// ZEC isolated, routed to output 2
trade(t0+3100,30,5,true,'ZECUSDT');
const rz=send({topic:'flow:pull',_client:{socketId:'s'},payload:{symbol:'ZECUSDT',token:'z'}});
assert.equal(rz[0],null); assert.equal(rz[1].payload.rows.length,1); assert.equal(rz[1].payload.symbol,'ZECUSDT');
// metrics 10s
assert.equal(p.metrics.ten.buy_usd,401); assert.equal(p.metrics.last.sell_usd,98);
// stress: 500 trades/s for 130 s, no loss
const before=raw.n; let buyN=0;
for(let s=5;s<135;s++){fakeNow=t0+s*1000+50;for(let k=0;k<500;k++){const m=k%3===0;if(!m)buyN++;trade(t0+s*1000+k,100+k%7,0.01,m);} send({topic:'flow:tick'});}
fakeNow+=3000; send({topic:'flow:tick'});
const all=send({topic:'flow:pull',_client:{socketId:'s'},payload:{symbol:'BTCUSDT',token:'x'}})[0].payload;
const ring=all.buckets; const last130=ring.filter(b=>b.timestamp>=t0+5000);
assert.equal(last130.reduce((a,b)=>a+b.total_trades,0),130*500);
assert.ok(last130.filter(b=>b.timestamp<t0+135000).every(b=>b.total_trades===500)); assert.equal(last130.at(-1).total_trades,0);
assert.equal(all.trades,raw.n);
assert.equal(ring.at(-1).cvd_usd.toFixed(4),(raw.buy-raw.sell).toFixed(4));
console.log('OK · buckets en ring:',ring.length,'· filas precio en ventana:',all.rows.length+(all.more?'+':''),'· trades totales:',all.trades);
