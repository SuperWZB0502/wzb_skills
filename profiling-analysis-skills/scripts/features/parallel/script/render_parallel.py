"""Render the final report, caching its compressed payload separately from the UI template."""
import argparse,json
from pathlib import Path
from workflow_support import load_json,write_json,file_state,code_key,digest,update_section

def payload_from_html(path):
    text=Path(path).read_text(encoding='utf-8');marker='<script type="application/json" id="pp-schedule-data">';start=text.index(marker)+len(marker)
    return text[start:text.index('</script>',start)]

def render(output_dir,html,payload_html=None):
    out=Path(output_dir);source=out/'unified_report.json';script=Path(__file__).resolve().parent
    from refresh_domains import refresh
    refresh(out)
    from parallel_domain_split import START,END
    from presentation_summary import collect_summary,encode_summary
    pp=load_json(out/'report.json')
    manifest=load_json(out/'analysis_manifest.json') if (out/'analysis_manifest.json').exists() else {}
    presentation=collect_summary(pp,manifest.get('input',{}).get('log_dir'))
    write_json(out/'presentation_summary.json',presentation)
    from operator_details import collect_details
    details,details_stats=collect_details(out)
    # The detail sidecar has its own source/code cache; validate once per version.
    check_key=digest({'details':file_state(out/'operator_details.json'),'validator':code_key(script,['validate_operator_details.py','operator_details.py'])})
    check_meta=out/'operator_details_validation.meta.json';check_file=out/'operator_details_validation.json'
    verified=check_meta.exists() and check_file.exists() and load_json(check_meta).get('key')==check_key and load_json(check_meta).get('result_state')==file_state(check_file)
    if not verified:
        from validate_operator_details import validate as validate_details
        checked=validate_details(out)
        if not checked['passed']:raise ValueError('算子详情来源核对失败：'+str(checked['errors'][:5]))
        write_json(check_meta,{'key':check_key,'result_state':file_state(check_file)})
    details_stats['validation_passed']=True
    detail_data=load_json(out/'operator_details.json')
    key=digest({'report':file_state(source),'details':file_state(out/'operator_details.json'),'encoder':code_key(script,['timeline_payload.py','compact_timeline.py'])})
    cached=out/'html_payload.json';meta_path=out/'html_payload.meta.json';hit=False
    if cached.exists() and meta_path.exists():
        meta=load_json(meta_path)
        hit=meta.get('source_key')==key and meta.get('payload_state')==file_state(cached)
    if hit:encoded=cached.read_text(encoding='utf-8')
    else:
        from timeline_payload import encode_report,_unpack,decode_report
        if payload_html:
            encoded=payload_from_html(payload_html);envelope=json.loads(encoded)
            data=_unpack(envelope['metadata']) if envelope.get('encoding') in ('gzip-base64-chunks','gzip-base64-columns-v1') else decode_report(envelope)
            pp=load_json(out/'report.json');summary=load_json(out/'unified_summary.json')
            if data.get('profiling_path')!=pp.get('profiling_path') or data.get('stats',{}).get('event_count')!=summary['stats']['event_count'] or data.get('time_origin_us')!=pp['time_origin_us']:raise ValueError('指定HTML载荷与当前报告不属于同一输入/结果。')
        else:encoded=encode_report(load_json(source),detail_data.get('compute_event_refs',{}))
        temporary=cached.with_suffix('.tmp');temporary.write_text(encoded,encoding='utf-8');temporary.replace(cached)
        envelope=json.loads(encoded)
        write_json(meta_path,{'source_key':key,'payload_state':file_state(cached),'encoding':envelope.get('encoding')})
    from operator_details import encode_details
    envelope=json.loads(encoded)
    details=encode_details(detail_data,embedded_refs=envelope.get('detail_refs_embedded',False))
    section=(script/'parallel_timeline.html').read_text(encoding='utf-8').replace('__DATA__',encoded).replace('__SUMMARY__',encode_summary(presentation)).replace('__DETAILS__',details).replace('__COLUMN_UI__',(script/'compact_timeline.js').read_text(encoding='utf-8')).replace('__DETAIL_UI__',(script/'detail_panels.js').read_text(encoding='utf-8'))
    update_section(html,START,END,section)
    return {'html':str(Path(html).resolve()),'payload_cache_hit':hit,'payload':str(cached),'operator_details':details_stats}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--html',type=Path,required=True);p.add_argument('--payload-from-html',type=Path);a=p.parse_args();print(render(a.output_dir,a.html,a.payload_from_html))
