const escapeHtml=value=>String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
function coordinatePairs(value,result=[]){if(Array.isArray(value)&&value.length>=2&&typeof value[0]==='number'&&typeof value[1]==='number'){result.push(value);return result}if(Array.isArray(value))value.forEach(child=>coordinatePairs(child,result));return result}
const svgNamespace='http://www.w3.org/2000/svg',districtColors=['#2e7d63','#d7903f','#526ea8','#a85967','#8a6d3b','#5d8f91','#8064a2','#789447'];
const PROFILE_LABELS={'generic-jow':'Ogólny — okręgi jednomandatowe (JOW)','generic-proportional':'Ogólny — proporcjonalny','sejm':'Sejm','senat':'Senat','europarlament':'Parlament Europejski','rada-gminy-do-20k':'Rada gminy do 20 tys.','rada-gminy-powyzej-20k':'Rada gminy powyżej 20 tys.','rada-powiatu':'Rada powiatu','sejmik':'Sejmik województwa'};
const friendlyProfile=id=>PROFILE_LABELS[String(id).replace(/^pl-/,'').replace(/@.*$/,'')]??id;
const LEVEL_LABELS={powiat:'powiaty',gmina:'gminy',precinct:'obwody'};
let loadedResult=null,loadedScenario=null,loadedPlans={},loadedUnits={wojewodztwa:[],powiaty:[],gminy:[]};
function geometryPolygons(geometry){if(!geometry)return[];if(geometry.type==='Polygon')return[geometry.coordinates];if(geometry.type==='MultiPolygon')return geometry.coordinates;return[]}
function renderGeoJson(data,{selectable=false,target='#map'}={}){
  const svg=document.querySelector(target),coordinates=data.features.flatMap(feature=>coordinatePairs(feature.geometry?.coordinates??[]));svg.replaceChildren();
  if(!coordinates.length){const text=document.createElementNS(svgNamespace,'text');text.setAttribute('x','500');text.setAttribute('y','300');text.setAttribute('text-anchor','middle');text.textContent='Plan nie zawiera geometrii.';svg.append(text);return}
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  coordinates.forEach(point=>{minX=Math.min(minX,point[0]);maxX=Math.max(maxX,point[0]);minY=Math.min(minY,point[1]);maxY=Math.max(maxY,point[1])});
  const width=950,height=550,scale=Math.min(width/Math.max(maxX-minX,1e-12),height/Math.max(maxY-minY,1e-12)),offsetX=(1000-(maxX-minX)*scale)/2,offsetY=(600-(maxY-minY)*scale)/2;
  const project=point=>[offsetX+(point[0]-minX)*scale,600-(offsetY+(point[1]-minY)*scale)];
  data.features.forEach(feature=>geometryPolygons(feature.geometry).forEach(polygon=>{
    const path=document.createElementNS(svgNamespace,'path'),district=Number(feature.properties?.district??0),commands=polygon.map(ring=>ring.map((point,index)=>{const [x,y]=project(point);return`${index?'L':'M'}${x.toFixed(2)},${y.toFixed(2)}`}).join(' ')+' Z').join(' ');
    const node=String(feature.properties?.node??feature.properties?.key??'');path.setAttribute('d',commands);path.dataset.node=node;path.setAttribute('fill',selectable?'#2e7d63':districtColors[Math.abs(district)%districtColors.length]);path.setAttribute('fill-rule','evenodd');path.setAttribute('stroke','#152820');path.setAttribute('stroke-width','1.2');path.setAttribute('vector-effect','non-scaling-stroke');
    const title=document.createElementNS(svgNamespace,'title');title.textContent=`${feature.properties?.node??'jednostka'}${feature.properties?.district!==undefined?` — okręg ${feature.properties.district}`:''}`;path.append(title);svg.append(path);
  }));
}

