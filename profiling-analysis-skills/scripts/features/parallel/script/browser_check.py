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
        # The UI defaults to PP/GLOBAL; sum this Step's source events in those domains.
        import ijson
        expected=0
        with (out/'unified_report.json').open('rb') as handle:
            for event in ijson.items(handle,'events.item'):
                if str(event['step'])==step and event['kind']=='communication' and event.get('domain') in ('PP','GLOBAL'):expected+=1
        actual=int(root.locator('[data-pp=total-count]').inner_text())
        if expected is not None and actual!=expected:errors.append(f'图中通信计数{actual}与当前Step来源{expected}不符')
        expected_domains=len(DOMAINS)+int(bool(load_json(out/'unified_summary.json').get('stats',{}).get('by_domain',{}).get('unresolved')))
        if root.locator('input[data-domain]').count()!=expected_domains:errors.append('通信域控件不完整')
        checked=root.locator('input[data-domain]:checked').evaluate_all('(items)=>items.map(e=>e.dataset.domain)')
        phases=root.locator('input[data-phase]:checked').evaluate_all('(items)=>items.map(e=>e.dataset.phase)')
        if checked!=['PP','GLOBAL']:errors.append('默认通信域应为PP/GLOBAL')
        if phases!=['f','b']:errors.append('默认标注应为f/b')
        root.screenshot(path=str(out/'browser.png'));browser.close()
    if any(r.startswith(('http:','https:')) for r in requests):errors.append('出现在线依赖请求')
    result={'passed':not errors,'html':str(Path(html).resolve()),'step':step,'communication_events':actual,'errors':errors};write_json(out/'browser_validation.json',result)
    if errors:raise ValueError(str(errors))
    return result
