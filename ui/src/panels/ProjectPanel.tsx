// Project browser: the projects the daemon knows, open one onto the canvas, create one with its custody setting (NDA
// toggle and allowed grades) and engine pins. An NDA project allows C0 and C1 routes only, the same rule the daemon
// enforces (`plate_daemon.projects.check_custody`); the form simply cannot ask for more.

import { type FormEvent, type ReactElement, useState } from "react";
import { useGraphStore } from "../store";
import { createProject, errorMessage, getProjectGraph, listProjects } from "./api";
import { formatTime } from "./format";
import { usePanelsStore } from "./store";
import { C, GRADE_COLOR, S, tint } from "./theme";
import { CUSTODY_GRADES, type CustodyGrade, NDA_GRADES, type ProjectSummary } from "./types";
import { useLoad } from "./useLoad";

export const PROJECT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

function GradeChips({ grades }: { grades: readonly CustodyGrade[] }): ReactElement {
  return (
    <span style={S.row}>
      {grades.map((g) => (
        <span key={g} style={tint(GRADE_COLOR[g])}>
          {g}
        </span>
      ))}
    </span>
  );
}

function PinsTable({ pins }: { pins: Record<string, string> }): ReactElement {
  const rows = Object.entries(pins);
  if (rows.length === 0) return <p style={{ ...S.muted, margin: 0 }}>No engine pins: the router picks the current version.</p>;
  return (
    <table style={{ borderCollapse: "collapse", width: "100%" }} data-testid="pins-table">
      <thead>
        <tr style={S.muted}>
          <th style={{ textAlign: "left", fontWeight: 500 }}>Engine</th>
          <th style={{ textAlign: "left", fontWeight: 500 }}>Pinned version</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([engine, version]) => (
          <tr key={engine}>
            <td style={S.mono}>{engine}</td>
            <td style={S.mono}>{version}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface PinRow {
  key: number;
  engine: string;
  version: string;
}

function CreateForm({ onCreated }: { onCreated: (p: ProjectSummary) => void }): ReactElement {
  const [id, setId] = useState("");
  const [nda, setNda] = useState(false);
  const [grades, setGrades] = useState<CustodyGrade[]>([...CUSTODY_GRADES]);
  const [pins, setPins] = useState<PinRow[]>([]);
  const [nextKey, setNextKey] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const toggleNda = (on: boolean) => {
    setNda(on);
    if (on) {
      const kept = grades.filter((g) => (NDA_GRADES as readonly string[]).includes(g));
      setGrades(kept.length > 0 ? kept : [...NDA_GRADES]);
    }
  };
  const toggleGrade = (g: CustodyGrade, on: boolean) =>
    setGrades((cur) => CUSTODY_GRADES.filter((x) => (x === g ? on : cur.includes(x))));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const project_id = id.trim();
    if (!PROJECT_ID_RE.test(project_id)) {
      setError("Project id: lower-case letters, digits, '-' and '_', starting with a letter or digit (64 at most).");
      return;
    }
    if (grades.length === 0) {
      setError("Allow at least one custody grade.");
      return;
    }
    const engine_pins: Record<string, string> = {};
    for (const p of pins) {
      const engine = p.engine.trim();
      if (!engine && !p.version.trim()) continue;
      if (!engine || !p.version.trim()) {
        setError("A pin needs both an engine id and a version.");
        return;
      }
      if (engine in engine_pins) {
        setError(`Engine ${engine} is pinned twice.`);
        return;
      }
      engine_pins[engine] = p.version.trim();
    }
    setError(null);
    setBusy(true);
    try {
      const made = await createProject({ project_id, custody: { nda, allowed_grades: grades }, engine_pins });
      setId("");
      setPins([]);
      onCreated(made);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} style={{ ...S.section, background: C.bg }} aria-label="New project" data-testid="create-project">
      <h3 style={S.h}>New project</h3>
      <input
        style={S.input}
        value={id}
        onChange={(e) => setId(e.target.value)}
        placeholder="project-id"
        aria-label="Project id"
      />
      <label style={S.row}>
        <input type="checkbox" checked={nda} onChange={(e) => toggleNda(e.target.checked)} />
        NDA project (C0 and C1 routes only)
      </label>
      <fieldset style={{ ...S.row, border: 0, margin: 0, padding: 0 }}>
        <legend style={{ ...S.muted, padding: 0 }}>Allowed custody grades</legend>
        {CUSTODY_GRADES.map((g) => {
          const locked = nda && !(NDA_GRADES as readonly string[]).includes(g);
          return (
            <label key={g} style={S.row}>
              <input
                type="checkbox"
                checked={grades.includes(g)}
                disabled={locked}
                onChange={(e) => toggleGrade(g, e.target.checked)}
                aria-label={`Allow ${g}`}
              />
              <span style={{ color: locked ? C.muted : GRADE_COLOR[g] }}>{g}</span>
            </label>
          );
        })}
      </fieldset>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span style={S.muted}>Engine pins</span>
        {pins.map((p) => (
          <div key={p.key} style={S.row}>
            <input
              style={{ ...S.input, flex: 1 }}
              value={p.engine}
              placeholder="nano-banana-2.1"
              aria-label="Pinned engine id"
              onChange={(e) => setPins((cur) => cur.map((x) => (x.key === p.key ? { ...x, engine: e.target.value } : x)))}
            />
            <input
              style={{ ...S.input, width: 70 }}
              value={p.version}
              placeholder="version"
              aria-label="Pinned version"
              onChange={(e) => setPins((cur) => cur.map((x) => (x.key === p.key ? { ...x, version: e.target.value } : x)))}
            />
            <button
              type="button"
              style={S.btn}
              aria-label="Remove pin"
              onClick={() => setPins((cur) => cur.filter((x) => x.key !== p.key))}
            >
              x
            </button>
          </div>
        ))}
        <button
          type="button"
          style={{ ...S.btn, alignSelf: "flex-start" }}
          onClick={() => {
            setPins((cur) => [...cur, { key: nextKey, engine: "", version: "" }]);
            setNextKey((k) => k + 1);
          }}
        >
          Add pin
        </button>
      </div>
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      <button type="submit" style={{ ...S.btn, ...S.btnPrimary, alignSelf: "flex-start" }} disabled={busy}>
        Create project
      </button>
    </form>
  );
}

export function ProjectPanel(): ReactElement {
  const openId = useGraphStore((s) => s.projectId);
  const loadProject = useGraphStore((s) => s.loadProject);
  const loadRuns = usePanelsStore((s) => s.loadRuns);
  const { data, loading, error, reload } = useLoad("projects", listProjects);
  const [openError, setOpenError] = useState<string | null>(null);

  const open = async (id: string) => {
    setOpenError(null);
    try {
      const graph = await getProjectGraph(id);
      const same = useGraphStore.getState().projectId === id;
      loadProject(graph, id);
      if (same) await loadRuns(id); // the runs effect only fires when the project changes
    } catch (err) {
      setOpenError(`Could not open ${id}: ${errorMessage(err)}`);
    }
  };

  const projects = data ?? [];
  const current = projects.find((p) => p.project_id === openId);

  return (
    <section style={S.section} aria-label="Projects" data-testid="project-panel">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>Projects</h3>
        <button type="button" style={S.btn} onClick={reload} disabled={loading}>
          Refresh
        </button>
      </div>
      {loading ? (
        <p style={S.muted} role="status">
          Loading projects...
        </p>
      ) : null}
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      {openError ? (
        <p style={S.error} role="alert">
          {openError}
        </p>
      ) : null}
      {!loading && !error && projects.length === 0 ? <p style={S.muted}>No projects yet. Create one below.</p> : null}
      <ul style={S.list}>
        {projects.map((p) => (
          <li
            key={p.project_id}
            style={{ ...S.item, cursor: "default", ...(p.project_id === openId ? S.itemOn : {}) }}
            data-testid="project-item"
            data-project-id={p.project_id}
          >
            <span style={{ ...S.row, justifyContent: "space-between" }}>
              <strong style={S.mono}>{p.project_id}</strong>
              <button type="button" style={S.btn} onClick={() => void open(p.project_id)} aria-label={`Open ${p.project_id}`}>
                {p.project_id === openId ? "Reload" : "Open"}
              </button>
            </span>
            <span style={S.row}>
              {p.custody.nda ? <span style={tint(C.advisory)}>NDA</span> : null}
              <GradeChips grades={p.custody.allowed_grades} />
            </span>
            <span style={S.muted}>{formatTime(p.created_utc)}</span>
          </li>
        ))}
      </ul>
      {current ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <h3 style={S.h}>Engine pins: {current.project_id}</h3>
          <PinsTable pins={current.engine_pins} />
        </div>
      ) : null}
      <CreateForm
        onCreated={(p) => {
          reload();
          void open(p.project_id);
        }}
      />
    </section>
  );
}
