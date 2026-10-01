'use strict';
const $ = id => document.getElementById(id);
const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
const fmt = value => Number(value || 0).toLocaleString();
const stamp = value => value ? new Date(value).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : '—';
const duration = value => value == null ? '—' : value < 60 ? `${Number(value).toFixed(1)} s` : value < 3600 ? `${Math.floor(value / 60)} m ${Math.floor(value % 60)} s` : value < 86400 ? `${(value / 3600).toFixed(1)} h` : `${(value / 86400).toFixed(1)} d`;
const colors = {'Star':'#679ddd','Galaxy':'#df9b52','Supernova':'#ab88db','Other transient':'#d382b2','Nebula or ISM':'#68bba9','Star cluster or association':'#8aa4c9','Galaxy group or cluster':'#bca174','Compact object':'#8690de','Solar-system object':'#c1b660','Other':'#75a9ae','Unknown':'#95a1aa','Pending':'#657785','Mixed':'#adc6d0'};
const color = label => colors[label] || '#6da9b4';
const confidenceColors = {high:'#3da787',medium:'#dca044',low:'#d57467',none:'#95a1aa',pending:'#679ddd'};
function chip(label, palette = colors) { const node = el('span',label,'chip');node.style.setProperty('--chip-color',palette[label] || '#95a1aa');return node; }
const serviceColors = {Completed:'#3da787',Failed:'#d57467',Running:'#dca044','Not recorded':'#95a1aa'};
let snapshot = null, skyData = null, page = 1, generation = 0, aladin = null, objectCatalog = null, selectedDetail = null, detailGeneration = 0;

