#!/usr/bin/env python3
"""Optional isolated Chromium benchmark. Requires playwright + psutil, not runtime dependencies.

Each navigation starts a new browser process. --output-dir must be new. Metrics
are measurements of this browser/host; RSS includes shared pages, not unique RAM.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import tempfile
import threading
import time


def benchmark(html, output):
    import psutil
    from playwright.sync_api import sync_playwright
    output.mkdir(parents=True)
    tmp = output/'tmp';tmp.mkdir()
    os.environ['TMPDIR'] = str(tmp.resolve());tempfile.tempdir = str(tmp.resolve())
    errors, requests, samples = [], [], []
    stop = threading.Event();owner = psutil.Process()
    def monitor():
        while not stop.wait(.05):
            rss=0
            for proc in owner.children(recursive=True):
                try:
                    if '--type=renderer' in proc.cmdline():rss+=proc.memory_info().rss
                except (psutil.NoSuchProcess,psutil.AccessDenied):pass
            samples.append({'elapsed':time.perf_counter()-start,'renderer_rss_bytes':rss})
    start=time.perf_counter();thread=threading.Thread(target=monitor,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':1600,'height':1150},accept_downloads=True)
            page.add_init_script('window.__MSINSIGHT_BENCHMARK__=true')
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:requests.append(request.url))
            cdp=page.context.new_cdp_session(page);cdp.send('Performance.enable')
            loading=time.perf_counter();page.goto(Path(html).resolve().as_uri(),wait_until='commit',timeout=180000)
            heap=[]
            while True:
                metrics={m['name']:m['value'] for m in cdp.send('Performance.getMetrics')['metrics']};heap.append(metrics.get('JSHeapUsedSize',0))
                state=page.locator('#pp-schedule').get_attribute('data-ready') if page.locator('#pp-schedule').count() else None
                if state=='error':raise RuntimeError(page.locator('[data-pp=loading]').inner_text())
                if state=='true':break
                if time.perf_counter()-loading>240:raise TimeoutError('HTML loading exceeded 240 seconds')
                page.wait_for_timeout(100)
            page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
            ready=time.perf_counter()-loading
            root=page.locator('#pp-schedule');count=int(root.locator('[data-pp=total-count]').inner_text());step=root.locator('[data-pp=step]').input_value()
            heap.append({m['name']:m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}.get('JSHeapUsedSize',0))
            loading_rss_peak=max((s['renderer_rss_bytes'] for s in samples),default=0)
            cdp.send('HeapProfiler.collectGarbage')
            retained={m['name']:m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
            default_rss=samples[-1]['renderer_rss_bytes'] if samples else 0
            root.screenshot(path=str(output/'default.png'))
            with page.expect_download() as download:
                root.locator('[data-pp=download]').click()
            download.value.save_as(output/'filtered.csv')
            # Measure the existing UI, including dense compute/copy/Free and zoom.
            timings={}
            for kind in ['compute','copy','free']:
                box=root.locator('input[data-stream="'+kind+'"]')
                t=time.perf_counter();box.evaluate('(element)=>element.click()')
                page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                timings[kind+'_toggle_seconds']=time.perf_counter()-t
            chart=root.locator('[data-pp=chart]');rect=chart.bounding_box()
            page.mouse.move(rect['x']+rect['width']*.6,rect['y']+80)
            page.evaluate('window.__frames=[];window.__measureFrames=true;let previous=performance.now();function tick(t){if(!window.__measureFrames)return;window.__frames.push(t-previous);previous=t;requestAnimationFrame(tick);}requestAnimationFrame(tick)')
            t=time.perf_counter();page.keyboard.down('w');page.wait_for_timeout(400);page.keyboard.up('w');page.wait_for_timeout(500)
            frames=page.evaluate('window.__measureFrames=false;window.__frames')
            timings['zoom_wall_seconds']=time.perf_counter()-t
            positive=sorted(v for v in frames if v>0);timings['zoom_frame_p95_ms']=positive[min(len(positive)-1,int(len(positive)*.95))] if positive else None
            timings['zoom_frames']=len(frames)
            page.keyboard.press('Escape');page.wait_for_timeout(200)
            root.screenshot(path=str(output/'all-streams.png'))
            result={'html':str(Path(html).resolve()),'html_bytes':Path(html).stat().st_size,'browser':browser.version,'ready_seconds':ready,
                    'js_heap_sampled_peak_bytes':max(heap,default=0),'js_heap_retained_bytes':retained.get('JSHeapUsedSize'),
                    'renderer_rss_loading_peak_bytes':loading_rss_peak,'renderer_rss_at_ready_bytes':default_rss,'default_event_count':count,'step':step,'interactions':timings,
                    'errors':errors,'external_requests':[r for r in requests if r.startswith(('http:','https:'))]}
            if page.evaluate('!!window.__MSINSIGHT_DATA__'):
                result['packed_checks']=page.evaluate('''()=>{const {data,computeRefFor}=window.__MSINSIGHT_DATA__;return {event_count:data.events.length,refs:data.events.filter(e=>e.kind==='compute'&&computeRefFor(e)).length,unreleased_payload_nodes:document.querySelectorAll('#pp-schedule-data,#operator-details-data').length};}''')
            browser.close()
    finally:
        stop.set();thread.join(timeout=5)
    result['renderer_rss_sampled_peak_bytes']=max((s['renderer_rss_bytes'] for s in samples),default=0)
    (output/'resource_samples.json').write_text(json.dumps(samples))
    (output/'measurement.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    if result['errors'] or result['external_requests']:raise RuntimeError(str(result))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',action='append',required=True,help='NAME=/absolute/path/report.html; repeat for comparisons')
    p.add_argument('--runs',type=int,default=3);p.add_argument('--output-dir',type=Path,required=True)
    args=p.parse_args()
    if args.runs<1:p.error('runs must be positive')
    if args.output_dir.exists():p.error('Use a new output directory for each experiment')
    args.output_dir.mkdir(parents=True)
    cases=[item.split('=',1) for item in args.case];measurements=[]
    for run in range(args.runs):
        for name,html in (cases if run%2==0 else list(reversed(cases))):
            if not name or Path(name).name!=name:p.error('case NAME must be a single directory name')
            value=benchmark(html,args.output_dir/name/f'run{run+1}');value.update(case=name,run=run+1);measurements.append(value)
            print(json.dumps(value,ensure_ascii=False),flush=True)
    summary={name:{key:statistics.median(m[key] for m in measurements if m['case']==name) for key in ['html_bytes','ready_seconds','js_heap_retained_bytes','js_heap_sampled_peak_bytes','renderer_rss_sampled_peak_bytes','renderer_rss_loading_peak_bytes','renderer_rss_at_ready_bytes']} for name,_ in cases}
    (args.output_dir/'comparison.json').write_text(json.dumps({'measurements':measurements,'medians':summary},ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
