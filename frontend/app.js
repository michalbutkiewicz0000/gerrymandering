const escapeHtml=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
function coordinatePairs(value,result=[]){if(Array.isArray(value)&&value.length>=2&&typeof value[0]==='number'&&typeof value[1]==='number'){result.push(value);return result}if(Array.isArray(value))value.forEach(child=>coordinatePairs(child,result));return result}
const svgNamespace='http://www.w3.org/2000/svg',districtColors=['#2e7d63','#d7903f','#526ea8','#a85967','#8a6d3b','#5d8f91','#8064a2','#789447'];
function geometryPolygons(geometry){if(!geometry)return[];if(geometry.type==='Polygon')return[geometry.coordinates];if(geometry.type==='MultiPolygon')return geometry.coordinates;return[]}
function renderGeoJson(data){
  const svg=document.querySelector('#map'),coordinates=data.features.flatMap(feature=>coordinatePairs(feature.geometry?.coordinates??[]));svg.replaceChildren();
  if(!coordinates.length){const text=document.createElementNS(svgNamespace,'text');text.setAttribute('x','500');text.setAttribute('y','300');text.setAttribute('text-anchor','middle');text.textContent='Plan nie zawiera geometrii.';svg.append(text);return}
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  coordinates.forEach(point=>{minX=Math.min(minX,point[0]);maxX=Math.max(maxX,point[0]);minY=Math.min(minY,point[1]);maxY=Math.max(maxY,point[1])});
  const width=950,height=550,scale=Math.min(width/Math.max(maxX-minX,1e-12),height/Math.max(maxY-minY,1e-12)),offsetX=(1000-(maxX-minX)*scale)/2,offsetY=(600-(maxY-minY)*scale)/2;
  const project=point=>[offsetX+(point[0]-minX)*scale,600-(offsetY+(point[1]-minY)*scale)];
  data.features.forEach(feature=>geometryPolygons(feature.geometry).forEach(polygon=>{
    const path=document.createElementNS(svgNamespace,'path'),district=Number(feature.properties?.district??0),commands=polygon.map(ring=>ring.map((point,index)=>{const [x,y]=project(point);return`${index?'L':'M'}${x.toFixed(2)},${y.toFixed(2)}`}).join(' ')+' Z').join(' ');
    path.setAttribute('d',commands);path.setAttribute('fill',districtColors[Math.abs(district)%districtColors.length]);path.setAttribute('fill-rule','evenodd');path.setAttribute('stroke','#152820');path.setAttribute('stroke-width','1.2');path.setAttribute('vector-effect','non-scaling-stroke');
    const title=document.createElementNS(svgNamespace,'title');title.textContent=`${feature.properties?.node??'jednostka'} — okręg ${feature.properties?.district??'—'}`;path.append(title);svg.append(path);
  }));
}

async function showRun(id,alternative=null){
  const query=alternative===null?'':`&alternative=${alternative}`;
  const mapResponse=await fetch(`/api/optimizations/${id}/export?format=geojson${query}`);if(mapResponse.ok)renderGeoJson(await mapResponse.json());
  const run=await fetch(`/api/optimizations/${id}`).then(response=>response.json());
  const buttons=[`<button data-plan="main">Plan główny</button>`].concat(run.alternatives.map((_,index)=>`<button data-plan="${index}">Alternatywa ${index+1}</button>`)).join('');
  const exportLinks=run.incumbent?['json','csv','geojson','gpkg','html'].map(format=>`<a class="button-link" href="/api/optimizations/${id}/export?format=${format}${alternative===null?'':`&alternative=${alternative}`}">${format.toUpperCase()}</a>`).join(''):'';
  const cancellable=['QUEUED','RUNNING','FEASIBLE_CHECKPOINT'].includes(run.status)?`<button id="cancel-run">Anuluj zadanie</button>`:'';
  document.querySelector('#run-details').innerHTML=`<h3>${escapeHtml(run.status)} — ${escapeHtml(run.request.target)}</h3><p>${escapeHtml(run.message)}</p><p>Mandaty celu: <strong>${run.incumbent?.target_seats??'—'}</strong> · Certyfikat: <strong>${run.certificate_verified?'zweryfikowany':'niezweryfikowany'}</strong></p><div class="variant-buttons">${buttons}</div><div class="actions">${exportLinks}${cancellable}</div>`;
  document.querySelectorAll('#run-details [data-plan]').forEach(button=>button.onclick=()=>showRun(id,button.dataset.plan==='main'?null:Number(button.dataset.plan)));
  const cancel=document.querySelector('#cancel-run');if(cancel)cancel.onclick=async()=>{cancel.disabled=true;await fetch(`/api/optimizations/${id}/cancel`,{method:'POST'});await showRun(id,alternative);await load()};
  const certificate=document.querySelector('#certificate-details');certificate.innerHTML='';
  if(run.certificate_path){
    const response=await fetch(`/api/optimizations/${id}/certificate`);
    if(response.ok){
      const manifest=await response.json(),proofs=manifest.proofs??[];
      certificate.innerHTML=`<h3>Manifest dowodu</h3><p>Algorytm: <code>${escapeHtml(manifest.algorithm)}</code> · schemat: ${escapeHtml(manifest.schema_version??1)} · etapów: ${proofs.length}/${escapeHtml(manifest.expected_stages)}</p>${manifest.integrity_verified!==undefined?`<p>Integralność plików: <strong class="${manifest.integrity_verified?'ok':'warn'}">${manifest.integrity_verified?'potwierdzona':'NIEPOTWIERDZONA'}</strong> — ${escapeHtml(manifest.integrity_detail)}</p>`:''}<p>Żądanie SHA-256: <code>${escapeHtml(manifest.request_sha256??'brak w starszym manifeście')}</code></p><div class="proof-list">${proofs.map(proof=>`<div><strong>${escapeHtml(proof.stage??'etap')}</strong><span class="${proof.verified?'ok':'warn'}">${proof.verified?'zweryfikowany':'błąd'}</span><small>model <code>${escapeHtml(proof.model_sha256??'—')}</code><br>dowód <code>${escapeHtml(proof.proof_sha256??proof.sha256??'—')}</code></small></div>`).join('')}</div>`;
    }else{certificate.innerHTML='<p class="notice">Manifest certyfikatu jest niedostępny lub niespójny z zadaniem.</p>'}
  }
}

