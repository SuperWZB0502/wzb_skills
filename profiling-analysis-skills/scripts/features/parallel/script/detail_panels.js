// Shared renderer for selected and hovered operators, phases and range summaries.
const selectedTableKinds=new Set(kindOrder),allPrefixes=[...data.events.reduce((names,event)=>names.add(prefixName(event)),new Set())].sort(),selectedPrefixes=new Set(allPrefixes),regionPerformanceCache=new Map();
function prefixName(event){return event.name.split('__')[0];}
function detailElement(tag,text,className){const element=document.createElement(tag);if(text!=null)element.textContent=String(text);if(className)element.className=className;return element;}
function detailValue(value){if(value==null)return '-';const text=String(value).trim();return ['', '-', '\u2014', 'N/A', 'NA', 'NONE', 'NULL', 'UNDEFINED', 'DT_UNDEFINED', 'UNKNOWN', '未确认', '?'].includes(text.toUpperCase())?'-':text;}
function detailTable(target,label,headers,rows,attributes={}){if(label)target.append(detailElement('div',label,'detail-section-title'));const wrapper=detailElement('div',null,'detail-table-scroll'),table=detailElement('table',null,'info-table');for(const [key,value]of Object.entries(attributes))table.dataset[key]=value;const head=document.createElement('thead'),line=document.createElement('tr');for(const caption of headers)line.append(detailElement('th',caption));head.append(line);const body=document.createElement('tbody');for(const values of rows){const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');if(value instanceof Node)cell.append(value);else cell.textContent=detailValue(value);row.append(cell);}body.append(row);}table.append(head,body);wrapper.append(table);target.append(wrapper);return table;}
function numberText(value,digits=2){return detailValue(value)==='-'||!Number.isFinite(Number(value))?'-':Number(value).toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:digits});}
function flopText(value,partial=false){return value==null?'-':(partial?'≥ ':'')+numberText(value,0);}
function mfuText(value,partial=false){return value==null?'-':(partial?'≥ ':'')+numberText(value)+'%';}
function recordFor(event){if(operatorDetails.compute_refs_embedded)return event._detail_record>=0?operatorDetails.compute_records?.[event._detail_record]:null;const ref=computeRefFor(event);return ref?operatorDetails.compute_records?.[ref[0]]:null;}
function eventPerformance(event){const performance=recordFor(event)?.performance,raw=performance?.flops,flops=raw==null?null:Number(raw),peak=operatorDetails.hardware?.[String(event.rank)]?.peak_flops,mfu=flops!=null&&peak>0&&event._duration>0?flops/(event._duration/1e6*peak)*100:null;return{flops,mfu,basis:performance?.basis||'未采集或未唯一关联计算量证据',peak};}
function summarizeCompute(events,start,end,ranksInScope=null){const candidates=events.filter(e=>e.kind==='compute'&&e._end>=start&&e._start<=end),compute=candidates.filter(e=>e._start>=start&&e._end<=end),boundary=candidates.length-compute.length,sampled=new Set(ranksInScope||candidates.map(e=>Number(e.rank)));let flops=0,known=0;for(const event of compute){const value=eventPerformance(event).flops;if(value!=null){flops+=value;known++;}}let peak=0,allPeaks=true;for(const rank of sampled){const value=operatorDetails.hardware?.[String(rank)]?.peak_flops;if(!(value>0))allPeaks=false;else peak+=Number(value);}return{flops:known?flops:null,known,total:compute.length,partial:known<compute.length||boundary>0,boundary,mfu:known&&allPeaks&&peak>0&&end>start?flops/((end-start)/1e6*peak)*100:null,start,end,ranks:[...sampled],peak};}
function regionPerformance(region){
 if(!regionPerformanceCache.has(region.region_id)){
  const start=Number(region.display_start_us??region.start_us),end=Number(region.display_end_us??region.end_us);
  const ids=region.model_role?new Set(region.event_ids):null;
  const events=(eventsByStep.get(String(region.step))||[]).filter(e=>e.kind==='compute'&&Number(e.rank)===Number(region.rank)&&(!ids||ids.has(String(e.event_id))));
  regionPerformanceCache.set(region.region_id,summarizeCompute(events,start,end,[Number(region.rank)]));
 }
 return regionPerformanceCache.get(region.region_id);
}
function performanceNote(stats){return '计算量已知 '+stats.known+'/'+stats.total+' 个计算算子'+(stats.partial?'；含未知项，≥ 表示已知部分下界':'')+(stats.boundary?'；另有 '+stats.boundary+' 个跨边界计算事件未分摊FLOPs':'')+'。区间MFU = 已知FLOPs ÷ (区间时长 × 涉及实采卡BF16/FP16峰值之和)，不相加百分比，不外推未采集卡。';}
function tensorSlots(raw){const value=String(raw??'').trim().replace(/^"|"$/g,'');return !value||value==='N/A'?[]:value.split(';').map(v=>v.trim());}
function ioTable(target,record,direction){
 const source=direction==='输入'?'Input':'Output',shapes=tensorSlots(record[source+' Shapes']),types=tensorSlots(record[source+' Data Types']),formats=tensorSlots(record[source+' Formats']);let count=Math.max(shapes.length,types.length,formats.length);while(count>0&&![shapes[count-1],types[count-1],formats[count-1]].some(Boolean))count--;if(!count)return;
 const cells=items=>Array.from({length:count},(_,i)=>detailValue(items[i]));
 const shape=value=>{if(value==='-')return value;const raw=value.replace(/^\[|\]$/g,'').trim();return /^-?\d+(?:\s*,\s*-?\d+)*$/.test(raw)?'['+raw.split(',').map(v=>v.trim()).join(', ')+']':raw===''?'[]':'-';};
 detailTable(target,direction+'信息：',[direction+'序号',...Array.from({length:count},(_,i)=>String(i))],[['shape',...cells(shapes).map(shape)],['dtype',...cells(types)],['format',...cells(formats)]],{io:source.toLowerCase()});
}
function rankCell(ranks,basis='proven'){const values=[...new Set(ranks.filter(rank=>rank!=null&&Number.isInteger(Number(rank))).map(Number))].sort((a,b)=>a-b),text=values.length?values.join(', '):'-',span=detailElement('span',text,'rank-list');span.dataset.rankList='true';span.dataset.endpointBasis=basis;span.title=text;return span;}
// Preserve exported zero values; never turn small nonzero bandwidth into 0.00.
function bandwidthText(value){if(detailValue(value)==='-'||!Number.isFinite(Number(value)))return '-';const n=Number(value);if(n===0)return '采集值为0';return Math.abs(n)<0.01?Number(n.toPrecision(3)).toString():numberText(n);}
function communicationTransportRows(detail){return detail.transports?.length?detail.transports:(detail.links||[]).map(row=>({transport:detailValue(row.raw?.['Transport Type'])+'（链路样本）',...row.raw}));}
function communicationBandwidthText(detail){const rows=communicationTransportRows(detail);return rows.length?rows.map(row=>row.transport+': '+bandwidthText(row['Bandwidth(GB/s)'])).join('; '):detail.bandwidth_observed?'未记录有效传输':'-';}
function commDuration(value){return detailValue(value)==='-'||!Number.isFinite(Number(value))?'-':time(Number(value)*1000);}
function communicationOverview(target,event){
 const detail=operatorDetails.communication?.[String(event.event_id)],values=[stageOf(event.rank),event.rank,kindNames[event.kind],eventDomains(event).join(' / '),groupLabel(groupMap.get(String(event.group_id))),time(event._start),time(event._end),commDuration(detail?.time?.['Transit Time(ms)']),commDuration(detail?.time?.['Wait Time(ms)']),time(event._duration)];
 const table=detailTable(target,'总览：',[],[values],{detailOverview:'true',communicationOverview:'true'}),head=table.tHead,upper=document.createElement('tr'),lower=document.createElement('tr');
 for(const label of ['Stage','Rank','事件类型','通信域','通信组','起始','结束','持续时间']){const cell=detailElement('th',label);if(label==='持续时间'){cell.colSpan=3;cell.scope='colgroup';}else{cell.rowSpan=2;cell.scope='col';}upper.append(cell);}for(const label of ['传输','等待','总计']){const cell=detailElement('th',label);cell.scope='col';lower.append(cell);}head.replaceChildren(upper,lower);
}
function renderCommunication(target,event){
 const detail=operatorDetails.communication?.[String(event.event_id)]||{},group=groupMap.get(String(event.group_id)),members=detail.members?.length?detail.members:group?.members||[],collective=/allgather|alltoall|allreduce|reducescatter|broadcast/.test(event.name.toLowerCase().replace(/[^a-z]/g,'')),endpoints=detail.endpoints||[],send=endpoints.map(row=>row.source_rank),recv=endpoints.map(row=>row.target_rank);
 let rows=[];
 if(detail.transports?.length){rows=detail.transports.map(row=>[row.transport,rankCell(collective?[event.rank]:send,collective?'group-members':'proven'),rankCell(collective?members.filter(rank=>Number(rank)!==Number(event.rank)):recv,collective?'group-members':'proven'),detailValue(row['Transit Size(MB)'])==='-'?'-':numberText(row['Transit Size(MB)'],6)+' MB',commDuration(row['Transit Time(ms)']),bandwidthText(row['Bandwidth(GB/s)'])]);}
 else if(detail.links?.length){rows=detail.links.map(row=>[detailValue(row.raw['Transport Type'])+'（链路样本）',rankCell(collective?[event.rank]:[row.source_rank],collective?'group-members':'sample'),rankCell(collective?members.filter(rank=>Number(rank)!==Number(event.rank)):[row.target_rank],collective?'group-members':'sample'),detailValue(row.raw['Transit Size(MB)'])==='-'?'-':numberText(row.raw['Transit Size(MB)'],6)+' MB',commDuration(row.raw['Transit Time(ms)']),bandwidthText(row.raw['Bandwidth(GB/s)'])]);}
 else rows=[['-',rankCell(collective?[event.rank]:send,collective?'group-members':'proven'),rankCell(collective?members.filter(rank=>Number(rank)!==Number(event.rank)):recv,collective?'group-members':'proven'),'-','-',detail.bandwidth_observed?'未记录有效传输':'-']];
 if(!collective&&!detail.transports?.length&&endpoints.length){const protocol=endpoints.map(row=>['协议载荷',rankCell([row.source_rank]),rankCell([row.target_rank]),row.payload_bytes==null?'-':numberText(row.payload_bytes,0)+' B','-',detail.bandwidth_observed?'未记录有效传输':'-']);rows=detail.links?.length?[...protocol,...rows]:protocol;}
 detailTable(target,'传输信息：',['传输类型','发送rank','接收rank','通信量','传输时间','带宽（GB/s）'],rows,{transmission:'true'});
 if(detail.bandwidth_observed&&!detail.transports?.length&&!detail.links?.length)target.append(detailElement('p','原始导出未记录有效传输；不代表该算子没有通信或对端没有发送。','detail-note'));
 if(communicationTransportRows(detail).some(row=>Number(row['Bandwidth(GB/s)'])===0&&detailValue(row['Bandwidth(GB/s)'])!=='-'))target.append(detailElement('p','采集值为0：原始导出带宽字段为0，不代表物理链路带宽为零；可能受导出精度或统计口径影响。','detail-note'));
 if(collective)target.append(detailElement('p','收发对象按通信组展示：本卡 → 其他成员；不代表逐链路采样。','detail-note'));
}
function renderSubject(target,subject,hover=false){target.replaceChildren();if(hover)target.style.width='max-content';const isRegion=!!subject?.region_id,isRange=subject?.kind==='range',isEvent=subject?.event_id!=null;let title='当前视窗',start=viewStart,end=viewEnd,subjectRanks=orderedRanks(),stats=null;const headers=[],values=[];const add=(name,value)=>{headers.push(name);values.push(value);};
 if(isEvent){title=subject.name;start=subject._start;end=subject._end;subjectRanks=[Number(subject.rank)];}
 if(isRegion){title=(subject.model_role?subject.model_role+' ':'')+subject.label+' · '+(subject.phase==='f'?'前向':subject.phase==='b'?'反向':subject.phase==='r'?'逐层重计算':'优化器更新');start=Number(subject.display_start_us??subject.start_us);end=Number(subject.display_end_us??subject.end_us);subjectRanks=[Number(subject.rank)];if(subject.phase!=='opt')stats=regionPerformance(subject);}
 if(isRange){title='框选范围';start=subject.start;end=subject.end;subjectRanks=subject.ranks;stats=summarizeCompute(subject.events,start,end,subjectRanks);}
 if(stats&&!stats.total)stats=null;target.append(detailElement('div',title,'detail-title'));
 const isCommunication=isEvent&&subject.kind==='communication';
 if((isEvent||isRegion||isRange)&&!isCommunication){add('Stage',[...new Set(subjectRanks.map(rank=>stageOf(rank)??'?'))].join(', '));add('Rank',subjectRanks.join(', '));if(isEvent)add('事件类型',kindNames[subject.kind]);if(isEvent&&subject.kind==='communication'){add('通信域',eventDomain(subject));add('通信组',groupLabel(groupMap.get(String(subject.group_id))));}add('起始',time(start));add('结束',time(end));add('持续时间',time(end-start));}
 if(isEvent&&subject.kind==='compute'){const p=eventPerformance(subject);add('FLOPs',flopText(p.flops));add('MFU',mfuText(p.mfu));}
 if(stats){add('总FLOPs',flopText(stats.flops,stats.partial));add('总MFU',mfuText(stats.mfu,stats.partial));}
 if(!subject){for(const [label,field,value] of [['起始','start',viewStart],['结束','end',viewEnd],['持续时间','duration',viewEnd-viewStart]]){const cell=detailElement('span',time(value));cell.dataset.viewField=field;add(label,cell);}}
 if(isCommunication)communicationOverview(target,subject);else detailTable(target,'总览：',headers,[values],{detailOverview:'true'});
 if(isEvent&&subject.kind==='compute'){const record=recordFor(subject),ref=computeRefFor(subject);if(record){ioTable(target,record,'输入');ioTable(target,record,'输出');target.append(detailElement('p','来源：kernel_details.csv 第 '+ref[1]+' 行。','detail-note'));}target.append(detailElement('p',eventPerformance(subject).basis+'；MFU按本卡BF16/FP16模型峰值归一化，非硬件PMU利用率。','detail-note'));}
 if(isEvent&&subject.kind==='communication')renderCommunication(target,subject);
 if(stats)target.append(detailElement('p',performanceNote(stats),'detail-note'));
 if(isRegion&&subject.model_role){
  target.append(detailElement('p','已归层设备算子 '+subject.kernel_count+' 个 · '+subject.basis,'detail-note'));
  if(subject.kernel_count&&subject.framework_start_us!=null&&subject.framework_end_us!=null){
   target.append(detailElement('p','CPU 框架区间 '+time(subject.framework_start_us)+' 至 '+time(subject.framework_end_us),'detail-note'));
  }
 }
 else if(isRegion&&subject.basis&&!hover)target.append(detailElement('p',subject.basis,'detail-note'));
 if(!subject&&!hover)target.append(detailElement('p','单击算子或计算标注查看详情；框选可复制Stage/Rank与时间区间。','detail-note'));
}
function updateDetailView(){for(const [field,value]of [['start',viewStart],['duration',viewEnd-viewStart],['end',viewEnd]]){const cell=q('selection').querySelector('[data-view-field='+field+']');if(cell)cell.textContent=time(value);}}
function initializeTableChoices(){for(const [type,values,selected,label]of [['kind',kindOrder,selectedTableKinds,key=>kindNames[key]],['prefix',allPrefixes,selectedPrefixes,key=>key]]){const options=q('table-'+type+'-options');for(const value of values){const line=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=true;input.dataset.tableChoice=type;input.value=value;line.append(input,document.createTextNode(label(value)));input.addEventListener('change',()=>{input.checked?selected.add(value):selected.delete(value);page=1;refreshTableChoiceLabels();updateResults();});options.append(line);}for(const action of ['all','none'])q('table-'+type+'-'+action).addEventListener('click',()=>{selected.clear();for(const input of options.querySelectorAll('input')){input.checked=action==='all';if(input.checked)selected.add(input.value);}page=1;refreshTableChoiceLabels();updateResults();});}refreshTableChoiceLabels();}
function refreshTableChoiceLabels(){q('table-kind-label').textContent=selectedTableKinds.size+'/'+kindOrder.length;}
function updatePerformanceTotals(){const a=rangeSelection?.start??viewStart,b=rangeSelection?.end??viewEnd,scopeRanks=rangeSelection?[...new Set(rangeSelection.bands.map(b=>b.rank))]:null,stats=summarizeCompute(visible,a,b,scopeRanks);const target=q('total-performance');target.hidden=!stats.total;target.textContent=stats.total?' · 总FLOPs '+flopText(stats.flops,stats.partial)+' · 总MFU '+mfuText(stats.mfu,stats.partial):'';target.title=performanceNote(stats);target.dataset.known=stats.known;target.dataset.total=stats.total;target.dataset.flops=stats.flops??'';target.dataset.mfu=stats.mfu??'';}
function initializeGuides(){
 const button=q('guide-toggle'),panel=q('guide-panel');
 let layoutFrame=0;
 function position(){
  layoutFrame=0;
  if(panel.hidden)return;
  const anchor=button.getBoundingClientRect();
  panel.style.visibility=anchor.bottom<=0||anchor.top>=innerHeight?'hidden':'visible';
  panel.style.width=Math.min(740,innerWidth-24)+'px';
  panel.style.left=Math.max(12,Math.min(innerWidth-panel.offsetWidth-12,anchor.right-panel.offsetWidth))+'px';
  const above=anchor.top-20,below=innerHeight-anchor.bottom-20;
  const nearTop=below>above&&panel.scrollHeight>above;
  panel.style.top=nearTop?anchor.bottom+8+'px':'auto';
  panel.style.bottom=nearTop?'auto':Math.max(0,innerHeight-anchor.top+8)+'px';
  panel.style.maxHeight=Math.max(0,nearTop?below:above)+'px';
 }
 function schedule(){if(!layoutFrame)layoutFrame=requestAnimationFrame(position);}
 button.addEventListener('click',()=>{panel.hidden=!panel.hidden;button.setAttribute('aria-expanded',String(!panel.hidden));position();});
 document.addEventListener('scroll',schedule,true);window.addEventListener('resize',schedule);
 q('fullscreen').addEventListener('click',schedule);
 new MutationObserver(schedule).observe(root,{attributes:true,attributeFilter:['class']});
}

