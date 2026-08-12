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
            document.getElementById("straddle-hero-spot").innerText = `$${(activeStraddle ? (activeStraddle.btc_entry_spot || btcSpot) : btcSpot).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            document.getElementById("straddle-hero-pnl").innerText = `${(activeStraddle ? (activeStraddle.pnl_realized || 0) : 0) >= 0 ? '+' : ''}$${(activeStraddle ? (activeStraddle.pnl_realized || 0) : 0).toFixed(2)}`;
            
            const liveNetPrem = (data.straddle.live_call_mark || 180.50) + (data.straddle.live_put_mark || 195.20);
            document.getElementById("straddle-hero-premium").innerText = `$${(activeStraddle ? (activeStraddle.net_straddle_ask || liveNetPrem) : liveNetPrem).toFixed(2)}`;
            
            // Update Straddle Live Monitor UI
            const monitor = data.straddle.live_monitoring || {};
            const serverTimeStr = monitor.server_time || '00:00:00';
            document.getElementById("straddle-live-time").innerText = `Server Time: ${serverTimeStr}`;
            
            const timeStatus = document.getElementById("cond-time-status");
            timeStatus.innerText = monitor.cond_time_window_valid ? "✅ Active" : "❌ Inactive";
            timeStatus.style.color = monitor.cond_time_window_valid ? "var(--accent-emerald)" : "var(--accent-rose)";

            const premiumStatus = document.getElementById("cond-premium-status");
            premiumStatus.innerText = monitor.cond_premium_valid ? "✅ Valid" : "❌ Limit Exceeded";
            premiumStatus.style.color = monitor.cond_premium_valid ? "var(--accent-emerald)" : "var(--accent-rose)";

            const gapStatus = document.getElementById("cond-gap-status");
            gapStatus.innerText = monitor.cond_premium_gap_valid ? "✅ Valid" : "❌ Gap Exceeded";
            gapStatus.style.color = monitor.cond_premium_gap_valid ? "var(--accent-emerald)" : "var(--accent-rose)";

            const weekendStatus = document.getElementById("cond-weekend-status");
            weekendStatus.innerText = monitor.cond_weekend_skip ? "❌ Skip Session (Weekend)" : "✅ Active Session";
            weekendStatus.style.color = monitor.cond_weekend_skip ? "var(--accent-rose)" : "var(--accent-emerald)";

            document.getElementById("straddle-short-limit").innerText = `$${(monitor.short_limit_price || 0.00).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
            document.getElementById("straddle-long-limit").innerText = `$${(monitor.long_limit_price || 0.00).toLocaleString(undefined, {minimumFractionDigits: 2})}`;

            // Real-time Countdown Timer Calculations
            function getSessionRelativeSecs(str) {
                if (!str) return 0;
                const parts = str.split(":").map(Number);
                const h = parts[0] || 0;
                const m = parts[1] || 0;
                const s = parts[2] || 0;
                const totalSecs = h * 3600 + m * 60 + s;
                
                // Expiry Session boundary starts at 13:31:00 IST = 48660 seconds
                const sessionStartSecs = 13 * 3600 + 31 * 60;
                
                if (totalSecs >= sessionStartSecs) {
                    return totalSecs - sessionStartSecs;
                } else {
                    return (totalSecs + 24 * 3600) - sessionStartSecs;
                }
            }
            
            const currentRelSecs = getSessionRelativeSecs(serverTimeStr);
            
            function calculateCountdown(targetTimeStr) {
                if (!targetTimeStr) return "00:00:00";
                let targetRelSecs = getSessionRelativeSecs(targetTimeStr);
                let diff = targetRelSecs - currentRelSecs;
                if (diff < 0) {
                    return "00:00:00";
                }
                const hr = Math.floor(diff / 3600);
                const mn = Math.floor((diff % 3600) / 60);
                const sc = diff % 60;
                return `${String(hr).padStart(2, '0')}:${String(mn).padStart(2, '0')}:${String(sc).padStart(2, '0')}`;
            }

            const wStart = monitor.window_start;
            const wEnd = monitor.window_end;
            const cutoff = monitor.futures_entry_cutoff;
            const sqEnd = monitor.sq_end;

            // 1. Time To Open
            if (currentRelSecs < getSessionRelativeSecs(wStart)) {
                document.getElementById("timer-to-open").innerText = calculateCountdown(wStart);
            } else {
                document.getElementById("timer-to-open").innerText = "Closed / Active";
            }

            // 2. Window Time Left
            if (currentRelSecs >= getSessionRelativeSecs(wStart) && currentRelSecs <= getSessionRelativeSecs(wEnd)) {
                document.getElementById("timer-window-left").innerText = calculateCountdown(wEnd);
            } else {
                document.getElementById("timer-window-left").innerText = "00:00:00";
            }

            // 3. Hedge Cutoff Timer
            if (currentRelSecs <= getSessionRelativeSecs(cutoff)) {
                document.getElementById("timer-cutoff-left").innerText = calculateCountdown(cutoff);
            } else {
                document.getElementById("timer-cutoff-left").innerText = "Cutoff Reached";
            }

            // 4. Squareoff Timer
            if (currentRelSecs <= getSessionRelativeSecs(sqEnd)) {
                document.getElementById("timer-sq-left").innerText = calculateCountdown(sqEnd);
            } else {
                document.getElementById("timer-sq-left").innerText = "Squareoff Reached";
            }

            const liveCallAsk = data.straddle.live_call_mark || 180.50;
            const livePutAsk = data.straddle.live_put_mark || 195.20;

            if (activeStraddle) {
                // Use trade_qty from API (actual configured qty), not a hardcoded fallback
                const qty = parseFloat(data.straddle.trade_qty || activeStraddle.qty || 0.1);

                // === CALL LEG ===
                const callEntryPrice = activeStraddle.call_ask || 0;   // stored as mark price at entry
                document.getElementById("call-strike-val").innerText = activeStraddle.call_strike || "-";
                document.getElementById("call-entry-val").innerText = callEntryPrice > 0 ? `$${callEntryPrice.toFixed(2)}` : "-";
                document.getElementById("call-ask-val").innerText = `$${liveCallAsk.toFixed(2)}`;

                // Call PnL = (live mark - entry mark) × qty
                const callPnl = (liveCallAsk - callEntryPrice) * qty;
                const callPnlEl = document.getElementById("call-pnl-val");
                callPnlEl.innerText = `${callPnl >= 0 ? '+' : ''}$${callPnl.toFixed(2)}`;
                callPnlEl.style.color = callPnl >= 0 ? "var(--accent-emerald)" : "var(--accent-rose)";

                // === PUT LEG ===
                const putEntryPrice = activeStraddle.put_ask || 0;    // stored as mark price at entry
                document.getElementById("put-strike-val").innerText = activeStraddle.put_strike || "-";
                document.getElementById("put-entry-val").innerText = putEntryPrice > 0 ? `$${putEntryPrice.toFixed(2)}` : "-";
                document.getElementById("put-ask-val").innerText = `$${livePutAsk.toFixed(2)}`;

                // Put PnL = (live mark - entry mark) × qty
                const putPnl = (livePutAsk - putEntryPrice) * qty;
                const putPnlEl = document.getElementById("put-pnl-val");
                putPnlEl.innerText = `${putPnl >= 0 ? '+' : ''}$${putPnl.toFixed(2)}`;
                putPnlEl.style.color = putPnl >= 0 ? "var(--accent-emerald)" : "var(--accent-rose)";

                // === FUTURES LEG ===
                let futPnl = 0.0;
                if (activeStraddle.futures_entry_price) {
                    // futSide: +1 = long (expecting price to rise), -1 = short (expecting price to fall)
                    const futSide = (activeStraddle.futures_tp_price > activeStraddle.futures_entry_price) ? 1 : -1;
                    // Futures PnL = direction × (current mark - entry) × qty
                    futPnl = futSide * (btcMark - activeStraddle.futures_entry_price) * qty;
                }
                const futPnlEl = document.getElementById("fut-pnl-val");
                futPnlEl.innerText = `${futPnl >= 0 ? '+' : ''}$${futPnl.toFixed(2)}`;
                futPnlEl.style.color = futPnl >= 0 ? "var(--accent-emerald)" : "var(--accent-rose)";

                document.getElementById("fut-entry-val").innerText = activeStraddle.futures_entry_price ? `$${activeStraddle.futures_entry_price.toLocaleString(undefined, {minimumFractionDigits: 2})}` : '-';
                document.getElementById("fut-tp-val").innerText = activeStraddle.futures_tp_price ? `$${activeStraddle.futures_tp_price.toLocaleString(undefined, {minimumFractionDigits: 2})}` : '-';

                // === NET SESSION PnL (call + put + futures) ===
                const netSessionPnl = callPnl + putPnl + futPnl;
                const sessionPnlEl = document.getElementById("straddle-hero-pnl");
                sessionPnlEl.innerText = `${netSessionPnl >= 0 ? '+' : ''}$${netSessionPnl.toFixed(2)}`;
                sessionPnlEl.style.color = netSessionPnl >= 0 ? "var(--accent-emerald)" : "var(--accent-rose)";

            } else {
                document.getElementById("call-strike-val").innerText = data.straddle.live_call_strike || (Math.round(btcSpot / 100) * 100);
                document.getElementById("call-entry-val").innerText = "-";
                document.getElementById("call-ask-val").innerText = `$${liveCallAsk.toFixed(2)}`;
                document.getElementById("call-pnl-val").innerText = "+$0.00";
                document.getElementById("call-pnl-val").style.color = "var(--text-muted)";

                document.getElementById("put-strike-val").innerText = data.straddle.live_put_strike || (Math.round(btcSpot / 100) * 100);
                document.getElementById("put-entry-val").innerText = "-";
                document.getElementById("put-ask-val").innerText = `$${livePutAsk.toFixed(2)}`;
                document.getElementById("put-pnl-val").innerText = "+$0.00";
                document.getElementById("put-pnl-val").style.color = "var(--text-muted)";

                document.getElementById("fut-entry-val").innerText = "-";
                document.getElementById("fut-tp-val").innerText = "-";
                document.getElementById("fut-pnl-val").innerText = "+$0.00";
                document.getElementById("fut-pnl-val").style.color = "var(--text-muted)";

                document.getElementById("straddle-hero-pnl").innerText = "+$0.00";
                document.getElementById("straddle-hero-pnl").style.color = "var(--accent-emerald)";
            }
            document.getElementById("straddle-hero-wallet").innerText = `$${data.wallet.paper_wallet_usdt.toLocaleString(undefined, {minimumFractionDigits: 2})}`;

            // Update Hedge Hero Cards
            const activeHedge = data.hedge.active_session;
            if (activeHedge) {
                document.getElementById("hedge-hero-bull").innerText = `LONG @ $${(activeHedge.bull_entry || (btcSpot - 50)).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("hedge-hero-bear").innerText = `SHORT @ $${(activeHedge.bear_entry || (btcSpot + 50)).toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("hedge-hero-total-pnl").innerText = `${(activeHedge.realized_pnl || 0) >= 0 ? '+' : ''}$${(activeHedge.realized_pnl || 0).toFixed(2)}`;
            } else {
                const bullPrice = data.hedge.live_bull_entry || (btcSpot - 50);
                const bearPrice = data.hedge.live_bear_entry || (btcSpot + 50);
                document.getElementById("hedge-hero-bull").innerText = `LONG @ $${bullPrice.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("hedge-hero-bear").innerText = `SHORT @ $${bearPrice.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
                document.getElementById("hedge-hero-total-pnl").innerText = `+$0.00`;
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
                if (straddleBody && straddleBody.innerHTML !== rowsHtml) straddleBody.innerHTML = rowsHtml;
                if (straddleViewBody && straddleViewBody.innerHTML !== rowsHtml) straddleViewBody.innerHTML = rowsHtml;
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
                if (hedgeBody && hedgeBody.innerHTML !== hedgeRowsHtml) hedgeBody.innerHTML = hedgeRowsHtml;
                if (hedgeViewBody && hedgeViewBody.innerHTML !== hedgeRowsHtml) hedgeViewBody.innerHTML = hedgeRowsHtml;
            }

function formatDisplayTime(dtStr) {
    if (!dtStr) return '-';
    return String(dtStr).replace("T", " ").slice(0, 19);
}

            // Render Straddle Trade Orders & Fills Table
            const straddleOrdersViewBody = document.getElementById("straddle-orders-view-body");
            if (data.straddle.orders && data.straddle.orders.length > 0) {
                const ordersHtml = data.straddle.orders.map(o => {
                    let statusClass = "badge-info";
                    if (o.status === "FILLED") statusClass = "badge-success";
                    else if (o.status === "PENDING") statusClass = "badge-warning";
                    else if (o.status === "CANCELLED" || o.status === "EXPIRED") statusClass = "badge-danger";

                    const timeStr = formatDisplayTime(o.created_at);

                    return `
                    <tr>
                        <td><b>#${o.id}</b></td>
                        <td>Session #${o.session_id}</td>
                        <td><code>${o.symbol}</code></td>
                        <td><span class="badge ${o.side === 'BUY' ? 'badge-success' : 'badge-danger'}">${o.side}</span></td>
                        <td><span class="badge badge-info">${o.leg_label || 'OPTION'}</span></td>
                        <td>${o.qty}</td>
                        <td>$${(o.price || 0).toFixed(2)}</td>
                        <td><span class="badge ${statusClass}">${o.status}</span></td>
                        <td style="color: var(--text-muted); font-size: 12px;">${timeStr}</td>
                    </tr>
                    `;
                }).join("");
                if (straddleOrdersViewBody && straddleOrdersViewBody.innerHTML !== ordersHtml) straddleOrdersViewBody.innerHTML = ordersHtml;
            } else if (straddleOrdersViewBody) {
                const emptyOrdersHtml = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted);">No trade orders submitted yet.</td></tr>';
                if (straddleOrdersViewBody.innerHTML !== emptyOrdersHtml) straddleOrdersViewBody.innerHTML = emptyOrdersHtml;
            }

            // Render Straddle Wallet Ledger Table
            const straddleLedgerViewBody = document.getElementById("straddle-ledger-view-body");
            if (data.straddle.ledger && data.straddle.ledger.length > 0) {
                const ledgerHtml = data.straddle.ledger.map(l => {
                    const timeStr = formatDisplayTime(l.created_at);
                    return `
                    <tr>
                        <td><b>#${l.id}</b></td>
                        <td>Session #${l.session_id || '-'}</td>
                        <td><span class="badge badge-info">${l.entry_type}</span></td>
                        <td style="color: ${(l.amount || 0) >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)'};">${(l.amount || 0) >= 0 ? '+' : ''}$${(l.amount || 0).toFixed(2)}</td>
                        <td>$${(l.balance_after || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td style="font-size: 12px;">${l.detail || l.description || '-'}</td>
                    </tr>
                    `;
                }).join("");
                if (straddleLedgerViewBody && straddleLedgerViewBody.innerHTML !== ledgerHtml) straddleLedgerViewBody.innerHTML = ledgerHtml;
            } else if (straddleLedgerViewBody) {
                const emptyLedgerHtml = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No wallet ledger entries logged.</td></tr>';
                if (straddleLedgerViewBody.innerHTML !== emptyLedgerHtml) straddleLedgerViewBody.innerHTML = emptyLedgerHtml;
            }

            // Render Hedge Open Positions Table
            const hedgePositionsViewBody = document.getElementById("hedge-positions-view-body");
            if (data.hedge.positions && data.hedge.positions.length > 0) {
                const posHtml = data.hedge.positions.map(p => `
                    <tr>
                        <td><b>#${p.id}</b></td>
                        <td>${p.symbol}</td>
                        <td><span class="badge ${p.side === 'BUY' ? 'badge-success' : 'badge-danger'}">${p.side}</span></td>
                        <td>$${(p.entry_price || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
                        <td>${p.qty}</td>
                        <td>${p.leverage}x</td>
                        <td style="color: ${(p.unrealized_pnl || 0) >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)'};">$${(p.unrealized_pnl || 0).toFixed(2)}</td>
                    </tr>
                `).join("");
                if (hedgePositionsViewBody && hedgePositionsViewBody.innerHTML !== posHtml) hedgePositionsViewBody.innerHTML = posHtml;
            } else if (hedgePositionsViewBody) {
                const emptyPosHtml = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No open hedge positions active.</td></tr>';
                if (hedgePositionsViewBody.innerHTML !== emptyPosHtml) hedgePositionsViewBody.innerHTML = emptyPosHtml;
            }
                hedgePositionsViewBody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">No open hedge positions active.</td></tr>';
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
    "MAX_PREMIUM_GAP", "SKIP_WEEKENDS", "FUTURES_TP_MULTIPLIER", "FUTURES_LEVERAGE", "PAPER_WALLET_USDT"
];

const STRADDLE_DEFAULTS = {
    "WINDOW_START": "05:00",
    "WINDOW_END": "07:30",
    "FUTURES_ENTRY_CUTOFF": "11:00",
    "SQ_START": "11:00",
    "SQ_END": "12:30",
    "FUTURES_SQUAREOFF": "12:30",
    "STRADDLE_EXPIRY_TIME": "13:30",
    "TRADE_QTY": "10",
    "MAX_TOTAL_MARK": "400.0",
    "SKIP_WEEKENDS": "1",
    "MAX_PREMIUM_GAP": "150.0",
    "FUTURES_TP_MULTIPLIER": "2",
    "FUTURES_LEVERAGE": "10",
    "PAPER_WALLET_USDT": "100000.0"
};

async function loadStraddleConfig() {
    try {
        const res = await fetch("/api/v1/config/straddle", {
            headers: { "Authorization": `Bearer ${authToken}` }
        });
        if (res.ok) {
            const data = await res.json();
            for (let [k, v] of Object.entries(data.active)) {
                const input = document.getElementById(`cfg_straddle_${k}`);
                if (input) {
                    // Leave blank (show placeholder) when value is the system default
                    if (STRADDLE_DEFAULTS[k] && String(v) === String(STRADDLE_DEFAULTS[k])) {
                        input.value = "";
                    } else {
                        input.value = v;
                    }
                }
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
        if (input) {
            // If blank, use system default; otherwise use user value
            const val = input.value.trim();
            payload[k] = (val === "" && STRADDLE_DEFAULTS[k]) ? STRADDLE_DEFAULTS[k] : val;
        }
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
