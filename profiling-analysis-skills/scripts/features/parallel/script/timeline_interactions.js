const axisRowHeight = 26;
const axisLabelBaseline = 17;
const flagTop = axisRowHeight;
const laneTop = flagTop + axisRowHeight;
const plotTop = laneTop + 6;
const flagClickIntervalMs = 200;
const { flag_colors: flagColors, flag_path: flagShape } =
    JSON.parse(root.querySelector('#parallel-mindstudio-markers').textContent);
const flagSteps = new Map();
let pendingFlag = null;
let flagMenuTarget = null;
let flagMenuReturn = null;
let flagEditorTarget = null;
let flagRightPress = null;
let flagDrag = null;
let flagSelection = null;
let shortcutRange = null;
let shortcutFlagRange = null;
const zoomForward = [];
let lastZoomTap = null;

// Adapted from TimelineMarker.tsx transformTimeToLeft (MindStudio, MulanPSL-2.0).
function transformTimeToLeft(domainStart, domainEnd, timestamp, width) {
    return domainEnd === domainStart ? 0 : width * (timestamp - domainStart) / (domainEnd - domainStart);
}

function flagState(step) {
    if (!flagSteps.has(step)) flagSteps.set(step, { next: 1, markers: [], names: new Map() });
    return flagSteps.get(step);
}

function flagPrefix(marker) {
    return String(marker.id).padStart(2, '0') + '-';
}

function flagName(marker) {
    return flagPrefix(marker) + marker.name;
}

function flagTime(value) {
    const unit = q('unit').value;
    return (value / scales[unit]).toFixed(3) + ' ' + symbols[unit];
}

function flagIntervals(state) {
    const bounds = [{ id: 'start', time: fullStart },
        ...state.markers.filter(marker => marker.checked && marker.time > fullStart && marker.time < fullEnd),
        { id: 'end', time: fullEnd }];
    return bounds.slice(1).map((right, index) => {
        const left = bounds[index];
        const key = left.id + ':' + right.id;
        return { key, start: left.time, end: right.time, name: state.names.get(key) || '' };
    });
}

function localFlagTime(x) {
    return viewStart + (x - scene.left) / scene.plot * (viewEnd - viewStart);
}

function redrawFlags() {
    if (!scene) return;
    delete q('operator-tooltip').dataset.subjectKey;
    hideHover();
    scene.flagsKey = null;
    drawFlagTrack();
}

function drawFlagTrack() {
    const step = q('step').value;
    const key = [step, viewStart, viewEnd, q('unit').value].join('|');
    if (key === scene.flagsKey) return;
    scene.flagsKey = key;
    const state = flagState(step);
    const x = t => scene.left + transformTimeToLeft(viewStart, viewEnd, t, scene.plot);
    const center = flagTop + axisLabelBaseline;
    const track = scene.flagTrack;
    track.replaceChildren();
    for (const interval of flagIntervals(state)) {
        const left = x(Math.max(viewStart, interval.start));
        const right = x(Math.min(viewEnd, interval.end));
        if (right <= left) continue;
        const duration = flagTime(interval.end - interval.start);
        const full = interval.name ? interval.name + ' · ' + duration : duration;
        const caption = textWidth(full, '11px system-ui') + 12 <= right - left ? full : duration;
        track.append(svg('rect', { x: left, y: flagTop, width: right - left, height: laneTop - flagTop,
            fill: 'transparent', stroke: '#e0e8ef', 'data-flag-interval': interval.key }));
        if (textWidth(caption, '11px system-ui') + 6 <= right - left) {
            track.append(svg('text', { x: (left + right) / 2, y: center, fill: '#425870',
                'font-size': 11, 'text-anchor': 'middle', 'pointer-events': 'none' }, caption));
        }
    }
    const selected = new Set(selectedFlags().map(marker => marker.id));
    if (flagSelection) {
        const start = x(Math.max(flagSelection.start, viewStart));
        const end = x(Math.min(flagSelection.end, viewEnd));
        if (end > start) track.append(svg('rect', {
            x: start, y: flagTop, width: end - start, height: axisRowHeight,
            fill: '#2783f323', stroke: '#2783f3', 'pointer-events': 'none', 'data-pp': 'flag-brush',
        }));
    }
    scene.flagGuides.replaceChildren();
    for (const marker of state.markers) {
        if (!marker.checked || marker.time < viewStart || marker.time > viewEnd) continue;
        const at = x(marker.time);
        scene.flagGuides.append(svg('line', { x1: at, x2: at, y1: flagTop + 3, y2: scene.height - 12,
            stroke: marker.color, 'stroke-width': 1, 'stroke-dasharray': '4 3', opacity: .65 }));
        const flag = svg('g', { transform: 'translate(' + (at - 3.83) + ' ' + flagTop + ')',
            'data-flag-id': marker.id, tabindex: '0', role: 'button',
            'aria-label': flagName(marker) + ' · ' + flagTime(marker.time) });
        flag.append(svg('rect', { x: -3, y: 0, width: 21, height: 20, fill: 'transparent' }),
            svg('path', { d: flagShape, fill: marker.color, 'pointer-events': 'none' }));
        if (selected.has(marker.id)) {
            flag.dataset.flagSelected = 'true';
            flag.firstElementChild.setAttribute('fill', '#c4e0ff');
        }
        track.append(flag);
    }
    positionFlagDelete();
}

