let authToken = localStorage.getItem("token");
let ws = null;

document.addEventListener("DOMContentLoaded", () => {
    if (authToken) {
        initApp();
    } else {
        document.getElementById("login-modal").style.display = "flex";
        document.getElementById("app-container").style.display = "none";
    }

    document.getElementById("login-form").addEventListener("submit", handleLogin);
    document.getElementById("straddle-config-form").addEventListener("submit", saveStraddleConfig);
    document.getElementById("hedge-config-form").addEventListener("submit", saveHedgeConfig);
});

async function handleLogin(e) {
    e.preventDefault();
    const user = document.getElementById("login-username").value;
    const pass = document.getElementById("login-password").value;
    const errDiv = document.getElementById("login-error");

    const formData = new URLSearchParams();
    formData.append("username", user);
    formData.append("password", pass);

    try {
        const res = await fetch("/api/v1/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: formData
        });

        if (res.ok) {
            const data = await res.json();
            authToken = data.access_token;
            localStorage.setItem("token", authToken);
            localStorage.setItem("username", data.username);
            localStorage.setItem("role", data.role);
            errDiv.style.display = "none";
            initApp();
        } else {
            errDiv.innerText = "Invalid credentials. Try admin / Admin@123";
            errDiv.style.display = "block";
        }
    } catch (err) {
        errDiv.innerText = "Connection error. Ensure backend is running.";
        errDiv.style.display = "block";
    }
}

function logout() {
    localStorage.clear();
    location.reload();
}

function initApp() {
    document.getElementById("login-modal").style.display = "none";
    document.getElementById("app-container").style.display = "block";

    document.getElementById("user-display-name").innerText = localStorage.getItem("username") || "Admin";
    document.getElementById("user-display-role").innerText = (localStorage.getItem("role") || "ADMIN").toUpperCase();

    fetchSnapshot();
    connectWebSocket();
    loadStraddleConfig();
    loadHedgeConfig();
    loadAuditLogs();
}

