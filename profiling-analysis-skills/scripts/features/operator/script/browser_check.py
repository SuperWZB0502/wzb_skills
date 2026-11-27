"""Smoke-test the offline operator page and its complete CSV download."""
import csv
import math
from pathlib import Path


def check_chart(page, root, mode):
    chart = root.locator('.op-chart').first
    canvas = chart.locator('canvas')
    tooltip = chart.get_by_role('tooltip')
    page.mouse.move(0, 0)
    first_color = chart.locator('.op-legend i').first.evaluate('node => getComputedStyle(node).backgroundColor')
    if first_color != 'rgb(0, 98, 220)':
        raise ValueError('算子饼图起始颜色与 MindStudio 不一致')
    point = canvas.evaluate("""canvas => {
        const x = canvas.width / 2;
        const y = canvas.height / 2;
        const radius = Math.min(x, y) * 0.25;
        const scale = window.devicePixelRatio || 1;
        return {x: x / scale + 1, y: (y - radius) / scale};
    }""")
    before = canvas.evaluate('canvas => canvas.toDataURL()')
    canvas.hover(position=point)
    tooltip.wait_for()
    if 'μs' not in tooltip.inner_text() or '%' not in tooltip.inner_text():
        raise ValueError('算子饼图悬停未显示耗时和占比')
    first_position = tooltip.get_attribute('style')
    canvas.hover(position={'x': point['x'], 'y': point['y'] + 8})
    if tooltip.get_attribute('style') == first_position:
        raise ValueError('算子饼图浮窗未跟随鼠标')
    page.wait_for_function('(before) => document.querySelector(".op-chart canvas").toDataURL() !== before', arg=before)
    page.mouse.move(0, 0)
    tooltip.wait_for(state='hidden')
    first = chart.locator('.op-legend button').first
    percent = first.locator('.op-legend-percent').inner_text()
    fraction = float(percent.lstrip('<').rstrip('%')) / 100
    if fraction > .035:
        canvas.scroll_into_view_if_needed()
        bounds = canvas.bounding_box()
        radius = min(bounds['width'], bounds['height']) * .28
        center = (bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)

        def move(angle):
            page.mouse.move(center[0] + radius * math.sin(angle), center[1] - radius * math.cos(angle))

        original = root.locator('.op-table-area > .op-page > span').inner_text()
        move(fraction * math.tau * .15)
        page.mouse.down()
        page.wait_for_timeout(280)
        move(fraction * math.tau * .85)
        page.mouse.up()
        if root.locator('.op-table-area > .op-page > span').inner_text() == original:
            raise ValueError('扇形框选未更新算子表')
        canvas.hover(position={'x': bounds['width'] / 2 + radius * math.sin(.4),
                               'y': bounds['height'] / 2 - radius * math.cos(.4)})
        if percent not in tooltip.inner_text():
            raise ValueError('框选后扇区占比发生变化')
        up = chart.get_by_role('button', name='上一级', exact=True)
        up.click()
        if root.locator('.op-table-area > .op-page > span').inner_text() != original or not up.is_disabled():
            raise ValueError('上一级未恢复框选前的算子表')
        chart.get_by_role('button', name='重置饼图').click()
        if root.locator('.op-table-area > .op-page > span').inner_text() != original:
            raise ValueError('饼图重置未恢复算子表')
    cores = root.locator('.op-chart').nth(1)
    core_checks = cores.get_by_role('checkbox')
    if core_checks.count():
        names = set(core_checks.evaluate_all('nodes => nodes.map(node => node.ariaLabel)'))
        total_label = cores.locator('.op-core-summary')
        left_pie = canvas.bounding_box()
        right_pie = cores.locator('canvas').bounding_box()
        summary = total_label.bounding_box()
        if abs(left_pie['y'] - right_pie['y']) > 1:
            raise ValueError('左右饼图的画布未对齐')
        if summary['y'] + summary['height'] > right_pie['y'] or abs(summary['x'] - right_pie['x']) > 1:
            raise ValueError('已选核合计占比未显示在右侧饼图上方')
        cube_y = cores.get_by_role('button', name='cube', exact=True).bounding_box()['y']
        vector_y = cores.get_by_role('button', name='vector', exact=True).bounding_box()['y']
        if abs(cube_y - vector_y) > 1:
            raise ValueError('cube 和 vector 按钮未在同一行')
        core_picker = root.locator('.op-table-area > .op-filters').get_by_role('button', name='加速器核选项')
        core_menu = root.locator('.op-table-area > .op-filters .op-combo-menu:visible')

        def check_core_choices(expected, action, select_all=False):
            core_picker.click()
            enabled = core_menu.locator('input:enabled')
            if set(enabled.evaluate_all('nodes => nodes.map(node => node.value)')) != expected:
                raise ValueError(f'{action}后表格的可选核未同步饼图')
            if select_all:
                core_menu.get_by_role('button', name='全选', exact=True).click()
                if core_menu.locator('input:enabled:not(:checked), input:disabled:checked').count():
                    raise ValueError(f'{action}后表格全选未遵守饼图核范围')
            core_picker.click()

        for preset, expected in [('cube', {'AI_CORE', 'MIX_AIC'}),
                                 ('vector', {'AI_VECTOR_CORE', 'MIX_AIV'}), ('清除', set()), ('全选', names)]:
            cores.get_by_role('button', name=preset, exact=True).click()
            checked = set(core_checks.evaluate_all('nodes => nodes.filter(node => node.checked).map(node => node.ariaLabel)'))
            if checked != names & expected:
                raise ValueError(f'{preset} 应用的加速器核选项不正确')
            check_core_choices(checked, preset, select_all=True)
            reported = float(total_label.inner_text().split()[-1].lstrip('<').rstrip('%'))
            if preset in ('清除', '全选') and reported != (0 if preset == '清除' else 100):
                raise ValueError(f'{preset} 后已选核合计占比错误')
            if preset in ('cube', 'vector'):
                labels = cores.locator('li:has(input:checked) .op-legend-percent').all_inner_texts()
                if abs(reported - sum(float(value.lstrip('<').rstrip('%')) for value in labels)) > .021:
                    raise ValueError(f'{preset} 后已选核合计占比错误')
        first_core = core_checks.first
        first_name = first_core.get_attribute('aria-label')
        first_core.uncheck()
        check_core_choices(names - {first_name}, '取消勾选单个核')
        first_core.check()
        check_core_choices(names, '重新勾选单个核')
        if len(names) > 1:
            first_percent = cores.locator('.op-legend-percent').first.inner_text()
            fraction = float(first_percent.lstrip('<').rstrip('%')) / 100
            if fraction > .035:
                core_canvas = cores.locator('canvas')
                core_canvas.scroll_into_view_if_needed()
                bounds = core_canvas.bounding_box()
                radius = min(bounds['width'], bounds['height']) * .28

                def move_in_core_slice(position):
                    angle = fraction * math.tau * position
                    page.mouse.move(bounds['x'] + bounds['width'] / 2 + radius * math.sin(angle),
                                    bounds['y'] + bounds['height'] / 2 - radius * math.cos(angle))

                move_in_core_slice(.15)
                page.mouse.down()
                page.wait_for_timeout(280)
                move_in_core_slice(.75)
                page.mouse.up()
                check_core_choices({first_name}, '框选第一个核扇区')
                cores.get_by_role('button', name='上一级', exact=True).click()
                check_core_choices(names, '撤销核框选')
        cores.get_by_role('button', name='重置饼图').click()
    mode.select_option('compute')
    others = chart.get_by_role('button', name='查看 Others 明细')
    if others.count():
        if chart.locator('.op-legend button').count() != 50:
            raise ValueError('计算算子饼图未按 49 项加 Others 汇总')
        others.click()
        chart.get_by_role('heading', name='Others 明细').wait_for()
        if chart.locator('.op-legend button').count() < 2:
            raise ValueError('Others 明细缺少尾部算子')
        chart.get_by_role('button', name='上一级', exact=True).click()
        others.wait_for()
        # Locate an interior pixel of the Others wedge using its assigned color.
        color = others.locator('i').evaluate('node => getComputedStyle(node).backgroundColor.match(/\\d+/g).map(Number)')
        point = canvas.evaluate("""(canvas, rgb) => {
            const ctx = canvas.getContext('2d');
            const x = canvas.width / 2;
            const y = canvas.height / 2;
            const radius = Math.min(x, y) * 0.5;
            const scale = window.devicePixelRatio || 1;
            for (let i = 0; i < 360; i++) {
                const angle = i * Math.PI / 180;
                const dx = Math.cos(angle) * radius + x;
                const dy = Math.sin(angle) * radius + y;
                const pixel = ctx.getImageData(dx, dy, 1, 1).data;
                if (pixel.slice(0, 3).every((value, index) => value === rgb[index])) {
                    return {x: dx / scale, y: dy / scale};
                }
            }
            return null;
        }""", color)
        if point is None:
            raise ValueError('Others 扇区未绘制')
        canvas.click(position=point)
        chart.get_by_role('heading', name='Others 明细').wait_for()
        chart.get_by_role('button', name='上一级', exact=True).click()
    mode.select_option('compute-type')