function refreshFlagCount(state) {
    q('flag-count').textContent = state.markers.filter(marker => marker.checked).length + '/' + state.markers.length;
}

function refreshFlagList() {
    const state = flagState(q('step').value);
    refreshFlagCount(state);
    const list = q('flag-list');
    list.replaceChildren();
    for (const marker of state.markers) {
        const row = detailElement('label');
        const input = detailElement('input');
        input.type = 'checkbox';
        input.checked = marker.checked;
        input.setAttribute('aria-label', flagName(marker));
        input.addEventListener('change', () => {
            marker.checked = input.checked;
            refreshFlagCount(state);
            redrawFlags();
        });
        const swatch = detailElement('i', null, 'flag-swatch');
        swatch.style.background = marker.color;
        row.title = flagName(marker);
        row.append(input, swatch, detailElement('span', flagTime(marker.time)));
        list.append(row);
    }
}

function flagAt(target) {
    const id = target.closest('[data-flag-id]')?.dataset.flagId;
    return flagState(q('step').value).markers.find(marker => String(marker.id) === id);
}

function intervalAt(time) {
    return flagIntervals(flagState(q('step').value)).find(item => time >= item.start && time < item.end);
}

function clearPendingFlag() {
    if (pendingFlag) clearTimeout(pendingFlag.timer);
    const click = pendingFlag;
    pendingFlag = null;
    return click;
}

// TimelineMarker.tsx addNewFlag: append point, sort by time, then redraw.
function insertFlag(click) {
    insertFlags(click.step, [click.time]);
}

function insertFlags(step, times) {
    const state = flagState(step);
    for (const rawTime of times) {
        if (!Number.isFinite(rawTime) || rawTime < fullStart || rawTime > fullEnd) continue;
        const time = Math.max(fullStart, Math.min(fullEnd, Math.round(rawTime * 1000) / 1000));
        if (state.markers.some(marker => marker.time === time)) continue;
        const id = state.next++;
        state.markers.push({ id, time, name: 'flag', color: flagColors[(id - 1) % flagColors.length], checked: true });
    }
    state.markers.sort((a, b) => a.time - b.time);
    refreshFlagList();
    redrawFlags();
}

function deleteFlags(markers) {
    const state = flagState(q('step').value);
    const ids = new Set(markers.map(marker => marker.id));
    state.markers = state.markers.filter(marker => !ids.has(marker.id));
    for (const key of state.names.keys()) {
        if (key.split(':').some(part => ids.has(Number(part)))) state.names.delete(key);
    }
    flagSelection = null;
    refreshFlagList();
    redrawFlags();
    positionFlagDelete();
}

// MindStudio actionFlagMarkCreation uses the time below the horizontal cursor for point markers.
function insertFlagAtPointer() {
    if (pointerX === null || pointerY === null) return;
    if (pointerY < laneTop) {
        const box = q('chart').getBoundingClientRect();
        const scale = box.width / scene.width;
        const target = document.elementFromPoint(box.left + pointerX * scale, box.top + pointerY * scale);
        const marker = target && flagAt(target);
        if (marker) deleteFlags([marker]);
        return;
    }
    if (!pointerInside) return;
    const time = localFlagTime(pointerX);
    if (time > fullStart && time < fullEnd) insertFlag({ step: q('step').value, time });
}

