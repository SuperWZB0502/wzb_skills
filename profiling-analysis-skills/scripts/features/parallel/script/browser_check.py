"""Optional offline HTML smoke check, deriving counts from this run rather than fixed samples."""
from pathlib import Path
from workflow_support import load_json,write_json
from parallel_domain_registry import DOMAINS


def check_operation_guide(root):
    button = root.locator('[data-pp=guide-toggle]')
    panel = root.locator('[data-pp=guide-panel]')
    button.click()
    assert panel.is_visible()
    keys = panel.locator('.guide-key kbd').all_text_contents()
    assert keys == list('QWEASDF')
    for index in (0, 2):
        note = panel.locator('.guide-key-upper').nth(index).locator('.guide-key-note')
        assert note.evaluate('node => getComputedStyle(node).textAlign') == 'left'
        lines = note.locator(':scope > div').all_text_contents()
        assert lines[1].startswith('单击 · ') and lines[2].startswith('双击 · ')
    assert panel.locator('.guide-mouse-shape').is_visible()
    button.click()
    assert panel.is_hidden()
    for control, key in (('zoom-selection', 'E'), ('flag-zoom-selection', 'E'),
                         ('flag-delete-selection', 'F'), ('flag-selection-ends', 'F')):
        assert root.locator(f'[data-pp={control}]').text_content().endswith(f'（{key}）')


