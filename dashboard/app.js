/**
 * PolyAgent Dashboard Logic - Single Page Application Core
 * Manages WebSocket streams, REST API updates, canvas drawings, and UI interactivity.
 */

(function () {
    const API_BASE = window.location.origin;
    const WS_BASE = window.location.origin.replace(/^http/, 'ws');
    
    // UI Elements
    const elements = {
        wsStatus: document.getElementById('ws-status'),
        modeBadge: document.getElementById('mode-badge'),
        btnPause: document.getElementById('btn-pause'),
        metricPnl: document.getElementById('metric-pnl'),
        metricWinrate: document.getElementById('metric-winrate'),
        metricSharpe: document.getElementById('metric-sharpe'),
        metricExposure: document.getElementById('metric-exposure'),
        metricBalance: document.getElementById('metric-balance'),
        signalsCount: document.getElementById('signals-count'),
        signalsList: document.getElementById('signals-list'),
        positionsTable: document.getElementById('positions-table').querySelector('tbody'),
        ordersTable: document.getElementById('orders-table').querySelector('tbody'),
        auditLogList: document.getElementById('audit-log-list'),
        canvas: document.getElementById('pnl-chart'),
    };

    // State Variables
    let ws = null;
    let reconnectInterval = 3000;
    let isPaused = false;
    let performanceHistory = []; // P&L values for charting

    // Initialize Canvas Size
    function initCanvas() {
        const dpr = window.devicePixelRatio || 1;
        const rect = elements.canvas.getBoundingClientRect();
        elements.canvas.width = rect.width * dpr;
        elements.canvas.height = rect.height * dpr;
        const ctx = elements.canvas.getContext('2d');
        ctx.scale(dpr, dpr);
    }

    // Number Formatters
    const format = {
        usd: (val) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val),
        pct: (val) => `${(val * 100).toFixed(1)}%`,
        num: (val, dec = 2) => Number(val).toFixed(dec),
        time: (isoStr) => {
            const date = new Date(isoStr);
            const now = new Date();
            const diffMs = now - date;
            const diffMins = Math.floor(diffMs / 60000);
            
            if (diffMins < 1) return 'Just now';
            if (diffMins < 60) return `${diffMins}m ago`;
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
    };

    // ── WebSocket Client ──────────────────────────────────────────────

    function connectWebSocket() {
        elements.wsStatus.innerHTML = '<span class="status-dot pulsing"></span><span class="status-label">CONNECTING</span>';
        elements.wsStatus.className = 'status-indicator';
        
        ws = new WebSocket(`${WS_BASE}/ws`);

        ws.onopen = () => {
            logger.info("Real-time WebSockets connection established");
            elements.wsStatus.innerHTML = '<span class="status-dot online"></span><span class="status-label">ONLINE</span>';
            // Start heartbeats
            setInterval(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'ping' }));
                }
            }, 30000);
        };

        ws.onclose = () => {
            elements.wsStatus.innerHTML = '<span class="status-dot offline"></span><span class="status-label">OFFLINE</span>';
            logger.warn("WebSocket closed. Attempting reconnect in 3s...");
            setTimeout(connectWebSocket, reconnectInterval);
        };

        ws.onerror = (err) => {
            logger.error("WebSocket encountered an error: " + err.message);
        };

        ws.onmessage = (event) => {
            try {
                const message = JSON.parse(event.data);
                if (message.type === 'pong') return;
                
                handleWebSocketMessage(message);
            } catch (e) {
                console.error("Failed to parse websocket message", e);
            }
        };
    }

    function handleWebSocketMessage(msg) {
        const { type, data } = msg;
        
        if (type === 'signal_detected') {
            logger.info(`Agent: signal detected -> Type: ${data.type.toUpperCase()} | Edge: ${(data.edge * 100).toFixed(1)}%`);
            addSignalToFeed(data, true);
            // Fetch updated status to refresh count and signal history
            fetchData('/api/signals', renderSignals);
        } else if (type === 'signal_approved') {
            logger.info(`RiskManager: approved signal -> Market: ${data.question.substring(0, 30)}...`);
            fetchData('/api/signals', renderSignals);
        } else if (type === 'paper_order_filled') {
            logger.success(`Executor: simulated order FILLED -> Cost: $${(data.price * data.size).toFixed(2)}`);
            // Refresh tables and metrics instantly
            refreshAllData();
        } else if (type === 'system_alert') {
            logger.alert(data.message, data.level);
            if (data.level === 'CRITICAL') {
                setPauseButtonState(true);
            }
        }
    }

    // ── REST API Updates ──────────────────────────────────────────────

    async function fetchData(url, callback) {
        try {
            const resp = await fetch(`${API_BASE}${url}`);
            if (!resp.ok) throw new Error(`HTTP Error ${resp.status}`);
            const data = await resp.json();
            callback(data);
        } catch (e) {
            console.error(`Fetch failed for URL ${url}:`, e);
        }
    }

    function refreshAllData() {
        fetchData('/api/status', renderStatus);
        fetchData('/api/performance', renderPerformance);
        fetchData('/api/positions', renderPositions);
        fetchData('/api/orders', renderOrders);
        fetchData('/api/signals', renderSignals);
    }

    // ── Render Utilities ─────────────────────────────────────────────

    function renderStatus(status) {
        isPaused = status.trading_paused || false;
        setPauseButtonState(isPaused);
        elements.modeBadge.className = `badge mode-badge ${status.mode}-mode`;
        elements.modeBadge.innerText = `${status.mode.toUpperCase()} MODE`;
        
        // Update agent states in DOM
        const agents = ['scanner', 'analyst', 'arbitrage', 'risk_manager', 'executor'];
        agents.forEach(agentName => {
            const row = document.getElementById(`agent-${agentName}`);
            if (row) {
                const badge = row.querySelector('.agent-badge');
                if (isPaused) {
                    badge.innerText = 'PAUSED';
                    badge.className = 'agent-badge';
                } else {
                    badge.innerText = 'RUNNING';
                    badge.className = 'agent-badge active';
                }
            }
        });
        
        // Render audit logs
        if (status.recent_actions && status.recent_actions.length > 0) {
            elements.auditLogList.innerHTML = '';
            status.recent_actions.forEach(action => {
                const row = document.createElement('div');
                row.className = 'log-line';
                
                const timeStr = new Date(action.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                row.innerHTML = `<span class="time">[${timeStr}]</span> <span class="agent">[${action.agent}]</span> <span class="action">${action.action}</span> <span class="details text-muted">${JSON.stringify(action.details)}</span>`;
                elements.auditLogList.appendChild(row);
            });
        }
    }

    function renderPerformance(perf) {
        elements.metricPnl.innerText = format.usd(perf.total_pnl);
        elements.metricPnl.className = 'metric-value ' + (perf.total_pnl >= 0 ? 'positive' : 'negative');
        elements.metricWinrate.innerText = format.pct(perf.win_rate);
        elements.metricSharpe.innerText = format.num(perf.sharpe_ratio, 2);
        
        // Append current valuation to performanceHistory for graph
        if (performanceHistory.length === 0 || performanceHistory[performanceHistory.length - 1] !== perf.total_pnl) {
            performanceHistory.push(perf.total_pnl);
            // Limit to 30 elements
            if (performanceHistory.length > 30) performanceHistory.shift();
            drawChart();
        }
    }

    function renderPositions(positions) {
        let exposure = 0;
        let balance = 10000; // default benchmark
        
        elements.positionsTable.innerHTML = '';
        if (positions.length === 0) {
            elements.positionsTable.innerHTML = '<tr><td colspan="7" class="text-center">No open positions. Ready to execute signals.</td></tr>';
        } else {
            positions.forEach(p => {
                exposure += p.size * p.current_price;
                const row = document.createElement('tr');
                const pnlClass = p.unrealized_pnl >= 0 ? 'positive' : 'negative';
                
                row.innerHTML = `
                    <td>${p.market_question}</td>
                    <td class="text-center font-mono" style="font-size: 10px;">${p.token_id.substring(0, 10)}...</td>
                    <td class="text-center font-mono"><span class="badge ${p.side === 'buy' ? 'mode-badge' : 'live-mode'}">${p.side.toUpperCase()}</span></td>
                    <td>${format.num(p.size, 1)}</td>
                    <td>${format.usd(p.entry_price)}</td>
                    <td>${format.usd(p.current_price)}</td>
                    <td><span class="pnl-badge ${pnlClass}">${p.unrealized_pnl >= 0 ? '+' : ''}${format.usd(p.unrealized_pnl)}</span></td>
                `;
                elements.positionsTable.appendChild(row);
            });
        }
        
        elements.metricExposure.innerText = format.usd(exposure);
    }

    function renderOrders(orders) {
        elements.ordersTable.innerHTML = '';
        if (orders.length === 0) {
            elements.ordersTable.innerHTML = '<tr><td colspan="6" class="text-center">No recent orders submitted yet.</td></tr>';
        } else {
            orders.forEach(o => {
                const row = document.createElement('tr');
                const sideClass = o.side === 'buy' ? 'mode-badge' : 'live-mode';
                const statusClass = `order-badge ${o.status.toLowerCase()}`;
                const timeStr = new Date(o.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                
                row.innerHTML = `
                    <td>${timeStr}</td>
                    <td>${o.market_question}</td>
                    <td class="text-center"><span class="badge ${sideClass}">${o.side.toUpperCase()}</span></td>
                    <td>${format.usd(o.price)}</td>
                    <td>${format.num(o.size, 1)}</td>
                    <td class="text-center"><span class="${statusClass}">${o.status}</span></td>
                `;
                elements.ordersTable.appendChild(row);
            });
        }
    }

    function renderSignals(signals) {
        const activeSignals = signals.filter(s => s.status === 'pending');
        elements.signalsCount.innerText = `${activeSignals.length} PENDING`;
        
        elements.signalsList.innerHTML = '';
        if (signals.length === 0) {
            elements.signalsList.innerHTML = '<div class="feed-empty">No trading signals detected yet. Scanner is scanning...</div>';
        } else {
            signals.forEach(s => {
                addSignalToFeed(s, false);
            });
        }
    }

    function addSignalToFeed(s, prepend = false) {
        const div = document.createElement('div');
        div.className = `signal-card ${s.type}`;
        
        // Clean details display depending on signal type
        let detailStr = '';
        if (s.type === 'statistical') {
            const sideStr = s.data.side === 'buy' ? 'BUY YES' : 'BUY NO';
            detailStr = `Target Price: $${s.data.target_price.toFixed(4)} | Rec Side: <strong>${sideStr}</strong>`;
        } else if (s.type === 'internal_arb') {
            detailStr = `YES Price: $${s.data.yes_price.toFixed(4)} | NO Price: $${s.data.no_price.toFixed(4)} | ROI: ${(s.data.roi_pct).toFixed(2)}%`;
        } else if (s.type === 'negative_risk') {
            detailStr = `YES Sum: $${s.data.sum_yes_prices.toFixed(2)} | ROI: ${(s.data.roi_pct).toFixed(2)}%`;
        }

        const question = s.market_question || s.question || "Polymarket Event";

        div.innerHTML = `
            <div class="sig-meta">
                <span class="sig-type">${s.type.replace('_', ' ')}</span>
                <span class="sig-time">${format.time(s.created_at || new Date().toISOString())}</span>
            </div>
            <div class="sig-question">${question}</div>
            <div class="sig-metrics">
                <div class="sig-edge">Edge: <span>${format.pct(s.edge_pct || s.edge)}</span></div>
                <div class="sig-confidence">Confidence: <span>${format.pct(s.confidence)}</span></div>
            </div>
            <div class="sig-details" style="font-size: 11px; color: var(--color-slate); font-family: var(--font-mono);">${detailStr}</div>
            <div class="sig-action">${s.status}</div>
        `;

        if (prepend && elements.signalsList.firstChild) {
            elements.signalsList.insertBefore(div, elements.signalsList.firstChild);
        } else {
            elements.signalsList.appendChild(div);
        }
    }

    // ── HTML5 Canvas P&L Chart Drawing ───────────────────────────────

    function drawChart() {
        const ctx = elements.canvas.getContext('2d');
        const width = elements.canvas.width / (window.devicePixelRatio || 1);
        const height = elements.canvas.height / (window.devicePixelRatio || 1);
        
        ctx.clearRect(0, 0, width, height);

        // Standard gradient background
        const bgGrad = ctx.createLinearGradient(0, 0, 0, height);
        bgGrad.addColorStop(0, 'rgba(0, 255, 136, 0.05)');
        bgGrad.addColorStop(1, 'rgba(0, 255, 136, 0)');
        
        const history = [...performanceHistory];
        if (history.length === 0) history.push(0);
        if (history.length === 1) history.push(history[0]); // need 2 points for a line

        const minVal = Math.min(...history, 0) - 2;
        const maxVal = Math.max(...history, 5) + 2;
        const valRange = maxVal - minVal;

        const getX = (idx) => (idx / (history.length - 1)) * (width - 30) + 15;
        const getY = (val) => height - 15 - ((val - minVal) / valRange) * (height - 30);

        // Draw grid lines
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.02)';
        ctx.lineWidth = 1;
        for (let i = 1; i < 4; i++) {
            const y = (height / 4) * i;
            ctx.beginPath();
            ctx.moveTo(15, y);
            ctx.lineTo(width - 15, y);
            ctx.stroke();
        }

        // Draw line shadow/glow
        ctx.strokeStyle = 'rgba(0, 255, 136, 0.2)';
        ctx.lineWidth = 6;
        ctx.beginPath();
        ctx.moveTo(getX(0), getY(history[0]));
        for (let i = 1; i < history.length; i++) {
            ctx.lineTo(getX(i), getY(history[i]));
        }
        ctx.stroke();

        // Draw primary line
        ctx.strokeStyle = '#00ff88';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(getX(0), getY(history[0]));
        for (let i = 1; i < history.length; i++) {
            ctx.lineTo(getX(i), getY(history[i]));
        }
        ctx.stroke();

        // Fill area below chart
        ctx.fillStyle = bgGrad;
        ctx.beginPath();
        ctx.moveTo(getX(0), height - 15);
        for (let i = 0; i < history.length; i++) {
            ctx.lineTo(getX(i), getY(history[i]));
        }
        ctx.lineTo(getX(history.length - 1), height - 15);
        ctx.closePath();
        ctx.fill();

        // Draw node markers
        ctx.fillStyle = '#07090e';
        ctx.strokeStyle = '#00ff88';
        ctx.lineWidth = 2;
        history.forEach((val, i) => {
            if (i === 0 || i === history.length - 1 || history.length < 10) {
                ctx.beginPath();
                ctx.arc(getX(i), getY(val), 4, 0, Math.PI * 2);
                ctx.fill();
                ctx.stroke();
            }
        });
    }

    // ── Log Console Output ───────────────────────────────────────────

    const logger = {
        _log: (text, cls = '') => {
            const row = document.createElement('div');
            row.className = `log-line ${cls}`;
            const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            row.innerHTML = `<span class="time">[${timeStr}]</span> ${text}`;
            elements.auditLogList.prepend(row);
            // Limit to 50 lines in console
            while (elements.auditLogList.children.length > 50) {
                elements.auditLogList.removeChild(elements.auditLogList.lastChild);
            }
        },
        info: (txt) => logger._log(`<span class="agent">[INFO]</span> ${txt}`),
        success: (txt) => logger._log(`<span class="action">[FILL]</span> <span style="color: var(--color-emerald);">${txt}</span>`),
        warn: (txt) => logger._log(`<span style="color: var(--color-amber);">[WARN]</span> ${txt}`),
        alert: (txt, lvl) => logger._log(`<span style="color: var(--color-rose); font-weight: 700;">[${lvl}]</span> ${txt}`),
    };

    // ── System Pause Control ──────────────────────────────────────────

    async function togglePause() {
        try {
            const resp = await fetch(`${API_BASE}/api/trading/pause`, { method: 'POST' });
            if (!resp.ok) throw new Error("HTTP error pausing system");
            const res = await resp.json();
            
            isPaused = res.trading_paused;
            setPauseButtonState(isPaused);
            logger.warn(`Manual Override: System ${isPaused ? 'PAUSED' : 'RESUMED'}`);
            
            // Refresh Status to toggle badges
            fetchData('/api/status', renderStatus);
        } catch (e) {
            console.error("Failed to toggle pause", e);
        }
    }

    function setPauseButtonState(paused) {
        if (paused) {
            elements.btnPause.className = "btn btn-danger btn-pause";
            elements.btnPause.innerHTML = '<span class="icon">▶</span> <span class="label">RESUME SYSTEM</span>';
        } else {
            elements.btnPause.className = "btn btn-primary btn-pause";
            elements.btnPause.innerHTML = '<span class="icon">⏸</span> <span class="label">PAUSE SYSTEM</span>';
        }
    }

    // ── Initialization ───────────────────────────────────────────────

    function init() {
        initCanvas();
        connectWebSocket();
        refreshAllData();
        
        // Setup Polling fallback (in case WebSockets disconnect)
        setInterval(refreshAllData, 10000);

        // Event Listeners
        elements.btnPause.addEventListener('click', togglePause);
        window.addEventListener('resize', () => {
            initCanvas();
            drawChart();
        });
    }

    document.addEventListener('DOMContentLoaded', init);
})();