async function fetchSnapshot() {
    try {
        const res = await fetch("/api/v1/dashboard/snapshot", {
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const data = await res.json();
            document.getElementById("ticker-btc-mark").innerText = `$${data.market.btc_mark_price.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            document.getElementById("ticker-btc-spot").innerText = `$${data.market.btc_spot_price.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            document.getElementById("stat-wallet").innerText = `$${data.wallet.paper_wallet_usdt.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            document.getElementById("stat-straddle-state").innerText = data.straddle.state;
            document.getElementById("stat-hedge-state").innerText = data.hedge.state;

            // Render Straddle Dashboard Sessions Table
            const straddleBody = document.getElementById("straddle-sessions-table-body");
            if (data.straddle.history && data.straddle.history.length > 0) {
                straddleBody.innerHTML = data.straddle.history.map(s => `
                    <tr>
                        <td><b>#${s.id}</b></td>
                        <td>${s.expiry_sym || s.expiry_dt}</td>
                        <td><span class="badge ${s.status === 'OPEN' ? 'badge-success' : 'badge-warning'}">${s.status}</span></td>
                        <td>$${(s.btc_entry_spot || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>${s.call_strike || '-'}</td>
                        <td>${s.put_strike || '-'}</td>
                        <td>$${(s.net_straddle_ask || 0).toFixed(2)}</td>
                        <td style="color: ${s.pnl_realized >= 0 ? 'var(--success)' : 'var(--danger)'};">$${s.pnl_realized.toFixed(2)}</td>
                        <td>${s.exit_reason || '-'}</td>
                    </tr>
                `).join("");
            }

            // Render Hedge Dashboard Sessions Table
            const hedgeBody = document.getElementById("hedge-sessions-table-body");
            if (data.hedge.history && data.hedge.history.length > 0) {
                hedgeBody.innerHTML = data.hedge.history.map(h => `
                    <tr>
                        <td><b>#${h.id}</b></td>
                        <td>${h.symbol}</td>
                        <td><span class="badge ${h.status === 'Open' ? 'badge-success' : 'badge-warning'}">${h.status}</span></td>
                        <td>$${(h.bull_entry || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>$${(h.bear_entry || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>$${(h.bull_exit || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>$${(h.bear_exit || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td style="color: ${h.realized_pnl >= 0 ? 'var(--success)' : 'var(--danger)'};">$${h.realized_pnl.toFixed(2)}</td>
                        <td>${h.exit_reason || '-'}</td>
                    </tr>
                `).join("");
            }
        }
    } catch (err) {
        console.error("Error fetching snapshot:", err);
    }
}

function connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/dashboard/ws/live`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.btc_mark) {
            document.getElementById("ticker-btc-mark").innerText = `$${data.btc_mark.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
        }
        if (data.btc_spot) {
            document.getElementById("ticker-btc-spot").innerText = `$${data.btc_spot.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
        }
    };

    ws.onclose = () => {
        setTimeout(connectWebSocket, 3000);
    };
}

function switchTab(tabId) {
    document.querySelectorAll(".tab-content").forEach(el => el.style.display = "none");
    document.querySelectorAll(".tab-btn").forEach(el => el.classList.remove("active"));

    document.getElementById(`tab-${tabId}`).style.display = "block";
    event.target.classList.add("active");

    if (tabId === "audit-logs") loadAuditLogs();
}

async function emergencySquareoff(algo) {
    if (!confirm(`Are you sure you want to execute EMERGENCY SQUARE-OFF for ${algo.toUpperCase()}? This will close active positions immediately.`)) {
        return;
    }

    try {
        const res = await fetch(`/api/v1/dashboard/${algo}/squareoff`, {
            method: "POST",
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const data = await res.json();
            alert(data.message);
            fetchSnapshot();
        }
    } catch (err) {
        alert(`Error executing emergency squareoff for ${algo}`);
    }
}

const STRADDLE_FIELDS = [
    "RUNTIME_MODE", "BOT_ENABLED", "PAPER_TRADING_ENABLED", "WORKER_ID",
    "BINANCE_API_KEY", "BINANCE_SECRET_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    "WINDOW_START", "WINDOW_END", "FUTURES_ENTRY_CUTOFF", "SQ_START", "SQ_END",
    "FUTURES_SQUAREOFF", "STRADDLE_EXPIRY_TIME", "TRADE_QTY", "MIN_STRIKE_GAP",
    "MAX_TOTAL_MARK", "MAX_PREMIUM_GAP", "FUTURES_TP_MULTIPLIER", "FUTURES_LEVERAGE",
    "PAPER_WALLET_USDT"
];

async function loadStraddleConfig() {
    try {
        const res = await fetch("/api/v1/config/straddle", {
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const data = await res.json();
            for (let [k, v] of Object.entries(data.active)) {
                const input = document.getElementById(`cfg_straddle_${k}`);
                if (input) input.value = v;
            }

            if (Object.keys(data.pending).length > 0) {
                document.getElementById("straddle-deferred-badge").style.display = "inline-block";
            } else {
                document.getElementById("straddle-deferred-badge").style.display = "none";
            }
        }
    } catch (err) {
        console.error(err);
    }
}

async function saveStraddleConfig(e) {
    e.preventDefault();
    const payload = {};
    STRADDLE_FIELDS.forEach(k => {
        const input = document.getElementById(`cfg_straddle_${k}`);
        if (input) payload[k] = input.value;
    });

    try {
        const res = await fetch("/api/v1/config/straddle", {
            method: "POST",
            headers: { 
                "Content-Type": "application/json",
                "Authorization": `Bearer ${authToken}` 
            },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            const data = await res.json();
            alert(data.message);
            loadStraddleConfig();
        }
    } catch (err) {
        alert("Error saving straddle configuration.");
    }
}

const HEDGE_FIELDS = [
    "RUNTIME_MODE", "ENGINE_ENABLED", "PAPER_TRADING_ENABLED", "GLOBAL_PAUSE",
    "MAX_OPTION_SPEND", "Q_MAX_BTC", "WORKER_POLL_SECONDS", "FILL_TIMEOUT_SEC"
];

async function loadHedgeConfig() {
    try {
        const res = await fetch("/api/v1/config/hedge", {
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const data = await res.json();
            for (let [k, v] of Object.entries(data.active)) {
                const input = document.getElementById(`cfg_hedge_${k}`);
                if (input) input.value = v;
            }

            // Render Hedge Strategy Cards (Bullish Hedge & Bearish Hedge Rules)
            const grid = document.getElementById("hedge-strategy-cards-grid");
            if (data.strategies && data.strategies.length > 0) {
                grid.innerHTML = data.strategies.map(s => `
                    <div class="stat-card" style="background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border-color);">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                            <div style="font-weight: 700; font-size: 16px;">${s.strategy_name}</div>
                            <span class="badge ${s.enabled ? 'badge-success' : 'badge-danger'}">${s.enabled ? 'Enabled' : 'Disabled'}</span>
                        </div>
                        <div style="font-size: 13px; color: var(--text-muted); display: flex; flex-direction: column; gap: 8px;">
                            <div><b>Direction:</b> <span style="color: var(--primary);">${s.direction}</span></div>
                            <div><b>Trade Window Open:</b> ${s.trade_start}</div>
                            <div><b>Trade Window Close:</b> ${s.trade_end}</div>
                            <div><b>Force Close Squareoff:</b> ${s.force_close}</div>
                            <div><b>Contract Qty:</b> ${s.contract_qty} BTC</div>
                            <div><b>Max Premium Limit:</b> $${s.max_premium}</div>
                        </div>
                    </div>
                `).join("");
            }
        }
    } catch (err) {
        console.error(err);
    }
}

async function saveHedgeConfig(e) {
    e.preventDefault();
    const payload = {};
    HEDGE_FIELDS.forEach(k => {
        const input = document.getElementById(`cfg_hedge_${k}`);
        if (input) payload[k] = input.value;
    });

    try {
        const res = await fetch("/api/v1/config/hedge", {
            method: "POST",
            headers: { 
                "Content-Type": "application/json",
                "Authorization": `Bearer ${authToken}` 
            },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            const data = await res.json();
            alert(data.message);
            loadHedgeConfig();
        }
    } catch (err) {
        alert("Error saving hedge configuration.");
    }
}

async function loadAuditLogs() {
    try {
        const res = await fetch("/api/v1/audit/logs", {
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const logs = await res.json();
            const tbody = document.getElementById("audit-logs-table-body");
            if (logs.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-muted);">No audit logs recorded yet.</td></tr>`;
                return;
            }

            tbody.innerHTML = logs.map(l => `
                <tr>
                    <td>${l.created_at}</td>
                    <td>${l.user_email}</td>
                    <td><span class="badge badge-info">${l.config_type}</span></td>
                    <td><b>${l.field_name}</b></td>
                    <td><span style="color: var(--danger);">${l.old_value || '-'}</span></td>
                    <td><span style="color: var(--success);">${l.new_value}</span></td>
                    <td><span class="badge ${l.apply_mode === 'IMMEDIATE' ? 'badge-success' : 'badge-warning'}">${l.apply_mode}</span></td>
                    <td><span class="badge badge-success">${l.status}</span></td>
                </tr>
            `).join("");
        }
    } catch (err) {
        console.error(err);
    }
}
