import { useState } from 'react';
import { createRoot } from 'react-dom/client';

type PlanNode = { id: number; type: string; relation: string | null; depth: number; self_ms: number; total_ms: number; est_rows: number; actual_rows: number; loops: number };
type Finding = { severity: 'high' | 'medium' | 'low'; kind: string; node_id: number; message: string; suggestion: string | null };
type Report = { execution_ms: number; planning_ms: number; nodes: PlanNode[]; findings: Finding[] };

const SEV = { high: '#c62828', medium: '#945700', low: '#0277bd' };
const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${n.toFixed(1)} ms`);

function App() {
  const [input, setInput] = useState('');
  const [mode, setMode] = useState<'plan' | 'sql'>('plan');
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<number | null>(null);

  async function run() {
    setError(null);
    try {
      const body = mode === 'plan' ? { plan: JSON.parse(input) } : { sql: input };
      const r = await fetch(mode === 'plan' ? '/analyze' : '/explain', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail ?? r.statusText);
      setReport(j);
    } catch (e) { setError((e as Error).message); }
  }

  const max = report ? Math.max(...report.nodes.map((n) => n.self_ms), 0.001) : 1;
  const flagged = new Map(report?.findings.map((f) => [f.node_id, f.severity]) ?? []);

  return (
    <main style={{ fontFamily: 'system-ui', padding: 16, maxWidth: 1100, margin: '0 auto' }}>
      <h1>QueryScope</h1>
      <div>
        <label><input type="radio" checked={mode === 'plan'} onChange={() => setMode('plan')} /> Paste EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)</label>{' '}
        <label><input type="radio" checked={mode === 'sql'} onChange={() => setMode('sql')} /> Run a SELECT on the replica</label>
      </div>
      <textarea value={input} onChange={(e) => setInput(e.target.value)} rows={10} style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }} />
      <button onClick={run}>Analyze</button>
      {error && <p style={{ color: '#c62828' }}>{error}</p>}
      {report && (
        <>
          <p>Execution <b>{fmt(report.execution_ms)}</b> · planning {fmt(report.planning_ms)} · {report.findings.length} finding(s)</p>
          {report.findings.map((f, i) => (
            <div key={i} onMouseEnter={() => setFocus(f.node_id)} style={{ borderLeft: `4px solid ${SEV[f.severity]}`, padding: '6px 10px', margin: '6px 0', background: '#fafafa' }}>
              <div>{f.message}</div>
              {f.suggestion && <code style={{ display: 'block', marginTop: 4, color: '#1f5fbf' }}>{f.suggestion}</code>}
            </div>
          ))}
          <table style={{ width: '100%', fontSize: 13, marginTop: 12, borderCollapse: 'collapse' }}>
            <thead><tr><th align="left">Node</th><th align="right">Self</th><th style={{ width: '30%' }} /><th align="right">Rows (est → actual)</th><th align="right">Loops</th></tr></thead>
            <tbody>
              {report.nodes.map((n) => (
                <tr key={n.id} style={{ background: focus === n.id ? '#eef4ff' : undefined }}>
                  <td style={{ paddingLeft: 8 + n.depth * 16 }}>
                    {flagged.has(n.id) && <span style={{ color: SEV[flagged.get(n.id)!] }}>● </span>}
                    {n.type}{n.relation ? <b> {n.relation}</b> : null}
                  </td>
                  <td align="right">{fmt(n.self_ms)}</td>
                  <td><div style={{ height: 8, background: '#c62828', opacity: 0.25 + 0.75 * (n.self_ms / max), width: `${(n.self_ms / max) * 100}%` }} /></td>
                  <td align="right">{Math.round(n.est_rows).toLocaleString()} → {Math.round(n.actual_rows).toLocaleString()}</td>
                  <td align="right">{n.loops}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
