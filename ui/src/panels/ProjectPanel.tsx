// Project browser: the projects the daemon knows, open one onto the canvas, create one with its custody setting (NDA
// toggle and allowed grades) and engine pins. An NDA project allows C0 and C1 routes only, the same rule the daemon
// enforces (`plate_daemon.projects.check_custody`); the form simply cannot ask for more. For the open project the
// panel also edits custody (loosening needs a reason, which the daemon ledgers) and imports a client's plate (Tier C,
// consent record required) or packet kit; the imported packet then appears in the Plate editor's picker.

import { type FormEvent, type ReactElement, useState } from "react";
import { useGraphStore } from "../store";
import { createProject, errorMessage, getProjectGraph, listProjects } from "./api";
import { formatTime } from "./format";
import {
  CONSENT_REQUIRED,
  type ImportAccepted,
  importKit,
  importTierC,
  loosensCustody,
  MAX_CONSENT_CHARS,
  MIN_REASON_CHARS,
  putCustody,
} from "./importApi";
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

/** The grade set after the NDA flag changes: NDA keeps C0 and C1 only (at least those two when nothing was left). */
function gradesForNda(grades: readonly CustodyGrade[], nda: boolean): CustodyGrade[] {
  if (!nda) return [...grades];
  const kept = grades.filter((g) => (NDA_GRADES as readonly string[]).includes(g));
  return kept.length > 0 ? kept : [...NDA_GRADES];
}

const sameGrades = (a: readonly CustodyGrade[], b: readonly CustodyGrade[]): boolean =>
  a.length === b.length && a.every((g) => b.includes(g));

/**
 * The open project's custody and its editor. Tightening posts as is; loosening (NDA off, or a grade added) asks for a
 * reason first, because the daemon refuses it without one and ledgers it as an override.
 */
function CustodyRow({ project, onSaved }: { project: ProjectSummary; onSaved: () => void }): ReactElement {
  const [editing, setEditing] = useState(false);
  const [nda, setNda] = useState(project.custody.nda);
  const [grades, setGrades] = useState<CustodyGrade[]>(project.custody.allowed_grades);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loosens = loosensCustody(project.custody, { nda, allowed_grades: grades });
  const changed = nda !== project.custody.nda || !sameGrades(grades, project.custody.allowed_grades);

  const start = () => {
    setNda(project.custody.nda);
    setGrades(project.custody.allowed_grades);
    setReason("");
    setError(null);
    setEditing(true);
  };
  const toggleNda = (on: boolean) => {
    setNda(on);
    setGrades((cur) => gradesForNda(cur, on));
  };
  const toggleGrade = (g: CustodyGrade, on: boolean) =>
    setGrades((cur) => CUSTODY_GRADES.filter((x) => (x === g ? on : cur.includes(x))));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (grades.length === 0) {
      setError("Allow at least one custody grade.");
      return;
    }
    const why = reason.trim();
    if (loosens && why === "") {
      setError("reason required");
      return;
    }
    if (loosens && why.length < MIN_REASON_CHARS) {
      setError(`reason must be at least ${MIN_REASON_CHARS} characters`);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await putCustody(project.project_id, { nda, allowed_grades: grades, ...(why !== "" ? { reason: why } : {}) });
      setEditing(false);
      setReason("");
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }} data-testid="custody-row">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>Custody: {project.project_id}</h3>
        {editing ? null : (
          <button type="button" style={S.btn} onClick={start} aria-label="Edit custody">
            Edit
          </button>
        )}
      </div>
      <span style={S.row} data-testid="custody-current">
        {project.custody.nda ? <span style={tint(C.advisory)}>NDA</span> : <span style={S.chip}>not NDA</span>}
        <GradeChips grades={project.custody.allowed_grades} />
      </span>
      {editing ? (
        <form onSubmit={submit} noValidate style={{ ...S.section, background: C.bg }} aria-label="Custody editor" data-testid="custody-edit">
          <label style={S.row}>
            <input type="checkbox" checked={nda} onChange={(e) => toggleNda(e.target.checked)} />
            NDA on (C0 and C1 routes only)
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
                    aria-label={`Custody allow ${g}`}
                  />
                  <span style={{ color: locked ? C.muted : GRADE_COLOR[g] }}>{g}</span>
                </label>
              );
            })}
          </fieldset>
          {loosens ? (
            <p style={{ ...S.muted, margin: 0 }}>
              This loosens the project's custody. A reason of {MIN_REASON_CHARS} characters or more is required and goes
              in the ledger.
            </p>
          ) : null}
          <textarea
            style={{ ...S.input, minHeight: 40, resize: "vertical" }}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={loosens ? "Why is this being loosened?" : "Reason (optional when tightening)"}
            aria-label="Custody reason"
          />
          {error ? (
            <p style={S.error} role="alert">
              {error}
            </p>
          ) : null}
          <span style={S.row}>
            <button type="submit" style={{ ...S.btn, ...S.btnPrimary }} disabled={busy || !changed}>
              Save custody
            </button>
            <button type="button" style={S.btn} onClick={() => setEditing(false)} disabled={busy}>
              Cancel
            </button>
          </span>
        </form>
      ) : null}
    </div>
  );
}

type ImportMode = "tierc" | "kit";

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/**
 * Import a client's plate into the open project: a finished image with no scene behind it becomes a Tier C packet
 * (consent record required, the same rule as the daemon's 428), or a client packet kit's folder plus the plate
 * becomes a Tier A packet. The daemon answers 202 and ingests in the background; nothing else is done here, the
 * packet turns up in the Plate editor's picker through its `packet.ingested` event.
 */
