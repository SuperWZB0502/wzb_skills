#!/usr/bin/env python3
"""Verify every packed HTML event and embedded compute reference against canonical files.

This optional audit streams canonical events and unpacks one bounded block at a
time. It never rescans source profiling or substitutes rounded numeric times.
"""
import argparse
import json
from pathlib import Path
import ijson
from timeline_payload import _unpack, decode_report
from compact_timeline import decode_columns, read_numbers
from render_parallel import payload_from_html


def metadata(path):
    builder=ijson.common.ObjectBuilder()
    with Path(path).open('rb') as handle:
        for prefix,event,value in ijson.parse(handle,use_float=True):
            if prefix=='events' or prefix.startswith('events.') or (prefix=='' and event=='map_key' and value=='events'):
                continue
            builder.event(event,value)
    return builder.value


def validate(html,output_dir):
    out=Path(output_dir);envelope=json.loads(payload_from_html(html));errors=[];count=refs_count=0
    def fail(message):
        if len(errors)<20:errors.append(message)
    details=json.loads((out/'operator_details.json').read_text())
    refs=details.get('compute_event_refs',{})
    actual_meta=_unpack(envelope['metadata']) if 'metadata' in envelope else {k:v for k,v in decode_report(envelope).items() if k!='events'}
    if actual_meta!=metadata(out/'unified_report.json'):fail('metadata differs from canonical JSON')
    def blocks():
        if envelope.get('encoding')=='gzip-base64-columns-v1':
            for part in envelope['event_chunks']:
                block=_unpack(part);rows=decode_columns(block['columns'],block['count'])
                embedded=block.get('compute_refs')
                reference_rows=list(zip(read_numbers(embedded['records']),read_numbers(embedded['lines']))) if embedded else [None]*len(rows)
                yield rows,reference_rows
        elif envelope.get('encoding')=='gzip-base64-chunks':
            for part in envelope['event_chunks']:yield _unpack(part),None
        else:yield decode_report(envelope)['events'],None
    with (out/'unified_report.json').open('rb') as handle:
        source=ijson.items(handle,'events.item',use_float=True)
        for rows,reference_rows in blocks():
            for i,row in enumerate(rows):
                expected=next(source,None)
                if row!=expected:fail('event mismatch at '+str(count)+': '+str(row.get('event_id')))
                if envelope.get('detail_refs_embedded'):
                    record,line=reference_rows[i];actual=None if record<0 else [record,line]
                    expected_ref=refs.get(str(row['event_id']))
                    if actual!=expected_ref:fail('compute reference mismatch: '+str(row['event_id']))
                    refs_count+=int(actual is not None)
                count+=1
        if next(source,None) is not None:fail('canonical events were truncated')
    # Validate the second data block, including fields omitted only because they
    # are represented by the aligned numeric references above.
    text=Path(html).read_text();marker='<script type="application/json" id="operator-details-data">';start=text.index(marker)+len(marker)
    actual_details=_unpack(json.loads(text[start:text.index('</script>',start)]))
    expected_details=dict(details)
    if envelope.get('detail_refs_embedded'):
        expected_details.pop('compute_event_refs',None);expected_details['compute_refs_embedded']=True
    if actual_details!=expected_details:fail('operator details differ from canonical JSON')
    if envelope.get('event_count',count)!=count:fail('envelope event count differs')
    return {'passed':not errors,'encoding':envelope.get('encoding'),'events_checked':count,'compute_refs_checked':refs_count,'errors':errors}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--html',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--result',type=Path)
    args=p.parse_args();result=validate(args.html,args.output_dir)
    if args.result:args.result.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False));return 0 if result['passed'] else 2

if __name__=='__main__':raise SystemExit(main())
