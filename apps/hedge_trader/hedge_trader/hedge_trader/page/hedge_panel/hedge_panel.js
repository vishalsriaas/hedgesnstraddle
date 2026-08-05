frappe.pages["hedge-panel"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Hedge Chart Panel"),
		single_column: true,
	});

	const state = {
		port: 8100,
		directPath: "/panel",
	};

	const root = $(wrapper).find(".layout-main-section");
	root.empty().addClass("runtime-panel-page");

	const frame = $(`
		<div class="runtime-panel-shell">
			<div class="runtime-panel-toolbar">
				<div>
					<div class="runtime-panel-title">${__("Hedge Trader")}</div>
					<div class="runtime-panel-url"></div>
				</div>
				<div class="runtime-panel-actions">
					<button class="btn btn-xs btn-default runtime-panel-open">${__("Open")}</button>
					<button class="btn btn-xs btn-default runtime-panel-refresh">${__("Refresh")}</button>
				</div>
			</div>
			<iframe class="runtime-panel-frame" title="${__("Hedge Chart Panel")}"></iframe>
		</div>
	`).appendTo(root);

	const iframe = frame.find("iframe")[0];
	const urlLabel = frame.find(".runtime-panel-url");

	const panel_url = () => {
		const protocol = window.location.protocol === "https:" ? "https:" : "http:";
		const host = window.location.hostname || "localhost";
		return `${protocol}//${host}:${state.port}${state.directPath}`;
	};

	const load = () => {
		const url = panel_url();
		urlLabel.text(url);
		iframe.src = url;
	};

	page.set_primary_action(__("Open"), () => window.open(panel_url(), "_blank"));
	page.add_action_item(__("Refresh"), load);
	page.add_action_item(__("Hedge Trader Workspace"), () => frappe.set_route("workspace", "Hedge Trader"));

	frame.find(".runtime-panel-open").on("click", () => window.open(panel_url(), "_blank"));
	frame.find(".runtime-panel-refresh").on("click", load);

	load();
};