function flagClick(event, x) {
    const time = localFlagTime(x);
    const interval = intervalAt(time);
    if (!interval || time <= fullStart || time >= fullEnd) return;
    const step = q('step').value;
    if (pendingFlag) {
        const previous = clearPendingFlag();
        if (previous.step === step && previous.interval === interval.key &&
            event.timeStamp - previous.clickedAt <= flagClickIntervalMs &&
            Math.hypot(event.clientX - previous.x, event.clientY - previous.y) <= 6) {
            openTimeWindow(interval.start, interval.end, zoomFocus);
            return;
        }
        insertFlag(previous);
    }
    const current = intervalAt(time);
    if (!current) return;
    pendingFlag = { step, time, interval: current.key, x: event.clientX, y: event.clientY, clickedAt: event.timeStamp,
        timer: setTimeout(() => insertFlag(clearPendingFlag()), flagClickIntervalMs) };
}

function selectedFlags() {
    if (!flagSelection) return [];
    return flagState(q('step').value).markers.filter(marker =>
        marker.checked && marker.time >= flagSelection.start && marker.time <= flagSelection.end
    );
}

function beginFlagDrag(event, x) {
    clearFlagSelection();
    flagDrag = {
        pointer: event.pointerId, x: event.clientX, y: event.clientY, start: localFlagTime(x),
        marker: flagAt(event.target), moved: false,
    };
    q('chart').setPointerCapture(event.pointerId);
}

function moveFlagDrag(event, x) {
    if (flagDrag?.pointer !== event.pointerId) return;
    if (Math.hypot(event.clientX - flagDrag.x, event.clientY - flagDrag.y) > 6) {
        if (!flagDrag.moved) clearPendingFlag();
        flagDrag.moved = true;
    }
    if (!flagDrag.moved) return;
    const end = localFlagTime(Math.max(scene.left, Math.min(scene.left + scene.plot, x)));
    flagSelection = { start: Math.min(flagDrag.start, end), end: Math.max(flagDrag.start, end),
        rightward: end >= flagDrag.start };
    redrawFlags();
}

function finishFlagDrag(event, point) {
    if (flagDrag?.pointer !== event.pointerId) return false;
    moveFlagDrag(event, point.x);
    const current = flagDrag;
    flagDrag = null;
    const chart = q('chart');
    if (chart.hasPointerCapture(event.pointerId)) chart.releasePointerCapture(event.pointerId);
    if (current.moved) {
        shortcutFlagRange = flagSelection;
        positionFlagDelete();
    }
    else if (!current.marker && point.y >= flagTop && point.y < laneTop &&
             point.x >= scene.left && point.x <= scene.left + scene.plot) flagClick(event, point.x);
    return true;
}

function clearFlagSelection() {
    if (!flagSelection) return false;
    flagSelection = null;
    redrawFlags();
    positionFlagDelete();
    return true;
}

function appendFlagTimes(target, marker, interval) {
    if (marker) detailTable(target, '', ['时间'], [[flagTime(marker.time)]]);
    else detailTable(target, '', ['起始', '结束', '持续时间'],
        [[flagTime(interval.start), flagTime(interval.end), flagTime(interval.end - interval.start)]]);
}

function flagHover(event, x) {
    const marker = flagAt(event.target);
    const interval = marker ? null : intervalAt(localFlagTime(x));
    if (!marker && !interval) return hideHover();
    const target = q('operator-tooltip');
    const subjectKey = [q('step').value, q('unit').value, marker ? 'flag|' + marker.id : 'interval|' + interval.key].join('|');
    if (target.dataset.subjectKey !== subjectKey) {
        target.dataset.subjectKey = subjectKey;
        delete target.dataset.eventId;
        target.style.width = 'max-content';
        target.replaceChildren(detailElement('div', marker ? flagName(marker) : (interval.name || '未命名区间'), 'detail-title'));
        appendFlagTimes(target, marker, interval);
    }
    placeTimelineTooltip(target, event, chartYToClient(laneTop));
}

function positionFlagPicker() {
    const picker = q('flag-picker');
    if (!picker.open) return;
    const options = picker.querySelector('.stage-options');
    const anchor = picker.querySelector('summary').getBoundingClientRect();
    options.style.top = anchor.bottom + 4 + 'px';
    options.style.maxHeight = Math.max(0, innerHeight - anchor.bottom - 12) + 'px';
    options.style.left = Math.max(8, Math.min(innerWidth - options.offsetWidth - 8, anchor.left)) + 'px';
}

