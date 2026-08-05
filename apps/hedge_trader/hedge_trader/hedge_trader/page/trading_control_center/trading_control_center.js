frappe.pages["trading-control-center"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Trading Control Center"),
		single_column: true,
	});
	const root = $(wrapper).find(".layout-main-section").addClass("tcc-page");
	const state = { timer: null, loading: false, data: null };

	const esc = (value) => $("<div>").text(value == null ? "—" : String(value)).html();
	const money = (value) => {
		const number = Number(value || 0);
		return `${number < 0 ? "−" : ""}$${Math.abs(number).toLocaleString(undefined, {
			minimumFractionDigits: 2, maximumFractionDigits: 2,
		})}`;
	};
	const badge = (text, good) =>
		`<span class="tcc-badge ${good ? "good" : "bad"}">${esc(text)}</span>`;
	const table = (columns, rows) => `
		<div class="tcc-table-wrap"><table class="table table-sm">
			<thead><tr>${columns.map((column) => `<th>${esc(column.label)}</th>`).join("")}</tr></thead>
			<tbody>${rows.length ? rows.map((row) => `<tr>${columns.map((column) =>
				`<td>${column.render ? column.render(row[column.key], row) : esc(row[column.key])}</td>`
			).join("")}</tr>`).join("") : `<tr><td colspan="${columns.length}" class="text-muted">No records</td></tr>`}</tbody>
		</table></div>`;

	const commandButtons = (algo) => `
		<div class="tcc-actions">
			<button class="btn btn-xs btn-danger tcc-command" data-algo="${algo}" data-command="EMERGENCY_SQUARE_OFF">${__("Emergency Square-off")}</button>
		</div>`;

	const render = (data) => {
		state.data = data;
		const health = data.health || {};
		const hedge = data.hedge || {};
		const straddle = data.straddle || {};
		const issues = data.audit_issues || [];
		const components = health.components || [];
		const canOperate = data.permissions && data.permissions.can_operate;
		const settings = data.settings || {};
		const hSettings = settings.hedge || {};
		const sSettings = settings.straddle || {};
		root.html(`
			<div class="tcc-topline">
				<div>${badge(health.healthy ? "SYSTEM HEALTHY" : "ATTENTION REQUIRED", health.healthy)}</div>
				<div class="text-muted">${__("Snapshot")}: ${esc(data.generated_at)}</div>
			</div>
			<div class="tcc-grid tcc-metrics">
				<div class="tcc-card"><div class="label">MariaDB</div><div class="value">${health.database && health.database.healthy ? "Connected" : "Incomplete"}</div></div>
				<div class="tcc-card"><div class="label">Hedge Open Positions</div><div class="value">${(hedge.positions || []).length}</div></div>
				<div class="tcc-card"><div class="label">Straddle Wallet</div><div class="value">${money(straddle.balance)}</div></div>
				<div class="tcc-card"><div class="label">Audit Issues</div><div class="value ${issues.length ? "danger" : ""}">${issues.length}</div></div>
			</div>
			<section class="tcc-section">
				<div class="tcc-section-head"><h4>System Configurations</h4></div>
				<div class="tcc-config-summary" style="display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 15px; padding: 12px; background: var(--control-bg, #f8f9fa); border-radius: 4px; border: 1px solid var(--border-color, #e2e8f0); font-size: 13px;">
					<div>
						<strong>Hedge Settings:</strong>
						Engine: ${badge(hSettings.engine_enabled ? "Enabled" : "Disabled", hSettings.engine_enabled)} |
						Pause: ${badge(hSettings.global_pause ? "PAUSED" : "Active", !hSettings.global_pause)} |
						Mode: <span class="text-muted">${esc(hSettings.runtime_mode)} (${hSettings.paper_trading_enabled ? "Paper" : "Live"})</span>
					</div>
					<div style="border-left: 1px solid var(--border-color, #e2e8f0); padding-left: 20px;">
						<strong>Straddle Settings:</strong>
						Bot: ${badge(sSettings.bot_enabled ? "Enabled" : "Disabled", sSettings.bot_enabled)} |
						Mode: <span class="text-muted">${esc(sSettings.runtime_mode)} (${sSettings.paper_trading_enabled ? "Paper" : "Live"})</span>
					</div>
					<div style="border-left: 1px solid var(--border-color, #e2e8f0); padding-left: 20px;">
						<strong>Hedge Strategies:</strong>
						${settings.hedge_strategies && settings.hedge_strategies.length ? settings.hedge_strategies.map(s => `${esc(s.strategy_name)}: ${badge(s.enabled ? "Enabled" : "Disabled", s.enabled)}`).join(" | ") : '<span class="text-muted">None</span>'}
					</div>
				</div>
			</section>
			<section class="tcc-section">
				<div class="tcc-section-head"><h4>Runtime Health</h4></div>
				${table([
					{key:"algo",label:"Algo"}, {key:"component",label:"Component"},
					{key:"status",label:"Status",render:(v,r)=>badge(v,r.healthy)},
					{key:"age_seconds",label:"Age (sec)"}, {key:"worker_id",label:"Worker"},
					{key:"summary",label:"Summary"},
				], components)}
			</section>
			<section class="tcc-section">
				<div class="tcc-section-head"><div><h4>Hedge Traders</h4><small>MariaDB recovery state and immutable fills</small></div>${canOperate ? commandButtons("hedge") : ""}</div>
				${table([
					{key:"trader_name",label:"Trader"}, {key:"session_id",label:"Session"},
					{key:"status",label:"Status"}, {key:"entry_price",label:"Entry",render:money},
					{key:"futures_pnl",label:"Futures P&L",render:money},
					{key:"hedge_pnl",label:"Option P&L",render:money},
					{key:"total_pnl",label:"Total P&L",render:money},
				], hedge.sessions || [])}
			</section>
			<section class="tcc-section">
				<div class="tcc-section-head"><div><h4>Straddle Trader</h4><small>Wallet, options and futures session accounting</small></div>${canOperate ? commandButtons("straddle") : ""}</div>
				${table([
					{key:"id",label:"Session"}, {key:"expiry_dt",label:"Expiry"}, {key:"state",label:"State"},
					{key:"total_premium",label:"Premium",render:money},
					{key:"long_entry",label:"Long Entry",render:money},
					{key:"short_entry",label:"Short Entry",render:money},
					{key:"options_pnl",label:"Options P&L",render:money},
					{key:"futures_pnl",label:"Futures P&L",render:money},
					{key:"net_pnl",label:"Net P&L",render:money},
				], straddle.sessions || [])}
			</section>
			<section class="tcc-section">
				<div class="tcc-section-head"><h4>Self-Audit</h4></div>
				${table([
					{key:"severity",label:"Severity",render:(v)=>badge(v,v!=="Critical")},
					{key:"algo",label:"Algo"}, {key:"type",label:"Issue"},
					{key:"reference",label:"Reference"}, {key:"detail",label:"Detail"},
				], issues)}
			</section>
		`);
		root.find(".tcc-command").on("click", async function () {
			const algo = this.dataset.algo;
			const command = this.dataset.command;
			const destructive = command.includes("SQUARE");
			const submit = () => frappe.call({
				method: "hedge_trader.trading.control_center.issue_command",
				type: "POST",
				args: {algo, command, confirmed: destructive ? 1 : 0},
				freeze: true,
				callback: () => {
					frappe.show_alert({message: __("Command queued; waiting for worker acknowledgement."), indicator: "orange"});
					load();
				},
			});
			if (destructive) {
				frappe.confirm(
					__("This queues an emergency square-off for {0}. Continue?", [algo]),
					submit
				);
			} else {
				submit();
			}
		});
	};

	const load = () => {
		if (state.loading) return;
		state.loading = true;
		frappe.call({
			method: "hedge_trader.trading.control_center.get_dashboard",
			callback: (response) => render(response.message || {}),
			always: () => { state.loading = false; },
		});
	};
	page.set_primary_action(__("Refresh"), load);
	page.add_action_item(__("Hedge Panel"), () => frappe.set_route("hedge-panel"));
	page.add_action_item(__("Straddle Dashboard"), () => frappe.set_route("straddle-dashboard"));
	load();
	state.timer = setInterval(load, 15000);
	$(wrapper).on("remove", () => clearInterval(state.timer));
};
