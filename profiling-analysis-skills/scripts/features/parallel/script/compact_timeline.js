// Lossless column reader. Event views share a prototype; nested args stay lazy.
const packedRowIndex=Symbol('timeline-row-index'),packedReaders=Symbol('timeline-columns'),eventViewPrototype={},hotEventFields=['kind','rank','step','name'];
function binaryColumn(value){
 const binary=atob(value.data),bytes=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i);value.data=null;
 const constructors={B:Uint8Array,H:Uint16Array,I:Uint32Array,i:Int32Array,d:Float64Array},Ctor=constructors[value.type];
 if(!Ctor||bytes.byteLength%Ctor.BYTES_PER_ELEMENT)throw new Error('列式数据长度或类型错误');
 if(new Uint8Array(new Uint16Array([1]).buffer)[0]===1)return new Ctor(bytes.buffer);
 const view=new DataView(bytes.buffer),result=new Ctor(bytes.byteLength/Ctor.BYTES_PER_ELEMENT),methods={B:'getUint8',H:'getUint16',I:'getUint32',i:'getInt32',d:'getFloat64'};
 for(let i=0;i<result.length;i++)result[i]=view[methods[value.type]](i*Ctor.BYTES_PER_ELEMENT,true);return result;
}
function columnReader(c,count){
 const present=c.present?binaryColumn(c.present):null;let get,number;
 if(c.t==='absent')get=()=>undefined;
 else if(c.t==='constant')get=()=>c.v;
 else if(c.t==='number'){const a=binaryColumn(c.a);get=i=>a[i];number=get;}
 else if(c.t==='dictionary'){const a=binaryColumn(c.a),values=c.values;get=i=>values[a[i]];}
 else if(c.t==='id'){const prefix=columnReader(c.prefix,count),a=binaryColumn(c.a);get=i=>prefix.get(i)+':'+a[i];}
 else if(c.t==='decimal'){
  const a=binaryColumn(c.a),digits=columnReader(c.digits,count),scale=c.scale,base=c.safe?Number(c.base):BigInt(c.base),factor=10**scale;
  get=i=>{const n=c.safe?base+a[i]:base+BigInt(a[i]),negative=n<0,raw=String(negative?-n:n).padStart(scale+1,'0'),d=digits.get(i);return(negative?'-':'')+(scale?raw.slice(0,-scale):raw)+(d?'.'+raw.slice(-scale).slice(0,d):'');};
  number=c.safe?i=>(base+a[i])/factor:i=>Number(get(i));
 }
 else if(c.t==='object'){const fields=Object.entries(c.columns).map(([key,column])=>[key,columnReader(column,count)]),cache=new Map();get=i=>{if(!cache.has(i)){const value=Object.create(null);for(const [key,reader]of fields){const item=reader.get(i);if(item!==undefined)value[key]=item;}cache.set(i,value);}return cache.get(i);};}
 else throw new Error('不支持的列式数据类型：'+c.t);
 return {get:present?i=>present[i]?get(i):undefined:get,number:present?i=>present[i]?(number?number(i):Number(get(i))):NaN:number||((i)=>Number(get(i)))};
}
function rowFactory(columns,count,refs=null,eventViews=false){
 const readers=Object.fromEntries(Object.entries(columns).map(([key,column])=>[key,columnReader(column,count)])),prototype=eventViews?eventViewPrototype:{};
 for(const key of Object.keys(readers)){
  if(eventViews&&hotEventFields.includes(key))continue;
  if(eventViews){if(!Object.hasOwn(prototype,key))Object.defineProperty(prototype,key,{get(){return this[packedReaders][key]?.get(this[packedRowIndex]);}});}
  else Object.defineProperty(prototype,key,{get(){return readers[key].get(this[packedRowIndex]);}});
 }
 if(!eventViews)for(const [key,source]of [['_start','start_us'],['_end','end_us'],['_duration','duration_us']])if(readers[source])Object.defineProperty(prototype,key,{get(){return readers[source].number(this[packedRowIndex]);}});
 const records=refs?binaryColumn(refs.records):null,lines=refs?binaryColumn(refs.lines):null;
 if(eventViews&&!Object.hasOwn(prototype,'_detail_ref'))Object.defineProperty(prototype,'_detail_ref',{get(){return this._detail_record<0?null:[this._detail_record,this._detail_line];}});
 if(!Object.hasOwn(prototype,'toJSON'))Object.defineProperty(prototype,'toJSON',{value(){const result={},source=eventViews?this[packedReaders]:readers;for(const [key,reader]of Object.entries(source)){const value=reader.get(this[packedRowIndex]);if(value!==undefined)result[key]=value;}return result;}});
 return i=>{const row=Object.create(prototype);row[packedRowIndex]=i;
  if(eventViews){row[packedReaders]=readers;for(const key of hotEventFields)row[key]=readers[key]?.get(i);row._start=readers.start_us?.number(i);row._end=readers.end_us?.number(i);row._duration=readers.duration_us?.number(i);row._detail_record=records?records[i]:-1;row._detail_line=lines?lines[i]:0;}
  return row;
 };
}
function readPackedRows(part){const make=rowFactory(part.columns,part.count,part.compute_refs,true),rows=new Array(part.count);for(let i=0;i<rows.length;i++)rows[i]=make(i);return rows;}
