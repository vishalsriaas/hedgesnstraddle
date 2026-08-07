let authToken = localStorage.getItem("token") || "";
let currentUser = JSON.parse(localStorage.getItem("user") || "{}");
let ws = null;

document.addEventListener("DOMContentLoaded", () => {
    if (!authToken) {
        window.location.href = "/";
        return;
    }

    document.getElementById("user-email-display").innerText = currentUser.email || "Admin";

    // Nav Switcher
    const navItems = document.querySelectorAll(".nav-item[data-view]");
    navItems.forEach(item => {
        item.addEventListener("click", () => {
            navItems.forEach(n => n.classList.remove("active"));
            item.classList.add("active");

            const targetView = item.getAttribute("data-view");
            document.querySelectorAll(".content-view").forEach(v => v.style.display = "none");
            
            const el = document.getElementById(`view-${targetView}`);
            if (el) el.style.display = "block";

            // Header titles
            const titleMap = {
                "straddle-dashboard": ["Live Straddle Bot Dashboard", "Real-Time Straddle Execution, Live PnL & Order Feeds"],
                "hedge-dashboard": ["Live Hedge Trader Dashboard", "Dual-Leg Directional Hedge Engine, Active Position Feeds"],
                "straddle-config": ["Straddle Bot Settings", "Runtime Modes, Credentials, Entry Rules, and Tuning"],
                "straddle-sessions": ["Straddle Trading Session Records", "Historical Straddle Bot Execution Trajectories"],
                "straddle-orders": ["Straddle Trade Order & Fill Records", "Execution Order Audit Trail & Binance Fill Log"],
                "straddle-ledger": ["Straddle Wallet Ledger Entries", "Virtual & Live Balance Adjustment Ledger"],
                "hedge-strategies": ["Hedge Strategy Config Rules", "Role-Based Parameter Configuration (1st Trader vs 2nd Trader)"],
                "hedge-config": ["Hedge Trader Settings (Global Parameters)", "Global Engine Controls, Spending Limits & Safety Protocols"],
                "hedge-sessions": ["Hedge Trading Session Records", "Dual-Leg Strategy Session Execution History"],
                "hedge-positions": ["Hedge Open Position & Trade Orders", "Live Positions, Floating PnL & Orders Audit"],
                "hedge-events": ["Hedge Macro Event Records", "High-Impact Macro Economic Blackout Dates"],
                "audit-logs": ["Config Audit Logs", "System Parameter Change Audits"],
                "trade-reports": ["CSV Trade Reports", "Export Complete Trade & PnL History"]
            };

            if (titleMap[targetView]) {
                document.getElementById("workspace-title").innerText = titleMap[targetView][0];
                document.getElementById("workspace-subtitle").innerText = titleMap[targetView][1];
            }

            // Trigger view specific loaders
            if (targetView === "straddle-config") loadStraddleConfig();
            if (targetView === "hedge-config" || targetView === "hedge-strategies") loadHedgeConfig();
            if (targetView === "audit-logs") loadAuditLogs();
        });
    });

    // Logout
    document.getElementById("logout-btn").addEventListener("click", () => {
        localStorage.removeItem("token");
        localStorage.removeItem("user");
        window.location.href = "/";
    });

    // Forms
    document.getElementById("straddle-config-form").addEventListener("submit", saveStraddleConfig);
    document.getElementById("hedge-config-form").addEventListener("submit", saveHedgeConfig);

    // Initial Load & WebSocket
    fetchSnapshot();
    connectWebSocket();
    setInterval(fetchSnapshot, 3000);
});