function insertSelectionFlags() {
    if (!zoomableSelection()) return false;
    insertFlags(q('step').value, [rangeSelection.start, rangeSelection.end]);
    return true;
}

function deleteSelectedFlags() {
    const markers = selectedFlags();
    if (!markers.length) return false;
    deleteFlags(markers);
    return true;
}

function zoomToFlagSelection() {
    if (!flagSelection || flagSelection.end - flagSelection.start < .001) return false;
    openTimeWindow(flagSelection.start, flagSelection.end, zoomFocus);
    return true;
}

function closeFlagMenu(restoreFocus = false) {
    q('flag-menu').hidden = true;
    flagMenuTarget = null;
    if (restoreFocus && flagMenuReturn?.isConnected) flagMenuReturn.focus({ preventScroll: true });
    flagMenuReturn = null;
}

function flagContext(event, x) {
    event.preventDefault();
    if (!flagRightPress || Math.hypot(event.clientX - flagRightPress.x, event.clientY - flagRightPress.y) > 6) return;
    const marker = flagAt(event.target);
    const interval = marker ? null : intervalAt(localFlagTime(x));
    if (!marker && !interval) return;
    closeFlagMenu();
    hideHover();
    flagMenuTarget = { step: q('step').value, marker, interval };
    flagMenuReturn = event.target.closest('[data-flag-id]') || q('scroll');
    const menu = q('flag-menu');
    q('flag-delete').hidden = !marker;
    menu.hidden = false;
    menu.style.left = Math.max(8, Math.min(innerWidth - menu.offsetWidth - 8, event.clientX)) + 'px';
    menu.style.top = Math.max(8, Math.min(innerHeight - menu.offsetHeight - 8, event.clientY)) + 'px';
    q('flag-rename').focus({ preventScroll: true });
}

// Adapted from MindStudio TimelineMarker.tsx SingleFlagEditElement.
function openFlagEditor() {
    if (!flagMenuTarget) return;
    const anchor = q('flag-menu').getBoundingClientRect();
    const { marker, interval } = flagMenuTarget;
    flagEditorTarget = { ...flagMenuTarget, returnFocus: flagMenuReturn };
    closeFlagMenu();
    hideHover();
    stopKeys();
    const editor = q('flag-editor');
    const times = q('flag-editor-times');
    const name = q('flag-editor-name');
    const prefix = q('flag-editor-prefix');
    q('flag-editor-title').textContent = marker ? '旗子详情' : '区间详情';
    name.value = marker ? marker.name : interval.name;
    prefix.hidden = !marker;
    prefix.textContent = marker ? flagPrefix(marker) : '';
    times.replaceChildren();
    appendFlagTimes(times, marker, interval);
    editor.style.left = '8px';
    editor.style.top = '8px';
    editor.showModal();
    editor.style.left = Math.max(8, Math.min(innerWidth - editor.offsetWidth - 8, anchor.left)) + 'px';
    editor.style.top = Math.max(8, Math.min(innerHeight - editor.offsetHeight - 8, anchor.top)) + 'px';
    name.select();
}