function applyForm(body){
  body.profile_id=document.querySelector('#profile').value||body.profile_id;
  body.target_kind=document.querySelector('#target-kind').value;
  body.target=document.querySelector('#target').value.trim();
  body.alternatives=Number(document.querySelector('#alternatives').value);
  body.candidate_anchor=body.target_kind==='candidate'?document.querySelector('#candidate-anchor').value.trim()||null:null;
  body.rules=body.rules??{};body.rules.district_count=Number(document.querySelector('#district-count').value);
  const seats=document.querySelector('#seats').value.trim();body.rules.seats_per_district=seats.startsWith('{')?JSON.parse(seats):Number(seats);
  body.rules.population_tolerance=Number(document.querySelector('#population-tolerance').value);
  const maxCut=document.querySelector('#max-cut-border').value;body.rules.max_cut_border_m=maxCut===''?null:Number(maxCut);
  body.rules.allowed_edge_kinds=Array.from(document.querySelector('#edge-kinds').selectedOptions).map(option=>option.value);
  return body;
}

function populateForm(body){
  document.querySelector('#profile').value=body.profile_id;
  document.querySelector('#target-kind').value=body.target_kind;
  document.querySelector('#target').value=body.target;
  document.querySelector('#alternatives').value=body.alternatives;
  document.querySelector('#candidate-anchor').value=body.candidate_anchor??'';
  document.querySelector('#district-count').value=body.rules.district_count;
  document.querySelector('#seats').value=typeof body.rules.seats_per_district==='object'?JSON.stringify(body.rules.seats_per_district):body.rules.seats_per_district;
  document.querySelector('#population-tolerance').value=body.rules.population_tolerance;
  document.querySelector('#max-cut-border').value=body.rules.max_cut_border_m??'';
  const allowed=new Set(body.rules.allowed_edge_kinds??['physical']);Array.from(document.querySelector('#edge-kinds').options).forEach(option=>option.selected=allowed.has(option.value));
}

async function loadData(){
  const snapshot=document.querySelector('#snapshot').value,scenario=document.querySelector('#scenario').value,status=document.querySelector('#load-data-status');
  if(!snapshot||!scenario){status.textContent='Wybierz migawkę i scenariusz.';return}
  try{
    const [graph,votes]=await Promise.all([
      fetch(`/api/graphs/${snapshot}`).then(response=>{if(!response.ok)throw new Error('Brak aktualnego grafu dla migawki');return response.json()}),
      fetch(`/api/scenarios/${scenario}`).then(response=>{if(!response.ok)throw new Error('Brak scenariusza');return response.json()}),
    ]);
    if(votes.snapshot_id&&votes.snapshot_id!==snapshot)throw new Error('Scenariusz należy do innej migawki');
    const body=JSON.parse(document.querySelector('#request').value);body.nodes=graph.node_ids;body.edges=graph.edges;body.scenario=votes;
    document.querySelector('#request').value=JSON.stringify(applyForm(body),null,2);status.textContent=`Załadowano ${graph.node_ids.length} węzłów i ${graph.edges.length} krawędzi.`;
  }catch(error){status.textContent=`Błąd: ${error.message}`}
}

