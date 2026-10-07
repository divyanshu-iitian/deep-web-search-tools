"""Standalone resumable organization CSV discovery, with no outreach side effects.

python -m deepsearch.organization_csv input.csv --output enriched.csv --start 840147 --end 841190
"""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from deepsearch.config import settings
from deepsearch.engine import SearchEngine
from deepsearch.storage import Storage
from deepsearch.organizations import OrganizationEngine,OrganizationRequest

async def enrich(input_path,output_path,start=None,end=None):
    if input_path.resolve()==output_path.resolve():raise ValueError('Output must differ from the original input file')
    content=input_path.read_text(encoding='utf-8-sig')
    if len(content.encode('utf-8'))>10_000_000:raise ValueError('CSV exceeds 10 MB')
    reader=csv.DictReader(io.StringIO(content));columns=reader.fieldnames or [];rows=list(reader)
    if not {'record_id','organization_name'}.issubset(columns) or not rows or len(rows)>10000:raise ValueError('Supply 1-10000 rows with record_id and organization_name')
    ids=[x['record_id'] for x in rows]
    if len(ids)!=len(set(ids)) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}',x) for x in ids):raise ValueError('Use unique safe record IDs')
    extra=['discovery_status','discovery_warnings','contact_candidates','background']
    fields=columns+[x for x in ['website','contact_name','contact_title','contact_email','email_source','verification_tool','verification_status','phone','linkedin_url','research_notes','source_urls','confidence','time_spent_min']+extra if x not in columns]
    output_path.parent.mkdir(parents=True,exist_ok=True)
    checkpoint=output_path.with_suffix(output_path.suffix+'.checkpoint.json')
    fingerprint=hashlib.sha256((content+str(start)+str(end)).encode()).hexdigest()
    state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {'fingerprint':fingerprint,'results':{}}
    if state['fingerprint']!=fingerprint:raise ValueError('Checkpoint belongs to another input/range; use another output filename')
    engine=OrganizationEngine(SearchEngine(settings,Storage(settings.data_dir)))
    for row in rows:
        rid=row['record_id']
        if start is not None and (not rid.isdigit() or not start<=int(rid)<=end):continue
        if row.get('do_not_contact','').lower() in {'true','yes','1'}:continue
        if rid not in state['results']:
            try:
                result=await asyncio.wait_for(engine.run(OrganizationRequest(**{k:row.get(k,'') for k in ['organization_name','website','state','county','country','record_type','contact_name']})),150)
            except (TimeoutError,ValueError) as exc:result={'status':'failed','warnings':[type(exc).__name__],'contacts':[]}
            state['results'][rid]=result
            temp=checkpoint.with_suffix('.tmp');temp.write_text(json.dumps(state),encoding='utf-8');temp.replace(checkpoint)
        print(rid,state['results'][rid]['status'],flush=True)
    temp=output_path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for original in rows:
            row=dict(original);result=state['results'].get(row['record_id'])
            if result:
                row.update(discovery_status=result['status'],discovery_warnings='; '.join(result.get('warnings',[])),website=result.get('website',''),contact_candidates=len(result['contacts']))
                for key in ('contact_name','contact_title','contact_email','email_source','verification_tool','verification_status','phone','linkedin_url'):row[key]=''
                best=next((x for x in result['contacts'] if x.get('name') and x.get('identity_status')=='supported' and x.get('relevance_score',0)>=50),None)
                if best:row.update(contact_name=best['name'],contact_title=best['title'],contact_email=best['email'],email_source=best['source_url'],background=best['background'],phone=best.get('phone',''),linkedin_url=best.get('linkedin_url',''))
                row['verification_status']='NOT_CHECKED'  # The standalone tool has no mailbox verifier.
                for key,fact in result.get('facts',{}).items():
                    if key in fields:row[key]=fact['value']
                row['source_urls']='; '.join(x['url'] for x in result.get('evidence',[]))
                row['time_spent_min']=result.get('time_spent_min',0)
            writer.writerow({k:("'"+str(v) if str(v).startswith(('=','+','-','@')) and k!='record_id' else v) for k,v in row.items()})
    temp.replace(output_path)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('input',type=Path);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--start',type=int);parser.add_argument('--end',type=int)
    args=parser.parse_args()
    if (args.start is None)!=(args.end is None) or args.start is not None and args.start>args.end:parser.error('Supply both ascending range endpoints')
    asyncio.run(enrich(args.input,args.output,args.start,args.end))

if __name__=='__main__':main()