function ImportForm({ projectId }: { projectId: string | null }): ReactElement {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<ImportMode>("tierc");
  const [plate, setPlate] = useState("");
  const [shot, setShot] = useState("");
  const [consent, setConsent] = useState("");
  const [folder, setFolder] = useState("");
  const [camera, setCamera] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [accepted, setAccepted] = useState<ImportAccepted | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (projectId === null) return;
    const path = plate.trim();
    const shotId = shot.trim();
    const found: string[] = [];
    if (mode === "tierc") {
      if (path === "") found.push("Plate path is required.");
      if (shotId === "") found.push("Shot id is required.");
      if (consent.trim() === "") found.push(CONSENT_REQUIRED);
    } else {
      if (folder.trim() === "") found.push("Kit folder is required.");
      if (camera.trim() === "") found.push("Camera name is required.");
      if (path === "") found.push("Plate path is required.");
    }
    setAccepted(null);
    setProblems(found);
    if (found.length > 0) return;
    setBusy(true);
    try {
      const made =
        mode === "tierc"
          ? await importTierC(projectId, { plate_path: path, shot_id: shotId, consent_record: consent.trim() })
          : await importKit(projectId, {
              folder: folder.trim(),
              camera: camera.trim(),
              plate_path: path,
              ...(shotId !== "" ? { shot_id: shotId } : {}),
            });
      setAccepted(made);
      // the consent record belongs to this plate alone; the kit folder is usually reused for the next camera
      setPlate("");
      setShot("");
      setConsent("");
      setCamera("");
    } catch (err) {
      setProblems([errorMessage(err)]);
    } finally {
      setBusy(false);
    }
  };

  const warnings = accepted ? strings(accepted.warnings) : [];
  const flags = accepted ? strings(accepted.flags) : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }} data-testid="import-client">
      <button
        type="button"
        style={{ ...S.btn, alignSelf: "flex-start" }}
        aria-expanded={open}
        aria-controls="import-client-body"
        onClick={() => setOpen((o) => !o)}
      >
        Import client plate
      </button>
      {open ? (
        <form
          id="import-client-body"
          onSubmit={submit}
          noValidate
          style={{ ...S.section, background: C.bg }}
          aria-label="Client import"
        >
          {projectId === null ? <p style={{ ...S.muted, margin: 0 }}>Open a project to import into it.</p> : null}
          <fieldset style={{ ...S.row, border: 0, margin: 0, padding: 0 }}>
            <legend style={{ ...S.muted, padding: 0 }}>Import mode</legend>
            <label style={S.row}>
              <input type="radio" name="import-mode" checked={mode === "tierc"} onChange={() => setMode("tierc")} />
              Tier C plate
            </label>
            <label style={S.row}>
              <input type="radio" name="import-mode" checked={mode === "kit"} onChange={() => setMode("kit")} />
              Client kit
            </label>
          </fieldset>
          {mode === "kit" ? (
            <>
              <input
                style={S.input}
                value={folder}
                onChange={(e) => setFolder(e.target.value)}
                placeholder="kit output folder (absolute path on the daemon's machine)"
                aria-label="Kit folder"
              />
              <input
                style={S.input}
                value={camera}
                onChange={(e) => setCamera(e.target.value)}
                placeholder="camera name as exported by the kit"
                aria-label="Camera name"
              />
            </>
          ) : null}
          <input
            style={S.input}
            value={plate}
            onChange={(e) => setPlate(e.target.value)}
            placeholder="the client's plate (absolute path on the daemon's machine)"
            aria-label="Plate path"
          />
          <input
            style={S.input}
            value={shot}
            onChange={(e) => setShot(e.target.value)}
            placeholder={mode === "kit" ? "shot-id (optional)" : "shot-id"}
            aria-label="Shot id"
          />
          {mode === "tierc" ? (
            <>
              <span style={S.muted}>Consent record (required for a Tier C plate)</span>
              <textarea
                style={{ ...S.input, minHeight: 56, resize: "vertical" }}
                value={consent}
                maxLength={MAX_CONSENT_CHARS}
                onChange={(e) => setConsent(e.target.value)}
                placeholder="Who agreed to what, and when."
                aria-label="Consent record"
                aria-required="true"
              />
            </>
          ) : null}
          {problems.length > 0 ? (
            <div style={S.error} role="alert">
              {problems.map((m) => (
                <div key={m}>{m}</div>
              ))}
            </div>
          ) : null}
          {accepted ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }} role="status" data-testid="import-result">
              <span>{`packet ${accepted.packet_id} ${accepted.status || "ingesting"}`}</span>
              {accepted.stub ? (
                <span style={S.muted}>Some Tier C layers came from stub models; treat depth and masks as placeholders.</span>
              ) : null}
              {flags.length > 0 ? (
                <span style={S.row}>
                  {flags.map((f) => (
                    <span key={f} style={tint(C.advisory)}>
                      {f}
                    </span>
                  ))}
                </span>
              ) : null}
              {warnings.map((w) => (
                <span key={w} style={S.muted}>
                  {w}
                </span>
              ))}
            </div>
          ) : null}
          <button
            type="submit"
            style={{ ...S.btn, ...S.btnPrimary, alignSelf: "flex-start" }}
            disabled={busy || projectId === null}
          >
            {mode === "tierc" ? "Import Tier C plate" : "Import client kit"}
          </button>
        </form>
      ) : null}
    </div>
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
      {current ? <CustodyRow key={`custody:${current.project_id}`} project={current} onSaved={reload} /> : null}
      <ImportForm key={`import:${openId ?? ""}`} projectId={openId} />
      <CreateForm
        onCreated={(p) => {
          reload();
          void open(p.project_id);
        }}
      />
    </section>
  );
}
