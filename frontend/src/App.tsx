import { useEffect, useState } from "react";

type Health = { status: string; database: string; storage: string };

// Заглушка дня 1: показывает, что SPA собрана и API доступно через nginx.
export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json() as Promise<Health>)
      .then(setHealth)
      .catch(() => setError(true));
  }, []);

  return (
    <main style={{ fontFamily: "Roboto, Arial, sans-serif", padding: 24 }}>
      <h1>YouTube Clone</h1>
      <p>Интерфейс в разработке.</p>
      <p>
        Состояние API:{" "}
        {error ? "недоступно" : health ? `${health.status} (БД: ${health.database}, хранилище: ${health.storage})` : "проверка…"}
      </p>
    </main>
  );
}