function renderAggregate(target,aggregate){renderSubject(target,{kind:'range',start:aggregate.start,end:aggregate.end,ranks:[Number(aggregate.rank)],events:[]},true);target.querySelector('.detail-title').textContent='合并 '+aggregate.count+' 个'+kindNames[aggregate.kind]+'事件';target.append(detailElement('p','此处合并显示多个细小事件；放大后可选择单个算子。','detail-note'));}

function restoreDefaultDomains(){selectedDomains.clear();for(const domain of availableDomains)selectedDomains.add(domain);syncDomainInputs();refreshGroupOptions(true);}
function isCommunicationBlank(x,y){return scene&&x>=scene.left&&x<=scene.left+scene.plot&&scene.layouts.some(row=>row.laneList.some(lane=>lane.kind==='communication'&&y>=lane.y-4&&y<lane.y-4+lane.height));}
function renderBlankDomains(tooltip){delete tooltip.dataset.subjectKey;delete tooltip.dataset.eventId;delete tooltip.dataset.rank;tooltip.style.width='max-content';const label=domain=>domain==='unresolved'?'未确认':domain;tooltip.replaceChildren(...[true,false].map(selected=>detailElement('div',(selected?'已勾选通信域：':'未勾选通信域：')+(domainNames.filter(domain=>selectedDomains.has(domain)===selected).map(label).join('、')||'无'),'blank-domain-row')));}
