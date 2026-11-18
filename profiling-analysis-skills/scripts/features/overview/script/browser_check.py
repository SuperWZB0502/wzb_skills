"""Optional data-driven offline overview smoke check; no fixed rank/GBS/Step."""
from pathlib import Path
from workflow_support import load_json,write_json

def check_html(html,output_dir):
    try:from playwright.sync_api import sync_playwright
    except ImportError as exc:raise RuntimeError('浏览器验收是可选项；需要Playwright及Chromium。') from exc
    out=Path(output_dir);report=load_json(out/'report.json');errors=[];requests=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox']);page=browser.new_page(viewport={'width':1600,'height':1150})
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url));page.goto(Path(html).resolve().as_uri(),timeout=180000)
        root=page.locator('#rank-step-time');step=root.get_by_label('Step',exact=True).input_value()
        if root.locator('tbody tr').count()!=len(report['steps'][step]):errors.append('表格行数与当前Step不符')
        if root.locator('th').count()!=18 or root.locator('.chip').count()!=11:errors.append('列/配置色块数量不符')
        root.get_by_role('switch',name='百分比显示').check();root.screenshot(path=str(out/'browser.png'));browser.close()
    if any(r.startswith(('http:','https:')) for r in requests):errors.append('出现在线依赖请求')
    result={'passed':not errors,'step':step,'rows':len(report['steps'][step]),'errors':errors};write_json(out/'browser_validation.json',result)
    if errors:raise ValueError(str(errors))
    return result