function initializeFlagTrack() {
    q('chart-content').style.setProperty('--axis-row-height', axisRowHeight + 'px');
    q('flag-picker').addEventListener('toggle', positionFlagPicker);
    window.addEventListener('resize', positionFlagPicker);
    for (const [action, checked] of [['flag-all', true], ['flag-none', false]]) {
        q(action).addEventListener('click', () => {
            for (const marker of flagState(q('step').value).markers) marker.checked = checked;
            refreshFlagList();
            redrawFlags();
        });
    }
    const editor = q('flag-editor');
    q('flag-rename').addEventListener('click', openFlagEditor);
    function saveName() {
        if (!flagEditorTarget) return;
        const { step, marker, interval } = flagEditorTarget;
        const value = q('flag-editor-name').value.trim();
        if (marker) marker.name = value || 'flag';
        else flagState(step).names.set(interval.key, value);
        editor.close();
        refreshFlagList();
        redrawFlags();
    }
    q('flag-editor-save').addEventListener('click', saveName);
    q('flag-editor-name').addEventListener('keydown', event => {
        if (event.key !== 'Enter' || event.isComposing) return;
        event.preventDefault();
        saveName();
    });
    q('flag-editor-cancel').addEventListener('click', () => editor.close());
    editor.addEventListener('close', () => {
        if (editor.open) return;
        const previous = flagEditorTarget?.returnFocus;
        flagEditorTarget = null;
        (previous?.isConnected ? previous : q('scroll')).focus({ preventScroll: true });
    });
    q('flag-delete').addEventListener('click', () => {
        if (!flagMenuTarget?.marker) return;
        const marker = flagMenuTarget.marker;
        closeFlagMenu();
        q('scroll').focus({ preventScroll: true });
        deleteFlags([marker]);
    });
    document.addEventListener('pointerdown', event => {
        if (!q('flag-menu').contains(event.target)) closeFlagMenu();
    });
    q('zoom-selection').addEventListener('click', () => {
        q('scroll').focus({ preventScroll: true });
        zoomToRange();
    });
    q('flag-delete-selection').addEventListener('click', () => {
        q('scroll').focus({ preventScroll: true });
        deleteSelectedFlags();
    });
    q('flag-zoom-selection').addEventListener('click', () => {
        q('scroll').focus({ preventScroll: true });
        zoomToFlagSelection();
    });
    q('flag-selection-ends').addEventListener('click', insertSelectionFlags);
    for (const [side, field] of [['left', '_start'], ['right', '_end']]) {
        q('event-flag-' + side).addEventListener('click', () => {
            const event = eventMap.get(String(selectedId));
            if (event) insertFlags(q('step').value, [event[field]]);
        });
    }
    document.addEventListener('scroll', () => {
        positionFlagPicker();
        positionZoomButton();
        positionFlagDelete();
    }, true);
}

function captureZoomState(previous) {
    return {
        start: previous?.viewStart ?? viewStart,
        end: previous?.viewEnd ?? viewEnd,
        focus: zoomFocus,
        range: previous ? previous.previousRange : rangeSelection,
        ids: previous ? previous.previousIds : boxEventIds,
        flagRange: flagSelection,
        scrollTop: q('scroll').scrollTop,
    };
}

function applyZoomState(state) {
    stopKeys();
    clearPendingFlag();
    zoomAnchor = null;
    if (frame) cancelAnimationFrame(frame);
    frame = lastFrame = 0;
    zoomFocus = state.focus;
    rangeSelection = state.range;
    shortcutRange = shortcutFlagRange = null;
    boxEventIds = state.ids;
    flagSelection = state.flagRange;
    selectedId = selectedRegionId = selectedFlowId = null;
    viewStart = targetStart = state.start;
    viewEnd = targetEnd = state.end;
    page = 1;
    q('chart').dataset.animating = 'false';
    makeScene();
    q('scroll').scrollTop = state.scrollTop;
    updateResults();
    selectedDetails();
}

// Adapted from MindStudio actionZoom.ts. Reversed snapshots make the same navigation bidirectional.
function handleZoomTap(key) {
    const now = performance.now();
    const jump = lastZoomTap?.key === key && now - lastZoomTap.time <= 300;
    lastZoomTap = jump ? null : { key, time: now };
    const backward = key === 'q';
    const source = backward ? zoomHistory : zoomForward;
    const destination = backward ? zoomForward : zoomHistory;
    const count = jump ? source.length : Math.min(source.length, 1);
    if (!count && !backward) return;
    let state = captureZoomState();
    for (let index = 0; index < count; index++) {
        destination.push(state);
        state = source.pop();
    }
    if (backward && (jump || !count)) {
        state = { start: fullStart, end: fullEnd, focus: null, range: null, ids: null, flagRange: null, scrollTop: 0 };
    }
    applyZoomState(state);
}

function zoomableSelection() {
    return rangeSelection && rangeSelection.end - rangeSelection.start >= .001 && rangeSelection.lanes.length;
}

