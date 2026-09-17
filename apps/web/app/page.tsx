const milestones = [
  ["M0", "Engineering baseline", "active"],
  ["M1", "Auth / Tenant / RBAC", "next"],
  ["M2", "Model Gateway", "next"],
  ["M3", "Reliable Knowledge Hub", "next"],
];

export default function Home() {
  return (
    <main className="shell">
      <section className="hero">
        <p className="eyebrow">ENTERPRISE AGENT RUNTIME &amp; CONTROL PLANE</p>
        <h1>AgentHub</h1>
        <p className="lede">
          A staged foundation for explainable, durable and evaluable enterprise agents.
        </p>
      </section>
      <section className="card" aria-labelledby="status-title">
        <div className="card-header">
          <div>
            <p className="eyebrow">DELIVERY STATUS</p>
            <h2 id="status-title">Milestone roadmap</h2>
          </div>
          <span className="badge">M0 IN PROGRESS</span>
        </div>
        <div className="milestones">
          {milestones.map(([id, label, status]) => (
            <div className={`milestone ${status}`} key={id}>
              <span className="milestone-id">{id}</span>
              <span>{label}</span>
              <span className="milestone-status">{status === "active" ? "Active" : "Queued"}</span>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
