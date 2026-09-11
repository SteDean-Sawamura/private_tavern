/* 酒馆 - 游戏界面辅助函数 */

// Stream support for game actions (SSE)
async function submitActionStream(action) {
    if (!currentSaveId || isStreaming) return;
    isStreaming = true;
    _clearNotifStack();  // 新一轮开始时清除上一轮的通知
    _showGameOverlay(true);

    const prevStateSnap = currentState ? Object.assign({}, currentState) : null;
    const choicesArea = document.getElementById('choices-area');
    const prevChoicesHtml = choicesArea.innerHTML;
    document.querySelectorAll('.choice-btn').forEach(b => b.disabled = true);
    document.getElementById('freeform-input').disabled = true;
    choicesArea.innerHTML = '';

    // Create a new narrative block
    const content = document.getElementById('narrative-content');
    const block = document.createElement('div');
    block.className = 'turn-block latest';

    // Show player action
    if (action.text) {
        const actionDiv = document.createElement('div');
        actionDiv.className = 'turn-action';
        actionDiv.textContent = action.text;
        block.appendChild(actionDiv);
    }

    // Collapsible thinking area (hidden until content arrives)
    const thinkWrap = document.createElement('div');
    thinkWrap.className = 'think-block';
    thinkWrap.style.display = 'none';
    thinkWrap.innerHTML = '<div class="think-header" onclick="this.parentElement.classList.toggle(\'open\')">&#128161; AI 思考过程 <span class="toggle-arrow">&#9662;</span></div><div class="think-body"></div>';
    block.appendChild(thinkWrap);

    const textDiv = document.createElement('div');
    textDiv.className = 'narrative-text';
    block.appendChild(textDiv);

    // Collapsible agent tool-call area (hidden until agentic events arrive)
    const agentToolsWrap = document.createElement('div');
    agentToolsWrap.className = 'agent-tools-block';
    agentToolsWrap.style.display = 'none';
    agentToolsWrap.innerHTML = '<div class="agent-tools-header" onclick="this.parentElement.classList.toggle(\'open\')">\u{1F527} Agent 工具调用 <span class="toggle-arrow">▾</span></div><div class="agent-tools-body"></div>';
    block.insertBefore(agentToolsWrap, textDiv);

    // Agent controls: stop button + inject input (shown during streaming)
    const agentControls = document.createElement('div');
    agentControls.className = 'agent-controls';
    agentControls.style.display = 'none';
    agentControls.innerHTML =
        '<button class="stop-btn" title="停止生成">■ 停止</button>' +
        '<input class="inject-input" placeholder="插话…" />' +
        '<button class="inject-btn">发送</button>';
    block.appendChild(agentControls);

    // Wire up stop button
    const stopBtn = agentControls.querySelector('.stop-btn');
    stopBtn.addEventListener('click', async () => {
        try {
            const token = localStorage.getItem('tavern_api_token');
            const hdrs = { 'Content-Type': 'application/json' };
            if (token) hdrs['Authorization'] = 'Bearer ' + token;
            await fetch('/api/game/' + currentSaveId + '/abort', { method: 'POST', headers: hdrs });
            stopBtn.disabled = true;
            stopBtn.innerHTML = '正在停止...';
        } catch (_) {}
    });

    // Wire up inject button
    const injectInput = agentControls.querySelector('.inject-input');
    const injectBtn = agentControls.querySelector('.inject-btn');
    const doInject = async () => {
        const msg = injectInput.value.trim();
        if (!msg) return;
        injectInput.value = '';
        try {
            const token = localStorage.getItem('tavern_api_token');
            const hdrs = { 'Content-Type': 'application/json' };
            if (token) hdrs['Authorization'] = 'Bearer ' + token;
            await fetch('/api/game/' + currentSaveId + '/inject', {
                method: 'POST', headers: hdrs,
                body: JSON.stringify({ message: msg }),
            });
        } catch (_) {}
    };
    injectBtn.addEventListener('click', doInject);
    injectInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doInject(); });

    content.appendChild(block);

    // Remove "latest" from previous blocks
    content.querySelectorAll('.turn-block.latest').forEach(b => {
        if (b !== block) b.classList.remove('latest');
    });

    let timeoutId = null;
    let _scrollRAF = null;
    const _scrollToBottom = () => {
        if (_scrollRAF) return;
        _scrollRAF = requestAnimationFrame(() => {
            const area = document.getElementById('narrative-area');
            area.scrollTop = area.scrollHeight;
            _scrollRAF = null;
        });
    };
    try {
        const sseHeaders = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('tavern_api_token');
        if (token) sseHeaders['Authorization'] = `Bearer ${token}`;

        const controller = new AbortController();
        timeoutId = setTimeout(() => controller.abort(), 300000); // 5 min timeout

        const response = await fetch(`/api/game/${currentSaveId}/action/stream`, {
            method: 'POST',
            headers: sseHeaders,
            body: JSON.stringify(action),
            signal: controller.signal,
        });

        if (!response.ok) {
            let errMsg = `HTTP ${response.status}`;
            try { const errBody = await response.json(); errMsg = errBody.detail || errMsg; } catch (_) {}
            throw new Error(errMsg);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    const data = line.slice(6);
                    if (data === '[DONE]') continue;

                    try {
                        const parsed = JSON.parse(data);
                        if (parsed.type === 'thinking') {
                            thinkWrap.style.display = '';
                            thinkWrap.querySelector('.think-body').insertAdjacentHTML('beforeend', escapeHtml(parsed.content).replace(/\n/g, '<br>'));
                        } else if (parsed.type === 'tool_call') {
                            // Agentic: show tool-call card
                            agentToolsWrap.style.display = '';
                            agentControls.style.display = '';
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const card = document.createElement('div');
                            card.className = 'agent-tool-card';
                            card.innerHTML =
                                '<span class="tool-label">[' + escapeHtml(parsed.label || '') + ' R' + (parsed.round || '') + ']</span> ' +
                                '<span class="tool-name">' + escapeHtml(parsed.tool_name || '') + '</span>' +
                                '(' + escapeHtml(JSON.stringify(parsed.tool_args || {}).slice(0, 120)) + ')' +
                                '<div class="tool-result">' + escapeHtml((parsed.tool_result || '').slice(0, 300)) + '</div>';
                            body.appendChild(card);
                            // Update header counter
                            const cnt = body.querySelectorAll('.agent-tool-card').length;
                            agentToolsWrap.querySelector('.agent-tools-header').innerHTML =
                                '\u{1F527} Agent 工具调用 (' + cnt + ') <span class="toggle-arrow">▾</span>';
                            _scrollToBottom();
                        } else if (parsed.type === 'agent_done') {
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = (parsed.label || 'Agent') + ' 完成，共' + (parsed.round || '?') + '轮';
                            body.appendChild(msg);
                        } else if (parsed.type === 'aborted') {
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = '已中断 (' + (parsed.label || '') + ')';
                            body.appendChild(msg);
                            agentControls.style.display = 'none';
                        } else if (parsed.type === 'user_inject') {
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = '用户插话: ' + (parsed.message || '').slice(0, 100);
                            body.appendChild(msg);
                        } else if (parsed.type === 'agent_max_rounds') {
                            const body = agentToolsWrap.querySelector('.agent-tools-body');
                            const msg = document.createElement('div');
                            msg.className = 'agent-status-msg';
                            msg.textContent = (parsed.label || 'Agent') + ' 达到最大轮数 (' + (parsed.max_rounds || '') + ')';
                            body.appendChild(msg);
                        } else if (parsed.type === 'text') {
                            const span = document.createElement('span');
                            span.className = 'stream-fade-in';
                            span.innerHTML = escapeHtml(parsed.content).replace(/\n/g, '<br>');
                            textDiv.appendChild(span);
                            _scrollToBottom();
                        } else if (parsed.type === 'narrative_revised') {
                            textDiv.innerHTML = escapeHtml(parsed.content).replace(/\n/g, '<br>');
                            _scrollToBottom();
                        } else if (parsed.type === 'final') {
                            agentControls.style.display = 'none';
                            const _s = (label, fn) => { try { fn(); } catch(e) { console.error(`[stream:${label}]`, e); } };
                            _s('renderChoices', () => renderChoices(parsed.choices || []));
                            _s('renderDice', () => renderDice(parsed.dice_rolls));
                            _s('showStateChanges', () => showStateChanges(parsed.state_changes));
                            _s('showTriggeredEvents', () => showTriggeredEvents(parsed.triggered_events));
                            _s('showExpiredStates', () => showExpiredStates(parsed.expired_states));
                            _s('showCheckResult', () => showCheckResult(parsed.check_result));
                            _s('showTriggeredConsequences', () => showTriggeredConsequences(parsed.triggered_consequences));
                            _s('showAchievedMilestones', () => showAchievedMilestones(parsed.achieved_milestones));
                            _s('showMilestoneProgress', () => showMilestoneProgress(parsed.milestone_progress));
                            _s('showThresholdEvents', () => showThresholdEvents(parsed.threshold_events));
                            _s('showLoreHints', () => showDataBankPanel(parsed.activated_lore, parsed.databank_hits));
                            _s('showWarnings', () => showWarnings(parsed.warnings));
                            _s('showNpcInterjections', () => showNpcInterjections(parsed.npc_interjections));
                            _s('showNpcAttitudeNotifications', () => showNpcAttitudeNotifications(parsed.npc_attitude_notifications));
                            _s('showEmotionLabel', () => showEmotionLabel(parsed.emotion));
                            _s('showTriggerNotifications', () => showTriggerNotifications(parsed.trigger_notifications));
                            _s('showStoryTreeUpdates', () => showStoryTreeUpdates(parsed.story_tree_updates));
                            _s('renderSceneImage', () => { if (parsed.scene_image) renderSceneImage(parsed.scene_image); });
                            currentState = parsed.state;
                            if (parsed.node_id) currentNodeId = parsed.node_id;
                            // Enhance narrative text after streaming completes
                            _s('enhanceNarrative', () => {
                                if (typeof enhanceNarrative === 'function') {
                                    // Strip stream-fade-in spans to get clean text for regex processing
                                    let cleanHtml = textDiv.innerHTML
                                        .replace(/<span class="stream-fade-in">/g, '')
                                        .replace(/<\/span>/g, '');
                                    textDiv.innerHTML = enhanceNarrative(cleanHtml);
                                }
                            });
                            _s('showTurnRecap', () => showTurnRecap(parsed, prevStateSnap));
                            _s('updateStatusPanel', () => updateStatusPanel(currentState));
                            _s('showChoiceRipples', () => showChoiceRipples(currentState));
                            _s('showMemoryEchoes', () => showMemoryEchoes(currentState));
                            _s('updateHeader', () => updateHeader());
                            _s('updateTurnIndicator', () => updateTurnIndicator());
                            if (parsed.game_over) _s('showGameOver', () => showGameOver(parsed.game_over, parsed.game_statistics));
                        }
                    } catch (e) { console.warn('[stream-event]', e); }
                }
            }
        }
    } catch (e) {
        const msg = e.name === 'AbortError' ? '请求超时，请重试' : e.message;
        const hasPartialText = textDiv && textDiv.textContent.trim().length > 0;
        if (hasPartialText) {
            // 保留已输出的部分文本，标注中断并提供重试按钮
            window._lastFailedAction = action;
            const retryBar = document.createElement('div');
            retryBar.className = 'stream-interrupted';
            retryBar.innerHTML = `<span>（生成中断：${escapeHtml(msg)}）</span> <button class="retry-btn" onclick="this.parentElement.remove(); submitActionStream(window._lastFailedAction)">重试</button>`;
            block.appendChild(retryBar);
        } else {
            // 无内容时移除失败的 turn block
            if (block && block.parentElement) block.remove();
        }
        // 短暂提示错误
        const toast = document.createElement('div');
        toast.className = 'toast-error';
        toast.textContent = `生成失败: ${msg}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
        // Restore previous choices so the player isn't stuck
        if (!choicesArea.children.length && prevChoicesHtml) {
            choicesArea.innerHTML = prevChoicesHtml;
        }
        // 刷新后端状态确保前端同步
        try {
            const freshState = await API.get(`/api/game/${currentSaveId}/state`);
            currentState = freshState;
            updateStatusPanel(currentState);
        } catch (_) { /* 刷新失败不阻塞 */ }
    } finally {
        if (timeoutId) clearTimeout(timeoutId);
        isStreaming = false;
        _showGameOverlay(false);
        document.getElementById('freeform-input').disabled = false;
        document.querySelectorAll('.choice-btn').forEach(b => b.disabled = false);
    }
}

// Format game time for display
function formatGameTime(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        const month = d.getMonth() + 1;
        const day = d.getDate();
        const hour = d.getHours().toString().padStart(2, '0');
        const min = d.getMinutes().toString().padStart(2, '0');
        const weekdays = ['日', '一', '二', '三', '四', '五', '六'];
        return `${month}月${day}日 周${weekdays[d.getDay()]} ${hour}:${min}`;
    } catch {
        return isoStr;
    }
}

function toggleAdventureLog() {
    const content = document.getElementById('adventure-log-content');
    const arrow = document.getElementById('log-toggle-arrow');
    if (content.classList.contains('open')) {
        content.classList.remove('open');
        arrow.classList.remove('open');
    } else {
        content.classList.add('open');
        arrow.classList.add('open');
        loadAdventureLog();
    }
}

// Keyboard shortcuts: 1-4 to select choices
document.addEventListener('keydown', (e) => {
    // Don't trigger when typing in inputs or textareas
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
    if (isStreaming) return;
    if (!currentSaveId) return;

    const key = e.key;
    if (key >= '1' && key <= '4') {
        const idx = parseInt(key) - 1;
        const buttons = document.querySelectorAll('#choices-area .choice-btn');
        if (idx < buttons.length && !buttons[idx].disabled) {
            buttons[idx].click();
        }
    }
});

/* ──── Scene Image Functions ──── */

function renderSceneImage(imageData) {
    const container = document.getElementById('scene-image-container');
    const genBar = document.getElementById('scene-image-gen-bar');
    if (!container) return;
    if (!imageData || (!imageData.base64 && !imageData.url)) {
        container.style.display = 'none';
        container.classList.remove('overlay-mode');
        const gp = document.getElementById('game-play-screen');
        if (gp) gp.classList.remove('img-overlay-active');
        // Show generate button if image provider is likely configured
        if (genBar && localStorage.getItem('tavern_img_provider_enabled')) {
            genBar.style.display = '';
        }
        return;
    }
    const img = document.getElementById('scene-image');
    const src = imageData.base64
        ? `data:image/png;base64,${imageData.base64}`
        : imageData.url;
    img.src = src;
    container.style.display = '';
    if (genBar) genBar.style.display = 'none';
    applyImageDisplayMode();
}

function toggleImageMode() {
    const current = localStorage.getItem('tavern_img_mode') || 'split';
    const next = current === 'split' ? 'overlay' : 'split';
    localStorage.setItem('tavern_img_mode', next);
    applyImageDisplayMode();
}

function applyImageDisplayMode() {
    const mode = localStorage.getItem('tavern_img_mode') || 'split';
    const container = document.getElementById('scene-image-container');
    const gamePlay = document.getElementById('game-play-screen');
    if (!container) return;

    if (mode === 'overlay') {
        container.classList.add('overlay-mode');
        if (gamePlay) gamePlay.classList.add('img-overlay-active');
    } else {
        container.classList.remove('overlay-mode');
        if (gamePlay) gamePlay.classList.remove('img-overlay-active');
    }
}

/* ──── Image Settings Panel ──── */

function onImgProviderChange() {
    const val = document.getElementById('img-provider-select').value;
    document.getElementById('img-openai-config').style.display = val === 'openai' ? '' : 'none';
    document.getElementById('img-gemini-config').style.display = val === 'gemini' ? '' : 'none';
    document.getElementById('img-comfyui-config').style.display = val === 'comfyui' ? '' : 'none';
}

/* ──── Image Profile Management ──── */

let _editingImgProfileId = null;

function _showImgStatus(msg, ok) {
    const el = document.getElementById('img-config-status');
    el.innerHTML = `<span style="color:var(--${ok ? 'success,#4caf50' : 'danger'})">${msg}</span>`;
    setTimeout(() => { el.innerHTML = ''; }, 3000);
}

async function loadImageProfiles() {
    try {
        const profiles = await API.get('/api/config/image/profiles');
        const container = document.getElementById('img-profiles-list');
        container.innerHTML = '';
        let hasActive = false;
        (profiles || []).forEach(p => {
            const badge = p.is_active ? '<span class="profile-badge">当前</span>' : '';
            if (p.is_active) hasActive = true;
            const providerLabels = { openai: 'OpenAI', gemini: 'Gemini', comfyui: 'ComfyUI' };
            const providerLabel = providerLabels[p.provider] || p.provider;
            const modeLabel = p.display_mode === 'overlay' ? '全屏背景' : '分区';
            let detail = `${providerLabel} | ${modeLabel}`;
            if (p.provider === 'openai' && p.openai) detail += ` | ${p.openai.model || 'gpt-image-1'}`;
            if (p.provider === 'gemini' && p.gemini) detail += ` | ${p.gemini.model || ''}`;
            container.innerHTML += `
                <div class="profile-card${p.is_active ? ' is-active' : ''}">
                    <div class="profile-info">
                        <div class="profile-name">${escapeHtml(p.name)}${badge}</div>
                        <div class="profile-detail">${escapeHtml(detail)}</div>
                    </div>
                    <div class="profile-actions">
                        ${!p.is_active ? `<button class="btn-primary" onclick="activateImageProfile('${p.id}')">启用</button>` : ''}
                        <button class="btn-secondary" onclick="editImageProfile('${p.id}')">编辑</button>
                        <button class="btn-secondary" onclick="deleteImageProfile('${p.id}')">删除</button>
                    </div>
                </div>`;
        });
        // Sync localStorage
        if (hasActive) {
            localStorage.setItem('tavern_img_provider_enabled', '1');
        } else {
            localStorage.removeItem('tavern_img_provider_enabled');
        }
        // Sync display mode from active profile
        const active = (profiles || []).find(p => p.is_active);
        if (active) localStorage.setItem('tavern_img_mode', active.display_mode || 'split');
    } catch (e) { console.warn('loadImageProfiles:', e); }
}

async function saveImageProfile() {
    const name = document.getElementById('img-profile-name').value.trim();
    const provider = document.getElementById('img-provider-select').value;
    const display_mode = document.getElementById('img-display-mode').value;
    if (!name) { alert('请填写配置名称'); return; }

    const body = { name, provider, display_mode };
    if (provider === 'openai') {
        body.openai = {
            api_key: document.getElementById('img-openai-key').value,
            base_url: document.getElementById('img-openai-base-url').value,
            model: document.getElementById('img-openai-model').value,
            size: document.getElementById('img-openai-size').value,
            quality: document.getElementById('img-openai-quality').value,
        };
    } else if (provider === 'gemini') {
        body.gemini = {
            api_key: document.getElementById('img-gemini-key').value,
            base_url: document.getElementById('img-gemini-base-url').value,
            model: document.getElementById('img-gemini-model').value,
        };
    } else if (provider === 'comfyui') {
        body.comfyui = {
            base_url: document.getElementById('img-comfyui-url').value,
            width: parseInt(document.getElementById('img-comfyui-width').value) || 1024,
            height: parseInt(document.getElementById('img-comfyui-height').value) || 576,
            steps: parseInt(document.getElementById('img-comfyui-steps').value) || 20,
            checkpoint: document.getElementById('img-comfyui-checkpoint').value,
        };
    }

    try {
        if (_editingImgProfileId) {
            await API.put(`/api/config/image/profiles/${_editingImgProfileId}`, body);
            _showImgStatus('配置已更新', true);
        } else {
            await API.post('/api/config/image/profiles', body);
            _showImgStatus('配置已保存', true);
        }
        resetImgProfileForm();
        loadImageProfiles();
    } catch (e) {
        _showImgStatus('保存失败: ' + e.message, false);
    }
}

async function activateImageProfile(id) {
    try {
        await API.post(`/api/config/image/profiles/${id}/activate`);
        _showImgStatus('已切换', true);
        loadImageProfiles();
    } catch (e) {
        _showImgStatus('切换失败: ' + e.message, false);
    }
}

async function deleteImageProfile(id) {
    if (!confirm('确定删除此图像配置？')) return;
    try {
        await API.del(`/api/config/image/profiles/${id}`);
        loadImageProfiles();
    } catch (e) {
        _showImgStatus('删除失败: ' + e.message, false);
    }
}

async function editImageProfile(id) {
    try {
        const profiles = await API.get('/api/config/image/profiles');
        const p = (profiles || []).find(x => x.id === id);
        if (!p) return;

        _editingImgProfileId = id;
        document.getElementById('img-profile-name').value = p.name || '';
        document.getElementById('img-provider-select').value = p.provider || 'openai';
        document.getElementById('img-display-mode').value = p.display_mode || 'split';

        // Clear sensitive fields first
        document.getElementById('img-openai-key').value = '';
        document.getElementById('img-openai-key').placeholder = p.openai?.has_api_key ? '留空保持不变' : 'sk-...';
        document.getElementById('img-gemini-key').value = '';
        document.getElementById('img-gemini-key').placeholder = p.gemini?.has_api_key ? '留空保持不变' : 'pk-...';

        if (p.openai) {
            document.getElementById('img-openai-base-url').value = p.openai.base_url || '';
            document.getElementById('img-openai-model').value = p.openai.model || 'gpt-image-1';
            document.getElementById('img-openai-size').value = p.openai.size || '1536x1024';
            document.getElementById('img-openai-quality').value = p.openai.quality || 'low';
        }
        if (p.gemini) {
            document.getElementById('img-gemini-base-url').value = p.gemini.base_url || '';
            document.getElementById('img-gemini-model').value = p.gemini.model || 'Gemini 3-Pro-Image-Preview';
        }
        if (p.comfyui) {
            document.getElementById('img-comfyui-url').value = p.comfyui.base_url || 'http://127.0.0.1:8188';
            document.getElementById('img-comfyui-width').value = p.comfyui.width || 1024;
            document.getElementById('img-comfyui-height').value = p.comfyui.height || 576;
            document.getElementById('img-comfyui-steps').value = p.comfyui.steps || 20;
            document.getElementById('img-comfyui-checkpoint').value = p.comfyui.checkpoint || '';
        }
        onImgProviderChange();
        document.getElementById('img-save-btn').textContent = '更新配置';
        document.getElementById('img-form-title').textContent = `编辑配置: ${p.name}`;
        document.getElementById('img-profile-name').scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (e) {
        _showImgStatus('加载失败: ' + e.message, false);
    }
}

function resetImgProfileForm() {
    _editingImgProfileId = null;
    document.getElementById('img-profile-name').value = '';
    document.getElementById('img-provider-select').value = 'openai';
    document.getElementById('img-openai-key').value = '';
    document.getElementById('img-openai-key').placeholder = 'sk-...';
    document.getElementById('img-openai-base-url').value = '';
    document.getElementById('img-openai-model').value = 'gpt-image-1';
    document.getElementById('img-openai-size').value = '1536x1024';
    document.getElementById('img-openai-quality').value = 'low';
    document.getElementById('img-gemini-key').value = '';
    document.getElementById('img-gemini-key').placeholder = 'pk-...';
    document.getElementById('img-gemini-base-url').value = '';
    document.getElementById('img-gemini-model').value = 'Gemini 3-Pro-Image-Preview';
    document.getElementById('img-comfyui-url').value = 'http://127.0.0.1:8188';
    document.getElementById('img-comfyui-width').value = '1024';
    document.getElementById('img-comfyui-height').value = '576';
    document.getElementById('img-comfyui-steps').value = '20';
    document.getElementById('img-comfyui-checkpoint').value = '';
    document.getElementById('img-display-mode').value = 'split';
    document.getElementById('img-save-btn').textContent = '保存配置';
    document.getElementById('img-form-title').textContent = '添加新配置';
    onImgProviderChange();
}

// Load image profiles when settings panel loads
document.addEventListener('DOMContentLoaded', () => { loadImageProfiles(); });

/* ──── Generate Scene Image for Current Turn ──── */

async function generateSceneImageForCurrent() {
    if (!currentSaveId || !currentNodeId) return;
    const btn = document.getElementById('btn-gen-scene-img');
    if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
    try {
        const headers = { 'Content-Type': 'application/json' };
        const token = localStorage.getItem('tavern_api_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const resp = await fetch(`/api/game/${currentSaveId}/generate-scene-image/${currentNodeId}`, {
            method: 'POST', headers
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        if (data.scene_image_path) {
            renderSceneImage({ url: data.scene_image_path });
        }
    } catch (e) {
        const toast = document.createElement('div');
        toast.className = 'toast-error';
        toast.textContent = `生成场景图失败: ${e.message}`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '生成背景图'; }
    }
}

/* ──── Feedback Regeneration ──── */

async function feedbackRegen() {
    if (!currentSaveId) return;
    const feedback = prompt('请描述你对这轮叙事的修改意见：');
    if (!feedback) return;
    const btn = document.getElementById('btn-feedback-regen');
    if (btn) { btn.disabled = true; }
    try {
        const data = await API.post(`/api/game/${currentSaveId}/feedback-regen`, { feedback });
        if (data.ok && data.narrative) {
            // 替换当前叙事文本
            const blocks = document.querySelectorAll('.turn-block.latest .narrative-text, .turn-block:last-child .narrative-text');
            const target = blocks.length ? blocks[blocks.length - 1] : null;
            if (target) {
                target.innerHTML = typeof enhanceNarrative === 'function'
                    ? enhanceNarrative(escapeHtml(data.narrative).replace(/\n/g, '<br>'))
                    : escapeHtml(data.narrative).replace(/\n/g, '<br>');
            }
        } else {
            alert(data.error || '重写失败');
        }
    } catch (e) {
        alert('反馈重写失败: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; }
    }
}