function positionZoomButton() {
    const actions = q('selection-controls');
    if (!zoomableSelection() || drag || !scene || !scene.selectionBox) {
        actions.hidden = true;
        return;
    }
    actions.hidden = false;
    const chartBox = q('chart').getBoundingClientRect();
    const clip = q('scroll').getBoundingClientRect();
    const scale = chartBox.width / scene.width;
    const { left, top, right, bottom } = scene.selectionBox;
    const l = chartBox.left + left * scale, t = chartBox.top + top * scale;
    const r = chartBox.left + right * scale, b = chartBox.top + bottom * scale;
    const layout = q('chart-content');
    const area = layout.getBoundingClientRect();
    const minX = Math.max(clip.left, 0), maxX = Math.min(clip.right, innerWidth);
    const minY = Math.max(clip.top, chartBox.top + laneTop * scale, 0), maxY = Math.min(clip.bottom, innerHeight);
    const buttons = [...actions.children];
    const width = Math.max(...buttons.map(button => button.offsetWidth));
    const height = buttons.reduce((total, button) => total + button.offsetHeight, 0) + 4;
    const gap = 5;
    const rightward = rangeSelection.rightward;
    const downward = rangeSelection.downward;
    const y = downward ? b : t;
    const fitY = Math.max(minY, Math.min(maxY - height, downward ? y - height : y));
    const horizontal = [[r + gap, fitY], [l - width - gap, fitY]];
    const candidates = rightward ? horizontal : horizontal.slice().reverse();
    const fit = candidates.find(([a, c]) => a >= minX && a + width <= maxX && c >= minY && c + height <= maxY);
    const parent = fit ? layout : q('box-status');
    if (actions.parentElement !== parent) parent.append(actions);
    if (fit) {
        actions.style.left = fit[0] - area.left + 'px';
        actions.style.top = fit[1] - area.top + 'px';
    }
}

function positionFlagDelete() {
    const actions = q('flag-selection-controls');
    if (!flagSelection || flagDrag || !scene || flagSelection.end - flagSelection.start < .001) {
        actions.hidden = true;
        return;
    }
    const chart = q('chart').getBoundingClientRect();
    const scale = chart.width / scene.width;
    const x = time => (scene.left + transformTimeToLeft(viewStart, viewEnd, time, scene.plot)) * scale;
    const left = x(Math.max(flagSelection.start, viewStart));
    const right = x(Math.min(flagSelection.end, viewEnd));
    if (right <= left) {
        actions.hidden = true;
        return;
    }
    const count = selectedFlags().length;
    const button = q('flag-delete-selection');
    button.hidden = !count;
    actions.hidden = false;
    const width = actions.offsetWidth;
    const candidates = [right + 5, left - width - 5];
    if (!flagSelection.rightward) candidates.reverse();
    const side = candidates.find(value => value >= 94 && value + width <= chart.width);
    actions.style.left = (side ?? Math.min(chart.width - width - 4, Math.max(4, right - width))) + 'px';
    actions.style.top = (side == null ? laneTop * scale + 5 : flagTop * scale + (axisRowHeight * scale - actions.offsetHeight) / 2) + 'px';
}

function positionEventFlagButtons() {
    const leftButton = q('event-flag-left');
    const rightButton = q('event-flag-right');
    const event = scene && eventMap.get(String(selectedId));
    const lane = event && eventLane(scene.rowMap.get(Number(event.rank)), event);
    const visible = lane && event._end > viewStart && event._start < viewEnd && !drag;
    leftButton.hidden = rightButton.hidden = !visible;
    if (!visible) return;
    const chart = q('chart').getBoundingClientRect();
    const scale = chart.width / scene.width;
    const x = time => (scene.left + transformTimeToLeft(viewStart, viewEnd, time, scene.plot)) * scale;
    const left = x(Math.max(event._start, viewStart));
    const right = x(Math.min(event._end, viewEnd));
    const gap = 4, leftWidth = leftButton.offsetWidth, rightWidth = rightButton.offsetWidth;
    const limit = chart.width - rightWidth;
    const combined = leftWidth + gap + rightWidth;
    const anchor = right - left >= combined ? left : (left + right - combined) / 2;
    const leftX = Math.max(0, Math.min(limit - gap - leftWidth, anchor));
    const rightX = Math.min(limit, Math.max(leftX + leftWidth + gap, right - rightWidth));
    const top = (lane.eventY + lane.eventHeight) * scale + gap;
    leftButton.style.left = leftX + 'px';
    rightButton.style.left = rightX + 'px';
    leftButton.style.top = rightButton.style.top = top + 'px';
    leftButton.disabled = event._start < fullStart;
    rightButton.disabled = event._end > fullEnd;
}
