import { useState, useEffect, useCallback } from "react";
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

export default function StatusBar() {
  const [services, setServices] = useState<ServiceInfo[]>([]);
  const [loading, setLoading] = useState(true);

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
    </header>
  );
}