async function fetchSnapshot() {
    try {
        const res = await fetch("/api/v1/dashboard/snapshot", {
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const data = await res.json();
            
            // Market Prices
            const btcMark = data.market.btc_mark_price;
            const btcSpot = data.market.btc_spot_price;
            document.getElementById("ticker-btc-mark").innerText = `$${btcMark.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            document.getElementById("ticker-btc-spot").innerText = `$${btcSpot.toLocaleString(undefined, {minimumFractionDigits: 2})}`;

            // Status Ribbons
            const sState = data.straddle.state;
            const hState = data.hedge.state;
            document.getElementById("ribbon-straddle-state").innerText = `Straddle Engine: ${sState}`;
            document.getElementById("ribbon-straddle-state").className = `badge ${sState === 'IN_TRADE' || sState === 'RUNNING' ? 'badge-success' : 'badge-info'}`;
            
            document.getElementById("ribbon-hedge-state").innerText = `Hedge Engine: ${hState}`;
            document.getElementById("ribbon-hedge-state").className = `badge ${hState === 'RUNNING' ? 'badge-success' : 'badge-info'}`;

            // Update Straddle Hero Cards
            const activeStraddle = data.straddle.active_session;
            if (activeStraddle) {
                document.getElementById("straddle-hero-spot").innerText = `$${(activeStraddle.btc_entry_spot || btcSpot).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("straddle-hero-pnl").innerText = `${(activeStraddle.pnl_realized || 0) >= 0 ? '+' : ''}$${(activeStraddle.pnl_realized || 0).toFixed(2)}`;
                document.getElementById("straddle-hero-premium").innerText = `$${(activeStraddle.net_straddle_ask || 0).toFixed(2)}`;
                
                document.getElementById("call-strike-val").innerText = activeStraddle.call_strike;
                document.getElementById("call-ask-val").innerText = `$${(activeStraddle.call_ask || 0).toFixed(2)}`;
                
                document.getElementById("put-strike-val").innerText = activeStraddle.put_strike;
                document.getElementById("put-ask-val").innerText = `$${(activeStraddle.put_ask || 0).toFixed(2)}`;

                document.getElementById("fut-entry-val").innerText = `$${(activeStraddle.futures_entry_price || btcMark).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("fut-tp-val").innerText = `$${(activeStraddle.futures_tp_price || (btcMark + 375)).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            } else {
                document.getElementById("call-strike-val").innerText = data.straddle.live_call_strike || (Math.round(btcSpot / 100) * 100 + 300);
                document.getElementById("call-ask-val").innerText = `$${(data.straddle.live_call_ask || 180.50).toFixed(2)}`;
                
                document.getElementById("put-strike-val").innerText = data.straddle.live_put_strike || (Math.round(btcSpot / 100) * 100 - 300);
                document.getElementById("put-ask-val").innerText = `$${(data.straddle.live_put_ask || 195.20).toFixed(2)}`;

                document.getElementById("fut-entry-val").innerText = `$${btcMark.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("fut-tp-val").innerText = `$${(btcMark + 375).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            }
            document.getElementById("straddle-hero-wallet").innerText = `$${data.wallet.paper_wallet_usdt.toLocaleString(undefined, {minimumFractionDigits: 2})}`;

            // Update Hedge Hero Cards
            const activeHedge = data.hedge.active_session;
            if (activeHedge) {
                document.getElementById("hedge-hero-bull").innerText = `LONG @ $${(activeHedge.bull_entry || (btcSpot - 50)).toLocaleString()}`;
                document.getElementById("hedge-hero-bear").innerText = `SHORT @ $${(activeHedge.bear_entry || (btcSpot + 50)).toLocaleString()}`;
                document.getElementById("hedge-hero-total-pnl").innerText = `${(activeHedge.realized_pnl || 0) >= 0 ? '+' : ''}$${(activeHedge.realized_pnl || 0).toFixed(2)}`;
            }

            // Render Session Tables
            const straddleBody = document.getElementById("straddle-sessions-table-body");
            const straddleViewBody = document.getElementById("straddle-sessions-view-body");
            if (data.straddle.history && data.straddle.history.length > 0) {
                const rowsHtml = data.straddle.history.map(s => `
                    <tr>
                        <td><b>#${s.id}</b></td>
                        <td>${s.expiry_sym || s.expiry_dt}</td>
                        <td><span class="badge ${s.status === 'OPEN' ? 'badge-success' : 'badge-warning'}">${s.status}</span></td>
                        <td>$${(s.btc_entry_spot || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>${s.call_strike || '-'}</td>
                        <td>${s.put_strike || '-'}</td>
                        <td>$${(s.net_straddle_ask || 0).toFixed(2)}</td>
                        <td style="color: ${(s.pnl_realized || 0) >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)'};">$${(s.pnl_realized || 0).toFixed(2)}</td>
                        <td>${s.exit_reason || '-'}</td>
                    </tr>
                `).join("");
                if (straddleBody) straddleBody.innerHTML = rowsHtml;
                if (straddleViewBody) straddleViewBody.innerHTML = rowsHtml;
            }

            const hedgeBody = document.getElementById("hedge-sessions-table-body");
            const hedgeViewBody = document.getElementById("hedge-sessions-view-body");
            if (data.hedge.history && data.hedge.history.length > 0) {
                const hedgeRowsHtml = data.hedge.history.map(h => `
                    <tr>
                        <td><b>#${h.id}</b></td>
                        <td>${h.symbol}</td>
                        <td><span class="badge ${h.status === 'Open' ? 'badge-success' : 'badge-warning'}">${h.status}</span></td>
                        <td>$${(h.bull_entry || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>$${(h.bear_entry || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>$${(h.bull_exit || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>$${(h.bear_exit || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td style="color: ${(h.realized_pnl || 0) >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)'};">$${(h.realized_pnl || 0).toFixed(2)}</td>
                        <td>${h.exit_reason || '-'}</td>
                    </tr>
                `).join("");
                if (hedgeBody) hedgeBody.innerHTML = hedgeRowsHtml;
                if (hedgeViewBody) hedgeViewBody.innerHTML = hedgeRowsHtml;
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
    "FUTURES_SQUAREOFF", "STRADDLE_EXPIRY_TIME", "TRADE_QTY", "MAX_TOTAL_MARK", 
    "FUTURES_TP_MULTIPLIER", "FUTURES_LEVERAGE", "PAPER_WALLET_USDT"
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

            // Render Role-Based Hedge Strategy Cards (1st Trader vs 2nd Trader)
            const grid = document.getElementById("hedge-strategy-cards-grid");
            if (data.strategies && data.strategies.length > 0) {
                grid.innerHTML = data.strategies.map(s => {
                    const startStr = s.trade_start || "05:00";
                    const endStr = s.trade_end || "07:00";
                    const fcStr = s.force_close || "13:00";

                    return `
                    <div class="glass-panel" style="background: rgba(15, 23, 42, 0.7); border: 1px solid var(--border-subtle); padding: 24px; border-radius: 12px; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.37);">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid var(--border-subtle);">
                            <div style="font-weight: 700; font-size: 17px; color: var(--brand-cyan); display: flex; align-items: center; gap: 8px;">
                                🎯 <span>${s.strategy_name} Configuration</span>
                            </div>
                            <span class="badge ${s.enabled ? 'badge-success' : 'badge-danger'}">${s.enabled ? 'Role Active' : 'Role Disabled'}</span>
                        </div>

                        <form onsubmit="saveRoleStrategyConfig(event, ${s.id}, '${s.strategy_name}')" style="display: grid; grid-template-columns: 1fr 1fr; gap: 18px;">
                            <div class="form-group">
                                <label>Trade Window Open (HH:MM)</label>
                                <input type="text" id="role_${s.id}_start" value="${startStr}" placeholder="05:00" style="font-family: var(--font-mono); font-size: 14px;">
                            </div>

                            <div class="form-group">
                                <label>Trade Window Close (HH:MM)</label>
                                <input type="text" id="role_${s.id}_end" value="${endStr}" placeholder="07:00" style="font-family: var(--font-mono); font-size: 14px;">
                            </div>

                            <div class="form-group">
                                <label>Force Close Squareoff (HH:MM)</label>
                                <input type="text" id="role_${s.id}_fc" value="${fcStr}" placeholder="13:00" style="font-family: var(--font-mono); font-size: 14px; color: var(--accent-amber);">
                            </div>

                            <div class="form-group">
                                <label>Contract Quantity (BTC)</label>
                                <input type="number" step="0.1" id="role_${s.id}_qty" value="${s.contract_qty || 1.0}" style="font-family: var(--font-mono); font-size: 14px;">
                            </div>

                            <div class="form-group">
                                <label>Max Premium Limit ($)</label>
                                <input type="number" step="1.0" id="role_${s.id}_max_prem" value="${s.max_premium || 250.0}" style="font-family: var(--font-mono); font-size: 14px; color: var(--accent-emerald); font-weight: 600;">
                            </div>

                            <div class="form-group">
                                <label>Max Time Value Limit ($)</label>
                                <input type="number" step="1.0" id="role_${s.id}_max_tv" value="${s.max_time_value || 229.0}" style="font-family: var(--font-mono); font-size: 14px;">
                            </div>

                            <div style="grid-column: 1 / -1; margin-top: 12px; text-align: right;">
                                <button type="submit" class="btn-primary" style="font-size: 13px; padding: 10px 22px; width: 100%; border-radius: 6px;">💾 Save ${s.strategy_name} Role Settings</button>
                            </div>
                        </form>
                    </div>
                `}).join("");
            }
        }
    } catch (err) {
        console.error(err);
    }
}

async function saveRoleStrategyConfig(e, stratId, stratName) {
    e.preventDefault();
    const startParts = (document.getElementById(`role_${stratId}_start`).value || "05:00").split(":");
    const endParts = (document.getElementById(`role_${stratId}_end`).value || "07:00").split(":");
    const fcParts = (document.getElementById(`role_${stratId}_fc`).value || "13:00").split(":");

    const payload = {
        id: stratId,
        strategy_name: stratName,
        trade_start_h: parseInt(startParts[0] || "5"),
        trade_start_m: parseInt(startParts[1] || "0"),
        trade_end_h: parseInt(endParts[0] || "7"),
        trade_end_m: parseInt(endParts[1] || "0"),
        force_close_h: parseInt(fcParts[0] || "13"),
        force_close_m: parseInt(fcParts[1] || "0"),
        contract_qty: parseFloat(document.getElementById(`role_${stratId}_qty`).value),
        max_premium: parseFloat(document.getElementById(`role_${stratId}_max_prem`).value),
        max_time_value: parseFloat(document.getElementById(`role_${stratId}_max_tv`).value)
    };

    try {
        const res = await fetch("/api/v1/config/hedge/strategies", {
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
        alert(`Error saving role strategy config for ${stratName}`);
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
                    <td><span style="color: var(--accent-rose);">${l.old_value || '-'}</span></td>
                    <td><span style="color: var(--accent-emerald);">${l.new_value}</span></td>
                    <td><span class="badge ${l.apply_mode === 'IMMEDIATE' ? 'badge-success' : 'badge-warning'}">${l.apply_mode}</span></td>
                    <td><span class="badge badge-success">${l.status}</span></td>
                </tr>
            `).join("");
        }
    } catch (err) {
        console.error(err);
    }
}