def check_flag_interactions(page, root):
    chart = root.locator('[data-pp=chart]')
    track = chart.locator('[data-pp=flag-track]')
    track.scroll_into_view_if_needed()
    bounds = chart.bounding_box()
    left = float(chart.get_attribute('data-left'))
    width = float(chart.get_attribute('data-plot-width'))
    intervals = track.locator('[data-flag-interval]')
    flag_top = float(intervals.first.get_attribute('y'))
    flag_height = float(intervals.first.get_attribute('height'))
    assert intervals.first.get_attribute('fill') == 'transparent', '旗位行应透出时间轴底色'
    time_label = chart.locator('[data-pp=axis-time-label]')
    flag_label = chart.locator('[data-pp=axis-flag-label]')
    font = '(label) => getComputedStyle(label).font'
    assert flag_top == flag_height, '时间刻度与旗位行应同高'
    assert flag_label.evaluate(font) == time_label.evaluate(font), '时间与旗位标题字体应一致'
    assert flag_label.get_attribute('x') == time_label.get_attribute('x'), '时间与旗位标题应左对齐'
    assert float(flag_label.get_attribute('y')) - float(time_label.get_attribute('y')) == flag_height
    boundaries = chart.locator('[data-pp=time-boundary]')
    assert boundaries.count() == 2
    assert boundaries.locator('..').first.locator('line').count() == 2, '时间刻度不应延伸网格线'
    for boundary in boundaries.all():
        assert float(boundary.get_attribute('y2')) == flag_top
    chooser = root.locator('.flag-picker summary')
    title = flag_label.bounding_box()
    assert chooser.bounding_box()['x'] > title['x'] + title['width'], '复选下拉应位于旗位标题右侧'

    def point(fraction, y=flag_top + flag_height / 2):
        return bounds['x'] + left + width * fraction, bounds['y'] + y

    markers = track.locator('[data-flag-id]')
    flag_count = root.locator('[data-pp=flag-count]')
    assert flag_count.inner_text() == '0/0'
    page.mouse.click(*point(.4))
    markers.first.wait_for()
    assert intervals.count() == 2, '首个旗位应将当前 Step 分成两段'
    assert flag_count.inner_text() == '1/1'
    chooser.click()
    checkbox = root.locator('[data-pp=flag-list] input[type=checkbox]')
    assert checkbox.is_checked()
    flag_row = checkbox.locator('..')
    assert flag_row.evaluate('row => [...row.children].map(child => child.tagName)') == ['INPUT', 'I', 'SPAN']
    root.locator('[data-pp=flag-none]').click()
    assert not checkbox.is_checked() and markers.count() == 0
    assert flag_count.inner_text() == '0/1'
    root.locator('[data-pp=flag-all]').click()
    assert checkbox.is_checked() and markers.count() == 1
    assert flag_count.inner_text() == '1/1'
    chooser.click()

    editor = root.locator('[data-pp=flag-editor]')
    name = editor.locator('[data-pp=flag-editor-name]')
    markers.first.click(button='right')
    root.locator('[data-pp=flag-rename]').click()
    assert editor.is_visible() and name.input_value() == 'flag'
    assert editor.locator('[data-pp=flag-editor-prefix]').inner_text() == '01-'
    assert editor.locator('[data-pp=flag-editor-times] th').all_text_contents() == ['时间']
    name.fill('通信边界')
    name.press('Enter')
    assert not editor.is_visible()
    assert markers.first.get_attribute('aria-label').startswith('01-通信边界')

    page.mouse.click(*point(.2), button='right')
    root.locator('[data-pp=flag-rename]').click()
    assert editor.is_visible()
    assert editor.locator('[data-pp=flag-editor-times] th').all_text_contents() == ['起始', '结束', '持续时间']
    name.fill('连贯区间')
    editor.locator('[data-pp=flag-editor-save]').click()
    page.mouse.move(*point(.21))
    assert '连贯区间' in root.locator('[data-pp=operator-tooltip]').inner_text()
    page.mouse.move(*point(.14))
    page.mouse.down(button='right')
    page.mouse.move(*point(.32, 105), steps=5)
    page.mouse.up(button='right')
    assert chart.get_attribute('data-zoom-depth') == '0', '旗位行的右键拖拽不应触发框选放大'
    page.mouse.dblclick(*point(.25))
    assert chart.get_attribute('data-zoom-depth') == '1', '区间双击应该放大'
    page.wait_for_timeout(220)
    assert markers.count() == 1, '区间双击不应生成新旗'
    page.mouse.click(*point(.25), button='right')
    root.locator('[data-pp=flag-rename]').click()
    assert name.input_value() == '连贯区间'
    name.fill('取消编辑')
    name.press('Escape')
    assert not editor.is_visible() and chart.get_attribute('data-zoom-depth') == '1'
    page.mouse.move(*point(.21))
    assert '连贯区间' in root.locator('[data-pp=operator-tooltip]').inner_text()
    page.keyboard.press('q')
    assert chart.get_attribute('data-zoom-depth') == '0', 'Q 应恢复上一级'
    page.mouse.move(*point(.2, flag_top + flag_height + 12))
    page.keyboard.press('f')
    assert markers.count() == 2, '泳道区域按 F 应在游标处插旗'
    page.mouse.move(*point(.3))
    page.keyboard.press('f')
    assert markers.count() == 2, '旗位行应沿用鼠标插旗操作'
    markers.first.hover()
    page.keyboard.press('f')
    assert markers.count() == 1, '悬停旗子时按 F 应删除该旗'
    page.mouse.move(*point(.2, flag_top + flag_height + 12))
    page.keyboard.press('f')
    page.mouse.move(*point(.12))
    page.mouse.down()
    page.mouse.move(*point(.48), steps=8)
    page.mouse.up()
    assert track.locator('[data-flag-selected]').count() == 2, '旗位框选应标出所选旗子'
    assert root.locator('[data-pp=flag-delete-selection]').is_visible()
    page.keyboard.press('f')
    assert markers.count() == 0, '旗位框选后按 F 应批量删除'

    def window():
        return (chart.get_attribute('data-view-start'), chart.get_attribute('data-view-end'))

    views = [window()]
    for depth in range(1, 4):
        page.mouse.move(*point(.2))
        page.mouse.down()
        page.mouse.move(*point(.7), steps=5)
        page.mouse.up()
        assert not root.locator('[data-pp=flag-delete-selection]').is_visible()
        if depth == 1:
            assert root.locator('[data-pp=flag-zoom-selection]').is_visible()
            page.keyboard.press('e')
        else:
            root.locator('[data-pp=flag-zoom-selection]').click()
        assert int(chart.get_attribute('data-zoom-depth')) == depth
        views.append(window())
    page.keyboard.press('Escape')
    assert window() == views[3], 'Esc 不应修改时间窗'
    page.keyboard.press('q')
    assert window() == views[2], (window(), views[2], chart.get_attribute('data-zoom-depth'))
    page.keyboard.press('q')
    assert window() == views[0], '连续按 Q 应回到全局'
    page.keyboard.press('e')
    assert window() == views[1]
    page.keyboard.press('e')
    assert window() == views[3], '连续按 E 应恢复到最深的历史视窗'
    page.keyboard.press('q')
    assert window() == views[2]
    page.mouse.move(*point(.32))
    page.mouse.down()
    page.mouse.move(*point(.63), steps=5)
    page.mouse.up()
    root.locator('[data-pp=flag-zoom-selection]').click()
    new_view = window()
    assert new_view != views[3]
    page.keyboard.press('e')
    assert window() == new_view, '新放大应替换此前可前进的视窗'

    if chart.locator('[data-lane-label]').count():
        page.mouse.move(*point(.24, flag_top + flag_height + 12))
        page.mouse.down()
        page.mouse.move(*point(.72, flag_top + flag_height + 75), steps=5)
        page.mouse.up()
        root.locator('[data-pp=zoom-selection]').wait_for()
        page.keyboard.press('f')
        assert markers.count() == 2, '框选后按 F 应在选区起止处各插一面旗'
        page.keyboard.press('e')
        assert int(chart.get_attribute('data-zoom-depth')) == 4, '框选后按 E 应放大选区'


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
        root.screenshot(path=str(out/'browser.png'))
        check_operation_guide(root)
        check_flag_interactions(page, root)
        browser.close()
    if any(r.startswith(('http:','https:')) for r in requests):errors.append('出现在线依赖请求')
    result={'passed':not errors,'html':str(Path(html).resolve()),'step':step,'communication_events':actual,'default_streams':checked_streams,'errors':errors};write_json(out/'browser_validation.json',result)
    if errors:raise ValueError(str(errors))
    return result