async function api(path, params = {}) {
  const response = await fetch(`${path}?${new URLSearchParams(params)}`, {signal:AbortSignal.timeout(15000)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Database request failed (${response.status})`);
  return data;
}
function filters(run) {
  const params = {run:run || snapshot?.selected_run?.run_id || 'latest', page:String(page), page_size:$('page-size').value, sky_mode:$('sky-mode').value};
  for (const id of ['search','instrument','category','confidence']) if ($(id).value) params[id] = $(id).value;
  return params;
}
function showError(error) {
  $('alert').hidden = false;
  $('alert').textContent = `${snapshot ? 'Displayed data is stale. ' : ''}${error.message} Use Refresh to try again.`;
  $('updated').textContent = snapshot ? `Last successful dashboard refresh: ${stamp(snapshot.refreshed_at)} · stale` : 'No database data available';
  if (!snapshot) { $('health-status').textContent = 'Database unavailable'; $('health-dot').className = 'dot bad'; }
}
function fillOptions(id, rows, title) {
  const previous = $(id).value;
  $(id).replaceChildren(new Option(title, ''), ...rows.map(row => new Option(`${row.label} (${fmt(row.count)})`, row.label)));
  if ([...$(id).options].some(option => option.value === previous)) $(id).value = previous;
}
function bars(id, rows, total, palette = null) {
  $(id).replaceChildren();
  if (!rows.length) { $(id).append(el('p','No results recorded yet.','muted')); return; }
  const max = Math.max(...rows.map(row => row.count), 1);
  for (const row of rows) {
    const node = el('div',undefined,'bar-row'), label = el('div',undefined,'bar-label');
    label.append(palette ? chip(row.label,palette) : el('span',row.label), el('span',`${fmt(row.count)} · ${total ? Math.round(row.count / total * 100) : 0}%`,'mono muted'));
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 100 6'); svg.setAttribute('preserveAspectRatio','none'); svg.classList.add('bar-svg'); svg.setAttribute('aria-hidden','true');
    const rect = document.createElementNS(svg.namespaceURI,'rect'); rect.setAttribute('width', String(row.count / max * 100)); rect.setAttribute('height','6'); rect.setAttribute('fill',palette ? (palette[row.label] || color(row.label)) : 'var(--accent)'); svg.append(rect);
    node.append(label,svg); $(id).append(node);
  }
}
function renderSnapshot(data) {
  const h = data.health, latest = h.latest_run, selected = data.selected_run, s = data.stats;
  const bad = latest?.status === 'partial' || h.failed_batches > 0;
  const status = !latest ? 'No runs recorded' : bad ? 'Latest run needs attention' : latest.status === 'running' ? 'Latest run in progress' : 'Latest run completed';
  $('health-status').textContent = h.overdue ? `${status} · update overdue` : status;
  $('health-dot').className = `dot ${bad ? 'bad' : h.overdue || latest?.status === 'running' ? 'warning' : latest ? 'good' : 'neutral'}`;
  $('mode').textContent = h.expected_interval_hours == null ? 'Manual testing' : `Expected every ${h.expected_interval_hours} h`;
  const latestDuration = data.history.find(row => row.run_id === latest?.run_id)?.duration_seconds;
  $('health-meta').textContent = latest ? `${latest.run_id} · ${latest.status === 'running' ? 'elapsed' : 'duration'} ${duration(latestDuration)} · data age ${duration(h.age_seconds)}` : 'Run the pipeline manually to populate this view.';
  $('last-success').textContent = latest ? `Started ${stamp(latest.started_at)} · finished ${stamp(latest.finished_at)} · last successful run ${stamp(h.last_success_at)}${h.overdue ? ' · no successful completion within the expected interval + 2 h grace' : ''}` : '';
  const runChoice = $('run').value;
  $('run').replaceChildren(new Option('Latest run · follows updates','latest'),...data.history.map(row => new Option(`${row.run_id} · ${row.status}`,row.run_id)));
  if (selected && runChoice !== 'latest' && !data.history.some(row => row.run_id === selected.run_id)) $('run').append(new Option(`${selected.run_id} · ${selected.status}`,selected.run_id));
  $('run').value = runChoice;
  $('scope').textContent = selected ? `Results: ${runChoice === 'latest' ? 'latest run' : 'historical run'} · ${selected.run_id}${s?.pending ? ` · ${fmt(s.pending)} pending classifications` : ''}` : 'No run results yet';
  $('totals').textContent = `Database: ${fmt(data.totals.observations)} unique spectra · ${fmt(data.totals.catalog_objects)} catalogue objects · ${fmt(data.totals.pipeline_runs)} runs`;
  $('metrics').replaceChildren();
  const metricRows = [ ['Spectra processed',s?.observations,`Selected run · ${fmt(s?.unique_objects)} linked objects`],['Selected matches',s?.matches,s?.observations ? `${Math.round(s.matches / s.observations * 100)}% of processed spectra` : 'Selected run'],['Unmatched spectra',s?.unmatched,`${fmt(s?.pending)} pending · unmatched is a valid result`],['Retries',h.retries,'Latest run · additional service attempts'],['Failed batches',h.failed_batches,'Latest run · requires attention when nonzero'] ];
  for (const [label,value,note] of metricRows) {
    const card = el('div',undefined,'metric'); card.append(el('div',label,'metric-label'),el('div',value == null ? '—' : fmt(value),'metric-value'),el('div',note,'metric-note')); $('metrics').append(card);
  }
  bars('categories',s?.categories || [],s?.observations,colors); bars('confidence-chart',s?.confidence || [],s?.observations,confidenceColors); bars('instruments-chart',s?.instruments || [],s?.observations);
  fillOptions('instrument',s?.instruments || [],'All instruments'); fillOptions('category',s?.categories || [],'All categories'); fillOptions('confidence',s?.confidence || [],'All confidence levels');
  $('services').replaceChildren();
  for (const [service,label] of [['eso','ESO metadata'],['simbad','SIMBAD positions'],['simbad_alias','SIMBAD aliases']]) {
    const calls = h.calls.filter(row => row.service === service);
    const state = calls.some(row => row.status === 'failed') ? 'Failed' : calls.some(row => row.status === 'running') ? 'Running' : calls.length ? 'Completed' : 'Not recorded';
    const node = el('div',undefined,'service'), top = el('div',undefined,'service-top');
    top.append(el('strong',label),chip(state,serviceColors));
    node.append(top,el('div',`${calls.length} batches · ${calls.reduce((n,row) => n + row.attempt_count,0)} attempts · ${duration(calls.reduce((n,row) => n + (row.elapsed_seconds || 0),0))}`,'service-meta')); $('services').append(node);
  }
  renderHistory(data.history);
  $('batches').replaceChildren();
  for (const row of h.calls) {
    const tr = el('tr');
    for (const value of [`${row.service} / ${row.batch_number}`,row.status,row.attempt_count,row.input_count,row.result_count ?? '—',duration(row.elapsed_seconds),row.error_message ? `${row.error_type || 'Error'}: ${row.error_message}` : '—']) tr.append(el('td',String(value)));
    $('batches').append(tr);
  }
  if (!h.calls.length) { const tr = el('tr'), td = el('td','No service calls recorded.','muted');td.colSpan=7;tr.append(td);$('batches').append(tr); }
  try { $('config').textContent = selected ? JSON.stringify(JSON.parse(selected.config_json),null,2) : 'No configuration available.'; } catch (_) { $('config').textContent = 'Stored configuration could not be decoded.'; }
  $('updated').textContent = `Database view refreshed ${stamp(data.refreshed_at)} · local time`;
}
function renderHistory(history) {
  $('history-chart').replaceChildren(); $('history-list').replaceChildren();
  if (!history.length) { $('history-chart').append(el('p','No runs recorded.','muted')); return; }
  if (history.length === 1) $('history-chart').append(el('p','One recorded run. Trends will appear as more runs finish.','muted'));
  const max = Math.max(...history.slice(0,8).map(row => row.duration_seconds || 0),1);
  for (const row of history.slice(0,8).reverse()) {
    const node = el('div',undefined,'history-row'), track = el('div',undefined,'history-track'), fill = el('div',undefined,'history-fill');
    fill.style.width = `${(row.duration_seconds || 0) / max * 100}%`; track.append(fill);
    node.append(el('span',new Date(row.started_at).toLocaleDateString(undefined,{month:'short',day:'numeric'}),'mono muted'),track,el('span',`${duration(row.duration_seconds)} · ${fmt(row.observations)} spectra`,'mono')); $('history-chart').append(node);
  }
  for (const row of history) {
    const entry = el('div',undefined,'history-entry'), button = el('button',row.run_id);
    button.addEventListener('click',() => { $('run').value = row.run_id; changeRun(); });
    entry.append(button,el('span',`${row.status} · ${stamp(row.started_at)} · ${duration(row.duration_seconds)} · ${fmt(row.observations)} spectra`,'mono muted')); $('history-list').append(entry);
  }
}
function renderResults(data, params) {
  $('results').replaceChildren(); page = data.page;
  for (const row of data.rows) {
    const tr = el('tr'), target = el('td'), button = el('button',row.target_name || 'Unnamed target','product-button');
    button.addEventListener('click',() => showDetail(row.eso_dp_id));
    target.append(button,el('span',row.eso_dp_id,'product-id')); tr.append(target,el('td',row.instrument_name || 'Unknown'),el('td',row.best_object_name || (row.confidence === 'pending' ? 'Pending' : 'No selected object')),el('td'));tr.lastChild.append(chip(row.broad_category));
    const confidence = el('td'); confidence.append(chip(row.confidence,confidenceColors)); tr.append(confidence,el('td',row.separation_arcsec == null ? '—' : `${Number(row.separation_arcsec).toFixed(2)}″`,'mono')); $('results').append(tr);
  }
  if (!data.rows.length) { const tr = el('tr'), cell = el('td','No spectra match these filters.','empty');cell.colSpan=6;tr.append(cell);$('results').append(tr); }
  $('result-count').textContent = `${fmt(data.total)} spectra${data.total ? ` · showing ${fmt((page-1)*data.page_size+1)}–${fmt(Math.min(page*data.page_size,data.total))}` : ''}`;
  $('page-count').textContent = `${fmt(page)} / ${fmt(data.pages)}`; $('next').textContent = $('page-size').value === 'all' ? 'Next batch →' : 'Next →'; $('previous').disabled=page<=1; $('next').disabled=page>=data.pages;
  if ($('page-size').value === 'all') $('result-count').textContent += ' · bounded batches; export CSV for all rows';
  $('export').href = `/api/export.csv?${new URLSearchParams(params)}`;
}
async function refresh() {
  const token = ++generation;
  $('refresh').disabled=true;
  try {
    const data = await api('/api/snapshot',{run:$('run').value});
    if (token !== generation) return;
    if (snapshot && snapshot.selected_run?.run_id !== data.selected_run?.run_id) {
      page=1; selectedDetail=null; ++detailGeneration; $('detail').hidden=true;
      for (const id of ['search','instrument','category','confidence']) $(id).value='';
    }
    // Fix the run ID for all requests, even if a new run appears mid-refresh.
    const params = filters(data.selected_run?.run_id);
    const [results, sky] = data.selected_run ? await Promise.all([api('/api/results',params),api('/api/sky',params)]) : [{rows:[],total:0,page:1,pages:1},{positions:[],objects:[],total_positions:0,total_objects:0}];
    if (token !== generation) return;
    snapshot=data; skyData=sky; renderSnapshot(data); renderResults(results,params); renderSky(); $('alert').hidden=true;
  } catch(error) { if (token === generation) showError(error); }
  finally { if(token===generation) $('refresh').disabled=false; }
}
async function refreshResults() {
  const token = ++generation, params = filters();
  $('refresh').disabled = true;
  try {
    const data = await api('/api/results',params);
    if(token === generation) { renderResults(data,params); $('alert').hidden=true; }
  } catch(error) { if(token === generation) showError(error); }
  finally { if(token === generation) $('refresh').disabled=false; }
}
function changeRun() { page=1; selectedDetail=null; ++detailGeneration; $('detail').hidden=true; for (const id of ['search','instrument','category','confidence']) $(id).value=''; refresh(); }

async function showDetail(product) {
  const token=++detailGeneration, run=snapshot?.selected_run?.run_id;
  if (!run) return;
  try {
    const data=await api('/api/detail',{run,product});
    if(token!==detailGeneration || run!==snapshot?.selected_run?.run_id) return;
    selectedDetail=data; const container=$('detail');container.replaceChildren();container.hidden=false;
    const top=el('div',undefined,'detail-top'), close=el('button','Close'); close.addEventListener('click',()=>{container.hidden=true;selectedDetail=null; ++detailGeneration; renderSky();});
    top.append(el('strong',data.target_name || data.eso_dp_id),close);container.append(top);
    const dl=el('dl');
    for(const [label,value] of [['Product',data.eso_dp_id],['Selected object',data.best_object_name || 'None'],['Classification',[data.broad_category,data.subcategory,data.classification_detail].filter(Boolean).join(' · ')],['Confidence',data.confidence],['Match method',data.match_method || 'Pending'],['Position',`${data.ra_deg.toFixed(6)}°, ${data.dec_deg.toFixed(6)}° (ICRS)`],['Search radius',`${(data.search_radius_deg*3600).toFixed(2)} arcsec`],['Candidates',data.candidate_group_count ?? 'Pending'],['Aliases',data.alias_complete == null ? 'Pending' : data.alias_complete ? 'Complete' : 'Incomplete'],['Versions',`${data.ranking_version || '—'} / ${data.taxonomy_version || '—'}`]]) {dl.append(el('dt',label),el('dd',String(value)));}
    container.append(dl);
    if(data.candidates.length) container.append(el('p',`Nearest catalogue candidates (up to 50, current metadata): ${data.candidates.map(row=>`${row.preferred_name || 'Unnamed'} (${row.separation_arcsec.toFixed(2)}″)`).join('; ')}`,'muted'));
    if(data.coincident_products.length>1) container.append(el('p',`${fmt(data.coincident_count)} products at these coordinates (listed up to 50): ${data.coincident_products.join(', ')}`,'mono muted'));
    if(data.access_url) { try { const url=new URL(data.access_url); if(['http:','https:'].includes(url.protocol)){const link=el('a','Open ESO product ↗');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';container.append(link);} } catch(_){} }
    if(aladin){aladin.gotoRaDec(data.ra_deg,data.dec_deg);aladin.setFoV(Math.max(0.02,data.search_radius_deg*8));renderSky();}
    container.scrollIntoView({behavior:'smooth',block:'nearest'});
  }catch(error){showError(error);}
}
function renderSky() {
  if (!skyData) return;
  const coverage = skyData.mode === 'moc';
  $('object-layer').disabled = coverage;
  $('sky-count').textContent = coverage ? `Coverage · ${fmt(skyData.covered_spectra)} / ${fmt(skyData.total_spectra)} spectra · order ${skyData.moc_order}` : `${fmt(skyData.positions.length)} / ${fmt(skyData.total_positions)} positions · ${fmt(skyData.objects.length)} / ${fmt(skyData.total_objects)} objects`;
  const categories = coverage ? skyData.coverage.map(row=>row.category) : [...new Set(skyData.positions.map(row=>row.category))];
  $('map-legend').replaceChildren(...categories.map(category=>chip(category)));
  if(!aladin) return;
  aladin.removeOverlays();
  if(coverage) {
    for(const item of skyData.coverage) aladin.addMOC(A.MOCFromJSON(item.moc,{name:`Spectra · ${item.category}`,color:color(item.category),opacity:0.7,lineWidth:2}));
    objectCatalog=null;
  } else {
  for(const category of categories){
    const cat=A.catalog({name:`Spectra · ${category}`,color:color(category),sourceSize:9,shape:'circle',onClick:source=>{if(source?.data?.product) showDetail(source.data.product);}});
    aladin.addCatalog(cat);cat.addSources(skyData.positions.filter(row=>row.category===category).map(row=>A.source(row.ra_deg,row.dec_deg,{product:row.eso_dp_id,count:row.count})));
  }
  objectCatalog=A.catalog({name:'Selected SIMBAD objects',color:'#e4cb80',sourceSize:8,shape:'plus'});aladin.addCatalog(objectCatalog);
  objectCatalog.addSources(skyData.objects.map(row=>A.source(row.ra_deg,row.dec_deg,{name:row.preferred_name || row.catalog_object_id})));
  if(!$('object-layer').checked)objectCatalog.hide();
  }
  if(selectedDetail){const overlay=A.graphicOverlay({name:'Prototype search region',color:'#f2d77c',lineWidth:2});aladin.addOverlay(overlay);overlay.add(A.circle(selectedDetail.ra_deg,selectedDetail.dec_deg,selectedDetail.search_radius_deg));}
}
function skyFailure(message) { $('sky-message').hidden=false;$('sky-message').replaceChildren(el('strong','Sky viewer unavailable'),el('p',message)); }
async function initSky() {
  try {
    await new Promise((resolve,reject)=>{
      const script=document.createElement('script');script.src='https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js';
      const timer=setTimeout(()=>reject(new Error('Aladin timed out. Check internet access and reload to retry.')),20000);
      script.onload=()=>{clearTimeout(timer);resolve();};script.onerror=()=>{clearTimeout(timer);reject(new Error('Aladin could not be fetched. Results and statistics remain available.'));};document.head.append(script);
    });
    let timer;
    try { await Promise.race([A.init,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Aladin initialization timed out. Check WebGL support and reload to retry.')),20000);})]); } finally {clearTimeout(timer);}
    aladin=A.aladin('#aladin',{survey:'https://alasky.cds.unistra.fr/MellingerRGB/',target:'266.4051 -28.936175',fov:180,projection:'STG',cooFrame:'ICRSd',showCooGridControl:true,showShareControl:false,showFullscreenControl:true});
    $('sky-message').hidden=true;
    if(selectedDetail){aladin.gotoRaDec(selectedDetail.ra_deg,selectedDetail.dec_deg);aladin.setFoV(Math.max(0.02,selectedDetail.search_radius_deg*8));}
    renderSky();
  }catch(error){skyFailure(error.message);}
}
$('refresh').addEventListener('click',refresh);$('run').addEventListener('change',changeRun);
$('theme').addEventListener('click',()=>{const current=document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');const theme=current==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('eso-monitor-theme',theme);}catch(_){} });
let searchTimer; $('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;refresh();},300);});
for(const id of ['instrument','category','confidence']) $(id).addEventListener('change',()=>{page=1;refresh();});
$('clear').addEventListener('click',()=>{for(const id of ['search','instrument','category','confidence']) $(id).value='';page=1;refresh();});
$('previous').addEventListener('click',()=>{page--;refreshResults();});$('next').addEventListener('click',()=>{page++;refreshResults();});
$('page-size').addEventListener('change',()=>{page=1;refreshResults();});
$('sky-mode').addEventListener('change',refresh);
$('object-layer').addEventListener('change',()=>{if(objectCatalog){if($('object-layer').checked)objectCatalog.show();else objectCatalog.hide();}});
$('reset-sky').addEventListener('click',()=>{if(aladin){aladin.gotoRaDec(266.4051,-28.936175);aladin.setFoV(180);}selectedDetail=null;++detailGeneration;$('detail').hidden=true;renderSky();});
refresh();initSky();setInterval(()=>{if(!document.hidden)refresh();},30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
