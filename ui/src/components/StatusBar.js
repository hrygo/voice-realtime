import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState, useEffect, useCallback } from "react";
import "../App.css";
const STATUS_LABELS = {
    ok: "运行中",
    unreachable: "不可达",
    timeout: "超时",
    error: "异常",
    checking: "检测中",
};
const STATUS_COLORS = {
    ok: "var(--green)",
    unreachable: "var(--red)",
    timeout: "var(--yellow)",
    error: "var(--red)",
    checking: "var(--text-secondary)",
};
function ServiceLight({ name, status }) {
    const color = STATUS_COLORS[status];
    return (_jsxs("div", { className: "service-light", children: [_jsx("span", { className: "light-dot", style: { backgroundColor: color }, title: STATUS_LABELS[status] }), _jsx("span", { className: "light-label", children: name })] }));
}
export default function StatusBar() {
    const [services, setServices] = useState([]);
    const [loading, setLoading] = useState(true);
    const fetchServices = useCallback(async () => {
        try {
            const resp = await fetch("/api/services");
            const data = await resp.json();
            setServices(data.services);
        }
        catch {
            // 探活失败不阻塞 UI
        }
        finally {
            setLoading(false);
        }
    }, []);
    useEffect(() => {
        fetchServices();
        const interval = setInterval(fetchServices, 10_000);
        return () => clearInterval(interval);
    }, [fetchServices]);
    if (loading && services.length === 0) {
        return (_jsx("header", { className: "status-bar", children: _jsx("span", { className: "loading-text", children: "\u52A0\u8F7D\u4E2D\u2026" }) }));
    }
    return (_jsxs("header", { className: "status-bar", children: [_jsx("h1", { className: "status-title", children: "Voice Studio" }), _jsx("div", { className: "status-lights", children: services.map((s) => (_jsx(ServiceLight, { name: s.name, status: s.status }, s.name))) })] }));
}
