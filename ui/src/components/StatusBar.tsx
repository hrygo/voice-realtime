import { useState, useEffect, useCallback } from "react";
import { applyTheme, useUISettingsStore, type Theme } from "../stores/uiSettingsStore";
import "../App.css";

type ServiceStatus = "ok" | "unreachable" | "timeout" | "error" | "checking";

interface ServiceInfo {
	name: string;
	status: ServiceStatus;
	url: string;
}

interface ServicesResponse {
	services: ServiceInfo[];
}

const STATUS_LABELS: Record<ServiceStatus, string> = {
	ok: "运行中",
	unreachable: "不可达",
	timeout: "超时",
	error: "异常",
	checking: "检测中",
};

const STATUS_COLORS: Record<ServiceStatus, string> = {
	ok: "var(--green)",
	unreachable: "var(--red)",
	timeout: "var(--yellow)",
	error: "var(--red)",
	checking: "var(--text-secondary)",
};

const THEME_LABELS: Record<Theme, string> = {
	light: "☀️",
	dark: "🌙",
	system: "💻",
};

const THEME_TITLES: Record<Theme, string> = {
	light: "亮色主题",
	dark: "暗色主题",
	system: "跟随系统",
};

const THEME_CYCLE: readonly Theme[] = ["light", "dark", "system"];

function ServiceLight({ name, status }: { name: string; status: ServiceStatus }) {
	const color = STATUS_COLORS[status];
	return (
		<div className="service-light">
			<span
				className="light-dot"
				style={{ backgroundColor: color }}
				title={STATUS_LABELS[status]}
			/>
			<span className="light-label">{name}</span>
		</div>
	);
}

/** 主题切换按钮：点击循环 light → dark → system。 */
function ThemeToggle() {
	const theme = useUISettingsStore((s) => s.theme);
	const setTheme = useUISettingsStore((s) => s.setTheme);

	const cycle = useCallback(() => {
		const current = THEME_CYCLE.indexOf(theme);
		const next = THEME_CYCLE[(current + 1) % THEME_CYCLE.length];
		if (next) setTheme(next);
	}, [theme, setTheme]);

	return (
		<button
			type="button"
			className="theme-toggle"
			onClick={cycle}
			aria-label={THEME_TITLES[theme]}
			title={THEME_TITLES[theme]}
		>
			{THEME_LABELS[theme]}
		</button>
	);
}

export default function StatusBar() {
	const [services, setServices] = useState<ServiceInfo[]>([]);
	const [loading, setLoading] = useState(true);

	/** 应用主题到 documentElement，监听系统偏好变化。 */
	useEffect(() => {
		const store = useUISettingsStore.getState();
		applyTheme(store.theme);

		const media = window.matchMedia("(prefers-color-scheme: dark)");
		const onChange = () => {
			const current = useUISettingsStore.getState();
			if (current.theme === "system") applyTheme("system");
		};
		media.addEventListener("change", onChange);
		return () => media.removeEventListener("change", onChange);
	}, []);

	const fetchServices = useCallback(async () => {
		try {
			const resp = await fetch("/api/services");
			const data: ServicesResponse = await resp.json();
			setServices(data.services);
		} catch {
			// 探活失败不阻塞 UI
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		fetchServices();
		const interval = setInterval(fetchServices, 10_000);
		return () => clearInterval(interval);
	}, [fetchServices]);

	if (loading && services.length === 0) {
		return (
			<header className="status-bar">
				<span className="loading-text">加载中…</span>
			</header>
		);
	}

	return (
		<header className="status-bar">
			<h1 className="status-title">Voice Studio</h1>
			<div className="status-lights">
				{services.map((s) => (
					<ServiceLight key={s.name} name={s.name} status={s.status} />
				))}
			</div>
			<div className="status-actions">
				<ThemeToggle />
			</div>
		</header>
	);
}