(() => {
  'use strict';

  const root = document.currentScript.closest('.operator-report');
  const mode = root.querySelector('[data-op="mode"]');
  const rank = root.querySelector('[data-op="rank"]');
  const host = root.querySelector('[data-op="host"]');
  const exportButton = root.querySelector('[data-op="export"]');
  const tableArea = root.querySelector('.op-table-area');
  const charts = root.querySelector('.op-charts');
  const message = root.querySelector('.op-message');
  // MindStudio Insight chartColors, modules/lib/src/utils/Common.tsx.
  const colors = [
    '#0062DC', '#279C6E', '#34B1B9', '#6037DB', '#FF5432', '#0077FF',
    '#FD2F2F', '#D53F78', '#2F9CE0', '#EE891D', '#AA38CE', '#75A105',
    '#E44222', '#0158C5', '#248B62', '#2F9EA6', '#026AE5', '#E32A2A',
    '#BF376C', '#2A8CC9', '#5631C4', '#D57B18', '#9831B9', '#699006',
    '#CB2F0F', '#004EB0', '#1F7C58', '#2A8D93', '#005FCC', '#CA2425',
    '#AA3260', '#247CB3', '#4C2CAF', '#BE6D17', '#882CA4', '#5D8004',
    '#FE6E50', '#2679E1', '#48AA82', '#51BCC3', '#278AFF', '#FD4444',
    '#DB5B8C', '#4CAAE4', '#7754E0', '#F09A3D', '#B655D4', '#89AF2A',
    '#FF876F', '#4D91E5', '#67B999', '#71C8CE', '#4DA0FF', '#FD5857',
    '#E178A0', '#6DB9E8', '#9073E5', '#F3AC60', '#C374DC', '#9EBD51',
  ];
  const maxPieCategories = 50;
  const number = Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 });
  const time = Intl.NumberFormat('zh-CN', { useGrouping: false, minimumFractionDigits: 3, maximumFractionDigits: 3 });
  const textSort = new Intl.Collator('zh-CN', { numeric: true });
  const computeColumns = [
    ['name', '名称'], ['type', '类型'], ['core', '加速器核'], ['start_ms', '开始时间(ms)', true],
    ['duration', '时长(μs)', true], ['wait', '等待时间(μs)', true], ['block', 'Block数量', true],
    ['input_shapes', '输入Shapes'], ['input_types', '输入数据类型'], ['input_formats', '输入格式'],
    ['output_shapes', '输出Shapes'], ['output_types', '输出数据类型'], ['output_formats', '输出格式'],
  ];
  const communicationColumns = computeColumns.filter(([key]) => ['name', 'type', 'start_ms', 'duration', 'wait'].includes(key));
  const groupColumns = [
    ['type', '类型'], ['core', '加速器核'], ['count', '数量', true],
    ['total', '总耗时(μs)', true], ['avg', '平均耗时(μs)', true],
    ['max', '最大耗时(μs)', true], ['min', '最小耗时(μs)', true],
  ];
  const formattedTimes = new Set(['total', 'avg', 'min', 'max', 'duration', 'wait']);
  const choiceFields = new Set(['type', 'core']);
  const missing = 'N/A';
  let cards;
  let observer;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function button(text, action, className) {
    const node = element('button', text, className);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }

  function selectOption(value, label) {
    return new Option(label, value);
  }

  function accessor(row, field) {
    return field.startsWith('pmu:') ? row.pmu?.[field.slice(4)] : row[field];
  }

  function cellValue(row, field) {
    const value = accessor(row, field);
    return value == null || String(value).trim() === '' ? missing : String(value);
  }

  function detailColumns(card, kind) {
    if (kind === 'communication') return communicationColumns;
    return [...computeColumns, ...card.metrics.map(metric => ['pmu:' + metric, metric, true])];
  }

  function selectRows(records, columns, defaultField, state) {
    const filters = Object.entries(state.filters).map(([key, value]) => [key, value.toLowerCase()]);
    const choices = Object.entries(state.choices ?? {});
    const visible = records.filter(row =>
      choices.every(([key, values]) => values.has(cellValue(row, key))) &&
      filters.every(([key, value]) => cellValue(row, key).toLowerCase().includes(value)));
    const field = state.field ?? defaultField;
    const numeric = columns.find(column => column[0] === field)?.[2];
    return visible.sort((a, b) => {
      const lhs = accessor(a, field);
      const rhs = accessor(b, field);
      if (lhs == null || rhs == null) return Number(lhs == null) - Number(rhs == null);
      const difference = numeric && Number.isFinite(+lhs) && Number.isFinite(+rhs)
        ? Number(lhs) - Number(rhs) : textSort.compare(lhs, rhs);
      return state.direction * difference;
    });
  }

  function table(container, records, columns, options = {}) {
    const state = { page: 1, size: 10, field: null, direction: -1, filters: {}, choices: {} };
    const defaultField = options.detail ? 'duration' : 'total';
    let expanded = null;
    let rows = selectRows(records, columns, defaultField, state);
    const filters = element('form', undefined, 'op-filters');
    const searchFields = columns.filter(([key]) => ['name', 'type', 'core'].includes(key));
    const selectors = new Map();
    for (const [key, title] of searchFields) {
      const input = element('input');
      input.type = 'search';
      input.name = key;
      input.maxLength = 200;
      const label = element('label', title);
      if (choiceFields.has(key)) {
        const control = element('div', undefined, 'op-combo');
        const menu = element('div', undefined, 'op-combo-menu');
        const actions = element('div', undefined, 'op-combo-actions');
        const choices = element('div', undefined, 'op-combo-options');
        menu.hidden = true;
        const values = [...new Set(records.map(row => cellValue(row, key)))].sort(textSort.compare);
        const checked = new Set(values);
        let available = values;
        const arrow = button(undefined, () => {
          if (menu.hidden) openChoices(false);
          else closeChoices();
        }, 'op-combo-arrow');
        arrow.ariaLabel = title + '选项';
        arrow.setAttribute('aria-expanded', 'false');
        const caret = element('span', undefined, 'op-chevron');
        caret.setAttribute('aria-hidden', 'true');
        arrow.append(caret);
        input.setAttribute('aria-label', title);
        input.setAttribute('autocomplete', 'off');
        function closeChoices() {
          menu.hidden = true;
          arrow.setAttribute('aria-expanded', 'false');
        }
        function openChoices(match) {
          for (const option of choices.children) {
            option.hidden = match && !option.textContent.toLowerCase().includes(input.value.toLowerCase());
          }
          menu.hidden = false;
          const left = control.getBoundingClientRect().left;
          menu.style.left = Math.max(4 - left, Math.min(0, window.innerWidth - left - menu.offsetWidth - 4)) + 'px';
          arrow.setAttribute('aria-expanded', 'true');
        }
        function refreshCount() {
          arrow.title = title + ' ' + available.filter(value => checked.has(value)).length + '/' + available.length;
        }
        function markAll(active) {
          for (const check of choices.querySelectorAll('input:enabled')) {
            check.checked = active;
            if (active) checked.add(check.value);
            else checked.delete(check.value);
          }
          refreshCount();
        }
        actions.append(...[['全选', true], ['清空', false]].map(([title, all]) =>
          button(title, () => { markAll(all); updateFilters(); })));
        for (const value of values) {
          const option = element('label');
          const check = element('input');
          check.type = 'checkbox';
          check.value = value;
          check.checked = true;
          check.addEventListener('change', () => {
            if (check.checked) checked.add(value);
            else checked.delete(value);
            refreshCount();
            updateFilters();
          });
          option.append(check, element('span', value));
          choices.append(option);
        }
        menu.append(actions, choices);
        refreshCount();
        input.addEventListener('input', () => {
          if (!menu.hidden) openChoices(true);
        });
        input.addEventListener('keydown', event => {
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            openChoices(true);
            (choices.querySelector('label:not([hidden]) input:enabled') ?? actions.firstElementChild)?.focus();
          }
        });
        control.addEventListener('keydown', event => {
          if (event.key === 'Escape') {
            closeChoices();
            input.focus();
          }
        });
        control.addEventListener('focusout', event => {
          if (!control.contains(event.relatedTarget)) closeChoices();
        });
        label.append(input);
        control.append(label, arrow, menu);
        filters.append(control);
        selectors.set(key, {
          open: () => { input.focus(); openChoices(false); },
          value: () => {
            const selected = available.filter(value => checked.has(value));
            return selected.length === available.length ? null : new Set(selected);
          },
          setAvailability: scope => {
            available = values.filter(value => scope.has(value));
            for (const check of choices.querySelectorAll('input')) {
              check.disabled = !scope.has(check.value);
              check.checked = !check.disabled && checked.has(check.value);
            }
            refreshCount();
          },
          clear: () => { markAll(true); closeChoices(); },
        });
      } else {
        label.append(input);
        filters.append(label);
      }
    }
    const submit = element('button', '搜索');
    submit.type = 'submit';
    filters.append(submit, button('重置', () => {
      filters.reset();
      for (const selector of selectors.values()) selector.clear();
      updateFilters();
    }));
    filters.addEventListener('submit', event => {
      event.preventDefault();
      updateFilters();
    });
    const scroller = element('div', undefined, 'op-scroll');
    const view = element('table');
    const cols = element('colgroup');
    const widths = columns.map(([key, title]) => key === 'type' || key === 'name' ? 246 :
      Math.max(98, Math.min(224, title.length * 8 + (selectors.has(key) ? 55 : 42))));
    if (options.card) widths.push(88);
    for (const width of widths) {
      const col = element('col');
      col.style.width = width + 'px';
      cols.append(col);
    }
    view.append(cols);
    scroller.append(view);
    const pager = element('div', undefined, 'op-page');
    container.replaceChildren(filters, scroller, pager);
    // Resolve the initial flexible widths once, so only the dragged column changes.
    const minimumWidth = widths.reduce((sum, width) => sum + width, 0);
    const extraWidth = Math.max(0, scroller.clientWidth - minimumWidth);
    const totalWidth = () => widths.reduce((sum, width) => sum + width, 0);
    for (const [index, col] of [...cols.children].entries()) {
      widths[index] += extraWidth * widths[index] / minimumWidth;
      col.style.width = widths[index] + 'px';
    }
    view.style.width = totalWidth() + 'px';

    function addResizeHandle(th, index, title) {
      const handle = element('span', undefined, 'op-resize');
      handle.title = '调整' + title + '列宽';
      handle.setAttribute('role', 'separator');
      handle.setAttribute('aria-orientation', 'vertical');
      handle.setAttribute('aria-label', handle.title);
      handle.tabIndex = 0;
      let drag;
      function resize(width) {
        widths[index] = Math.max(64, Math.round(width));
        cols.children[index].style.width = widths[index] + 'px';
        view.style.width = totalWidth() + 'px';
        handle.setAttribute('aria-valuenow', widths[index]);
      }
      handle.setAttribute('aria-valuemin', '64');
      handle.setAttribute('aria-valuenow', Math.round(widths[index]));
      handle.addEventListener('click', event => event.stopPropagation());
      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag = { id: event.pointerId, x: event.clientX, width: widths[index] };
        handle.setPointerCapture(event.pointerId);
      });
      handle.addEventListener('pointermove', event => {
        if (event.pointerId === drag?.id) resize(drag.width + event.clientX - drag.x);
      });
      handle.addEventListener('lostpointercapture', () => { drag = null; });
      handle.addEventListener('keydown', event => {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        resize(widths[index] + (event.key === 'ArrowRight' ? 8 : -8));
      });
      th.append(handle);
    }

    function updateFilters() {
      state.choices = Object.fromEntries([...selectors].map(([key, selector]) => [key, selector.value()])
        .filter(([, value]) => value));
      state.filters = Object.fromEntries([...new FormData(filters)]
        .filter(([, value]) => value.trim())
        .map(([field, value]) => [field, value.trim()]));
      rows = selectRows(records, columns, defaultField, state);
      state.page = 1;
      render();
    }

    function openDetails(row, cell) {
      table(cell, row.members.map(index => options.card.rows[index]), detailColumns(options.card, options.kind),
        { detail: true });
    }

    function closeDetails() {
      if (!expanded) return;
      expanded.row.remove();
      expanded.toggle.setAttribute('aria-expanded', 'false');
      expanded.toggle.ariaLabel = '详情';
      expanded = null;
    }

    function toggleDetails(event, parent, row) {
      const toggle = event.currentTarget;
      const alreadyOpen = expanded?.toggle === toggle;
      closeDetails();
      if (alreadyOpen) return;
      const detail = element('tr', undefined, 'op-sub');
      const cell = detail.insertCell();
      cell.className = 'op-inner';
      cell.colSpan = widths.length;
      parent.after(detail);
      toggle.setAttribute('aria-expanded', 'true');
      toggle.ariaLabel = '收起';
      expanded = { row: detail, toggle };
      openDetails(row, cell);
    }

    function changePage(next) {
      state.page = next;
      render();
    }

    function render() {
      closeDetails();
      const pageCount = Math.max(1, Math.ceil(rows.length / state.size));
      state.page = Math.min(state.page, pageCount);
      const header = view.createTHead();
      header.replaceChildren();
      const heading = header.insertRow();
      for (const [index, [key, title]] of columns.entries()) {
        const th = element('th', undefined, 'op-sortable');
        th.scope = 'col';
        th.dataset.field = key;
        if (state.field === key) th.setAttribute('aria-sort', state.direction === 1 ? 'ascending' : 'descending');
        const controls = element('div', undefined, 'op-th-controls');
        const sort = element('button', undefined, 'op-sort');
        sort.type = 'button';
        th.addEventListener('click', () => {
          state.direction = state.field === key && state.direction === 1 ? -1 : 1;
          state.field = state.field === key && th.getAttribute('aria-sort') === 'descending' ? null : key;
          if (state.field === null) state.direction = -1;
          rows = selectRows(records, columns, defaultField, state);
          changePage(1);
          header.querySelector(`[data-field=${CSS.escape(key)}] .op-sort`)?.focus({ preventScroll: true });
        });
        const sortIcon = element('span', undefined, 'op-sort-icon');
        sortIcon.setAttribute('aria-hidden', 'true');
        sort.append(element('span', title, 'op-heading-text'), sortIcon);
        controls.append(sort);
        const selector = selectors.get(key);
        if (selector) {
          const filter = button(undefined, event => {
            event.stopPropagation();
            selector.open();
          }, 'op-filter');
          filter.ariaLabel = filter.title = '筛选' + title;
          if (state.filters[key] || state.choices[key]) filter.classList.add('op-filter-active');
          const icon = element('span', undefined, 'op-filter-icon');
          icon.setAttribute('aria-hidden', 'true');
          filter.append(icon);
          controls.append(filter);
        }
        th.append(controls);
        addResizeHandle(th, index, title);
        heading.append(th);
      }
      if (options.card) {
        const actions = element('th', '操作');
        addResizeHandle(actions, widths.length - 1, '操作');
        heading.append(actions);
      }
      const tbody = view.tBodies[0] ?? view.createTBody();
      tbody.replaceChildren();
      if (!rows.length) {
        const cell = tbody.insertRow().insertCell();
        cell.colSpan = heading.cells.length;
        cell.textContent = '暂无数据';
      }
      const offset = (state.page - 1) * state.size;
      for (const [index, row] of rows.slice(offset, offset + state.size).entries()) {
        const tr = tbody.insertRow();
        tr.className = index % 2 ? 'op-data op-alt' : 'op-data';
        for (const [key] of columns) {
          const value = cellValue(row, key);
          const shown = value !== missing && formattedTimes.has(key) ? time.format(Number(value)) : value;
          const span = element('span', shown, 'op-cell-text');
          span.title = shown;
          tr.insertCell().append(span);
        }
        if (options.card) {
          const details = button('详情', event => toggleDetails(event, tr, row), 'op-expand');
          const icon = element('span', undefined, 'op-chevron');
          icon.setAttribute('aria-hidden', 'true');
          details.append(icon);
          details.setAttribute('aria-expanded', 'false');
          tr.insertCell().append(details);
        }
      }
      const previous = button('‹', () => changePage(state.page - 1));
      const next = button('›', () => changePage(state.page + 1));
      previous.title = previous.ariaLabel = '上一页';
      next.title = next.ariaLabel = '下一页';
      previous.disabled = state.page === 1;
      next.disabled = state.page >= pageCount;
      const page = element('input', undefined, 'op-current');
      page.type = 'number';
      page.min = '1';
      page.max = String(pageCount);
      page.value = state.page;
      page.ariaLabel = '页码';
      page.addEventListener('change', () => changePage(Math.min(pageCount, Math.max(1, Math.trunc(+page.value) || 1))));
      const size = element('select');
      size.ariaLabel = '每页条数';
      size.replaceChildren(...[10, 20, 50, 100].map(item => selectOption(item, item + ' 条/页')));
      size.value = state.size;
      size.addEventListener('change', () => { state.size = Number(size.value); changePage(1); });
      pager.replaceChildren(element('span', `共 ${number.format(rows.length)} 条 · 第 ${state.page}/${pageCount} 页`),
        previous, page, next, size);
    }
    render();
    return (next, coreScope) => {
      records = next;
      if (coreScope) selectors.get('core')?.setAvailability(coreScope);
      updateFilters();
    };
  }

  function drawChart(title, values, { groupTail, denominator, onBrush, onReset, coreChecks, onChecks, view } = {}) {
    const box = element('div', undefined, 'op-chart');
    const header = element('div', undefined, 'op-chart-header');
    const headingRow = element('div', undefined, 'op-chart-heading');
    const heading = element('h3', title);
    const back = button('上一级', stepBack);
    back.disabled = !view.history.length;
    const actions = element('div', undefined, 'op-chart-actions');
    const reset = button('重置', onReset);
    reset.ariaLabel = '重置饼图';
    actions.append(back, reset);
    headingRow.append(heading, actions);
    header.append(headingRow);
    const body = element('div', undefined, 'op-chart-body');
    const legend = element('ul', undefined, 'op-legend');
    const coreSummary = coreChecks ? element('div', undefined, 'op-core-summary') : null;
    coreSummary?.setAttribute('aria-live', 'polite');
    const canvas = element('canvas');
    const tooltip = element('div', undefined, 'op-tip');
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;
    const sorted = [...values].sort((a, b) => b[1] - a[1]);
    const total = denominator ?? sorted.reduce((sum, [, value]) => sum + value, 0);
    const toEntries = rows => rows.map(([name, value], i) =>
      ({ name, value, color: colors[i % colors.length], visible: true }));
    const tail = groupTail && sorted.length > maxPieCategories ? sorted.slice(maxPieCategories - 1) : [];
    const summaryEntries = toEntries(tail.length ? sorted.slice(0, maxPieCategories - 1) : sorted);
    if (tail.length) {
      const value = tail.reduce((sum, [, duration]) => sum + duration, 0);
      summaryEntries.push({ name: 'Others', value, color: colors[maxPieCategories - 1], visible: true,
        expandable: true, names: tail.map(([name]) => name) });
    }
    const context = canvas.getContext('2d');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const detailEntries = toEntries(tail);
    let entries = summaryEntries;
    let slices = [];
    let center = [0, 0];
    let radius = 0;
    let scale = 1;
    let active = null;
    let emphasized = null;
    let lift = 0;
    let animation = 0;
    let lastFrame = 0;
    let summaryScroll = 0;
    let gesture = null;
    let suppressClick = false;
    const fullCircle = 2 * Math.PI;
    const offset = -Math.PI / 2;

    function drawSlice(slice, extra = 0) {
      context.beginPath();
      context.moveTo(...center);
      context.arc(...center, radius + extra * scale, slice.start + offset, slice.end + offset);
      context.closePath();
      context.fillStyle = slice.entry.color;
      context.shadowBlur = extra * scale;
      context.shadowColor = 'rgba(0,0,0,.42)';
      context.fill();
      context.shadowBlur = 0;
    }

    function paint() {
      if (!context) return;
      context.clearRect(0, 0, canvas.width, canvas.height);
      if (!slices.length) {
        context.fillStyle = '#56666c';
        context.font = `${14 * scale}px system-ui`;
        context.textAlign = 'center';
        context.fillText('暂无耗时数据', ...center);
        return;
      }
      for (const slice of slices) {
        if (slice !== emphasized || !lift) drawSlice(slice);
      }
      if (emphasized && lift) drawSlice(emphasized, lift * 8);
      context.textAlign = 'center';
      context.textBaseline = 'middle';
      context.font = `${11 * scale}px system-ui`;
      for (const slice of slices) {
        const label = percentText(slice.percent);
        const distance = radius * 0.68;
        if ((slice.end - slice.start) * distance < context.measureText(label).width + 5 * scale) continue;
        const angle = (slice.start + slice.end) / 2 + offset;
        const rgb = slice.entry.color.slice(1).match(/../g).map(value => parseInt(value, 16));
        context.fillStyle = rgb[0] * .299 + rgb[1] * .587 + rgb[2] * .114 > 150 ? '#17212a' : '#fff';
        context.fillText(label, center[0] + Math.cos(angle) * distance, center[1] + Math.sin(angle) * distance);
      }
      if (gesture?.armed) {
        const start = gesture.start + offset;
        const end = start + gesture.sweep;
        context.beginPath();
        context.moveTo(...center);
        context.arc(...center, radius + 5 * scale, start, end, gesture.sweep < 0);
        context.closePath();
        context.fillStyle = 'rgba(255,255,255,.32)';
        context.fill();
        context.strokeStyle = '#253747';
        context.lineWidth = 1.5 * scale;
        context.stroke();
      }
    }

    function percentText(percent) {
      return percent > 0 && percent < .01 ? '<0.01%' : percent.toFixed(2) + '%';
    }

    function entryNames(entry) {
      return entry.names ?? [entry.name];
    }

    function layout() {
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
      const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      scale = ratio;
      center = [canvas.width / 2, canvas.height / 2];
      radius = Math.min(...center) * 0.88;
      const { selection } = view;
      const shown = entries.filter(entry => entry.visible && entry.value > 0 &&
        (!coreChecks || coreChecks.has(entry.name)) &&
        (!selection || entryNames(entry).some(name => selection.has(name))));
      const durations = shown.map(entry => entry.expandable && selection
        ? tail.reduce((sum, [name, value]) => sum + (selection.has(name) ? value : 0), 0)
        : entry.value);
      const sum = durations.reduce((total, value) => total + value, 0);
      if (coreSummary) {
        coreSummary.textContent = '已选核合计占比 ' + percentText(total ? sum / total * 100 : 0);
      }
      let start = 0;
      slices = shown.map((entry, index) => {
        const value = durations[index];
        const share = value / sum;
        const slice = { entry, value, start, end: start + share * fullCircle, percent: value / total * 100 };
        start = slice.end;
        return slice;
      });
      clearHover();
      paint();
    }

    function clearHover() {
      cancelAnimationFrame(animation);
      animation = 0;
      active = emphasized = null;
      lift = lastFrame = 0;
      tooltip.hidden = true;
      canvas.style.cursor = '';
    }

    function animate(now) {
      animation = 0;
      if (!canvas.isConnected) return;
      const target = active ? 1 : 0;
      const elapsed = lastFrame ? Math.min(now - lastFrame, 40) : 16;
      lastFrame = now;
      lift += Math.sign(target - lift) * Math.min(Math.abs(target - lift), elapsed / 145);
      paint();
      if (lift !== target) animation = requestAnimationFrame(animate);
      else {
        lastFrame = 0;
        if (!active) emphasized = null;
      }
    }

    function setActive(slice) {
      if (slice === active) return;
      active = slice;
      if (slice && slice !== emphasized) {
        emphasized = slice;
        lift = 0;
      }
      canvas.style.cursor = slice?.entry.expandable ? 'pointer' : '';
      if (reducedMotion.matches) {
        lift = slice ? 1 : 0;
        paint();
      } else if (!animation) {
        animation = requestAnimationFrame(animate);
      }
    }

    function pointerPosition(event) {
      const rect = canvas.getBoundingClientRect();
      const x = (event.clientX - rect.left) * scale - center[0];
      const y = (event.clientY - rect.top) * scale - center[1];
      const angle = (Math.atan2(y, x) - offset + fullCircle) % fullCircle;
      return { angle, distance: Math.hypot(x, y) };
    }

    function hitSlice(event) {
      const { angle, distance } = pointerPosition(event);
      const slice = slices.find(item => angle >= item.start && angle < item.end);
      const extra = active && slice === active ? lift * 8 * scale : 0;
      return slice && distance <= radius + extra ? slice : null;
    }

    function rememberView() {
      view.history.push({ selection: view.selection, drilled: view.drilled });
      back.disabled = false;
    }

    function stepBack() {
      const previous = view.history.pop();
      if (!previous) return;
      const changed = view.selection !== previous.selection;
      const restoreLevel = view.drilled !== previous.drilled;
      view.selection = previous.selection;
      view.drilled = previous.drilled;
      back.disabled = !view.history.length;
      if (restoreLevel) showLevel();
      else layout();
      if (changed) onBrush();
    }

    function openOthers() {
      rememberView();
      summaryScroll = legend.scrollTop;
      view.drilled = true;
      showLevel();
      back.focus({ preventScroll: true });
    }

    function showLevel() {
      const drilled = view.drilled && !!tail.length;
      entries = drilled ? detailEntries : summaryEntries;
      heading.textContent = drilled ? 'Others 明细' : title;
      canvas.setAttribute('aria-label', heading.textContent);
      const fragment = document.createDocumentFragment();
      for (const entry of entries) {
        const item = element('li');
        const percent = element('span', percentText(total ? entry.value / total * 100 : 0), 'op-legend-percent');
        if (coreChecks) {
          const label = element('label');
          const check = element('input');
          check.type = 'checkbox';
          check.checked = coreChecks.has(entry.name);
          check.ariaLabel = entry.name;
          check.addEventListener('change', () => {
            if (check.checked) coreChecks.add(entry.name);
            else coreChecks.delete(entry.name);
            layout();
            onChecks();
          });
          const swatch = element('i');
          swatch.style.backgroundColor = entry.color;
          label.append(check, swatch, element('span', entry.name), percent);
          item.append(label);
          fragment.append(item);
          continue;
        }
        const toggle = button(undefined, () => {
          if (entry.expandable) {
            openOthers();
            return;
          }
          entry.visible = !entry.visible;
          toggle.setAttribute('aria-pressed', String(entry.visible));
          layout();
        });
        toggle.title = `${entry.name} · ${number.format(entry.value)} μs`;
        if (entry.expandable) {
          toggle.classList.add('op-others');
          toggle.setAttribute('aria-label', '查看 Others 明细');
        } else {
          toggle.setAttribute('aria-pressed', String(entry.visible));
        }
        const swatch = element('i');
        swatch.style.backgroundColor = entry.color;
        toggle.append(swatch, element('span', entry.name), percent);
        item.append(toggle);
        fragment.append(item);
      }
      legend.replaceChildren(fragment);
      legend.scrollTop = drilled ? 0 : summaryScroll;
      layout();
    }

    canvas.addEventListener('pointermove', event => {
      if (gesture?.id === event.pointerId) {
        const { angle, distance } = pointerPosition(event);
        if (distance >= 4 * scale) {
          let delta = angle - gesture.previous;
          if (delta > Math.PI) delta -= fullCircle;
          if (delta < -Math.PI) delta += fullCircle;
          gesture.sweep = Math.max(-fullCircle, Math.min(fullCircle, gesture.sweep + delta));
          gesture.previous = angle;
        }
        if (gesture.armed) {
          paint();
          return;
        }
      }
      const slice = hitSlice(event);
      setActive(slice);
      tooltip.hidden = !slice;
      if (!slice) return;
      const { entry } = slice;
      tooltip.textContent = `${entry.name} · ${number.format(slice.value)} μs · ${percentText(slice.percent)}`;
      const rect = box.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      const gap = 12;
      const width = tooltip.offsetWidth;
      const height = tooltip.offsetHeight;
      tooltip.style.left = Math.max(4, Math.min(rect.width - width - 4,
        x + width + gap <= rect.width ? x + gap : x - width - gap)) + 'px';
      tooltip.style.top = Math.max(4, Math.min(rect.height - height - 4,
        y + height + gap <= rect.height ? y + gap : y - height - gap)) + 'px';
    });
    canvas.addEventListener('click', event => {
      if (suppressClick) { suppressClick = false; return; }
      if (hitSlice(event)?.entry.expandable) openOthers();
    });
    canvas.addEventListener('pointerdown', event => {
      suppressClick = false;
      if (!onBrush || event.button !== 0 || !hitSlice(event)) return;
      const { angle } = pointerPosition(event);
      gesture = { id: event.pointerId, start: angle, previous: angle, sweep: 0, armed: false };
      gesture.timer = setTimeout(() => {
        if (!gesture || !canvas.isConnected) return;
        gesture.armed = true;
        clearHover();
        paint();
      }, 240);
      canvas.setPointerCapture(event.pointerId);
    });

    function endGesture(event, apply) {
      if (!gesture || gesture.id !== event.pointerId) return;
      const { start, sweep, timer, armed } = gesture;
      clearTimeout(timer);
      gesture = null;
      if (armed && apply && Math.abs(sweep) >= Math.PI / 90) {
        const lower = Math.min(start, start + sweep);
        const upper = Math.max(start, start + sweep);
        const selected = slices.filter(slice => [-fullCircle, 0, fullCircle].some(shift =>
          upper > slice.start + shift && lower < slice.end + shift));
        if (selected.length) {
          suppressClick = true;
          const names = selected.flatMap(slice => entryNames(slice.entry));
          const { selection } = view;
          const next = new Set(selection ? names.filter(name => selection.has(name)) : names);
          if (!selection || next.size !== selection.size) {
            rememberView();
            view.selection = next;
            onBrush();
          }
          layout();
          return;
        }
      }
      paint();
    }
    canvas.addEventListener('pointerup', event => endGesture(event, true));
    canvas.addEventListener('pointercancel', event => endGesture(event, false));
    canvas.addEventListener('lostpointercapture', event => endGesture(event, false));
    canvas.addEventListener('pointerleave', () => {
      setActive(null);
      tooltip.hidden = true;
    });
    body.append(legend, canvas);
    box.append(header);
    if (coreChecks) {
      const toolbar = element('div', undefined, 'op-core-toolbar');
      const presets = element('div', undefined, 'op-core-presets');
      const coreTypes = element('div', undefined, 'op-core-types');
      const names = sorted.map(([name]) => name);
      const [all, clear, cube, vector] = [
        ['全选', names], ['清除', []], ['cube', ['AI_CORE', 'MIX_AIC']],
        ['vector', ['AI_VECTOR_CORE', 'MIX_AIV']],
      ].map(([label, keys]) => button(label, () => {
        coreChecks.clear();
        for (const key of keys) if (names.includes(key)) coreChecks.add(key);
        for (const check of legend.querySelectorAll('input')) check.checked = coreChecks.has(check.ariaLabel);
        view.selection = null;
        view.history.length = 0;
        back.disabled = true;
        layout();
        onBrush();
      }));
      coreTypes.append(cube, vector);
      presets.append(all, clear, coreTypes);
      toolbar.append(presets, coreSummary);
      header.append(toolbar);
    }
    box.append(body, tooltip);
    charts.append(box);
    observer = observer ?? new ResizeObserver(items => {
      for (const { target } of items) target.drawOperatorChart();
    });
    canvas.drawOperatorChart = layout;
    observer.observe(canvas);
    showLevel();
    return box;
  }

  function totals(records, category, timeField) {
    const mapped = new Map();
    for (const row of records) {
      const name = category(row) ?? '未知';
      mapped.set(name, (mapped.get(name) ?? 0) + Number(row[timeField]));
    }
    return mapped;
  }

  function csvValue(value) {
    const text = String(value ?? '');
    const safe = /^[\s]*[=+\-@]/.test(text) && !/^[+-]?\d+(\.\d+)?$/.test(text) ? "'" + text : text;
    return '"' + safe.replaceAll('"', '""') + '"';
  }

  function download(records, columns) {
    const lines = records.map(row => columns.map(([key]) => csvValue(accessor(row, key))).join(','));
    const header = columns.map(([, title]) => csvValue(title)).join(',');
    const blob = new Blob(['\ufeff', [header, ...lines].join('\r\n'), '\r\n'], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = element('a');
    link.href = url;
    const card = cards[Number(rank.value)].rank.replaceAll(/[^\p{L}\p{N}_.-]/gu, '_');
    link.download = `${mode.selectedOptions[0].textContent}_卡${card}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  function render() {
    const card = cards[Number(rank.value)];
    const kind = mode.value.split('-')[0];
    const grouped = mode.value.endsWith('-type');
    const details = card.rows.filter(row => row.kind === kind);
    const records = grouped ? card.statistics[kind] : details;
    const columns = grouped ? groupColumns.filter(([key]) => key !== 'core' || kind === 'compute')
      : detailColumns(card, kind);
    const label = mode.value === 'compute' ? '算子' : mode.value === 'compute-type' ? '算子类型' : mode.selectedOptions[0].textContent;
    const category = row => (grouped ? row.type : row.name) ?? '未知';
    const timeField = grouped ? 'total' : 'duration';
    const chartTotal = records.reduce((sum, row) => sum + Number(row[timeField]), 0);
    const coreName = row => row.core ?? '未知';
    const coreTotals = totals(details, coreName, 'duration');
    const checkedCores = new Set(coreTotals.keys());
    const refreshTable = table(tableArea, records, columns, grouped ? { card, kind } : { detail: true });
    const operatorView = { selection: null, drilled: false, history: [] };
    const coreView = { selection: null, drilled: false, history: [] };
    let leftChart = null;

    function isCoreVisible(core) {
      const coreBrush = coreView.selection;
      return checkedCores.has(core) && (!coreBrush || coreBrush.has(core));
    }

    function matchesCore(row) {
      return kind !== 'compute' || isCoreVisible(coreName(row));
    }

    function updateTable() {
      const operatorBrush = operatorView.selection;
      const coreScope = kind === 'compute'
        ? new Set([...checkedCores].filter(isCoreVisible).map(core => cellValue({ core }, 'core')))
        : undefined;
      refreshTable(records.filter(row => matchesCore(row) && (!operatorBrush || operatorBrush.has(category(row)))), coreScope);
    }

    function drawOperatorChart() {
      if (leftChart) {
        observer?.unobserve(leftChart.querySelector('canvas'));
        leftChart.remove();
      }
      leftChart = drawChart(`按${label}分组总耗时（μs）`, totals(records.filter(matchesCore), category, timeField), {
        groupTail: mode.value === 'compute',
        denominator: chartTotal,
        view: operatorView,
        onBrush: updateTable,
        onReset: resetCharts,
      });
      charts.prepend(leftChart);
    }

    function updateCoreScope() {
      drawOperatorChart();
      updateTable();
    }

    function resetCharts() {
      observer?.disconnect();
      charts.replaceChildren();
      leftChart = null;
      for (const view of [operatorView, coreView]) {
        view.selection = null;
        view.drilled = false;
        view.history.length = 0;
      }
      checkedCores.clear();
      for (const core of coreTotals.keys()) checkedCores.add(core);
      drawOperatorChart();
      if (kind === 'compute') {
        drawChart('按加速器核分组总耗时（μs）', coreTotals, {
          coreChecks: checkedCores,
          view: coreView,
          onChecks: updateCoreScope,
          onBrush: updateCoreScope,
          onReset: resetCharts,
        });
      }
      updateTable();
    }
    resetCharts();
    if (mode.value === 'compute') {
      const help = element('details', undefined, 'op-help');
      help.append(element('summary', '单位说明'),
        element('span', '开始时间为毫秒（ms），时长和等待时间为微秒（μs）。PMU 单位以字段名为准。'));
      tableArea.prepend(help);
    }
    exportButton.disabled = !records.length;
    exportButton.onclick = () => download(selectRows(records, columns, grouped ? 'total' : 'duration',
      { filters: {}, field: null, direction: -1 }), columns);
  }

  function switchHost() {
    const candidates = [...cards.entries()].filter(([, card]) => host.parentElement.hidden ||
      host.value === (card.host ?? ''));
    rank.replaceChildren();
    for (const [index, card] of candidates) {
      const duplicate = candidates.some(([i, other]) => i !== index && other.rank === card.rank);
      rank.add(selectOption(index, duplicate ? `${card.rank} · ${card.source}` : card.rank));
    }
    render();
  }

  async function load() {
    const payload = root.querySelector('.op-payload').textContent.trim();
    const bytes = Uint8Array.from(atob(payload), character => character.charCodeAt(0));
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    cards = await new Response(stream).json();
    if (cards.some(card => card.host)) {
      host.parentElement.hidden = false;
      for (const name of new Set(cards.map(card => card.host ?? ''))) {
        host.add(selectOption(name, name || '未知'));
      }
    }
    rank.addEventListener('change', render);
    host.addEventListener('change', switchHost);
    mode.addEventListener('change', render);
    tableArea.hidden = false;
    switchHost();
    message.hidden = true;
  }

  load().catch(error => { message.textContent = '算子数据加载失败：' + error.message; });
})();
