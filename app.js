const DATA_URL = 'data/watchlist.csv';
const columns = [
  'Stock','Company Name','Entry Type','Cross Date','Cross Price','LTP','Growth %','Max Growth %','Drawdown %',
  'Monthly RSI','Weekly RSI','Hourly RSI','Prev Monthly RSI','Days Since Cross',
  'H-RSI Touch ≤30 Count','H-RSI Touch Dates','M-RSI Status','W-RSI Status','H-RSI Status','ISIN Code','Instrument Key'
];
let allRows = [];

function parseCSV(text) {
  const rows=[]; let row=[]; let cell=''; let quoted=false;
  for(let i=0;i<text.length;i++) {
    const ch=text[i], next=text[i+1];
    if(ch==='"') { if(quoted && next==='"'){ cell+='"'; i++; } else quoted=!quoted; }
    else if(ch===',' && !quoted){ row.push(cell); cell=''; }
    else if((ch==='\n' || ch==='\r') && !quoted){ if(ch==='\r' && next==='\n') i++; row.push(cell); cell=''; if(row.some(x=>x!=='')) rows.push(row); row=[]; }
    else cell+=ch;
  }
  if(cell!=='' || row.length){ row.push(cell); if(row.some(x=>x!=='')) rows.push(row); }
  if(!rows.length) return [];
  const headers=rows[0];
  return rows.slice(1).map(r => Object.fromEntries(headers.map((h,i)=>[h,(r[i]??'')] )));
}

function num(v){ const n=parseFloat(String(v).replace('%','')); return Number.isFinite(n)?n:null; }
function esc(v){ return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function pct(v){ const n=num(v); return n===null?'—':`${n.toFixed(2)}%`; }
function clsStatus(v){ const s=String(v||''); if(s.includes('🟢')) return 'status-green'; if(s.includes('🔴')) return 'status-red'; if(s.includes('🔵')) return 'status-blue'; return ''; }

function render() {
  const q=document.getElementById('searchInput').value.trim().toLowerCase();
  const sort=document.getElementById('sortSelect').value;
  const dir=document.getElementById('directionSelect').value;
  let rows=allRows.filter(r=>Object.values(r).some(v=>String(v).toLowerCase().includes(q)));
  rows.sort((a,b)=>{
    let av=a[sort], bv=b[sort];
    const an=num(av), bn=num(bv);
    if(an!==null && bn!==null) return dir==='asc'?an-bn:bn-an;
    return dir==='asc'?String(av).localeCompare(String(bv)):String(bv).localeCompare(String(av));
  });

  const thead=document.querySelector('#watchlistTable thead');
  thead.innerHTML=`<tr>${columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>`;
  const tbody=document.querySelector('#watchlistTable tbody');
  tbody.innerHTML=rows.map(r=>`<tr>${columns.map(c=>{
    let v=r[c]??'';
    if(['Growth %','Max Growth %','Drawdown %'].includes(c)) v=pct(v);
    if(['Cross Price','LTP'].includes(c)){ const n=num(v); v=n===null?'—':n.toFixed(2); }
    if(['Monthly RSI','Weekly RSI','Hourly RSI','Prev Monthly RSI'].includes(c)){ const n=num(v); v=n===null?'—':n.toFixed(2); }
    const cls=c.includes('Status')?clsStatus(v):(['Growth %','Max Growth %','Drawdown %'].includes(c)?(num(v)>0?'positive':num(v)<0?'negative':''):'');
    if(c === 'Stock' && v){
  const symbol = String(v).trim();
  const chartUrl = `https://chartink.com/stocks/${encodeURIComponent(symbol)}.html`;

  v = `<a class="stock-link"
           href="${chartUrl}"
           target="_blank"
           rel="noopener noreferrer">${esc(symbol)}</a>`;

  return `<td class="${cls}">${v}</td>`;
}

  return `<td class="${cls}">${esc(v)}</td>`;
  }).join('')}</tr>`).join('');
  document.getElementById('emptyState').style.display=rows.length?'none':'block';

  document.getElementById('totalStocks').textContent=allRows.length;
  document.getElementById('monthlyAbove70').textContent=allRows.filter(r=>num(r['Monthly RSI'])>70).length;
  document.getElementById('hourlyBelow30').textContent=allRows.filter(r=>num(r['Hourly RSI'])<=30).length;
  document.getElementById('positiveGrowth').textContent=allRows.filter(r=>num(r['Growth %'])>0).length;
}

async function loadData() {
  document.getElementById('lastUpdated').textContent='Loading...';
  try {
    const res=await fetch(`${DATA_URL}?t=${Date.now()}`);
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    allRows=parseCSV(await res.text());
    render();
    document.getElementById('lastUpdated').textContent=`Updated: ${new Date().toLocaleString('en-IN')}`;
  } catch(e) {
    allRows=[]; render();
    document.getElementById('lastUpdated').textContent='No data file yet';
    console.error(e);
  }
}

document.getElementById('searchInput').addEventListener('input',render);
document.getElementById('sortSelect').addEventListener('change',render);
document.getElementById('directionSelect').addEventListener('change',render);
document.getElementById('refreshBtn').addEventListener('click',loadData);
loadData();
