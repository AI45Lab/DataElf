"""Bounded previews and exact, byte-paged replay. Truncation is always explicit."""
import json
from .sources import select


def json_page(value,offset=0,max_bytes=8000):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str).encode('utf-8')
    if offset>len(raw):raise ValueError('offset is past resource end')
    chunk=raw[offset:offset+max_bytes]
    while chunk:
        try:text=chunk.decode('utf-8');break
        except UnicodeDecodeError as exc:
            if exc.start==0 and offset:raise ValueError('offset must be at a UTF-8 character boundary')
            chunk=chunk[:exc.start]
    else:text=''
    end=offset+len(chunk)
    return {'encoding':'json','text':text,'offset':offset,'next_offset':end if end<len(raw) else None,
        'total_bytes':len(raw),'truncated':end<len(raw),'byte_paged':True}


def preview(value,depth=3):
    if isinstance(value,str):return value[:256],len(value)>256
    if isinstance(value,(dict,list)) and depth<=0:return {'preview_type':type(value).__name__,'size':len(value)},True
    if isinstance(value,dict):
        pairs=list(value.items());out={};truncated=len(pairs)>12
        for key,child in pairs[:12]:out[key],cut=preview(child,depth-1);truncated |= cut
        return out,truncated
    if isinstance(value,list):
        out=[];truncated=len(value)>5
        for child in value[:5]:v,cut=preview(child,depth-1);out.append(v);truncated |= cut
        return out,truncated
    return value,False


def sample(catalog,payload):
    rows=[];skipped=0;more=False;remaining=payload['max_bytes']
    for record in catalog.records:
        if record.source!=payload['source']:continue
        for suffix,value in select(record.data,payload['pointer']):
            if skipped<payload['offset']:skipped+=1;continue
            if len(rows)>=payload['limit']:more=True;break
            projected=value
            if payload['fields']:
                projected={p:[v for _,v in select(value,p)] for p in payload['fields']}
            data,cut=preview(projected,payload['depth'])
            row={'file':record.file,'pointer':record.pointer+suffix,'data':data,'preview':cut}
            size=len(json.dumps(row,ensure_ascii=False).encode('utf-8'))
            if size>remaining:
                if rows:more=True;break
                row['data']={'preview_type':type(value).__name__,'use_replay':True};row['preview']=True
                size=len(json.dumps(row,ensure_ascii=False).encode('utf-8'))
            rows.append(row);remaining-=size
        if more:break
    return {'records':rows,'offset':payload['offset'],'next_offset':payload['offset']+len(rows) if more else None,
        'truncated':more or any(r['preview'] for r in rows),'replay_hint':'Replay file/pointer with byte pagination to obtain the exact value.'}


def profile(catalog,source=None,field_path='',offset=0,limit=12):
    profiles=catalog.profile()
    if source is None:return {'sources':[{'source':key,'records':v['records'],'field_count':len(v['fields'])} for key,v in profiles.items()]}
    if source not in profiles:raise ValueError('unknown source')
    value=profiles[source];fields=[(k,v) for k,v in value['fields'].items() if not field_path or k==field_path or k.startswith(field_path+'/')]
    return {'source':source,'records':value['records'],'fields':dict(fields[offset:offset+limit]),
        'offset':offset,'next_offset':offset+limit if offset+limit<len(fields) else None,'total_fields':len(fields)}
