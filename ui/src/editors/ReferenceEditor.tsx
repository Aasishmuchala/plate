// Reference node editor (T97, decision 46): an image an engine reads, never pixels of the output. Three ways in: a
// file input, a drop target and a paste listener (Ctrl+V with an image on the clipboard while this editor is open).
// Each one stores the image in the open project's CAS with `POST /assets?kind=reference` (the daemon re-encodes it as an
// 8-bit RGB PNG, which drops all metadata, and shrinks a side over 4096), then writes the node:
//   params.referenceHash  the CAS hash (what the pipeline sends as the engine's `reference_images`)
//   outputHash            the same hash, so the card shows the thumbnail
//   params.role           style | object | sky | person | swatch (what the image is for; the card summary reads it)
//   params.note           free text for the artist
// The project's earlier uploads (`GET /projects/{pid}/references`) are listed below to pick one again. The node feeds
// only an Edit / People `refs`, a Motion `elements` or a Sky `sky` input; the canvas rules refuse any other edge.

import { type ChangeEvent, type DragEvent, type ReactElement, useEffect, useRef, useState } from "react";
import { useThumb } from "../canvas/assets";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { listReferences, type ReferenceItem, type StoredReference, uploadReference } from "./nodeApi";
import { asString, mergeParams, type Params, useNodeEditor } from "./params";
import { C, E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

export const REFERENCE_ROLES = ["style", "object", "sky", "person", "swatch"] as const;
export type ReferenceRole = (typeof REFERENCE_ROLES)[number];

/** The daemon refuses a reference over 32 MiB (413); say so before the upload. */
export const MAX_REFERENCE_BYTES = 32 * 1024 * 1024;
const REFERENCE_TYPES: Readonly<Record<string, string>> = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg" };

const isRole = (v: string): v is ReferenceRole => (REFERENCE_ROLES as readonly string[]).includes(v);
const short = (h: string): string => (h.length > 10 ? h.slice(0, 10) : h);

/**
 * The content type to upload `file` as: its own when it is a PNG or JPEG, else the one its extension names. Throws the
 * sentence to show when it is neither, or too large. Pure.
 */
export function referenceContentType(file: { name?: string; type: string; size: number }): string {
  if (file.size > MAX_REFERENCE_BYTES) throw new Error("a reference is at most 32 MiB");
  const type = file.type.toLowerCase();
  if (type === "image/png" || type === "image/jpeg") return type;
  const ext = /\.([a-z0-9]+)$/i.exec(file.name ?? "")?.[1]?.toLowerCase();
  const byExt = ext ? REFERENCE_TYPES[ext] : undefined;
  if (byExt && (type === "" || type === "application/octet-stream")) return byExt;
  throw new Error("a reference is a PNG or JPEG image");
}

/** The first image on a clipboard (a pasted screenshot or copied picture), or null. */
export function imageFromClipboard(data: Pick<DataTransfer, "items" | "files"> | null | undefined): File | null {
  if (!data) return null;
  const items = data.items;
  if (items) {
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (item && item.kind === "file" && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) return file;
      }
    }
  }
  const files = data.files;
  if (files) {
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i];
      if (file && file.type.startsWith("image/")) return file;
    }
  }
  return null;
}

async function fileBytes(file: Blob): Promise<Uint8Array<ArrayBuffer>> {
  const buf = typeof file.arrayBuffer === "function" ? await file.arrayBuffer() : await new Response(file).arrayBuffer();
  return new Uint8Array(buf);
}

/** Point the node at a reference image (or none): `params.referenceHash` and `outputHash` always move together. */
export function setReferenceImage(nodeId: string, hash: string | undefined, params: Params = {}): void {
  useGraphStore.setState((s) => ({
    nodes: s.nodes.map((n) => {
      if (n.id !== nodeId) return n;
      const data = { ...n.data, params: mergeParams(n.data.params, { ...params, referenceHash: hash }) };
      if (hash === undefined) delete data.outputHash;
      else data.outputHash = hash;
      return { ...n, data };
    }),
  }));
}

function Thumb({ hash, size }: { hash: string | null; size: number }): ReactElement {
  const thumb = useThumb(hash);
  const box = { width: size, height: Math.round(size * 0.6), flex: "none", borderRadius: 3, background: C.bg, objectFit: "cover" } as const;
  return thumb.state === "ready" ? <img src={thumb.url} alt="" style={box} /> : <span aria-hidden style={box} />;
}