async function loadReconstructionReport(){
  const snapshot=document.querySelector('#snapshot').value,container=document.querySelector('#reconstruction-report');
  if(!snapshot){container.textContent='Wybierz migawkę.';return}
  const response=await fetch(`/api/reconstruction/${snapshot}/report?failed_only=true&limit=50&offset=0`);
  if(response.status===404){container.textContent='Dla tej migawki nie ma jeszcze raportu rekonstrukcji.';return}
  if(!response.ok){container.textContent='Nie udało się odczytać raportu.';return}
  const payload=await response.json(),manifest=payload.manifest??{},complete=manifest.complete_country?'tak':'nie';
  container.innerHTML=`<dl><dt>Gminy poprawne</dt><dd>${escapeHtml(manifest.successful??'—')}</dd><dt>Błędy</dt><dd class="${(manifest.failed??payload.total)>0?'warn':'ok'}">${escapeHtml(manifest.failed??payload.total)}</dd><dt>Kompletny kraj</dt><dd>${complete}</dd></dl>${payload.reports.length?`<div class="quality-list">${payload.reports.map(report=>`<div><strong>${escapeHtml(report.teryt??'—')}</strong><span>${escapeHtml(report.error??'wymaga kontroli jakości')}</span></div>`).join('')}</div>`:'<p class="ok">Brak zgłoszonych błędów.</p>'}`;
}

async function loadAssets(){
  const [snapshots,scenarios]=await Promise.all([fetch('/api/snapshots').then(response=>response.json()),fetch('/api/scenarios?limit=200&offset=0').then(response=>response.json())]);
  document.querySelector('#snapshot').innerHTML='<option value="">— wybierz —</option>'+snapshots.map(item=>`<option value="${item.id}">${escapeHtml(item.election_id)} · ${escapeHtml(item.effective_date)}</option>`).join('');
  document.querySelector('#scenario').innerHTML='<option value="">— wybierz —</option>'+scenarios.map(item=>`<option value="${item.id}" data-snapshot="${item.snapshot_id??''}">${escapeHtml(item.name)}</option>`).join('');
}

async function submit(){
  const button=document.querySelector('#submit'),status=document.querySelector('#submit-status');button.disabled=true;
  try{
    const body=applyForm(JSON.parse(document.querySelector('#request').value));
    document.querySelector('#request').value=JSON.stringify(body,null,2);
    const response=await fetch('/api/optimizations',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
    const payload=await response.json();if(!response.ok)throw new Error(JSON.stringify(payload.detail));
    status.textContent=`Dodano ${payload.id}`;await load();
  }catch(error){status.textContent=`Błąd: ${error.message}`}finally{button.disabled=false}
}

async function load(){
  const health=await fetch('/health').then(response=>response.json());document.querySelector('#health').textContent=health.status==='ok'?'API działa':'Błąd';
  const capabilities=await fetch('/api/system/capabilities').then(response=>response.json());document.querySelector('#capabilities').innerHTML=`<dt>Duży solver exact</dt><dd class="${capabilities.certified_large_jobs?'ok':'warn'}">${capabilities.certified_large_jobs?'gotowy':'niedostępny'}</dd><dt>Biblioteka</dt><dd>${escapeHtml(capabilities.scip_detail)}</dd><dt>Mały solver</dt><dd>pełne wyczerpanie do ${capabilities.exhaustive_node_limit} węzłów</dd>`;
  const profiles=await fetch('/api/profiles').then(response=>response.json());
  document.querySelector('#profiles').innerHTML=Object.entries(profiles).map(([id,citation])=>`<li><strong>${escapeHtml(id)}</strong><br><small>${escapeHtml(citation)}</small></li>`).join('');
  const select=document.querySelector('#profile');if(!select.options.length)select.innerHTML=Object.keys(profiles).map(id=>`<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('');
  const runs=await fetch('/api/optimizations?limit=100&offset=0').then(response=>response.json());
  document.querySelector('#runs').innerHTML=runs.length?runs.map(run=>`<div class="run" data-id="${run.id}"><strong>${escapeHtml(run.status)}</strong><code>${escapeHtml(run.id)}</code><span>${run.incumbent?run.incumbent.target_seats+' mandatów':''}</span></div>`).join(''):'Brak zadań.';
  document.querySelectorAll('.run').forEach(element=>element.onclick=()=>showRun(element.dataset.id));
}

async function init(){
  await Promise.all([load(),loadAssets()]);
  const example=await fetch('/api/examples/small').then(response=>response.json());
  document.querySelector('#request').value=JSON.stringify(example,null,2);populateForm(example);
  document.querySelector('#submit').onclick=submit;
  document.querySelector('#load-data').onclick=loadData;
  document.querySelector('#snapshot').onchange=loadReconstructionReport;
}
init().catch(error=>{document.querySelector('#health').textContent=`Błąd: ${error.message}`});setInterval(load,5000);
