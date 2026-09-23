const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state,portfolio,activeTeam,sourceRequest,activeTab='lineup';

async function api(path,body){
 const res=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
 let data={};try{data=await res.json()}catch{}
 if(!res.ok)throw new Error(data.detail||`Request failed (${res.status})`);
 return data;
}
function toast(message){const t=$('#toast');t.textContent=message;t.hidden=false;setTimeout(()=>t.hidden=true,5000)}
function loading(on,text='Pulling rules, rosters, projections, and injuries.'){
 $('#landing').hidden=on;$('#results').hidden=true;$('#teamView').hidden=true;$('#loading').hidden=!on;$('#loadingText').textContent=text;
}
function weeks(){return Array.from({length:18},(_,i)=>`<option value="${i+1}">Week ${i+1}</option>`).join('')}
async function init(){state=await api('/api/state');$('#espnSeason').value=state.nfl.season;$('#weekSelect').innerHTML=weeks()}
init().catch(e=>toast(e.message));

$('#sleeperForm').onsubmit=async e=>{
 e.preventDefault();sourceRequest={kind:'sleeper',username:$('#sleeperUsername').value.trim()};await analyze();
};
$('#espnForm').onsubmit=async e=>{
 e.preventDefault();
 try{
  $('#findEspn').disabled=true;$('#findEspn').textContent='Finding teams…';
  const data=await api('/api/espn/teams',{league_id:$('#espnLeague').value.trim(),season:+$('#espnSeason').value});
  $('#espnTeam').innerHTML=data.teams.map(t=>`<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
  $('#espnTeamPicker').hidden=false;
 }catch(e){toast(e.message)}
 finally{$('#findEspn').disabled=false;$('#findEspn').textContent='Find teams'}
};
$('#analyzeEspn').onclick=async()=>{
 sourceRequest={kind:'espn',league_id:$('#espnLeague').value.trim(),team_id:$('#espnTeam').value,season:+$('#espnSeason').value};await analyze();
};
async function analyze(week){
 loading(true);
 try{
  let path,body={...sourceRequest};
  if(week)body.week=+week;
  if(sourceRequest.kind==='sleeper'){path='/api/analyze/sleeper';delete body.kind}
  else{path='/api/analyze/espn';delete body.kind}
  portfolio=await api(path,body);renderPortfolio();
 }catch(e){loading(false);$('#landing').hidden=false;toast(e.message)}
}
function renderPortfolio(){
 $('#loading').hidden=true;$('#landing').hidden=true;$('#teamView').hidden=true;$('#results').hidden=false;
 $('#weekSelect').value=portfolio.week;
 $('#resultTitle').textContent=`${portfolio.viewer?.label||'Your'} fantasy portfolio`;
 $('#resultMeta').textContent=`${portfolio.viewer?.platform||''} · ${portfolio.season} Week ${portfolio.week} · analyzed in memory`;
 const s=portfolio.summary||{};
 $('#summary').innerHTML=[
  ['Leagues',s.leagues],['Lineup changes',s.lineup_changes],['Waiver targets',s.waiver_targets],['Injury alerts',s.injury_alerts]
 ].map(x=>`<div class="stat"><span>${x[0]}</span><b>${x[1]??0}</b><small>this portfolio</small></div>`).join('');
 $('#teamGrid').innerHTML=portfolio.teams?.length?portfolio.teams.map((t,i)=>`<button class="team-card" onclick="openTeam(${i})"><span class="eyebrow">${esc(t.platform)} · WEEK ${portfolio.week}</span><h3>${esc(t.team)}</h3><p>${esc(t.league)}</p><div class="chips"><span class="chip">${esc(t.league_config?.format||'Standard')}</span><span class="chip">${esc(t.league_config?.scoring||'Platform scoring')}</span><span class="chip">${esc(t.league_config?.waivers||'Waivers')}</span></div><div class="quick"><div><small>Changes</small><b>${t.changes}</b></div><div><small>Need</small><b>${esc(t.need_text)}</b></div><div><small>Alerts</small><b>${t.urgent_alerts||0}</b></div></div></button>`).join(''):`<div class="empty">No active leagues were found for this season.</div>`;
}
window.openTeam=i=>{
 activeTeam=portfolio.teams[i];activeTab='lineup';$('#results').hidden=true;$('#teamView').hidden=false;scrollTo(0,0);
 $('#teamPlatform').textContent=`${activeTeam.platform} · WEEK ${portfolio.week}`;
 $('#teamTitle').textContent=activeTeam.team;$('#teamLeague').textContent=activeTeam.league;
 $('#teamNeed').textContent=activeTeam.need_text;$('#teamStrength').textContent=activeTeam.strength_text;
 const c=activeTeam.league_config||{};
 $('#settings').innerHTML=[
  ['Format',c.format],['Starting slots',c.starting_slots_text],['Scoring',c.scoring],['Draft',c.draft],['Waivers',c.waivers],['League',`${c.team_count||'—'} teams · ${c.league_type||'Redraft'}`]
 ].map(x=>`<div class="setting"><span>${x[0]}</span><b>${esc(x[1]||'Not reported')}</b></div>`).join('');
 [...$('#tabs').children].forEach(x=>x.classList.toggle('active',x.dataset.tab==='lineup'));renderDetail();
};
function player(p){return `<div class="player"><span class="avatar">${esc(p.position)}</span><span><b>${esc(p.name)}</b><small>${esc(p.team)} vs ${esc(p.opponent)}</small></span></div>`}
function table(title,desc,heads,rows,empty='No qualifying players found.'){
 return `<div class="table-card"><h2>${title}</h2><p>${desc}</p>${rows?`<table><thead><tr>${heads.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table>`:`<div class="empty">${empty}</div>`}</div>`;
}
function renderDetail(){
 let rows='',html='';
 if(activeTab==='lineup'){
  rows=activeTeam.lineup.map(p=>`<tr><td>${player(p)}</td><td class="${p.recommendation==='Start'?'good':''}">${p.recommendation} · ${esc(p.recommended_slot)}</td><td>${p.current}</td><td>${p.projected}</td><td>${p.lineup_value}</td><td>${p.recent}</td><td>${p.consistency_label}</td><td class="reason">${esc(p.reason)}</td></tr>`).join('');
  html=table('Recommended lineup','Every legal slot is optimized using this league’s scoring and FLEX/SUPERFLEX rules.',['Player','Recommendation','Current','Projection','Lineup value','Recent','Consistency','Evidence'],rows);
 }else if(activeTab==='waivers'){
  rows=activeTeam.waivers.map((p,i)=>`<tr><td>${i+1}</td><td>${player(p)}</td><td>${p.projected}</td><td>${Number(p.lineup_gain||0).toFixed(2)}</td><td>${activeTeam.league_config?.is_faab?`${p.faab_pct}% FAAB`:'Priority claim'}</td><td>${esc(p.drop||'Add for depth')}</td><td class="reason">${esc(p.why)}</td></tr>`).join('');
  html=table('Verified waiver targets','Candidates are absent from every roster in this league; lineup gain uses its exact rules.',['#','Target','Projection','Lineup gain','Bid / claim','Move','Why'],rows);
 }else if(activeTab==='targets'){
  rows=activeTeam.trade_targets.map((p,i)=>`<tr><td>${i+1}</td><td>${player(p)}</td><td>${esc(p.owner)}</td><td>${esc(p.market)}</td><td>${p.projected}</td><td>${Number(p.lineup_gain||0).toFixed(2)}</td><td class="reason">${esc(p.why)}</td></tr>`).join('');
  html=table('Trade targets','Ranked by marginal legal-lineup gain plus need and counterpart depth.',['#','Target','Manager','Tier','Projection','Lineup gain','Why'],rows);
 }else if(activeTab==='away'){
  rows=activeTeam.trade_away.map((p,i)=>`<tr><td>${i+1}</td><td>${player(p)}</td><td>${esc(p.market)}</td><td>${p.projected}</td><td>${Number(p.lineup_loss||0).toFixed(2)}</td><td class="reason">${esc(p.why)}</td></tr>`).join('');
  html=table('Players to consider shopping','The model protects players whose removal materially damages this league’s legal lineup.',['#','Player','Tier','Projection','Lineup loss','Why'],rows);
 }else{
  rows=(activeTeam.alerts||[]).map(p=>`<tr><td>${player(p)}</td><td class="${p.urgency>=2?'bad':''}">${esc(p.status)}</td><td>${esc(p.practice||'No practice note')}</td><td>${esc(p.kickoff||'TBD')}</td><td class="reason">${esc(p.update||'No additional update')}</td></tr>`).join('');
  html=table('Live player status','Check final inactives shortly before kickoff.',['Player','Status','Practice','Kickoff','Latest context'],rows,'No current injury alerts.');
 }
 $('#detail').innerHTML=html;
}
$('#tabs').onclick=e=>{if(e.target.dataset.tab){activeTab=e.target.dataset.tab;[...$('#tabs').children].forEach(x=>x.classList.toggle('active',x===e.target));renderDetail()}};
$('#weekSelect').onchange=e=>analyze(e.target.value);
$('#backPortfolio').onclick=()=>{ $('#teamView').hidden=true;$('#results').hidden=false;scrollTo(0,0)};
$('#startOver').onclick=()=>{portfolio=null;sourceRequest=null;$('#results').hidden=true;$('#teamView').hidden=true;$('#landing').hidden=false;scrollTo(0,0)};