export function ReferenceEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const referenceHash = asString(params.referenceHash);
  const roleParam = asString(params.role);
  const role: ReferenceRole = isRole(roleParam) ? roleParam : "style";
  const note = asString(params.note);
  const [refs, setRefs] = useState<ReferenceItem[] | null>(null);
  const [stored, setStored] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [loadRefs, listState] = useAction(async () => {
    setRefs(projectId ? await listReferences(projectId) : []);
  }, errorMessage);
  useEffect(() => {
    void loadRefs();
  }, [loadRefs, projectId]);

  const [upload, uploadState] = useAction(async (file: File) => {
    if (!projectId) throw new Error("open a project first: a reference is stored in its CAS");
    const contentType = referenceContentType(file);
    const result: StoredReference = await uploadReference(await fileBytes(file), projectId, contentType);
    setReferenceImage(nodeId, result.hash);
    const size = result.width !== null && result.height !== null ? ` (${result.width}x${result.height}${result.scaledFrom ? `, shrunk from ${result.scaledFrom[0]}x${result.scaledFrom[1]}` : ""})` : "";
    setStored(`stored ${short(result.hash)}${size}`);
    setRefs((cur) =>
      cur?.some((r) => r.hash === result.hash)
        ? cur
        : [...(cur ?? []), { hash: result.hash, width: result.width, height: result.height, bytes: null, uploadedUtc: null, note: null }],
    );
  }, errorMessage);

  // Ctrl+V with an image on the clipboard uploads it while this editor is open. The listener is registered once; it
  // reads the latest `upload` through a ref.
  const uploadRef = useRef(upload);
  uploadRef.current = upload;
  useEffect(() => {
    const onPaste = (e: Event): void => {
      const file = imageFromClipboard((e as ClipboardEvent).clipboardData);
      if (!file) return;
      e.preventDefault();
      void uploadRef.current(file);
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, []);

  const onFile = (e: ChangeEvent<HTMLInputElement>): void => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) void upload(file);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
    setDragging(false);
    const file = imageFromClipboard(e.dataTransfer);
    if (file) void upload(file);
    else setStored(null);
  };

  return (
    <EditorFrame kind="reference" nodeId={nodeId} title="Reference">
      <Section title="Image" testId="reference-image-section">
        <div
          data-testid="reference-drop"
          data-dragging={dragging}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          style={{ ...E.row, padding: 8, borderRadius: 6, border: `1px dashed ${dragging ? C.accent : C.line}`, background: C.bg }}
        >
          {referenceHash ? <Thumb hash={referenceHash} size={120} /> : null}
          <span style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
            <span style={E.note} data-testid="reference-hash">
              {referenceHash ? `reference ${short(referenceHash)}` : "no image yet: drop a PNG or JPEG here, paste with Ctrl+V, or choose a file"}
            </span>
            <span style={E.row}>
              <input ref={fileInput} type="file" accept="image/png,image/jpeg" hidden onChange={onFile} data-testid="reference-file" />
              <Button
                onClick={() => fileInput.current?.click()}
                disabled={!projectId || uploadState.pending}
                title={projectId ? "store an image in this project as a reference" : "open a project first"}
                testId="reference-choose"
              >
                {uploadState.pending ? "Uploading" : "Choose file…"}
              </Button>
              <Button
                onClick={() => {
                  setReferenceImage(nodeId, undefined);
                  setStored(null);
                }}
                disabled={!referenceHash}
                title="unlink the image from this node (it stays in the project)"
                testId="reference-clear"
              >
                Clear
              </Button>
            </span>
          </span>
        </div>
        {stored ? <Notice testId="reference-stored">{stored}</Notice> : null}
        {!projectId ? <Notice testId="reference-no-project">open a project to store a reference in its CAS</Notice> : null}
        {uploadState.error ? (
          <Notice tone="error" testId="reference-error">
            {uploadState.error}
          </Notice>
        ) : null}
        <span style={E.note}>an engine reads a reference as an extra input; it is never composited, so it cannot change a locked pixel</span>
      </Section>

      <Section title="Use" testId="reference-use-section">
        <Field label="role">
          <select style={E.input} value={role} aria-label="role" data-testid="reference-role" onChange={(e) => set({ role: e.target.value })}>
            {REFERENCE_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
        <Field label="note">
          <input
            style={E.wide}
            value={note}
            placeholder="what to take from it"
            aria-label="note"
            data-testid="reference-note"
            onChange={(e) => set({ note: e.target.value === "" ? undefined : e.target.value })}
          />
        </Field>
      </Section>

      <Section title="This project's references" testId="reference-list-section">
        {refs === null ? (
          <span style={E.note}>{listState.pending ? "loading references" : "references not loaded"}</span>
        ) : refs.length === 0 ? (
          <span style={E.note} data-testid="reference-empty">
            none uploaded yet
          </span>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }} data-testid="reference-list">
            {refs.map((r) => (
              <li key={r.hash}>
                <button
                  type="button"
                  data-testid={`reference-pick-${r.hash.slice(0, 8)}`}
                  aria-pressed={r.hash === referenceHash}
                  title={`use ${short(r.hash)} as this node's image`}
                  onClick={() => setReferenceImage(nodeId, r.hash)}
                  style={{
                    ...E.button,
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    width: "100%",
                    textAlign: "left",
                    background: C.bg,
                    ...(r.hash === referenceHash ? { borderColor: C.accent } : null),
                  }}
                >
                  <Thumb hash={r.hash} size={64} />
                  <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                    <span style={E.row}>
                      <strong>{short(r.hash)}</strong>
                      {r.hash === referenceHash ? <Chip tone="info">in use</Chip> : null}
                    </span>
                    <span style={E.note}>
                      {r.width !== null && r.height !== null ? `${r.width} x ${r.height}` : "size n/a"}
                      {r.note ? ` · ${r.note}` : ""}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {listState.error ? (
          <Notice tone="error" testId="reference-list-error">
            references: {listState.error}
          </Notice>
        ) : null}
      </Section>
    </EditorFrame>
  );
}