def check_html(html, output_dir):
    from playwright.sync_api import sync_playwright

    output = Path(output_dir)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel='chrome')
        except Exception:
            browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(Path(html).resolve().as_uri())
            root = page.locator('.operator-report')
            root.locator('.op-table-area').wait_for(state='visible')
            mode = root.locator('[data-op=mode]')
            if mode.locator('option').count() != 4 or mode.input_value() != 'compute-type':
                raise ValueError('算子页分组配置与验收范围不符')
            counts = {}
            for selection in ('compute-type', 'compute', 'communication', 'communication-type'):
                mode.select_option(selection)
                pager = root.locator('.op-table-area > .op-page > span')
                expected = int(pager.inner_text().split()[1].replace(',', ''))
                counts[selection] = expected
                if not expected:
                    continue
                if selection.endswith('-type'):
                    details = root.get_by_role('button', name='详情', exact=True).first
                    row = root.locator('.op-table-area > .op-scroll > table > tbody > .op-data').first
                    row_state = """row => [...row.cells].map(cell =>
                        [cell.textContent, cell.offsetLeft, cell.offsetWidth, cell.offsetHeight])"""
                    before = row.evaluate(row_state)
                    details.click()
                    root.locator('.op-sub table').wait_for()
                    if row.evaluate(row_state) != before:
                        raise ValueError('展开详情改变了原始行的内容或尺寸')
                    root.get_by_role('button', name='收起', exact=True).click()
                filters = root.locator('.op-table-area > .op-filters')
                next_page = root.get_by_role('button', name='下一页', exact=True)
                if expected > 10:
                    next_page.click()
                    if '第 2/' not in pager.inner_text():
                        raise ValueError('未翻到第二页')
                    root.get_by_role('button', name='上一页', exact=True).click()
                filters.locator('input').first.fill('__wzb_operator_unmatched__')
                filters.get_by_role('button', name='搜索', exact=True).click()
                root.get_by_text('暂无数据', exact=True).wait_for()
                with page.expect_download() as event:
                    root.get_by_role('button', name='导出 CSV').click()
                csv_path = output / f'checked-{selection}.csv'
                event.value.save_as(csv_path)
                with csv_path.open(encoding='utf-8-sig', newline='') as handle:
                    if sum(1 for _ in csv.reader(handle)) - 1 != expected:
                        raise ValueError(f'{selection} CSV 行数少于完整表格结果')
                filters.get_by_role('button', name='重置', exact=True).click()
            mode.select_option('compute-type')
            if counts['compute-type']:
                column = root.locator('.op-table-area > .op-scroll > table > thead th[data-field=total]')
                position = column.bounding_box()
                cell_padding = {'x': position['width'] - 10, 'y': 3}
                column.click(position=cell_padding)
                root.locator('th[aria-sort=ascending]').wait_for()
                column.click(position=cell_padding)
                root.locator('th[aria-sort=descending]').wait_for()
                column.click(position=cell_padding)
                if root.locator('th[aria-sort]').count():
                    raise ValueError('未恢复默认排序')
                if column.evaluate('node => getComputedStyle(node).textAlign') != 'left':
                    raise ValueError('算子详情表头未左对齐')
                handle = column.get_by_role('separator')
                handle.scroll_into_view_if_needed()
                grip = handle.bounding_box()
                original_width = column.bounding_box()['width']
                x, y = grip['x'] + grip['width'] / 2, grip['y'] + grip['height'] / 2
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(x + 35, y, steps=4)
                page.mouse.up()
                if column.bounding_box()['width'] < original_width + 30 or root.locator('th[aria-sort]').count():
                    raise ValueError('拖动表头边缘未改变列宽或误触排序')
                filters = root.locator('.op-table-area > .op-filters')
                for key, label, index in (('type', '类型', 0), ('core', '加速器核', 1)):
                    choices = filters.get_by_role('button', name=label + '选项')
                    visible_first = root.locator('.op-table-area > .op-scroll > table > tbody > .op-data').first
                    chosen = visible_first.locator('td').nth(index).inner_text()
                    choices.click()
                    menu = filters.locator('.op-combo-menu:visible')
                    menu.get_by_role('button', name='清空', exact=True).click()
                    root.get_by_text('暂无数据', exact=True).wait_for()
                    menu.get_by_role('checkbox', name=chosen, exact=True).check()
                    cells = root.locator(f'.op-table-area > .op-scroll > table > tbody > .op-data > td:nth-child({index + 1})')
                    if not cells.count() or set(cells.all_inner_texts()) != {chosen}:
                        raise ValueError(f'{label}选择项未按精确值筛选')
                    if cells.first.evaluate('node => getComputedStyle(node).textAlign') != 'left':
                        raise ValueError('算子详情数据未左对齐')
                    filter_icon = root.locator(f'th[data-field={key}] .op-filter-active')
                    if filter_icon.count() != 1:
                        raise ValueError(f'{label}筛选状态图标未点亮')
                    menu.get_by_role('button', name='全选', exact=True).click()
                    if menu.locator('input:not(:checked)').count():
                        raise ValueError(f'{label}全选未勾选全部选项')
                    filters.get_by_role('button', name='重置', exact=True).click()
                legend = root.locator('.op-legend button').first
                legend.click()
                if legend.get_attribute('aria-pressed') != 'false':
                    raise ValueError('图例无法隐藏耗时项')
                legend.click()
                if not root.locator('canvas').first.evaluate(
                        'c => c.getContext("2d").getImageData(0,0,c.width,c.height).data.some((v,i) => i%4===3 && v > 0)'):
                    raise ValueError('耗时图没有绘制')
                check_chart(page, root, mode)
            rank = root.locator('[data-op=rank]')
            if rank.locator('option').count() > 1:
                initial = rank.input_value()
                rank.select_option(index=1)
                if rank.input_value() == initial:
                    raise ValueError('切换卡序号失败')
                rank.select_option(initial)
            root.screenshot(path=str(output / 'operator-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            root.screenshot(path=str(output / 'operator-mobile.png'))
            if root.evaluate('el => el.scrollWidth > el.clientWidth + 1'):
                raise ValueError('小屏幕发生页面横向溢出')
            if errors:
                raise ValueError('浏览器脚本错误：' + '；'.join(errors))
            return {'passed': True, 'rows_by_mode': counts}
        finally:
            browser.close()
