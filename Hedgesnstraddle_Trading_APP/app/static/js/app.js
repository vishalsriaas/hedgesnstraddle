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
    ["WINDOW_START", "WINDOW_END", "FUTURES_ENTRY_CUTOFF", "SQ_START", "SQ_END", "TRADE_QTY", "FUTURES_LEVERAGE", "PAPER_WALLET_USDT"].forEach(k => {
        const val = document.getElementById(`cfg_straddle_${k}`).value;
        if (val) payload[k] = val;
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
        }
    } catch (err) {
        console.error(err);
    }
}

async function saveHedgeConfig(e) {
    e.preventDefault();
    const payload = {};
    ["SYMBOL", "LEVERAGE", "TRADE_QTY", "BULL_TARGET_PCT", "BEAR_TARGET_PCT"].forEach(k => {
        const val = document.getElementById(`cfg_hedge_${k}`).value;
        if (val) payload[k] = val;
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
