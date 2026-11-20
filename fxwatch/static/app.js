'use strict';
const $ = id => document.getElementById(id);
const codes = ['USD','EUR','HKD','JPY'];
const symbols = {USD:'$',EUR:'€',HKD:'HK$',JPY:'¥'};
const shortNames = {cgb:'广',hxb:'华',boc:'中',icbc:'工',cmb:'招',citic:'信'};
const defaults = {USD:10000,EUR:10000,HKD:10000,JPY:100000};
let selected = new URLSearchParams(location.search).get('currency') || 'USD';
if (!codes.includes(selected)) selected = 'USD';
let hours = 24, data = null, chartData = null, hiddenBanks = new Set(), requestId = 0, activeRefresh = false;
const amounts = {...defaults};
const esc = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const price = (v,cur=selected) => v == null ? '—' : Number(v).toFixed(cur==='JPY'?7:5);
const money = v => Number(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const dateFormat = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const stamp = v => v ? dateFormat.format(new Date(v)) : '—';
const timeOnly = v => v ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(v)) : '—';
const bankName = bank => data?.banks.find(b=>b.code===bank)?.name || bank;
function current(){ return data?.currencies.find(c=>c.code===selected); }
function getAmount(){const raw=$('amount').value;const n=Number(raw);return /^(?:\d+)(?:\.\d{1,2})?$/.test(raw)&&n>=.01&&n<=1e8?n:null;}
async function getJSON(path){const res=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(15000)});if(!res.ok)throw new Error(`HTTP ${res.status}`);return res.json();}
function renderCards(){
  $('currency-cards').innerHTML=data.currencies.map(c=>`<button class="currency-card ${c.code===selected?'selected':''}" data-currency="${c.code}" aria-pressed="${c.code===selected}"><div class="card-title"><div><span class="currency-symbol">${symbols[c.code]}</span><b>${esc(c.name)}<small>${c.code}</small></b></div><span class="card-arrow" aria-hidden="true">↗</span></div><div class="card-price"><span>¥</span>${price(c.best,c.code)}</div><div class="card-caption">${c.best===null?'有效报价不足，暂不排名':'当前有效样本最低 · 人民币 / 1 '+c.code}</div><div class="card-footer"><b>${c.best_banks.length?esc(c.best_banks.map(bankName).join(' / ')):'等待有效报价'}</b><span>${c.valid_banks}/${data.banks.length} 家有效</span></div></button>`).join('');
  for(const button of document.querySelectorAll('[data-currency]'))button.addEventListener('click',()=>selectCurrency(button.dataset.currency));
}
function renderComparison(){
  if(!data)return;
  const c=current(), amount=getAmount();
  $('currency-title').textContent=c.name;$('currency-code').textContent=selected;$('amount-currency').textContent=selected;
  $('coverage').textContent=`${c.valid_banks} / ${data.banks.length} 家报价有效`;
  $('amount-error').classList.toggle('hidden',amount!==null);
  $('spread').textContent=c.spread===null?'—':price(c.spread);
  $('day-low').textContent=price(c.observed_day_low);
  $('savings').textContent=amount!==null&&c.spread!==null?money(amount*Number(c.spread)):'—';
  $('savings-detail').textContent=amount===null?'填写有效金额后显示估算。':c.spread===null?'有效银行不足，暂不估算节省金额。':`购入 ${amount.toLocaleString('zh-CN')} ${selected}，相比当前有效样本中的最高报价。`;
  $('unit-note').textContent=selected==='JPY'?'日元按每 1 日元展示；乘以 100 即为每 100 日元的人民币价格。':'所有报价均已换算为每 1 单位外币。';
  const rows=data.rows.filter(r=>r.currency===selected).sort((a,b)=>(a.quality==='ok'?0:1)-(b.quality==='ok'?0:1)||(Number(a.sell??Infinity)-Number(b.sell??Infinity)));
  $('bank-rows').innerHTML=rows.map(r=>{
    const valid=r.quality==='ok', best=valid&&c.best!==null&&Number(r.sell)===Number(c.best);
    return `<tr class="${best?'best-row':valid?'':'invalid-row'}"><td><a class="bank-cell bank-link" href="${esc(r.source)}" target="_blank" rel="noopener noreferrer" title="打开${esc(r.name)}官方报价"><span class="bank-badge ${r.bank}">${shortNames[r.bank]}</span><span><span class="bank-name">${esc(r.name)}</span><span class="bank-abbr"> ${r.bank.toUpperCase()} ↗</span></span></a></td><td class="table-price">${price(r.sell)}</td><td class="estimated">${valid&&amount!==null?'¥ '+money(Number(r.sell)*amount):'—'}</td><td class="quote-time" title="采集于 ${esc(stamp(r.fetched_at))}">${timeOnly(r.quoted_at)}<small>${r.quoted_at?esc(r.quoted_at.slice(0,10)):'尚未获取'}</small></td><td><span class="status-badge ${best?'best':valid?'':'warning'}">${best?'最低':esc(r.status)}</span></td></tr>`;
  }).join('');
  $('download').href=`/api/export.csv?currency=${selected}&hours=${hours}`;
}
function selectCurrency(cur){
  if(selected===cur)return;
  const amount=getAmount();if(amount!==null)amounts[selected]=amount;
  selected=cur;$('amount').value=amounts[cur];
  const url=new URL(location.href);url.searchParams.set('currency',cur);history.replaceState(null,'',url);
  renderCards();renderComparison();loadHistory();
}
function renderLegend(){
  $('legend').innerHTML=data.banks.map(b=>`<button data-bank="${b.code}" aria-pressed="${!hiddenBanks.has(b.code)}" title="显示或隐藏${esc(b.name)}"><span class="legend-dot ${b.code}"></span>${esc(b.name)}</button>`).join('');
  for(const b of $('legend').querySelectorAll('button'))b.addEventListener('click',()=>{hiddenBanks.has(b.dataset.bank)?hiddenBanks.delete(b.dataset.bank):hiddenBanks.add(b.dataset.bank);renderLegend();renderChart();});
}
function renderChart(){
  if(!chartData)return;
  const visible=chartData.series.filter(s=>!hiddenBanks.has(s.bank));
  const points=visible.flatMap(s=>s.points.filter(p=>p.price!==null));
  if(!points.length){$('chart').innerHTML=`<div class="chart-empty"><strong>${visible.length?'历史正在积累':'曲线已全部隐藏'}</strong><span>${visible.length?'该时段还没有有效报价。服务运行后会自动记录，不填充模拟历史。':'点击上方银行名称，重新显示曲线。'}</span></div>`;return;}
  const values=points.map(p=>Number(p.price));let lo=Math.min(...values),hi=Math.max(...values);
  const pad=Math.max((hi-lo)*.18,selected==='JPY'?.000002:.0002);lo-=pad;hi+=pad;
  const start=Date.parse(chartData.from),end=Date.parse(chartData.to),W=Math.max(360,Math.min(1000,$('chart').clientWidth-20)),H=250; const plotWidth=W-90;
  const x=t=>70+(Date.parse(t)-start)/(end-start)*plotWidth,y=p=>20+(hi-Number(p))/(hi-lo)*180;
  let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${selected} 各银行历史现汇卖出价"><title>${selected} 历史报价，人民币每 1 外币</title>`;
  for(let i=0;i<5;i++){let yy=20+i*45;svg+=`<path class="chart-grid" d="M70 ${yy} H${W-20}"/><text class="chart-label" x="0" y="${yy+4}">${(hi-(hi-lo)*i/4).toFixed(selected==='JPY'?7:4)}</text>`;}
  for(let i=0;i<5;i++){const t=new Date(start+(end-start)*i/4);const label=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',...(hours===168?{month:'2-digit',day:'2-digit'}:{hour:'2-digit',minute:'2-digit',hour12:false})}).format(t);svg+=`<text class="chart-label" x="${70+i*plotWidth/4}" y="235" text-anchor="${i===0?'start':i===4?'end':'middle'}">${label}</text>`;}
  for(const s of visible){let path='',prev=null,dots='';for(const p of s.points){if(p.price===null){prev=null;continue;}const xx=x(p.t).toFixed(2),yy=y(p.price).toFixed(2);const contiguous=prev&&prev.segment===p.segment;path+=`${contiguous?'L':'M'}${xx},${yy} `;dots+=`<circle class="chart-point" cx="${xx}" cy="${yy}" r="2.5"><title>${esc(s.name)} · ${price(p.price)} 元/${selected}\n银行报价 ${esc(stamp(p.quoted_at))}\n采集 ${esc(stamp(p.t))}</title></circle>`;prev=p;}svg+=`<g class="${s.bank}"><path class="chart-line" d="${path}"/>${dots}</g>`;}
  $('chart').innerHTML=svg+'</svg>';
  $('history-note').textContent=`按实际采集时间展示 · 中断处不连线 · 悬停查看报价${chartData.sampled?' · 长时段保留极值抽样':''}`;
}
async function loadHistory(){
  const id=++requestId,cur=selected,range=hours;
  $('chart-unit').textContent=`${cur} · 人民币 / 1 外币`;
  $('download').href=`/api/export.csv?currency=${cur}&hours=${range}`;
  chartData=null;$('chart').innerHTML='<div class="chart-empty">正在读取历史记录…</div>';
  try{const result=await getJSON(`/api/history?currency=${cur}&hours=${range}`);if(id!==requestId)return;chartData=result;renderChart();}
  catch(e){if(id===requestId)$('chart').innerHTML='<div class="chart-empty"><strong>暂时无法读取走势</strong><span>请点击刷新数据重试。</span></div>';}
}
async function refresh(){
  if(activeRefresh)return;activeRefresh=true;$('refresh').disabled=true;
  try{
    data=await getJSON('/api/summary');
    $('cadence').textContent=`网页每 30 秒更新 · 银行每 ${data.interval_seconds%60===0?data.interval_seconds/60+' 分钟':data.interval_seconds+' 秒'}采集`;
    const valid=data.rows.filter(r=>r.quality==='ok').length,full=data.banks.length*codes.length;
    $('connection').classList.toggle('degraded',valid!==full);$('connection').innerHTML=`<i></i> ${valid===full?'数据正常':valid?'部分报价有效':'等待有效报价'}`;
    $('notice').classList.toggle('hidden',valid===full);
    $('notice').textContent=!data.last_cycle?'采集尚未开始。请启动采集服务，四个币种的数据将自动出现。':valid===0?'当前没有可用于比较的报价。已保留最近记录供查看，过期或失败报价不会参与排名。':`当前 ${valid}/${full} 条报价有效，最低价仅在有效样本中比较。`;
    $('collection-time').textContent=`最近采集完成 ${stamp(data.last_cycle)}${data.last_cycle&&Date.now()-Date.parse(data.last_cycle)>data.fetch_max_age_seconds*1000?' · 采集已延迟':''}`;
    $('last-render').textContent=`网页更新 ${timeOnly(data.as_of)}`;
    renderCards();renderComparison();renderLegend();await loadHistory();
  }catch(e){
    $('connection').classList.add('degraded');$('connection').innerHTML='<i></i> 连接中断';$('notice').classList.remove('hidden');$('notice').textContent='无法连接数据服务。下方最近记录仅供参考，当前排名与成本估算已暂停；30 秒后自动重试。';
    if(data){data={...data,currencies:data.currencies.map(c=>({...c,best:null,best_banks:[],spread:null,valid_banks:0})),rows:data.rows.map(r=>({...r,quality:'offline',status:'连接中断'}))};renderCards();renderComparison();}
  }finally{activeRefresh=false;$('refresh').disabled=false;}
}
$('amount').value=amounts[selected];
$('amount').addEventListener('input',()=>{const a=getAmount();if(a!==null)amounts[selected]=a;renderComparison();});
$('refresh').addEventListener('click',refresh);
for(const b of document.querySelectorAll('[data-hours]'))b.addEventListener('click',()=>{hours=Number(b.dataset.hours);for(const other of document.querySelectorAll('[data-hours]')){const on=Number(other.dataset.hours)===hours;other.classList.toggle('selected',on);other.setAttribute('aria-pressed',String(on));}loadHistory();});
$('today').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'short'}).format(new Date());
refresh();setInterval(refresh,30000);

window.addEventListener("resize",()=>{if(chartData)renderChart();});