function scenarioCommittees(scenario){const set=new Set();Object.values(scenario?.votes_by_unit??{}).forEach(row=>Object.keys(row??{}).forEach(name=>set.add(name)));return Array.from(set).sort((a,b)=>a.localeCompare(b,'pl'))}
function populateTargetChoices(){
  const select=document.querySelector('#target'),keep=select.value,names=scenarioCommittees(loadedScenario);
  if(!names.length){select.innerHTML='<option value="">— brak danych o komitetach —</option>';return}
  select.innerHTML=names.map(name=>`<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
  // Keep the user's choice only if it is a real committee in the loaded data;
  // otherwise pick the first one (drops the placeholder target from the example).
  select.value=names.includes(keep)?keep:names[0];
}
function toggleAnchorField(){document.querySelector('#anchor-field').hidden=document.querySelector('#target-kind').value!=='candidate'}

// ——— Rodzaj wyborów (profil) i kaskada jednostki ———
function currentPlan(){return loadedPlans[document.querySelector('#profile').value]||{}}
function scopeHint(plan){
  const level=LEVEL_LABELS[plan.unit_level]||'jednostki';
  if(!plan.scope_level)return`Jednostka podziału: ${level}. Liczymy cały kraj — powiatu nie wolno dzielić między okręgi.`;
  return`Jednostka podziału: ${level}. Wskaż ${plan.scope_level==='wojewodztwo'?'województwo':plan.scope_level}, policzymy tylko je.`;
}
function fillSelect(select,options,placeholder){select.innerHTML=`<option value="">${placeholder}</option>`+options.map(item=>`<option value="${item.code}">${escapeHtml(item.name)} (${item.code})</option>`).join('')}
function populateWojewodztwa(){fillSelect(document.querySelector('#sel-wojewodztwo'),loadedUnits.wojewodztwa,'— wybierz —')}
function refreshPowiaty(){const woj=document.querySelector('#sel-wojewodztwo').value;fillSelect(document.querySelector('#sel-powiat'),loadedUnits.powiaty.filter(item=>item.parent===woj),'— wybierz —');refreshGminy()}
function refreshGminy(){const pow=document.querySelector('#sel-powiat').value;fillSelect(document.querySelector('#sel-gmina'),loadedUnits.gminy.filter(item=>item.parent===pow),'— wybierz —')}
function selectedUnit(){const plan=currentPlan();if(plan.scope_level==='wojewodztwo')return document.querySelector('#sel-wojewodztwo').value;if(plan.scope_level==='powiat')return document.querySelector('#sel-powiat').value;if(plan.scope_level==='gmina')return document.querySelector('#sel-gmina').value;return''}
function updateCascade(){
  const plan=currentPlan(),scope=plan.scope_level;
  const showW=scope==='wojewodztwo'||scope==='powiat'||scope==='gmina';
  document.querySelector('#cascade').hidden=!showW;
  document.querySelector('#field-powiat').hidden=!(scope==='powiat'||scope==='gmina');
  document.querySelector('#field-gmina').hidden=scope!=='gmina';
  document.querySelector('#profile-hint').textContent=scopeHint(plan);
  // The select always carries a placeholder option, so fill it whenever no real
  // województwo is listed yet (options beyond the placeholder).
  if(showW&&document.querySelector('#sel-wojewodztwo').options.length<=1)populateWojewodztwa();
}

function statusExplanation(run){
  const seats=run.incumbent?.target_seats;
  switch(run.status){
    case'OPTIMAL':return{tone:'ok',text:`Znaleziono najlepszy możliwy podział. Faworyzowany cel zdobywa <strong>${seats??'—'}</strong> mandatów. Wynik jest udowodniony jako optymalny${run.certificate_verified?' i niezależnie zweryfikowany':''}.`};
    case'INFEASIBLE':return{tone:'warn',text:'Przy tych ustawieniach nie istnieje żaden dopuszczalny podział. Spróbuj zwiększyć dopuszczalną różnicę wielkości okręgów albo zmienić liczbę okręgów.'};
    case'FEASIBLE_CHECKPOINT':return{tone:'',text:`Znaleziono poprawny podział (cel zdobywa ${seats??'—'} mandatów), ale bez pełnego dowodu optymalności — duże zadanie może liczyć się dłużej.`};
    case'QUEUED':return{tone:'',text:'Zadanie czeka w kolejce…'};
    case'RUNNING':return{tone:'',text:'Trwa obliczanie najlepszego podziału…'};
    case'CANCELLED':return{tone:'warn',text:'Zadanie zostało anulowane.'};
    case'FAILED':return{tone:'warn',text:'Analiza nie powiodła się. Sprawdź ustawienia i spróbuj ponownie.'};
    default:return{tone:'',text:escapeHtml(run.message||run.status)};
  }
}

async function showRun(id,alternative=null){
  const query=alternative===null?'':`&alternative=${alternative}`;
  const mapResponse=await fetch(`/api/optimizations/${id}/export?format=geojson${query}`);if(mapResponse.ok)renderGeoJson(await mapResponse.json(),{target:'#result-map'});
  const run=await fetch(`/api/optimizations/${id}`).then(response=>response.json());
  const explain=statusExplanation(run);
  document.querySelector('#result-summary').innerHTML=`<p class="result-headline ${explain.tone}">${explain.text}</p><p class="result-sub">Cel: <strong>${escapeHtml(run.request.target)}</strong> · ${run.certificate_verified?'✓ wynik zweryfikowany':'wynik niezweryfikowany'}</p>`;
  const buttons=[`<button data-plan="main" class="ghost">Plan główny</button>`].concat(run.alternatives.map((_,index)=>`<button data-plan="${index}" class="ghost">Wariant ${index+1}</button>`)).join('');
  const exportLinks=run.incumbent?['json','csv','geojson','gpkg','html'].map(format=>`<a class="button-link" href="/api/optimizations/${id}/export?format=${format}${alternative===null?'':`&alternative=${alternative}`}">Pobierz ${format.toUpperCase()}</a>`).join(''):'';
  const cancellable=['QUEUED','RUNNING','FEASIBLE_CHECKPOINT'].includes(run.status)?`<button id="cancel-run" class="ghost">Anuluj zadanie</button>`:'';
  document.querySelector('#run-details').innerHTML=`<h3>${escapeHtml(run.request.target)}</h3><p class="tech-note">Status techniczny: <code>${escapeHtml(run.status)}</code>${run.message?` — ${escapeHtml(run.message)}`:''}</p>${run.alternatives.length?`<p class="step-help">Inne podziały o tym samym wyniku:</p><div class="variant-buttons">${buttons}</div>`:''}<div class="actions">${exportLinks}${cancellable}</div>`;
  document.querySelectorAll('#run-details [data-plan]').forEach(button=>button.onclick=()=>showRun(id,button.dataset.plan==='main'?null:Number(button.dataset.plan)));
  const cancel=document.querySelector('#cancel-run');if(cancel)cancel.onclick=async()=>{cancel.disabled=true;await fetch(`/api/optimizations/${id}/cancel`,{method:'POST'});await showRun(id,alternative);await load()};
  const certificate=document.querySelector('#certificate-details');certificate.innerHTML='';
  if(run.certificate_path){
    const response=await fetch(`/api/optimizations/${id}/certificate`);
    if(response.ok){
      const manifest=await response.json(),proofs=manifest.proofs??[];
      certificate.innerHTML=`<p>Wynik ma niezależny, matematyczny dowód poprawności (VIPR).</p><p>Algorytm: <code>${escapeHtml(manifest.algorithm)}</code> · schemat: ${escapeHtml(manifest.schema_version??1)} · etapów: ${proofs.length}/${escapeHtml(manifest.expected_stages)}</p>${manifest.integrity_verified!==undefined?`<p>Integralność plików: <strong class="${manifest.integrity_verified?'ok':'warn'}">${manifest.integrity_verified?'potwierdzona':'NIEPOTWIERDZONA'}</strong> — ${escapeHtml(manifest.integrity_detail)}</p>`:''}<p>Żądanie SHA-256: <code>${escapeHtml(manifest.request_sha256??'brak w starszym manifeście')}</code></p><div class="proof-list">${proofs.map(proof=>`<div><strong>${escapeHtml(proof.stage??'etap')}</strong><span class="${proof.verified?'ok':'warn'}">${proof.verified?'zweryfikowany':'błąd'}</span><small>model <code>${escapeHtml(proof.model_sha256??'—')}</code><br>dowód <code>${escapeHtml(proof.proof_sha256??proof.sha256??'—')}</code></small></div>`).join('')}</div>`;
    }else{certificate.innerHTML='<p class="notice">Manifest certyfikatu jest niedostępny lub niespójny z zadaniem.</p>'}
  }else{certificate.innerHTML='<p class="step-help">To zadanie nie ma osobnego certyfikatu (mały solver wyczerpujący dowodzi optimum bezpośrednio).</p>'}
}

function applyForm(body){
  body.profile_id=document.querySelector('#profile').value||body.profile_id;
  body.target_kind=document.querySelector('#target-kind').value;
  body.target=document.querySelector('#target').value.trim();
  body.alternatives=Number(document.querySelector('#alternatives').value);
  body.candidate_anchor=body.target_kind==='candidate'?document.querySelector('#candidate-anchor').value.trim()||null:null;
  body.rules=body.rules??{};body.rules.district_count=Number(document.querySelector('#district-count').value);
  const seats=document.querySelector('#seats').value.trim();body.rules.seats_per_district=seats.startsWith('{')?JSON.parse(seats):Number(seats);
  const tolerance=Number(document.querySelector('#population-tolerance-pct').value)/100;document.querySelector('#population-tolerance').value=tolerance;body.rules.population_tolerance=tolerance;
  const maxCut=document.querySelector('#max-cut-border').value;body.rules.max_cut_border_m=maxCut===''?null:Number(maxCut);
  body.rules.indivisible_parent_level=document.querySelector('#indivisible-parent-level').value.trim()||null;
  body.rules.allowed_edge_kinds=Array.from(document.querySelector('#edge-kinds').selectedOptions).map(option=>option.value);
  return body;
}

function buildRequestFromResult(){
  const base=JSON.parse(document.querySelector('#request').value||'{}'),result=loadedResult;
  const body={
    ...base,
    profile_id:document.querySelector('#profile').value||base.profile_id,
    scenario:result.scenario,
    nodes:result.graph.node_ids,
    edges:result.graph.edges,
    parent_by_node:{},
    container_by_node:result.container_by_node||{},
    geometry_by_node:Object.fromEntries(result.geometry.features.map(feature=>[String(feature.properties.node),feature.geometry])),
    base_assignment:null,
  };
  return applyForm(body);
}

function populateForm(body){
  document.querySelector('#profile').value=body.profile_id;
  document.querySelector('#target-kind').value=body.target_kind;toggleAnchorField();
  const targetSelect=document.querySelector('#target');if(body.target&&!Array.from(targetSelect.options).some(option=>option.value===body.target))targetSelect.insertAdjacentHTML('afterbegin',`<option value="${escapeHtml(body.target)}">${escapeHtml(body.target)}</option>`);targetSelect.value=body.target;
  document.querySelector('#alternatives').value=body.alternatives;
  document.querySelector('#candidate-anchor').value=body.candidate_anchor??'';
  document.querySelector('#district-count').value=body.rules.district_count;
  document.querySelector('#seats').value=typeof body.rules.seats_per_district==='object'?JSON.stringify(body.rules.seats_per_district):body.rules.seats_per_district;
  const tolerance=Number(body.rules.population_tolerance??0.1);document.querySelector('#population-tolerance').value=tolerance;document.querySelector('#population-tolerance-pct').value=Math.round(tolerance*100);
  document.querySelector('#max-cut-border').value=body.rules.max_cut_border_m??'';
  document.querySelector('#indivisible-parent-level').value=body.rules.indivisible_parent_level??'';
  const allowed=new Set(body.rules.allowed_edge_kinds??['physical']);Array.from(document.querySelector('#edge-kinds').options).forEach(option=>option.selected=allowed.has(option.value));
}

function autoSelectScenario(){
  const snapshot=document.querySelector('#snapshot').value,select=document.querySelector('#scenario');
  if(select.value){const current=select.selectedOptions[0];if(current&&current.dataset.snapshot===snapshot)return}
  const match=Array.from(select.options).find(option=>option.value&&option.dataset.snapshot===snapshot)||Array.from(select.options).find(option=>option.value);
  if(match)select.value=match.value;
}

async function prepareMap(){
  const snapshot=document.querySelector('#snapshot').value,profile=document.querySelector('#profile').value,plan=currentPlan(),status=document.querySelector('#prepare-status');
  if(!snapshot){status.textContent='Najpierw wybierz wybory w kroku 1.';return}
  autoSelectScenario();
  const scenario=document.querySelector('#scenario').value;
  if(!scenario){status.textContent='Dla tych wyborów nie ma jeszcze zestawu wyników.';return}
  let unit=null;
  if(plan.scope_level){unit=selectedUnit();if(!unit){status.textContent=`Wskaż jednostkę (${plan.scope_level==='wojewodztwo'?'województwo':plan.scope_level}).`;return}}
  status.textContent='Przygotowywanie mapy… (dla całego kraju może to potrwać kilka sekund)';
  try{
    const response=await fetch('/api/districting/assemble',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({snapshot_id:snapshot,profile_id:profile,scenario_id:scenario,unit})});
    const payload=await response.json();if(!response.ok)throw new Error(typeof payload.detail==='string'?payload.detail:JSON.stringify(payload.detail));
    loadedResult=payload;loadedScenario=payload.scenario;
    renderGeoJson(payload.geometry,{selectable:false,target:'#map'});
    populateTargetChoices();
    document.querySelector('#request').value=JSON.stringify(buildRequestFromResult(),null,2);
    const problems=payload.graph.errors?.length?` Uwaga: ${payload.graph.errors.length} problemów spójności grafu.`:'';
    status.textContent=`Gotowe: ${payload.graph.node_ids.length} jednostek (${LEVEL_LABELS[payload.unit_level]||payload.unit_level}). Przejdź do kroku 3.${problems}`;
  }catch(error){status.textContent=`Błąd: ${error.message}`}
}

async function loadReconstructionReport(){
  const snapshot=document.querySelector('#snapshot').value,container=document.querySelector('#reconstruction-report');
  if(!snapshot){container.textContent='Wybierz wybory (krok 1).';return}
  const response=await fetch(`/api/reconstruction/${snapshot}/report?failed_only=true&limit=50&offset=0`);
  if(response.status===404){container.textContent='Rekonstrukcja obwodów jest potrzebna tylko dla wyborów do rady gminy; dla tych wyborów jej nie ma.';return}
  if(!response.ok){container.textContent='Nie udało się odczytać raportu.';return}
  const payload=await response.json(),manifest=payload.manifest??{},complete=manifest.complete_country?'tak':'nie';
  container.innerHTML=`<dl><dt>Gminy poprawne</dt><dd>${escapeHtml(manifest.successful??'—')}</dd><dt>Błędy</dt><dd class="${(manifest.failed??payload.total)>0?'warn':'ok'}">${escapeHtml(manifest.failed??payload.total)}</dd><dt>Kompletny kraj</dt><dd>${complete}</dd></dl>${payload.reports.length?`<div class="quality-list">${payload.reports.map(report=>`<div><strong>${escapeHtml(report.teryt??'—')}</strong><span>${escapeHtml(report.error??'wymaga kontroli jakości')}</span></div>`).join('')}</div>`:'<p class="ok">Brak zgłoszonych błędów.</p>'}`;
}

async function loadAssets(){
  const [snapshots,scenarios,plans,unitOptions]=await Promise.all([
    fetch('/api/snapshots').then(response=>response.json()),
    fetch('/api/scenarios?limit=200&offset=0').then(response=>response.json()),
    fetch('/api/profiles/plans').then(response=>response.json()),
    fetch('/api/units').then(response=>response.json()),
  ]);
  loadedPlans=plans;loadedUnits=unitOptions;populateWojewodztwa();
  document.querySelector('#snapshot').innerHTML='<option value="">— wybierz wybory —</option>'+snapshots.map(item=>`<option value="${item.id}">${escapeHtml(item.election_id)} · ${escapeHtml(item.effective_date)}</option>`).join('');
  document.querySelector('#scenario').innerHTML='<option value="">— automatyczny —</option>'+scenarios.map(item=>`<option value="${item.id}" data-snapshot="${item.snapshot_id??''}">${escapeHtml(item.name)}</option>`).join('');
}

async function submit(){
  const button=document.querySelector('#submit'),status=document.querySelector('#submit-status');button.disabled=true;
  try{
    if(!loadedResult)throw new Error('Najpierw przygotuj mapę w kroku 2.');
    const body=applyForm(JSON.parse(document.querySelector('#request').value));
    document.querySelector('#request').value=JSON.stringify(body,null,2);
    status.textContent='Wysyłanie zadania…';
    const response=await fetch('/api/optimizations',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
    const payload=await response.json();if(!response.ok)throw new Error(typeof payload.detail==='string'?payload.detail:JSON.stringify(payload.detail));
    status.textContent='Zadanie uruchomione. Wynik pojawi się poniżej.';document.querySelector('#step-result').scrollIntoView({behavior:'smooth',block:'start'});await load();
  }catch(error){status.textContent=`Błąd: ${error.message}`}finally{button.disabled=false}
}

async function load(){
  const health=await fetch('/health').then(response=>response.json());
  document.querySelector('#health').textContent=health.status==='ok'?'API działa':'Błąd';
  const banner=document.querySelector('#system-banner');
  const capabilities=await fetch('/api/system/capabilities').then(response=>response.json());
  banner.textContent=health.status==='ok'?(capabilities.certified_large_jobs?'Program gotowy — dokładny solver z certyfikatem dostępny.':'Program działa — dokładny solver dostępny dla małych obszarów.'):'Problem z połączeniem z programem.';
  banner.className='system-banner '+(health.status==='ok'?'ok':'warn');
  document.querySelector('#capabilities').innerHTML=`<dt>Duży solver dokładny</dt><dd class="${capabilities.certified_large_jobs?'ok':'warn'}">${capabilities.certified_large_jobs?'gotowy (z certyfikatem)':'niedostępny'}</dd><dt>Biblioteka</dt><dd>${escapeHtml(capabilities.scip_detail)}</dd><dt>Mały solver</dt><dd>pełne wyczerpanie do ${capabilities.exhaustive_node_limit} jednostek</dd>`;
  const profiles=await fetch('/api/profiles').then(response=>response.json());
  document.querySelector('#profiles').innerHTML=Object.entries(profiles).map(([id,citation])=>`<li><strong>${escapeHtml(friendlyProfile(id))}</strong><br><small>${escapeHtml(citation)}</small></li>`).join('');
  const select=document.querySelector('#profile');if(!select.options.length)select.innerHTML=Object.keys(profiles).map(id=>`<option value="${escapeHtml(id)}">${escapeHtml(friendlyProfile(id))}</option>`).join('');
  const runs=await fetch('/api/optimizations?limit=100&offset=0').then(response=>response.json());
  document.querySelector('#runs').innerHTML=runs.length?runs.map(run=>`<div class="run" data-id="${run.id}"><strong>${escapeHtml(run.status)}</strong><code>${escapeHtml(run.id)}</code><span>${run.incumbent?run.incumbent.target_seats+' mandatów':''}</span></div>`).join(''):'Brak zadań. Uruchom analizę w kroku 4.';
  document.querySelectorAll('.run').forEach(element=>element.onclick=()=>showRun(element.dataset.id));
}

async function init(){
  await Promise.all([load(),loadAssets()]);
  const example=await fetch('/api/examples/small').then(response=>response.json());
  document.querySelector('#request').value=JSON.stringify(example,null,2);populateForm(example);
  updateCascade();
  document.querySelector('#submit').onclick=submit;
  document.querySelector('#prepare-map').onclick=prepareMap;
  document.querySelector('#profile').onchange=()=>{loadedResult=null;updateCascade()};
  document.querySelector('#sel-wojewodztwo').onchange=refreshPowiaty;
  document.querySelector('#sel-powiat').onchange=refreshGminy;
  document.querySelector('#target-kind').onchange=toggleAnchorField;
  document.querySelector('#snapshot').onchange=()=>{autoSelectScenario();loadReconstructionReport();const status=document.querySelector('#election-status');status.textContent=document.querySelector('#snapshot').value?'Wybory wybrane. Przejdź do kroku 2.':''};
}
init().catch(error=>{document.querySelector('#health').textContent=`Błąd: ${error.message}`;document.querySelector('#system-banner').textContent=`Błąd: ${error.message}`});setInterval(load,5000);
