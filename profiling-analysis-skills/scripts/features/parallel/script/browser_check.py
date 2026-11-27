"""Optional offline HTML smoke check, deriving counts from this run rather than fixed samples."""
from pathlib import Path
from workflow_support import load_json,write_json
from parallel_domain_registry import DOMAINS

def check_html(html,output_dir):
    try:from playwright.sync_api import sync_playwright
    except ImportError as exc:raise RuntimeError('浏览器验收是可选项；需要Playwright及Chromium。') from exc
    out=Path(output_dir);errors=[];requests=[];validation=load_json(out/'validation.json') if (out/'validation.json').exists() else {}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1600,'height':1150})
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url));page.goto(Path(html).resolve().as_uri(),timeout=180000)
        page.wait_for_function("document.querySelector('#pp-schedule')?.dataset.ready==='true'",timeout=180000)
        root=page.locator('#pp-schedule');step=root.locator('[data-pp=step]').input_value()
        import ijson
        expected=0
        active_domains=set()
        with (out/'unified_report.json').open('rb') as handle:
            options=next(ijson.items(handle,'analysis_options'),{})
            expected_streams=['communication'] if options.get('communication_only') else ['compute','communication','free']
            handle.seek(0)
            for event in ijson.items(handle,'events.item'):
                if event['kind']=='communication':
                    active_domains.add(event.get('domain') or 'unresolved')
                    active_domains.update(event.get('domain_aliases',[]))
                if str(event['step'])==step and event['kind'] in expected_streams:expected+=1
        checked_streams=root.locator('input[data-stream]:checked').evaluate_all('items => items.map(e => e.dataset.stream)')
        if checked_streams!=expected_streams:errors.append(f'默认显示流{checked_streams}与预期{expected_streams}不符')
        lanes=root.locator('[data-lane-label]').evaluate_all('items => [...new Set(items.map(e => e.dataset.laneLabel))]')
        if root.locator('[data-stage-row]').count() and set(lanes)!=set(expected_streams):
            errors.append(f'初始泳道{lanes}与预期{expected_streams}不符')
        actual=int(root.locator('[data-pp=total-count]').inner_text())
        if actual!=expected:errors.append(f'图中事件计数{actual}与当前Step来源{expected}不符')
        expected_domains=len(DOMAINS)+int(bool(load_json(out/'unified_summary.json').get('stats',{}).get('by_domain',{}).get('unresolved')))
        if root.locator('input[data-domain]').count()!=expected_domains:errors.append('通信域控件不完整')
        checked=root.locator('input[data-domain]:checked').evaluate_all('(items)=>items.map(e=>e.dataset.domain)')
        phases=root.locator('input[data-phase]:checked').evaluate_all('(items)=>items.map(e=>e.dataset.phase)')
        enabled=root.locator('input[data-domain]:enabled').evaluate_all('(items)=>items.map(e=>e.dataset.domain)')
        if set(checked)!=active_domains or set(enabled)!=active_domains:errors.append('可用通信域及默认选择不匹配')
        if phases!=['f','b']:errors.append('默认标注应为f/b')
        root.screenshot(path=str(out/'browser.png'));browser.close()
    if any(r.startswith(('http:','https:')) for r in requests):errors.append('出现在线依赖请求')
    result={'passed':not errors,'html':str(Path(html).resolve()),'step':step,'communication_events':actual,'default_streams':checked_streams,'errors':errors};write_json(out/'browser_validation.json',result)
    if errors:raise ValueError(str(errors))
    return result
